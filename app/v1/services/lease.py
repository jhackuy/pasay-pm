"""LeaseService: create/activate/terminate/list leases.

State machine: DRAFT → ACTIVE → TERMINATED | EXPIRED.
Unit status flips: AVAILABLE → OCCUPIED on activate, OCCUPIED → AVAILABLE on terminate.
Money parsed via parse_money() (Decimal only, AGENTS.md §4).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.core.money import parse_money
from app.core.permissions import (
    PermissionDenied,
    Principal,
    Role,
    require_org_scope,
)
from app.v1.models.base import LeaseState, UnitStatus
from app.v1.models.property import Unit
from app.v1.models.tenant_lease import Lease, LeaseContactStatus, Tenant
from app.v1.services.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.v1.services.property import set_unit_status


def _principal_from(
    user_id: int, org_id: int, role: str | Role,
) -> Principal:
    """Build a Principal from raw kwargs for module-level functions.

    Mirrors the helper used in property/tenant services: synthesizes a
    Principal with membership_state=ACTIVE so ``require_org_scope`` works.
    """
    parsed = role if isinstance(role, Role) else Role.parse(role)
    return Principal(
        user_id=user_id,
        org_id=org_id,
        role=parsed,
        membership_state="ACTIVE",
    )


def create_lease(
    db: Session,
    *,
    org_id: int,
    unit_id: int,
    tenant_id: int,
    start_date: date,
    end_date: date,
    monthly_rent: Decimal | str | int,
    deposit: Decimal | str | int = 0,
    owner_user_id: int,
    actor_role: str | Role,
) -> Any:
    """Create a lease in DRAFT state.

    Raises ``ValidationError`` if ``end_date <= start_date``.
    Raises ``ConflictError`` if the unit is not AVAILABLE or if there is
    an overlapping ACTIVE lease on the same unit.

    ``monthly_rent`` and ``deposit`` are parsed via ``parse_money``
    (Decimal-only, NEVER float — AGENTS.md §4).
    """
    if end_date <= start_date:
        raise ValidationError(
            "end_date must be strictly after start_date",
        )
    principal = _principal_from(owner_user_id, org_id, actor_role)
    require_org_scope(principal, org_id)
    if principal.role not in (Role.OWNER, Role.SECRETARY):
        raise PermissionDenied(
            "only OWNER/SECRETARY can create leases",
        )
    unit = db.get(Unit, unit_id)
    if unit is None or unit.org_id != org_id:
        raise NotFoundError(
            f"unit {unit_id} not found in org {org_id}",
        )
    tenant = db.get(Tenant, tenant_id)
    if tenant is None or tenant.org_id != org_id:
        raise NotFoundError(
            f"tenant {tenant_id} not found in org {org_id}",
        )
    if unit.status != UnitStatus.AVAILABLE.value:
        raise ConflictError(
            f"unit {unit_id} is not AVAILABLE "
            f"(status={unit.status})",
        )
    overlapping = (
        db.query(Lease)
        .filter(
            Lease.unit_id == unit_id,
            Lease.state == LeaseState.ACTIVE.value,
            Lease.end_date > start_date,
            Lease.start_date < end_date,
        )
        .one_or_none()
    )
    if overlapping is not None:
        raise ConflictError(
            f"unit {unit_id} already has overlapping ACTIVE "
            f"lease {overlapping.id}",
        )
    lease = Lease(
        org_id=org_id,
        unit_id=unit_id,
        tenant_id=tenant_id,
        start_date=start_date,
        end_date=end_date,
        monthly_rent=parse_money(monthly_rent),
        deposit=parse_money(deposit),
        state=LeaseState.DRAFT.value,
    )
    db.add(lease)
    db.commit()
    db.refresh(lease)
    return lease


def activate_lease(
    db: Session,
    *,
    org_id: int,
    lease_id: int,
    actor_user_id: int,
    actor_role: str | Role,
) -> Any:
    """DRAFT → ACTIVE. Flips unit status to OCCUPIED via ``set_unit_status``.

    The unit-status flip and the lease-state change are committed in the
    same SQL transaction: we mutate ``lease.state`` in-session first and
    then call ``set_unit_status`` whose ``db.commit()`` flushes both
    pending changes atomically.
    """
    principal = _principal_from(actor_user_id, org_id, actor_role)
    require_org_scope(principal, org_id)
    if principal.role not in (Role.OWNER, Role.SECRETARY):
        raise PermissionDenied(
            "only OWNER/SECRETARY can activate leases",
        )
    lease = db.get(Lease, lease_id)
    if lease is None or lease.org_id != org_id:
        raise NotFoundError(
            f"lease {lease_id} not found in org {org_id}",
        )
    if lease.state != LeaseState.DRAFT.value:
        raise ConflictError(
            f"lease {lease_id} cannot be activated from "
            f"state {lease.state}",
        )
    # Mutate lease state in-session; the set_unit_status call below
    # commits both changes in a single transaction.
    lease.state = LeaseState.ACTIVE.value
    set_unit_status(
        db,
        org_id=org_id,
        unit_id=lease.unit_id,
        status=UnitStatus.OCCUPIED,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
    )
    db.refresh(lease)
    return lease


def terminate_lease(
    db: Session,
    *,
    org_id: int,
    lease_id: int,
    actor_user_id: int,
    actor_role: str | Role,
) -> Any:
    """ACTIVE → TERMINATED. Flips unit status back to AVAILABLE.

    The unit-status flip and the lease-state change are committed in the
    same SQL transaction via ``set_unit_status``.
    """
    principal = _principal_from(actor_user_id, org_id, actor_role)
    require_org_scope(principal, org_id)
    if principal.role not in (Role.OWNER, Role.SECRETARY):
        raise PermissionDenied(
            "only OWNER/SECRETARY can terminate leases",
        )
    lease = db.get(Lease, lease_id)
    if lease is None or lease.org_id != org_id:
        raise NotFoundError(
            f"lease {lease_id} not found in org {org_id}",
        )
    if lease.state != LeaseState.ACTIVE.value:
        raise ConflictError(
            f"lease {lease_id} cannot be terminated from "
            f"state {lease.state}",
        )
    lease.state = LeaseState.TERMINATED.value
    set_unit_status(
        db,
        org_id=org_id,
        unit_id=lease.unit_id,
        status=UnitStatus.AVAILABLE,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
    )
    db.refresh(lease)
    return lease


class LeaseService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create_lease(
        self,
        principal: Principal,
        *,
        org_id: int,
        unit_id: int,
        tenant_id: int,
        start_date: date,
        end_date: date,
        monthly_rent: Decimal | str | int,
        deposit: Decimal | str | int = 0,
    ) -> Lease:
        require_org_scope(principal, org_id)
        if principal.role not in (Role.OWNER, Role.SECRETARY):
            raise PermissionDenied(
                "only OWNER/SECRETARY can create leases",
            )
        if end_date <= start_date:
            raise ValidationError(
                "end_date must be strictly after start_date",
            )
        unit = self.db.get(Unit, unit_id)
        if unit is None or unit.org_id != org_id:
            raise NotFoundError(
                f"unit {unit_id} not found in org {org_id}",
            )
        tenant = self.db.get(Tenant, tenant_id)
        if tenant is None or tenant.org_id != org_id:
            raise NotFoundError(
                f"tenant {tenant_id} not found in org {org_id}",
            )
        if unit.status != UnitStatus.AVAILABLE.value:
            raise ConflictError(
                f"unit {unit_id} is not AVAILABLE "
                f"(status={unit.status})",
            )
        overlapping = (
            self.db.query(Lease)
            .filter(
                Lease.unit_id == unit_id,
                Lease.state == LeaseState.ACTIVE.value,
                Lease.end_date > start_date,
                Lease.start_date < end_date,
            )
            .one_or_none()
        )
        if overlapping is not None:
            raise ConflictError(
                f"unit {unit_id} already has overlapping ACTIVE "
                f"lease {overlapping.id}",
            )
        lease = Lease(
            org_id=org_id,
            unit_id=unit_id,
            tenant_id=tenant_id,
            start_date=start_date,
            end_date=end_date,
            monthly_rent=parse_money(monthly_rent),
            deposit=parse_money(deposit),
            state=LeaseState.DRAFT.value,
        )
        self.db.add(lease)
        self.db.commit()
        self.db.refresh(lease)
        return lease

    def activate_lease(
        self,
        principal: Principal,
        *,
        org_id: int,
        lease_id: int,
    ) -> Lease:
        require_org_scope(principal, org_id)
        if principal.role not in (Role.OWNER, Role.SECRETARY):
            raise PermissionDenied(
                "only OWNER/SECRETARY can activate leases",
            )
        lease = self.db.get(Lease, lease_id)
        if lease is None or lease.org_id != org_id:
            raise NotFoundError(
                f"lease {lease_id} not found in org {org_id}",
            )
        if lease.state != LeaseState.DRAFT.value:
            raise ConflictError(
                f"lease {lease_id} cannot be activated from "
                f"state {lease.state}",
            )
        lease.state = LeaseState.ACTIVE.value
        unit = self.db.get(Unit, lease.unit_id)
        if unit is not None and unit.status == UnitStatus.AVAILABLE.value:
            unit.status = UnitStatus.OCCUPIED.value
        self.db.commit()
        self.db.refresh(lease)
        return lease

    def terminate_lease(
        self,
        principal: Principal,
        *,
        org_id: int,
        lease_id: int,
    ) -> Lease:
        require_org_scope(principal, org_id)
        if principal.role not in (Role.OWNER, Role.SECRETARY):
            raise PermissionDenied(
                "only OWNER/SECRETARY can terminate leases",
            )
        lease = self.db.get(Lease, lease_id)
        if lease is None or lease.org_id != org_id:
            raise NotFoundError(
                f"lease {lease_id} not found in org {org_id}",
            )
        if lease.state != LeaseState.ACTIVE.value:
            raise ConflictError(
                f"lease {lease_id} cannot be terminated from "
                f"state {lease.state}",
            )
        lease.state = LeaseState.TERMINATED.value
        unit = self.db.get(Unit, lease.unit_id)
        if unit is not None and unit.status == UnitStatus.OCCUPIED.value:
            unit.status = UnitStatus.AVAILABLE.value
        self.db.commit()
        self.db.refresh(lease)
        return lease

    def list_leases(
        self, principal: Principal, *, org_id: int,
    ) -> list[Lease]:
        require_org_scope(principal, org_id)
        return (
            self.db.query(Lease)
            .filter(Lease.org_id == org_id)
            .order_by(Lease.id.asc())
            .all()
        )

    def set_contact_status(
        self,
        principal: Principal,
        *,
        org_id: int,
        lease_id: int,
        contact_status: str | LeaseContactStatus,
    ) -> Lease:
        """Update the lease's contact/follow-up state.

        Used by the Telegram NL bridge (Tenant replied / Wrong number) and
        the Owner/Secretary contact flow. Owner + Secretary may update;
        the resulting state is one of the LeaseContactStatus values.
        """
        require_org_scope(principal, org_id)
        if principal.role not in (Role.OWNER, Role.SECRETARY):
            raise PermissionDenied(
                "only OWNER/SECRETARY can update lease contact status",
            )
        parsed = (
            contact_status
            if isinstance(contact_status, LeaseContactStatus)
            else LeaseContactStatus(contact_status)
        )
        lease = self.db.get(Lease, lease_id)
        if lease is None or lease.org_id != org_id:
            raise NotFoundError(
                f"lease {lease_id} not found in org {org_id}",
            )
        lease.contact_status = parsed.value
        self.db.commit()
        self.db.refresh(lease)
        return lease


__all__ = [
    "create_lease",
    "activate_lease",
    "terminate_lease",
    "LeaseService",
]