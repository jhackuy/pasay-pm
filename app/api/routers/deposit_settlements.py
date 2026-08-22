from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.database import get_db
from app.models.deposit_settlement import DepositSettlement, DepositSettlementStatus
from app.models.lease import Lease
from app.models.user import User
from app.schemas.common import MessageResponse
from app.schemas.deposit_settlement import (
    DepositSettlementConfirm,
    DepositSettlementCreate,
    DepositSettlementRead,
    DepositSettlementUpdate,
)
from app.services.audit import field_changes, record_audit, serialize_row
from app.services.deposit_settlement_service import (
    _jsonb_safe_deductions,
    confirm_settlement,
    mark_reconciled,
    update_settlement,
)
from app.services.organization_scope import (
    OrganizationRole,
    scope_exception_to_http,
    scoped_get_deposit_settlement,
    scoped_get_lease,
    scoped_get_move_out_inspection,
)

router = APIRouter(prefix="/deposit-settlements", tags=["deposit_settlements"])


@router.get("", response_model=list[DepositSettlementRead])
def list_settlements(
    lease_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.services.organization_scope import list_active_org_ids_for_user
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
    q = db.query(DepositSettlement).filter(DepositSettlement.lease_id.in_(org_lease_ids))
    if lease_id is not None:
        q = q.filter(DepositSettlement.lease_id == lease_id)
    return q.order_by(DepositSettlement.id.desc()).all()


@router.post("", response_model=DepositSettlementRead, status_code=status.HTTP_201_CREATED)
def create_settlement(
    payload: DepositSettlementCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        insp, membership = scoped_get_move_out_inspection(
            db, payload.move_out_inspection_id, for_user_id=user.id,
            role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY],
        )
        scoped_get_lease(
            db, insp.lease_id, for_user_id=user.id,
            role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY],
        )
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc

    existing = (
        db.query(DepositSettlement)
        .filter(DepositSettlement.move_out_inspection_id == insp.id)
        .first()
    )
    if existing is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "deposit_settlement_already_exists_for_inspection",
                "move_out_inspection_id": insp.id,
                "existing_settlement_id": existing.id,
                "hint": f"Use GET /deposit-settlements/{existing.id} to read the existing settlement; PATCH to edit DRAFT fields.",
            },
        )

    create_data = payload.model_dump()
    create_data.pop("move_out_inspection_id", None)
    create_data["deductions"] = _jsonb_safe_deductions(create_data.get("deductions"))
    obj = DepositSettlement(**create_data)
    obj.status = DepositSettlementStatus.DRAFT
    obj.lease_id = insp.lease_id
    obj.move_out_inspection_id = insp.id
    obj.created_by = user.id
    obj.updated_by = user.id
    db.add(obj)
    db.flush()
    lease = db.get(Lease, obj.lease_id)
    if lease is not None and lease.deposit_settlement_id is None:
        lease.deposit_settlement_id = obj.id
        lease.updated_by = user.id
    record_audit(
        db, table_name="deposit_settlements", record_id=obj.id, action="create",
        actor_id=user.id, new_value=serialize_row(obj),
    )
    db.commit()
    db.refresh(obj)
    return obj


@router.get("/{settlement_id}", response_model=DepositSettlementRead)
def get_settlement(
    settlement_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        obj, _membership = scoped_get_deposit_settlement(db, settlement_id, for_user_id=user.id)
        return obj
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc


@router.patch("/{settlement_id}", response_model=DepositSettlementRead)
def patch_settlement(
    settlement_id: int,
    payload: DepositSettlementUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        obj, _membership = scoped_get_deposit_settlement(
            db, settlement_id, for_user_id=user.id,
            role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY],
        )
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc
    updates = payload.model_dump(exclude_unset=True)
    for forbidden in ("status", "lease_id", "move_out_inspection_id"):
        updates.pop(forbidden, None)
    if not updates:
        return obj
    update_settlement(db, obj, updates=updates, actor_id=user.id)
    db.commit()
    db.refresh(obj)
    return obj


@router.post("/{settlement_id}/confirm", response_model=DepositSettlementRead)
def confirm_post(
    settlement_id: int,
    _payload: DepositSettlementConfirm | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        obj, _membership = scoped_get_deposit_settlement(
            db, settlement_id, for_user_id=user.id,
            role=[OrganizationRole.OWNER],
        )
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc
    confirm_settlement(db, obj, confirmed_at=datetime.now(timezone.utc), confirmed_by=user.id)
    db.commit()
    db.refresh(obj)
    return obj


@router.post("/{settlement_id}/reconcile", response_model=DepositSettlementRead)
def reconcile_post(
    settlement_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        obj, _membership = scoped_get_deposit_settlement(
            db, settlement_id, for_user_id=user.id,
            role=[OrganizationRole.OWNER],
        )
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc
    mark_reconciled(db, obj, actor_id=user.id, now=datetime.now(timezone.utc))
    db.commit()
    db.refresh(obj)
    return obj
