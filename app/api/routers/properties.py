from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.i18n import resolve_locale as _resolve_locale, t as _t
from app.database import get_db
from app.models.membership import Membership, MembershipState, OrganizationRole
from app.models.property import Property
from app.models.user import User
from app.schemas.common import MessageResponse, Paginated
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


@router.get("", response_model=Paginated[PropertyRead])
def list_properties(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    try:
        rows = scoped_list_properties(db, for_user_id=user.id)
    except Exception as exc:  # noqa: BLE001
        raise scope_exception_to_http(exc) from exc
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    total = len(rows)
    paged = rows[offset:offset + limit]
    return Paginated(items=paged, total=total, limit=limit, offset=offset)


@router.post("", response_model=PropertyRead, status_code=status.HTTP_201_CREATED)
def create_property(
    payload: PropertyCreate,
    accept_language: str | None = Header(default=None, include_in_schema=False),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    locale = _resolve_locale(
        membership_role=None,
        user_role_tier=str(getattr(user, "role", "") or ""),
        accept_language_header=accept_language,
    )
    try:
        resolve_org_membership(
            db,
            user.id,
            payload.organization_id,
            role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY],
        )
    except ScopeBlocked:
        raise HTTPException(status.HTTP_403_FORBIDDEN, _t("403_no_permission", locale)) from None

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
    accept_language: str | None = Header(default=None, include_in_schema=False),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _first = (
        db.query(Membership)
        .filter(
            Membership.user_id == user.id,
            Membership.state == MembershipState.ACTIVE,
            Membership.removed_at.is_(None),
        )
        .order_by(Membership.organization_id.asc())
        .first()
    )
    default_role = _first.role if _first is not None else None
    default_locale = _resolve_locale(
        membership_role=default_role,
        user_role_tier=str(getattr(user, "role", "") or ""),
        accept_language_header=accept_language,
    )
    prop = None
    membership = None
    try:
        prop, membership = scoped_get_property(db, property_id, for_user_id=user.id)
    except ScopeBlocked:
        raise HTTPException(status.HTTP_403_FORBIDDEN, _t("403_no_permission", default_locale)) from None
    except Exception as exc:  # noqa: BLE001
        mapped = scope_exception_to_http(exc)
        mem_role = membership.role if membership is not None else default_role
        locale = _resolve_locale(
            membership_role=mem_role,
            user_role_tier=str(getattr(user, "role", "") or ""),
            accept_language_header=accept_language,
        )
        if mapped.status_code == 404:
            raise HTTPException(404, _t("404_not_found", locale)) from None
        if mapped.status_code == 409:
            raise HTTPException(409, _t("409_conflict", locale)) from None
        raise mapped from exc
    mem_role = membership.role if membership is not None else default_role
    locale = _resolve_locale(
        membership_role=mem_role,
        user_role_tier=str(getattr(user, "role", "") or ""),
        accept_language_header=accept_language,
    )
    return prop


@router.patch("/{property_id}", response_model=PropertyRead)
def update_property(
    property_id: int,
    payload: PropertyUpdate,
    accept_language: str | None = Header(default=None, include_in_schema=False),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    locale = _resolve_locale(
        membership_role=None,
        user_role_tier=str(getattr(user, "role", "") or ""),
        accept_language_header=accept_language,
    )
    try:
        obj, membership = scoped_get_property(db, property_id, for_user_id=user.id)
    except ScopeBlocked:
        raise HTTPException(status.HTTP_403_FORBIDDEN, _t("403_no_permission", locale)) from None
    except Exception as exc:  # noqa: BLE001
        mapped = scope_exception_to_http(exc)
        if mapped.status_code == 404:
            raise HTTPException(404, _t("404_not_found", locale)) from None
        raise mapped from exc

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return obj

    try:
        if membership.role != OrganizationRole.OWNER:
            filter_secretary_property_updates(set(updates.keys()))
    except OwnerRequired:
        raise HTTPException(status.HTTP_403_FORBIDDEN, _t("403_no_permission", locale)) from None

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
    accept_language: str | None = Header(default=None, include_in_schema=False),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    locale = _resolve_locale(
        membership_role=None,
        user_role_tier=str(getattr(user, "role", "") or ""),
        accept_language_header=accept_language,
    )
    try:
        obj, membership = scoped_get_property(db, property_id, for_user_id=user.id)
    except ScopeBlocked:
        raise HTTPException(status.HTTP_403_FORBIDDEN, _t("403_no_permission", locale)) from None
    except Exception as exc:  # noqa: BLE001
        mapped = scope_exception_to_http(exc)
        if mapped.status_code == 404:
            raise HTTPException(404, _t("404_not_found", locale)) from None
        raise mapped from exc
    if membership.role != OrganizationRole.OWNER:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            _t("403_no_permission", locale),
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
