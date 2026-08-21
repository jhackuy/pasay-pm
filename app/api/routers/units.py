from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.database import get_db
from app.models.membership import OrganizationRole
from app.models.property import Property, Unit
from app.models.user import User
from app.schemas.common import MessageResponse
from app.schemas.property import UnitCreate, UnitRead, UnitUpdate
from app.services.audit import field_changes, record_audit, serialize_row
from app.services.organization_scope import scope_exception_to_http
from app.services.property_channel import (
    OwnerRequired,
    ScopeBlocked,
    filter_secretary_unit_updates,
    property_org_id,
    resolve_org_membership,
    scoped_get_unit,
)

router = APIRouter(prefix="/units", tags=["units"])


@router.get("", response_model=list[UnitRead])
def list_units(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    from app.services.property_channel import scoped_list_properties

    props = scoped_list_properties(db, for_user_id=user.id)
    if not props:
        return []
    prop_ids = [p.id for p in props]
    return (
        db.query(Unit)
        .filter(Unit.property_id.in_(prop_ids), Unit.deleted_at.is_(None))
        .order_by(Unit.id)
        .all()
    )


@router.post("", response_model=UnitRead, status_code=status.HTTP_201_CREATED)
def create_unit(
    payload: UnitCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    org_id = property_org_id(db, payload.property_id)
    if org_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Property not found or has no organization")
    try:
        resolve_org_membership(db, user.id, org_id, role=OrganizationRole.OWNER)
    except ScopeBlocked as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc

    prop = db.query(Property).filter(
        Property.id == payload.property_id, Property.deleted_at.is_(None)
    ).first()
    if prop is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Property not found")

    obj = Unit(**payload.model_dump())
    obj.created_by = user.id
    obj.updated_by = user.id
    db.add(obj)
    db.flush()
    record_audit(
        db,
        table_name="units",
        record_id=obj.id,
        action="create",
        actor_id=user.id,
        new_value=serialize_row(obj),
    )
    db.commit()
    db.refresh(obj)
    return obj


@router.get("/{unit_id}", response_model=UnitRead)
def get_unit(
    unit_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    try:
        unit, _membership = scoped_get_unit(db, unit_id, for_user_id=user.id)
    except Exception as exc:  # noqa: BLE001
        raise scope_exception_to_http(exc) from exc
    return unit


@router.patch("/{unit_id}", response_model=UnitRead)
def update_unit(
    unit_id: int,
    payload: UnitUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        obj, membership = scoped_get_unit(db, unit_id, for_user_id=user.id)
    except Exception as exc:  # noqa: BLE001
        raise scope_exception_to_http(exc) from exc

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return obj

    try:
        if membership.role != OrganizationRole.OWNER:
            filter_secretary_unit_updates(set(updates.keys()))
    except OwnerRequired as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc

    old = serialize_row(obj)
    changed = field_changes(obj, updates)
    for field, value in updates.items():
        setattr(obj, field, value)
    obj.updated_by = user.id
    _record_lifecycle(db, obj, old, updates, user.id)
    record_audit(
        db,
        table_name="units",
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


def _record_lifecycle(db: Session, obj: Unit, old: dict, updates: dict, actor_id: int) -> None:
    from app.models.property import UnitLifecycleEvent

    state_old = old.get("unit_state") or old.get("status")
    _status_upd = updates.get("status")
    state_new = updates.get("unit_state") or (_status_upd.value if _status_upd else None)
    if state_old == state_new or state_new is None:
        return
    event = UnitLifecycleEvent(
        unit_id=obj.id,
        from_status=state_old,
        to_status=state_new,
        reason="api_update",
        occurred_at=datetime.now(timezone.utc),
        created_by=actor_id,
        updated_by=actor_id,
    )
    db.add(event)
    db.flush()


@router.delete("/{unit_id}", response_model=MessageResponse)
def delete_unit(
    unit_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        obj, membership = scoped_get_unit(db, unit_id, for_user_id=user.id)
    except Exception as exc:  # noqa: BLE001
        raise scope_exception_to_http(exc) from exc
    if membership.role != OrganizationRole.OWNER:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only ACTIVE OWNER may soft-delete a Unit",
        )
    obj.deleted_at = datetime.now(timezone.utc)
    obj.updated_by = user.id
    record_audit(
        db,
        table_name="units",
        record_id=obj.id,
        action="soft_delete",
        actor_id=user.id,
        old_value=serialize_row(obj),
    )
    db.commit()
    return MessageResponse(detail="Unit deleted")
