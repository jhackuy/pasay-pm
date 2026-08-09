from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, manager_or_admin
from app.database import get_db
from app.models.commission import CommissionSettlement
from app.models.lease import Lease, LeaseStatus
from app.models.property import Unit, UnitStatus
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.common import MessageResponse
from app.schemas.lease import LeaseCreate, LeaseRead, LeaseUpdate
from app.services.audit import field_changes, record_audit, serialize_row

router = APIRouter(prefix="/leases", tags=["leases"])


def _get_or_404(db: Session, lease_id: int) -> Lease:
    obj = db.query(Lease).filter(Lease.id == lease_id, Lease.deleted_at.is_(None)).first()
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lease not found")
    return obj


def _visible_lease_ids(db: Session, user: User) -> set[int] | None:
    """For agents: leases related to their commission settlements. None = no filter."""
    if user.role != "agent":
        return None
    rows = (
        db.query(CommissionSettlement.lease_id)
        .filter(CommissionSettlement.agent_id == user.id)
        .all()
    )
    return {r[0] for r in rows}


def _sync_unit_status(db: Session, unit: Unit) -> None:
    """Recompute unit occupancy from its active leases."""
    active = (
        db.query(Lease)
        .filter(Lease.unit_id == unit.id, Lease.status == LeaseStatus.active, Lease.deleted_at.is_(None))
        .first()
    )
    if active is not None:
        unit.status = UnitStatus.occupied
    elif unit.status == UnitStatus.occupied:
        unit.status = UnitStatus.vacant


@router.get("", response_model=list[LeaseRead])
def list_leases(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    query = db.query(Lease).filter(Lease.deleted_at.is_(None))
    visible = _visible_lease_ids(db, user)
    if visible is not None:
        query = query.filter(Lease.id.in_(visible))
    return query.order_by(Lease.id).all()


@router.post("", response_model=LeaseRead, status_code=status.HTTP_201_CREATED)
def create_lease(
    payload: LeaseCreate,
    db: Session = Depends(get_db),
    user: User = Depends(manager_or_admin),
):
    unit = db.query(Unit).filter(Unit.id == payload.unit_id, Unit.deleted_at.is_(None)).first()
    if unit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unit not found")
    tenant = db.query(Tenant).filter(Tenant.id == payload.tenant_id, Tenant.deleted_at.is_(None)).first()
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tenant not found")
    if payload.status == LeaseStatus.active and unit.status == UnitStatus.occupied:
        raise HTTPException(status.HTTP_409_CONFLICT, "Unit is already occupied")

    obj = Lease(**payload.model_dump())
    obj.created_by = user.id
    obj.updated_by = user.id
    db.add(obj)
    db.flush()
    if obj.status == LeaseStatus.active:
        unit.status = UnitStatus.occupied
        unit.updated_by = user.id
    record_audit(
        db,
        table_name="leases",
        record_id=obj.id,
        action="create",
        actor_id=user.id,
        new_value=serialize_row(obj),
    )
    db.commit()
    db.refresh(obj)
    return obj


@router.get("/{lease_id}", response_model=LeaseRead)
def get_lease(
    lease_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    obj = _get_or_404(db, lease_id)
    visible = _visible_lease_ids(db, user)
    if visible is not None and obj.id not in visible:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions")
    return obj


@router.patch("/{lease_id}", response_model=LeaseRead)
def update_lease(
    lease_id: int,
    payload: LeaseUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(manager_or_admin),
):
    obj = _get_or_404(db, lease_id)
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return obj

    if updates.get("unit_id") is not None and updates["unit_id"] != obj.unit_id:
        unit = db.query(Unit).filter(Unit.id == updates["unit_id"], Unit.deleted_at.is_(None)).first()
        if unit is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Unit not found")
    if updates.get("tenant_id") is not None and updates["tenant_id"] != obj.tenant_id:
        tenant = db.query(Tenant).filter(Tenant.id == updates["tenant_id"], Tenant.deleted_at.is_(None)).first()
        if tenant is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Tenant not found")

    unit = db.query(Unit).filter(Unit.id == obj.unit_id, Unit.deleted_at.is_(None)).first()
    new_status = updates.get("status", obj.status)
    if new_status == LeaseStatus.active:
        conflicting = (
            db.query(Lease)
            .filter(
                Lease.unit_id == obj.unit_id,
                Lease.status == LeaseStatus.active,
                Lease.id != obj.id,
                Lease.deleted_at.is_(None),
            )
            .first()
        )
        if conflicting is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "Unit is already occupied by another active lease")

    old = serialize_row(obj)
    changed = field_changes(obj, updates)
    for field, value in updates.items():
        setattr(obj, field, value)
    obj.updated_by = user.id
    db.flush()  # make the status change visible to the occupancy re-check
    if unit is not None:
        if new_status == LeaseStatus.active:
            unit.status = UnitStatus.occupied
            unit.updated_by = user.id
        else:
            _sync_unit_status(db, unit)
    record_audit(
        db,
        table_name="leases",
        record_id=obj.id,
        action="update",
        actor_id=user.id,
        changed_fields=changed,
        old_value=old,
        new_value=serialize_row(obj),
    )
    db.commit()
    db.refresh(obj)
    return obj


@router.delete("/{lease_id}", response_model=MessageResponse)
def delete_lease(
    lease_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(manager_or_admin),
):
    obj = _get_or_404(db, lease_id)
    if obj.status == LeaseStatus.active:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Terminate the lease before deleting it"
        )
    from datetime import datetime, timezone

    obj.deleted_at = datetime.now(timezone.utc)
    obj.updated_by = user.id
    unit = db.query(Unit).filter(Unit.id == obj.unit_id, Unit.deleted_at.is_(None)).first()
    if unit is not None:
        _sync_unit_status(db, unit)
    record_audit(
        db,
        table_name="leases",
        record_id=obj.id,
        action="soft_delete",
        actor_id=user.id,
        old_value=serialize_row(obj),
    )
    db.commit()
    return MessageResponse(detail="Lease deleted")
