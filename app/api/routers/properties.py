from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.database import get_db
from app.models.membership import OrganizationRole
from app.models.property import Property
from app.models.user import User
from app.schemas.common import MessageResponse
from app.schemas.property import PropertyCreate, PropertyRead, PropertyUpdate
from app.services.audit import field_changes, record_audit, serialize_row
from app.services.organization_scope import scope_exception_to_http
from app.services.property_channel import (
    OwnerRequired,
    ScopeBlocked,
    filter_secretary_property_updates,
    resolve_org_membership,
    scoped_get_property,
    scoped_list_properties,
)

router = APIRouter(prefix="/properties", tags=["properties"])


@router.get("", response_model=list[PropertyRead])
def list_properties(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    try:
        return scoped_list_properties(db, for_user_id=user.id)
    except Exception as exc:  # noqa: BLE001
        raise scope_exception_to_http(exc) from exc


@router.post("", response_model=PropertyRead, status_code=status.HTTP_201_CREATED)
def create_property(
    payload: PropertyCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        resolve_org_membership(
            db,
            user.id,
            payload.organization_id,
            role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY],
        )
    except ScopeBlocked as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc

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
    user: User = Depends(get_current_user),
):
    try:
        prop, _membership = scoped_get_property(db, property_id, for_user_id=user.id)
    except Exception as exc:  # noqa: BLE001
        raise scope_exception_to_http(exc) from exc
    return prop


@router.patch("/{property_id}", response_model=PropertyRead)
def update_property(
    property_id: int,
    payload: PropertyUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        obj, membership = scoped_get_property(db, property_id, for_user_id=user.id)
    except Exception as exc:  # noqa: BLE001
        raise scope_exception_to_http(exc) from exc

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return obj

    try:
        if membership.role != OrganizationRole.OWNER:
            filter_secretary_property_updates(set(updates.keys()))
    except OwnerRequired as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc

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
    user: User = Depends(get_current_user),
):
    try:
        obj, membership = scoped_get_property(db, property_id, for_user_id=user.id)
    except Exception as exc:  # noqa: BLE001
        raise scope_exception_to_http(exc) from exc
    if membership.role != OrganizationRole.OWNER:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only ACTIVE OWNER may soft-delete a Property",
        )
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
