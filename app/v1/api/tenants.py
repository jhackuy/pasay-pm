"""Tenant API — thin router."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.permissions import PermissionDenied, Principal, Role
from app.v1.deps import get_current_principal, get_db_dep, require_role
from app.v1.schemas.tenant import TenantCreate, TenantRead
from app.v1.services.errors import NotFoundError
from app.v1.services.tenant import TenantService

router = APIRouter(prefix="/tenants", tags=["tenants"])


@router.post(
    "", response_model=TenantRead, status_code=status.HTTP_201_CREATED,
)
def create_tenant(
    body: TenantCreate,
    org_id: int,
    principal: Principal = Depends(require_role(Role.OWNER, Role.SECRETARY)),
    db: Session = Depends(get_db_dep),
) -> TenantRead:
    svc = TenantService(db)
    try:
        t = svc.create_tenant(
            principal,
            org_id=org_id,
            user_id=body.user_id,
            full_name=body.full_name,
            contact_phone=body.contact_phone,
            contact_email=body.contact_email,
        )
    except PermissionDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    return TenantRead.model_validate(t)


@router.get("", response_model=list[TenantRead])
def list_tenants(
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[TenantRead]:
    svc = TenantService(db)
    return [
        TenantRead.model_validate(t)
        for t in svc.list_tenants(principal, org_id=org_id)
    ]


@router.get("/{tenant_id}", response_model=TenantRead)
def get_tenant(
    tenant_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> TenantRead:
    svc = TenantService(db)
    try:
        t = svc.get_tenant(
            principal, org_id=org_id, tenant_id=tenant_id,
        )
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return TenantRead.model_validate(t)


@router.delete("/{tenant_id}", response_model=TenantRead)
def soft_delete_tenant(
    tenant_id: int,
    org_id: int,
    principal: Principal = Depends(require_role(Role.OWNER)),
    db: Session = Depends(get_db_dep),
) -> TenantRead:
    """Coverage Matrix Move-out 7.8: soft-delete a tenant (OWNER only).

    Sets ``archived_at`` (UTC); row is never erased. History is retained
    for audit. Idempotent: re-DELETE returns the same row unchanged.
    """
    svc = TenantService(db)
    try:
        t = svc.soft_delete(
            principal, org_id=org_id, tenant_id=tenant_id,
        )
    except PermissionDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return TenantRead.model_validate(t)
