"""LeaseService: create/activate/terminate/list/archive/supersede leases.

State machine: DRAFT → ACTIVE → TERMINATED | EXPIRED.
Unit status flips: AVAILABLE → OCCUPIED on activate, OCCUPIED → AVAILABLE on terminate.
Money parsed via parse_money() (Decimal only, AGENTS.md §4).

Coverage Matrix helpers:
- ``create_with_tenant`` (Property 2.6) — atomic tenant+lease creation
- ``supersede_with_new`` (Renewal 6.5) — terminate source + create+activate new
- ``archive`` (Renewal 6.7) — mark TERMINATED + archived_at, idempotent
"""
from __future__ import annotations

from datetime import date, datetime
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
from app.core.time import utcnow
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

    # ---- Coverage Matrix helpers (2.6 / 6.5 / 6.7) ----

    def create_with_tenant(
        self,
        principal: Principal,
        *,
        org_id: int,
        unit_id: int,
        tenant_full_name: str,
        tenant_contact_phone: str | None,
        tenant_contact_email: str | None,
        start_date: date,
        end_date: date,
        monthly_rent: Decimal | str | int,
        deposit: Decimal | str | int = 0,
    ) -> tuple[Tenant, Lease]:
        """Atomic tenant+lease creation (Coverage Matrix Property 2.6).

        Creates the tenant (in DRAFT until the lease is later activated)
        and the lease in one transaction. Used by the Mini App
        ``#/properties/{id}/register-tenant`` workflow.

        Raises ``ConflictError`` if the unit is not AVAILABLE, or if there
        is an overlapping ACTIVE lease on the same unit.
        """
        require_org_scope(principal, org_id)
        if principal.role not in (Role.OWNER, Role.SECRETARY):
            raise PermissionDenied(
                "only OWNER/SECRETARY can register tenants and leases",
            )
        if end_date <= start_date:
            raise ValidationError(
                "end_date must be strictly after start_date",
            )
        if not isinstance(tenant_full_name, str) or not tenant_full_name.strip():
            raise ValidationError(
                "tenant_full_name is required and must be non-empty",
            )
        unit = self.db.get(Unit, unit_id)
        if unit is None or unit.org_id != org_id:
            raise NotFoundError(
                f"unit {unit_id} not found in org {org_id}",
            )
        if unit.status != UnitStatus.AVAILABLE.value:
            raise ConflictError(
                f"unit {unit_id} is not AVAILABLE (status={unit.status})",
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
        tenant = Tenant(
            org_id=org_id,
            user_id=None,
            full_name=tenant_full_name.strip(),
            contact_phone=tenant_contact_phone,
            contact_email=tenant_contact_email,
        )
        self.db.add(tenant)
        self.db.flush()  # get tenant.id without committing
        lease = Lease(
            org_id=org_id,
            unit_id=unit_id,
            tenant_id=tenant.id,
            start_date=start_date,
            end_date=end_date,
            monthly_rent=parse_money(monthly_rent),
            deposit=parse_money(deposit),
            state=LeaseState.DRAFT.value,
        )
        self.db.add(lease)
        self.db.commit()
        self.db.refresh(tenant)
        self.db.refresh(lease)
        return tenant, lease

    def supersede_with_new(
        self,
        principal: Principal,
        *,
        org_id: int,
        source_lease_id: int,
        new_start_date: date,
        new_end_date: date,
        new_monthly_rent: Decimal | str | int,
        new_deposit: Decimal | str | int = 0,
    ) -> tuple[Lease, Lease]:
        """Coverage Matrix Renewal 6.5: supersede source lease with new.

        Atomically:
          1. Verify source lease is ACTIVE.
          2. Verify no overlapping ACTIVE lease on the same unit at the
             proposed dates.
          3. Terminate source (ACTIVE → TERMINATED, unit → AVAILABLE).
          4. Create new Lease in DRAFT with the proposed terms.
          5. Activate new lease (DRAFT → ACTIVE, unit → OCCUPIED).

        Returns (terminated_source, new_active).
        """
        require_org_scope(principal, org_id)
        if principal.role not in (Role.OWNER, Role.SECRETARY):
            raise PermissionDenied(
                "only OWNER/SECRETARY can supersede leases",
            )
        source = self.db.get(Lease, source_lease_id)
        if source is None or source.org_id != org_id:
            raise NotFoundError(
                f"lease {source_lease_id} not found in org {org_id}",
            )
        if source.state != LeaseState.ACTIVE.value:
            raise ConflictError(
                f"lease {source_lease_id} cannot be superseded from "
                f"state {source.state}",
            )
        overlapping = (
            self.db.query(Lease)
            .filter(
                Lease.org_id == org_id,
                Lease.unit_id == source.unit_id,
                Lease.id != source.id,
                Lease.state == LeaseState.ACTIVE.value,
                Lease.end_date > new_start_date,
                Lease.start_date < new_end_date,
            )
            .one_or_none()
        )
        if overlapping is not None:
            raise ConflictError(
                f"unit {source.unit_id} already has overlapping "
                f"ACTIVE lease {overlapping.id}",
            )
        # 1) Terminate source.
        source.state = LeaseState.TERMINATED.value
        source.archived_at = utcnow()
        # 2) Create new in DRAFT.
        new_lease = Lease(
            org_id=org_id,
            unit_id=source.unit_id,
            tenant_id=source.tenant_id,
            start_date=new_start_date,
            end_date=new_end_date,
            monthly_rent=parse_money(new_monthly_rent),
            deposit=parse_money(new_deposit),
            state=LeaseState.DRAFT.value,
        )
        self.db.add(new_lease)
        self.db.flush()
        # 3) Activate new.
        new_lease.state = LeaseState.ACTIVE.value
        # 4) Flip unit status.
        unit = self.db.get(Unit, source.unit_id)
        if unit is not None:
            unit.status = UnitStatus.OCCUPIED.value
        self.db.commit()
        self.db.refresh(source)
        self.db.refresh(new_lease)
        return source, new_lease

    def archive(
        self,
        principal: Principal,
        *,
        org_id: int,
        lease_id: int,
    ) -> Lease:
        """Coverage Matrix Renewal 6.7: archive a TERMINATED lease.

        Idempotent: if already archived, returns the lease unchanged.
        Sets ``archived_at`` (UTC). Cross-org → NotFoundError.
        """
        require_org_scope(principal, org_id)
        if principal.role not in (Role.OWNER, Role.SECRETARY):
            raise PermissionDenied(
                "only OWNER/SECRETARY can archive leases",
            )
        lease = self.db.get(Lease, lease_id)
        if lease is None or lease.org_id != org_id:
            raise NotFoundError(
                f"lease {lease_id} not found in org {org_id}",
            )
        if lease.archived_at is not None:
            return lease  # idempotent
        lease.archived_at = utcnow()
        self.db.commit()
        self.db.refresh(lease)
        return lease


__all__ = [
    "create_lease",
    "activate_lease",
    "terminate_lease",
    "LeaseService",
]