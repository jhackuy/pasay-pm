"""TenantService: create/list/get tenants; org-scope enforced."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.permissions import (
    PermissionDenied,
    Principal,
    Role,
    require_org_scope,
)
from app.v1.models.tenant_lease import Tenant
from app.v1.services.errors import NotFoundError


class TenantService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create_tenant(
        self,
        principal: Principal,
        *,
        org_id: int,
        user_id: int | None,
        full_name: str,
        contact_phone: str | None = None,
        contact_email: str | None = None,
    ) -> Tenant:
        require_org_scope(principal, org_id)
        if principal.role not in (Role.OWNER, Role.SECRETARY):
            raise PermissionDenied(
                "only OWNER/SECRETARY can create tenants",
            )
        t = Tenant(
            org_id=org_id,
            user_id=user_id,
            full_name=full_name,
            contact_phone=contact_phone,
            contact_email=contact_email,
        )
        self.db.add(t)
        self.db.commit()
        self.db.refresh(t)
        return t

    def list_tenants(
        self, principal: Principal, *, org_id: int,
    ) -> list[Tenant]:
        require_org_scope(principal, org_id)
        return (
            self.db.query(Tenant)
            .filter(Tenant.org_id == org_id)
            .order_by(Tenant.id.asc())
            .all()
        )

    def get_tenant(
        self,
        principal: Principal,
        *,
        org_id: int,
        tenant_id: int,
    ) -> Tenant:
        require_org_scope(principal, org_id)
        t = self.db.get(Tenant, tenant_id)
        if t is None or t.org_id != org_id:
            raise NotFoundError(
                f"tenant {tenant_id} not found in org {org_id}",
            )
        return t