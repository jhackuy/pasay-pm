from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, manager_or_admin
from app.database import get_db
from app.models.property import Property, Unit
from app.schemas.common import MessageResponse
from app.schemas.property import UnitCreate, UnitRead, UnitUpdate
from app.services.audit import field_changes, record_audit, serialize_row

router = APIRouter(prefix="/units", tags=["units"])


def _get_or_404(db: Session, unit_id: int) -> Unit:
    obj = db.query(Unit).filter(Unit.id == unit_id, Unit.deleted_at.is_(None)).first()
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unit not found")
    return obj


@router.get("", response_model=list[UnitRead])
def list_units(db: Session = Depends(get_db), _: Unit = Depends(get_current_user)):
    return db.query(Unit).filter(Unit.deleted_at.is_(None)).order_by(Unit.id).all()


@router.post("", response_model=UnitRead, status_code=status.HTTP_201_CREATED)
def create_unit(
    payload: UnitCreate,
    db: Session = Depends(get_db),
    user: Unit = Depends(manager_or_admin),
):
    prop = db.query(Property).filter(Property.id == payload.property_id).first()
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
    unit_id: int, db: Session = Depends(get_db), _: Unit = Depends(get_current_user)
):
    return _get_or_404(db, unit_id)


@router.patch("/{unit_id}", response_model=UnitRead)
def update_unit(
    unit_id: int,
    payload: UnitUpdate,
    db: Session = Depends(get_db),
    user: Unit = Depends(manager_or_admin),
):
    obj = _get_or_404(db, unit_id)
    updates = payload.model_dump(exclude_unset=True)
    if updates.get("property_id") is not None:
        prop = db.query(Property).filter(Property.id == updates["property_id"]).first()
        if prop is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Property not found")
    if not updates:
        return obj
    old = serialize_row(obj)
    changed = field_changes(obj, updates)
    for field, value in updates.items():
        setattr(obj, field, value)
    obj.updated_by = user.id
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


@router.delete("/{unit_id}", response_model=MessageResponse)
def delete_unit(
    unit_id: int,
    db: Session = Depends(get_db),
    user: Unit = Depends(manager_or_admin),
):
    obj = _get_or_404(db, unit_id)
    from datetime import datetime, timezone

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
