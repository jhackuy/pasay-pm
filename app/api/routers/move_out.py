from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.database import get_db
from app.models.move_out import MoveOutInspection, MoveOutInspectionStatus
from app.models.user import User
from app.schemas.move_out import (
    MoveOutInspectionConfirm,
    MoveOutInspectionCreate,
    MoveOutInspectionRead,
    MoveOutInspectionUpdate,
)
from app.schemas.common import MessageResponse
from app.services.audit import serialize_row
from app.services.organization_scope import (
    OrganizationRole,
    assert_co_org,
    lease_org_id,
    resolve_org_membership,
    scope_exception_to_http,
    scoped_get_lease,
    scoped_get_move_out_inspection,
    unit_org_id,
    tenant_org_id,
)
from app.services.move_out_workflow import (
    cancel_inspection,
    confirm_inspection,
    mark_inspected,
    schedule_inspection,
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
        if payload.unit_id is not None and payload.unit_id != lease.unit_id:
            u_org = unit_org_id(db, payload.unit_id)
            if u_org is None:
                raise LookupError("Unit not found")
            assert_co_org(db, user_org_id=membership.organization_id, object_org_id=u_org, object_kind="Unit", object_id=payload.unit_id)
        if payload.tenant_id is not None and payload.tenant_id != lease.tenant_id:
            t_org = tenant_org_id(db, payload.tenant_id)
            if t_org is None:
                raise LookupError("Tenant not found")
            assert_co_org(db, user_org_id=membership.organization_id, object_org_id=t_org, object_kind="Tenant", object_id=payload.tenant_id)
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc

    obj = schedule_inspection(
        db,
        lease_id=payload.lease_id,
        unit_id=payload.unit_id or lease.unit_id,
        tenant_id=payload.tenant_id or lease.tenant_id,
        scheduled_at=payload.scheduled_at,
        actor_id=user.id,
    )
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
    if not updates:
        return obj

    transition_to = updates.pop("status", None)
    if transition_to is not None and transition_to != obj.status and transition_to == MoveOutInspectionStatus.INSPECTED:
        findings = updates.get("findings") if "findings" in updates else obj.findings
        evidence_ids = updates.get("evidence_ids") if "evidence_ids" in updates else obj.evidence_ids
        inspected_at = updates.pop("inspected_at", None) or datetime.now(timezone.utc)
        for f, v in updates.items():
            if hasattr(obj, f) and v is not None:
                setattr(obj, f, v)
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

    if transition_to is not None and transition_to == MoveOutInspectionStatus.CANCELLED and obj.status not in (MoveOutInspectionStatus.CONFIRMED, MoveOutInspectionStatus.CANCELLED):
        for f, v in updates.items():
            if hasattr(obj, f) and v is not None:
                setattr(obj, f, v)
        cancelled_at = datetime.now(timezone.utc)
        cancel_inspection(db, obj, cancelled_at=cancelled_at, cancelled_by=user.id)
        db.commit()
        db.refresh(obj)
        return obj

    # Normal field-only PATCH (DRAFT data edit, no status transition)
    if obj.status not in (MoveOutInspectionStatus.CONFIRMED, MoveOutInspectionStatus.CANCELLED):
        from app.services.audit import field_changes, record_audit
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

    raise HTTPException(
        status.HTTP_409_CONFLICT,
        detail={
            "reason": "inspection_not_editable",
            "status": obj.status.value,
            "hint": "CONFIRMED / CANCELLED inspections are immutable",
        },
    )


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
    findings = updates.get("findings", obj.findings)
    evidence_ids = updates.get("evidence_ids", obj.evidence_ids)
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
