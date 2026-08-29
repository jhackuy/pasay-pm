"""LeaseService: create/activate/terminate/list leases.

State machine: DRAFT → ACTIVE → TERMINATED | EXPIRED.
Unit status flips: AVAILABLE → OCCUPIED on activate, OCCUPIED → AVAILABLE on terminate.
Money parsed via parse_money() (Decimal only, AGENTS.md §4).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.money import parse_money
from app.core.permissions import (
    PermissionDenied,
    Principal,
    Role,
    require_org_scope,
)
from app.v1.models.property import Unit
from app.v1.models.tenant_lease import Lease, Tenant
from app.v1.services.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)


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
        if unit.status != "AVAILABLE":
            raise ConflictError(
                f"unit {unit_id} is not AVAILABLE "
                f"(status={unit.status})",
            )
        overlapping = (
            self.db.query(Lease)
            .filter(
                Lease.unit_id == unit_id,
                Lease.state == "ACTIVE",
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
            state="DRAFT",
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
        if lease.state != "DRAFT":
            raise ConflictError(
                f"lease {lease_id} cannot be activated from "
                f"state {lease.state}",
            )
        lease.state = "ACTIVE"
        unit = self.db.get(Unit, lease.unit_id)
        if unit is not None and unit.status == "AVAILABLE":
            unit.status = "OCCUPIED"
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
        if lease.state != "ACTIVE":
            raise ConflictError(
                f"lease {lease_id} cannot be terminated from "
                f"state {lease.state}",
            )
        lease.state = "TERMINATED"
        unit = self.db.get(Unit, lease.unit_id)
        if unit is not None and unit.status == "OCCUPIED":
            unit.status = "AVAILABLE"
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