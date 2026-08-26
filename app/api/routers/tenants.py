from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.database import get_db
from app.models.membership import OrganizationRole
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.common import MessageResponse, Paginated
from app.schemas.tenant import TenantCreate, TenantPublic, TenantUpdate
from app.services.audit import field_changes, record_audit, serialize_row
from app.services.organization_scope import (
    CrossOrgReference,
    OwnerRequired,
    ScopeBlocked,
    resolve_org_membership,
    scope_exception_to_http,
    scoped_get_tenant,
    scoped_list_tenants,
)

router = APIRouter(prefix="/tenants", tags=["tenants"])


def _to_public(obj: Tenant) -> TenantPublic:
    return TenantPublic(
        id=obj.id,
        full_name=obj.full_name,
        phone=obj.phone,
        secondary_phone=obj.secondary_phone,
        telegram=obj.telegram,
        whatsapp=obj.whatsapp,
        email=obj.email,
        contact_status=obj.contact_status,
        last_confirmed_at=obj.last_confirmed_at,
        last_confirmed_by=obj.last_confirmed_by,
        notes=obj.notes,
        nationality=obj.nationality,
        emergency_name=obj.emergency_name,
        emergency_relationship=obj.emergency_relationship,
        emergency_phone=obj.emergency_phone,
        is_active=obj.is_active,
        id_registered=bool(obj.id_number),
        created_at=obj.created_at,
        updated_at=obj.updated_at,
    )


@router.get("", response_model=Paginated[TenantPublic])
def list_tenants(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        from app.services.organization_scope import list_active_org_ids_for_user
        orgs = list_active_org_ids_for_user(db, user.id)
        if not orgs:
            return Paginated(items=[], total=0, limit=max(1, min(limit, 500)), offset=max(0, offset))
        query = (
            db.query(Tenant)
            .filter(
                Tenant.organization_id.in_(orgs),
                Tenant.deleted_at.is_(None),
            )
            .order_by(Tenant.id)
        )
    except Exception as exc:
        from app.services.organization_scope import scope_exception_to_http
        raise scope_exception_to_http(exc) from exc
    total = query.count()
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    rows = [_to_public(obj) for obj in query.offset(offset).limit(limit).all()]
    return Paginated(items=rows, total=total, limit=limit, offset=offset)


@router.post("", response_model=TenantPublic, status_code=status.HTTP_201_CREATED)
def create_tenant(
    payload: TenantCreate,
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
    except (ScopeBlocked, LookupError) as exc:
        raise scope_exception_to_http(exc) from exc
    obj = Tenant(**payload.model_dump())
    obj.created_by = user.id
    obj.updated_by = user.id
    db.add(obj)
    db.flush()
    record_audit(
        db,
        table_name="tenants",
        record_id=obj.id,
        action="create",
        actor_id=user.id,
        new_value=serialize_row(obj),
    )
    db.commit()
    db.refresh(obj)
    return _to_public(obj)


@router.get("/{tenant_id}", response_model=TenantPublic)
def get_tenant(
    tenant_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        obj, _ = scoped_get_tenant(
            db,
            tenant_id,
            for_user_id=user.id,
            role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY],
        )
    except (LookupError, ScopeBlocked) as exc:
        raise scope_exception_to_http(exc) from exc
    return _to_public(obj)


@router.patch("/{tenant_id}", response_model=TenantPublic)
def update_tenant(
    tenant_id: int,
    payload: TenantUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        obj, _ = scoped_get_tenant(
            db,
            tenant_id,
            for_user_id=user.id,
            role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY],
        )
        updates = payload.model_dump(exclude_unset=True)
        if "organization_id" in updates:
            raise CrossOrgReference(
                "Tenant organization_id is immutable in this milestone; use a dedicated transfer workflow"
            )
    except (LookupError, ScopeBlocked, CrossOrgReference) as exc:
        raise scope_exception_to_http(exc) from exc
    if not updates:
        return _to_public(obj)
    old = serialize_row(obj)
    changed = field_changes(obj, updates)
    for field, value in updates.items():
        setattr(obj, field, value)
    obj.updated_by = user.id
    record_audit(
        db,
        table_name="tenants",
        record_id=obj.id,
        action="update",
        actor_id=user.id,
        changed_fields=changed,
        old_value=old,
        new_value=serialize_row(obj),
    )
    db.commit()
    db.refresh(obj)
    return _to_public(obj)


@router.delete("/{tenant_id}", response_model=MessageResponse)
def delete_tenant(
    tenant_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        obj, _ = scoped_get_tenant(
            db,
            tenant_id,
            for_user_id=user.id,
            role=OrganizationRole.OWNER,
        )
    except (LookupError, ScopeBlocked, OwnerRequired) as exc:
        raise scope_exception_to_http(exc) from exc
    from datetime import datetime, timezone

    obj.deleted_at = datetime.now(timezone.utc)
    obj.updated_by = user.id
    record_audit(
        db,
        table_name="tenants",
        record_id=obj.id,
        action="soft_delete",
        actor_id=user.id,
        old_value=serialize_row(obj),
    )
    db.commit()
    return MessageResponse(detail="Tenant deleted")
