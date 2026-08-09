from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, manager_or_admin
from app.database import get_db
from app.models.property import Property
from app.schemas.common import MessageResponse
from app.schemas.property import PropertyCreate, PropertyRead, PropertyUpdate
from app.services.audit import field_changes, record_audit, serialize_row

router = APIRouter(prefix="/properties", tags=["properties"])


def _get_or_404(db: Session, property_id: int) -> Property:
    obj = (
        db.query(Property)
        .filter(Property.id == property_id, Property.deleted_at.is_(None))
        .first()
    )
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Property not found")
    return obj


@router.get("", response_model=list[PropertyRead])
def list_properties(
    db: Session = Depends(get_db), _: Property = Depends(get_current_user)
):
    return (
        db.query(Property)
        .filter(Property.deleted_at.is_(None))
        .order_by(Property.id)
        .all()
    )


@router.post("", response_model=PropertyRead, status_code=status.HTTP_201_CREATED)
def create_property(
    payload: PropertyCreate,
    db: Session = Depends(get_db),
    user: Property = Depends(manager_or_admin),
):
    obj = Property(**payload.model_dump())
    obj.created_by = user.id
    obj.updated_by = user.id
    db.add(obj)
    db.flush()
    record_audit(
        db,
        table_name="properties",
        record_id=obj.id,
        action="create",
        actor_id=user.id,
        new_value=serialize_row(obj),
    )
    db.commit()
    db.refresh(obj)
    return obj


@router.get("/{property_id}", response_model=PropertyRead)
def get_property(
    property_id: int,
    db: Session = Depends(get_db),
    _: Property = Depends(get_current_user),
):
    return _get_or_404(db, property_id)


@router.patch("/{property_id}", response_model=PropertyRead)
def update_property(
    property_id: int,
    payload: PropertyUpdate,
    db: Session = Depends(get_db),
    user: Property = Depends(manager_or_admin),
):
    obj = _get_or_404(db, property_id)
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return obj
    old = serialize_row(obj)
    changed = field_changes(obj, updates)
    for field, value in updates.items():
        setattr(obj, field, value)
    obj.updated_by = user.id
    record_audit(
        db,
        table_name="properties",
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


@router.delete("/{property_id}", response_model=MessageResponse)
def delete_property(
    property_id: int,
    db: Session = Depends(get_db),
    user: Property = Depends(manager_or_admin),
):
    obj = _get_or_404(db, property_id)
    from datetime import datetime, timezone

    obj.deleted_at = datetime.now(timezone.utc)
    obj.updated_by = user.id
    record_audit(
        db,
        table_name="properties",
        record_id=obj.id,
        action="soft_delete",
        actor_id=user.id,
        old_value=serialize_row(obj),
    )
    db.commit()
    return MessageResponse(detail="Property deleted")
