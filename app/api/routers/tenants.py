from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, manager_or_admin
from app.database import get_db
from app.models.tenant import Tenant
from app.schemas.common import MessageResponse
from app.schemas.tenant import TenantCreate, TenantPublic, TenantUpdate
from app.services.audit import field_changes, record_audit, serialize_row

router = APIRouter(prefix="/tenants", tags=["tenants"])


def _get_or_404(db: Session, tenant_id: int) -> Tenant:
    obj = (
        db.query(Tenant)
        .filter(Tenant.id == tenant_id, Tenant.deleted_at.is_(None))
        .first()
    )
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tenant not found")
    return obj


def _to_public(obj: Tenant) -> TenantPublic:
    """Safe tenant read shape: the raw id_number / id file ids are NEVER
    returned (group / Daily Digest / archive bodies can only show the
    ``id_registered`` boolean = ``ID：已登记``)."""
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


@router.get("", response_model=list[TenantPublic])
def list_tenants(db: Session = Depends(get_db), _: Tenant = Depends(get_current_user)):
    return [
        _to_public(obj)
        for obj in db.query(Tenant).filter(Tenant.deleted_at.is_(None)).order_by(Tenant.id).all()
    ]


@router.post("", response_model=TenantPublic, status_code=status.HTTP_201_CREATED)
def create_tenant(
    payload: TenantCreate,
    db: Session = Depends(get_db),
    user: Tenant = Depends(manager_or_admin),
):
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
    tenant_id: int, db: Session = Depends(get_db), _: Tenant = Depends(get_current_user)
):
    return _to_public(_get_or_404(db, tenant_id))


@router.patch("/{tenant_id}", response_model=TenantPublic)
def update_tenant(
    tenant_id: int,
    payload: TenantUpdate,
    db: Session = Depends(get_db),
    user: Tenant = Depends(manager_or_admin),
):
    obj = _get_or_404(db, tenant_id)
    updates = payload.model_dump(exclude_unset=True)
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
    user: Tenant = Depends(manager_or_admin),
):
    obj = _get_or_404(db, tenant_id)
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
