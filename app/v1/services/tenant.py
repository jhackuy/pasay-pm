"""TenantService: create/list/get/soft-delete tenants; org-scope enforced.

This module exposes BOTH:
- A class-based ``TenantService`` for legacy routes.
- Module-level functions (``create_tenant``, ``get_tenant``) used by V1 handlers.

Tenant history retention (Coverage Matrix 7.8): soft-delete only, never
hard-delete. ``archived_at`` timestamp; ``list_tenants`` filters out
archived tenants by default but ``get_tenant`` still returns archived
tenants by id (audit trail).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.permissions import (
    PermissionDenied,
    Principal,
    Role,
    require_org_scope,
)
from app.core.time import utcnow
from app.v1.models.tenant_lease import Tenant
from app.v1.services.errors import (
    ConflictError,
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


def _ensure_role(principal: Principal, *allowed: Role) -> None:
    """Role guard helper."""
    if principal.role not in allowed:
        raise PermissionDenied(
            f"requires one of {[r.value for r in allowed]}; "
            f"got {principal.role.value!r}",
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
        """List ACTIVE (non-archived) tenants by default."""
        require_org_scope(principal, org_id)
        return (
            self.db.query(Tenant)
            .filter(
                Tenant.org_id == org_id,
                Tenant.archived_at.is_(None),
            )
            .order_by(Tenant.id.asc())
            .all()
        )

    def list_all_tenants(
        self, principal: Principal, *, org_id: int,
    ) -> list[Tenant]:
        """List every tenant (incl. archived) — OWNER/SECRETARY audit only."""
        require_org_scope(principal, org_id)
        if principal.role not in (Role.OWNER, Role.SECRETARY):
            raise PermissionDenied(
                "only OWNER/SECRETARY can list archived tenants",
            )
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

    def soft_delete(
        self,
        principal: Principal,
        *,
        org_id: int,
        tenant_id: int,
    ) -> Tenant:
        """Soft-delete a tenant (Coverage Matrix 7.8).

        Sets ``archived_at`` to the current UTC timestamp; never erases
        the row. After soft-delete, ``list_tenants`` no longer returns the
        tenant but ``get_tenant`` does (for audit purposes).

        OWNER-only. Cross-org → NotFoundError. Already-archived → no-op
        with idempotent return.
        """
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER)
        t = self.db.get(Tenant, tenant_id)
        if t is None or t.org_id != org_id:
            raise NotFoundError(
                f"tenant {tenant_id} not found in org {org_id}",
            )
        if t.archived_at is not None:
            return t  # idempotent: already archived
        t.archived_at = utcnow()
        self.db.commit()
        self.db.refresh(t)
        return t


__all__ = [
    "TenantService",
    "create_tenant",
    "get_tenant",
]