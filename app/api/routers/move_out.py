from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.database import get_db
from app.models.evidence import Evidence
from app.models.move_out import MoveOutInspection, MoveOutInspectionStatus
from app.models.user import User
from app.schemas.move_out import (
    FindingsItem,
    MoveOutInspectionConfirm,
    MoveOutInspectionCreate,
    MoveOutInspectionRead,
    MoveOutInspectionUpdate,
)
from app.schemas.common import MessageResponse
from app.services.audit import serialize_row
from app.services.organization_scope import (
    OrganizationRole,
    property_org_id,
    scope_exception_to_http,
    scoped_get_lease,
    scoped_get_move_out_inspection,
)
from app.services.move_out_workflow import (
    cancel_inspection,
    confirm_inspection,
    mark_inspected,
    schedule_inspection,
    validate_evidence_ids,
)

router = APIRouter(prefix="/move-out-inspections", tags=["move_out_inspections"])


@router.get("", response_model=list[MoveOutInspectionRead])
def list_inspections(
    lease_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.services.organization_scope import list_active_org_ids_for_user
    from app.models.lease import Lease
    from app.models.property import Property, Unit
    from sqlalchemy import select

    orgs = list_active_org_ids_for_user(db, user.id)
    if not orgs:
        return []
    org_property_ids = (
        select(Property.id).where(Property.organization_id.in_(orgs), Property.deleted_at.is_(None)).scalar_subquery()
    )
    org_unit_ids = select(Unit.id).where(Unit.property_id.in_(org_property_ids), Unit.deleted_at.is_(None)).scalar_subquery()
    org_lease_ids = select(Lease.id).where(Lease.unit_id.in_(org_unit_ids), Lease.deleted_at.is_(None)).scalar_subquery()
    q = db.query(MoveOutInspection).filter(MoveOutInspection.lease_id.in_(org_lease_ids))
    if lease_id is not None:
        q = q.filter(MoveOutInspection.lease_id == lease_id)
    return q.order_by(MoveOutInspection.id.desc()).all()


@router.post("", response_model=MoveOutInspectionRead, status_code=status.HTTP_201_CREATED)
def create_inspection(
    payload: MoveOutInspectionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        lease, membership = scoped_get_lease(
            db, payload.lease_id, for_user_id=user.id,
            role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY],
        )
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc

    existing = (
        db.query(MoveOutInspection)
        .filter(
            MoveOutInspection.lease_id == lease.id,
            MoveOutInspection.status.in_([MoveOutInspectionStatus.SCHEDULED, MoveOutInspectionStatus.INSPECTED]),
        )
        .first()
    )
    if existing is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "move_out_inspection_already_exists_for_lease",
                "lease_id": payload.lease_id,
                "existing_inspection_id": existing.id,
                "existing_status": existing.status.value,
                "hint": "GET /move-out-inspections/{id}",
            },
        )

    if payload.evidence_ids is not None:
        validate_evidence_ids(db, payload.evidence_ids, lease, membership)

    obj = schedule_inspection(
        db,
        lease_id=lease.id,
        unit_id=lease.unit_id,
        tenant_id=lease.tenant_id,
        scheduled_at=payload.scheduled_at,
        actor_id=user.id,
    )
    if payload.findings is not None:
        obj.findings = [fi.model_dump(mode="json") for fi in payload.findings]
    if payload.evidence_ids is not None:
        obj.evidence_ids = payload.evidence_ids
    if payload.notes is not None:
        obj.notes = payload.notes
    obj.updated_by = user.id
    db.commit()
    db.refresh(obj)
    return obj


@router.get("/{inspection_id}", response_model=MoveOutInspectionRead)
def get_inspection(
    inspection_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        obj, _membership = scoped_get_move_out_inspection(db, inspection_id, for_user_id=user.id)
        return obj
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc


@router.patch("/{inspection_id}", response_model=MoveOutInspectionRead)
def patch_inspection(
    inspection_id: int,
    payload: MoveOutInspectionUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        obj, _membership = scoped_get_move_out_inspection(
            db, inspection_id, for_user_id=user.id,
            role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY],
        )
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc

    updates = payload.model_dump(exclude_unset=True)
    for forbidden in ("lease_id", "unit_id", "tenant_id", "status"):
        updates.pop(forbidden, None)
    if not updates:
        return obj

    if obj.status in (MoveOutInspectionStatus.CONFIRMED, MoveOutInspectionStatus.CANCELLED):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "inspection_not_editable",
                "status": obj.status.value,
                "hint": "CONFIRMED / CANCELLED inspections are immutable",
            },
        )

    if "evidence_ids" in updates and updates["evidence_ids"] is not None:
        from app.models.lease import Lease
        lease = db.get(Lease, obj.lease_id)
        if lease is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "reason": "move_out_inspection_evidence_not_found",
                    "missing_evidence_ids": [],
                },
            )
        validate_evidence_ids(db, updates["evidence_ids"], lease, _membership)

    from app.services.audit import field_changes, record_audit
    if "findings" in updates and updates["findings"] is not None:
        updates["findings"] = [
            FindingsItem.model_validate(raw).model_dump(mode="json")
            for raw in updates["findings"]
        ]
    old = serialize_row(obj)
    changed = field_changes(obj, updates)
    for f, v in updates.items():
        if hasattr(obj, f):
            setattr(obj, f, v)
    obj.updated_by = user.id
    db.flush()
    record_audit(
        db, table_name="move_out_inspections", record_id=obj.id, action="update",
        actor_id=user.id, changed_fields=changed, old_value=old, new_value=serialize_row(obj),
    )
    db.commit()
    db.refresh(obj)
    return obj


@router.post("/{inspection_id}/inspect", response_model=MoveOutInspectionRead)
def inspect_post(
    inspection_id: int,
    payload: MoveOutInspectionUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        obj, _membership = scoped_get_move_out_inspection(
            db, inspection_id, for_user_id=user.id,
            role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY],
        )
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc
    updates = payload.model_dump(exclude_unset=True)
    if "findings" in updates and updates["findings"] is not None:
        findings = [
            FindingsItem.model_validate(raw).model_dump(mode="json")
            for raw in updates["findings"]
        ]
    else:
        findings = obj.findings
    evidence_ids = updates.get("evidence_ids", obj.evidence_ids)
    if evidence_ids is not None:
        from app.models.lease import Lease
        lease = db.get(Lease, obj.lease_id)
        if lease is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "reason": "move_out_inspection_evidence_not_found",
                    "missing_evidence_ids": [],
                },
            )
        validate_evidence_ids(db, evidence_ids, lease, _membership)
    inspected_at = updates.pop("inspected_at", None) or datetime.now(timezone.utc)
    mark_inspected(
        db, obj,
        findings=findings,
        evidence_ids=evidence_ids,
        inspected_at=inspected_at,
        actor_id=user.id,
    )
    db.commit()
    db.refresh(obj)
    return obj


@router.post("/{inspection_id}/confirm", response_model=MoveOutInspectionRead)
def confirm_post(
    inspection_id: int,
    _payload: MoveOutInspectionConfirm | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        obj, _membership = scoped_get_move_out_inspection(
            db, inspection_id, for_user_id=user.id,
            role=[OrganizationRole.OWNER],
        )
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc
    confirm_inspection(db, obj, confirmed_at=datetime.now(timezone.utc), actor_id=user.id)
    db.commit()
    db.refresh(obj)
    return obj


@router.post("/{inspection_id}/cancel", response_model=MoveOutInspectionRead)
def cancel_post(
    inspection_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        obj, _membership = scoped_get_move_out_inspection(
            db, inspection_id, for_user_id=user.id,
            role=[OrganizationRole.OWNER],
        )
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc
    cancel_inspection(db, obj, cancelled_at=datetime.now(timezone.utc), cancelled_by=user.id)
    db.commit()
    db.refresh(obj)
    return obj
