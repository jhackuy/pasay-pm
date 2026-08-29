"""TenantService: create/list/get tenants; org-scope enforced.

This module exposes BOTH:
- A class-based ``TenantService`` for legacy routes.
- Module-level functions (``create_tenant``, ``get_tenant``) used by V1 handlers.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.permissions import (
    PermissionDenied,
    Principal,
    Role,
    require_org_scope,
)
from app.v1.models.tenant_lease import Tenant
from app.v1.services.errors import (
    NotFoundError,
    ValidationError,
)


def _principal_from(
    user_id: int, org_id: int, role: str | Role,
) -> Principal:
    """Build a Principal from raw kwargs for module-level functions."""
    parsed = role if isinstance(role, Role) else Role.parse(role)
    return Principal(
        user_id=user_id,
        org_id=org_id,
        role=parsed,
        membership_state="ACTIVE",
    )


def create_tenant(
    db: Session,
    *,
    org_id: int,
    full_name: str,
    actor_user_id: int,
    actor_role: str | Role,
) -> Tenant:
    """Create a new Tenant in ``org_id``.

    Raises ``ValidationError`` if ``full_name`` is empty/whitespace.
    """
    if not isinstance(full_name, str) or not full_name.strip():
        raise ValidationError(
            "full_name is required and must be non-empty",
        )
    principal = _principal_from(actor_user_id, org_id, actor_role)
    require_org_scope(principal, org_id)
    if principal.role not in (Role.OWNER, Role.SECRETARY):
        raise PermissionDenied(
            "only OWNER/SECRETARY can create tenants",
        )
    t = Tenant(
        org_id=org_id,
        user_id=None,
        full_name=full_name.strip(),
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def get_tenant(
    db: Session,
    *,
    org_id: int,
    tenant_id: int,
    actor_user_id: int,
    actor_role: str | Role,
) -> Tenant:
    """Look up a Tenant by id; raise NotFoundError on cross-org."""
    principal = _principal_from(actor_user_id, org_id, actor_role)
    require_org_scope(principal, org_id)
    t = db.get(Tenant, tenant_id)
    if t is None or t.org_id != org_id:
        raise NotFoundError(
            f"tenant {tenant_id} not found in org {org_id}",
        )
    return t


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
        if not isinstance(full_name, str) or not full_name.strip():
            raise ValidationError(
                "full_name is required and must be non-empty",
            )
        t = Tenant(
            org_id=org_id,
            user_id=user_id,
            full_name=full_name.strip(),
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


__all__ = [
    "TenantService",
    "create_tenant",
    "get_tenant",
]