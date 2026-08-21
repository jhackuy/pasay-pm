"""Canonical Organization Scope Foundation (PASAY-MILESTONE-001).

Single home for every domain's organization-scoped read and write guards.

Ownership chains (M1 canonical):
    Property  -> organization_id  (the root boundary; NOT NULL after M1)
    Unit      -> property_id      -> Property.organization_id
    Lease     -> unit_id          -> Unit.property_id -> Property.organization_id
    Income    -> lease_id         -> Lease.unit_id -> ... -> Property.organization_id
    Expense   -> unit_id          -> Unit.property_id -> Property.organization_id
    Repair    -> unit_id/property_id  -> ... -> Property.organization_id
    Tenant    -> organization_id  (NOT NULL added in M1)

Permission semantics (§5 in M1 contract):
    * list:   only rows whose ownership chain resolves to an Organization
              where the caller has an ACTIVE Membership.
    * lookup: cross-org object -> 404 fail-closed (not 403).
    * action: same Org but Membership.role insufficient -> 403.
    * writes: any referenced foreign object (Unit/Tenant/Lease/Expense/...)
              MUST be co-org with the caller's ACTIVE Membership, else 409.

Philosophy:
    * New human-authority code paths in this module exclusively use
      Membership (ACTIVE + OWNER/SECRETARY).
    * Legacy users.role is NEVER consulted here.
    * NULL ownership chains -> LookupError (mapped to 404 at the router).
"""
from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.models.financial import Expense, Income
from app.models.lease import Lease
from app.models.membership import (
    Membership,
    MembershipState,
    OrganizationRole,
)
from app.models.property import Property, Unit
from app.models.repair import RepairOperation
from app.models.tenant import Tenant
from app.services.membership import has_active_membership

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ScopeBlocked(PermissionError):
    """Caller has no ACTIVE Membership in the target Organization."""


class OwnerRequired(PermissionError):
    """Caller is ACTIVE SECRETARY; the action needs ACTIVE OWNER."""


class CrossOrgReference(ValueError):
    """A create/update payload references an object belonging to a
    different Organization than the caller's ACTIVE Membership."""


# ---------------------------------------------------------------------------
# Membership resolution
# ---------------------------------------------------------------------------


def resolve_org_membership(
    db: Session,
    user_id: int,
    organization_id: int,
    *,
    role: OrganizationRole | Iterable[OrganizationRole] | None = None,
) -> Membership:
    """Return the ACTIVE Membership for (user_id, organization_id) or raise ScopeBlocked."""
    m = has_active_membership(db, user_id, organization_id, role=role)
    if m is None:
        if role is None:
            hint = "ACTIVE"
        elif isinstance(role, OrganizationRole):
            hint = f"ACTIVE {role.value}"
        else:
            hint = "ACTIVE " + "/".join(r.value for r in role)
        raise ScopeBlocked(
            f"user_id={user_id!r} has no {hint} membership in org={organization_id!r}"
        )
    return m


def list_active_org_ids_for_user(db: Session, user_id: int) -> list[int]:
    """Return every organization_id where the user holds an ACTIVE Membership.

    Used by all scoped_list_* helpers to JOIN-filter lists. Empty list = the
    caller sees nothing.
    """
    rows = (
        db.query(Membership.organization_id)
        .filter(
            Membership.user_id == user_id,
            Membership.state == MembershipState.ACTIVE,
            Membership.removed_at.is_(None),
        )
        .all()
    )
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Ownership-chain org_id resolvers
# ---------------------------------------------------------------------------


def property_org_id(db: Session, property_id: int) -> int | None:
    row = (
        db.query(Property.organization_id)
        .filter(Property.id == property_id, Property.deleted_at.is_(None))
        .one_or_none()
    )
    return row[0] if row else None


def unit_org_id(db: Session, unit_id: int) -> int | None:
    row = (
        db.query(Property.organization_id)
        .select_from(Unit)
        .join(Property, Property.id == Unit.property_id)
        .filter(Unit.id == unit_id, Unit.deleted_at.is_(None))
        .one_or_none()
    )
    return row[0] if row else None


def lease_org_id(db: Session, lease_id: int) -> int | None:
    row = (
        db.query(Property.organization_id)
        .select_from(Lease)
        .join(Unit, Unit.id == Lease.unit_id)
        .join(Property, Property.id == Unit.property_id)
        .filter(Lease.id == lease_id, Lease.deleted_at.is_(None))
        .one_or_none()
    )
    return row[0] if row else None


def income_org_id(db: Session, income_id: int) -> int | None:
    row = (
        db.query(Property.organization_id)
        .select_from(Income)
        .join(Lease, Lease.id == Income.lease_id)
        .join(Unit, Unit.id == Lease.unit_id)
        .join(Property, Property.id == Unit.property_id)
        .filter(Income.id == income_id)
        .one_or_none()
    )
    return row[0] if row else None


def expense_org_id(db: Session, expense_id: int) -> int | None:
    """Expense ownership: unit_id -> property -> org. If unit_id is NULL
    the expense has no canonical org boundary -> fail closed (None)."""
    row = (
        db.query(Property.organization_id)
        .select_from(Expense)
        .join(Unit, Unit.id == Expense.unit_id)
        .join(Property, Property.id == Unit.property_id)
        .filter(Expense.id == expense_id)
        .one_or_none()
    )
    return row[0] if row else None


def repair_org_id(db: Session, repair_id: int) -> int | None:
    """Resolve RepairOperation owner org through unit_id (preferred) then
    property_id (fallback)."""
    row = (
        db.query(RepairOperation.unit_id, RepairOperation.property_id)
        .filter(RepairOperation.id == repair_id)
        .one_or_none()
    )
    if row is None:
        return None
    unit_id, property_id = row
    if unit_id is not None:
        oid = unit_org_id(db, unit_id)
        if oid is not None:
            return oid
    if property_id is not None:
        return property_org_id(db, property_id)
    return None


def tenant_org_id(db: Session, tenant_id: int) -> int | None:
    row = (
        db.query(Tenant.organization_id)
        .filter(Tenant.id == tenant_id, Tenant.deleted_at.is_(None))
        .one_or_none()
    )
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Cross-org membership enforcement:
#   - caller not ACTIVE member of object org -> LookupError (404 fail-closed)
#   - caller ACTIVE member of object org but role insufficient -> OwnerRequired/ScopeBlocked (403)
# ---------------------------------------------------------------------------


def _resolve_scoped_membership(
    db: Session,
    *,
    for_user_id: int,
    object_org_id: int,
    role: OrganizationRole | Iterable[OrganizationRole] | None = None,
    object_kind: str = "object",
) -> Membership:
    """Resolve membership for a scoped object lookup.

    Callers outside the object's organization receive :class:`LookupError`
    (HTTP 404 fail-closed — no existence leakage). Same-org callers then pass
    through :func:`resolve_org_membership` for any additional role gate
    (OWNER vs SECRETARY — raises :class:`OwnerRequired` on 403 path).
    """
    if object_org_id not in list_active_org_ids_for_user(db, for_user_id):
        raise LookupError(f"{object_kind} not found")
    return resolve_org_membership(db, for_user_id, object_org_id, role=role)


# ---------------------------------------------------------------------------
# Cross-org reference guards (create/update payloads)
# ---------------------------------------------------------------------------


def assert_co_org(
    db: Session,
    *,
    user_org_id: int,
    object_org_id: int | None,
    object_kind: str,
    object_id: int | None = None,
) -> None:
    """Raise CrossOrgReference if object_org_id != user_org_id (or is None)."""
    if object_org_id is None or object_org_id != user_org_id:
        label = f"{object_kind}" + (f" id={object_id}" if object_id else "")
        raise CrossOrgReference(
            f"{label} does not belong to the caller's organization"
        )


# ---------------------------------------------------------------------------
# scoped_list_* — only rows in orgs where caller has ACTIVE Membership
# ---------------------------------------------------------------------------


def scoped_list_properties(db: Session, *, for_user_id: int) -> list[Property]:
    orgs = list_active_org_ids_for_user(db, for_user_id)
    if not orgs:
        return []
    return (
        db.query(Property)
        .filter(
            Property.organization_id.in_(orgs),
            Property.deleted_at.is_(None),
        )
        .order_by(Property.id)
        .all()
    )


def scoped_list_units(db: Session, *, for_user_id: int) -> list[Unit]:
    orgs = list_active_org_ids_for_user(db, for_user_id)
    if not orgs:
        return []
    return (
        db.query(Unit)
        .join(Property, Property.id == Unit.property_id)
        .filter(
            Property.organization_id.in_(orgs),
            Unit.deleted_at.is_(None),
        )
        .order_by(Unit.id)
        .all()
    )


def scoped_list_tenants(db: Session, *, for_user_id: int) -> list[Tenant]:
    orgs = list_active_org_ids_for_user(db, for_user_id)
    if not orgs:
        return []
    return (
        db.query(Tenant)
        .filter(
            Tenant.organization_id.in_(orgs),
            Tenant.deleted_at.is_(None),
        )
        .order_by(Tenant.id)
        .all()
    )


def scoped_list_leases(db: Session, *, for_user_id: int) -> list[Lease]:
    orgs = list_active_org_ids_for_user(db, for_user_id)
    if not orgs:
        return []
    return (
        db.query(Lease)
        .join(Unit, Unit.id == Lease.unit_id)
        .join(Property, Property.id == Unit.property_id)
        .filter(
            Property.organization_id.in_(orgs),
            Lease.deleted_at.is_(None),
        )
        .order_by(Lease.id)
        .all()
    )


def scoped_list_incomes(db: Session, *, for_user_id: int) -> list[Income]:
    orgs = list_active_org_ids_for_user(db, for_user_id)
    if not orgs:
        return []
    return (
        db.query(Income)
        .join(Lease, Lease.id == Income.lease_id)
        .join(Unit, Unit.id == Lease.unit_id)
        .join(Property, Property.id == Unit.property_id)
        .filter(Property.organization_id.in_(orgs))
        .order_by(Income.id)
        .all()
    )


def scoped_list_expenses(db: Session, *, for_user_id: int) -> list[Expense]:
    orgs = list_active_org_ids_for_user(db, for_user_id)
    if not orgs:
        return []
    return (
        db.query(Expense)
        .join(Unit, Unit.id == Expense.unit_id)
        .join(Property, Property.id == Unit.property_id)
        .filter(Property.organization_id.in_(orgs))
        .order_by(Expense.id)
        .all()
    )


def scoped_list_repairs(db: Session, *, for_user_id: int) -> list[RepairOperation]:
    orgs = list_active_org_ids_for_user(db, for_user_id)
    if not orgs:
        return []
    org_property_ids = [
        r[0]
        for r in db.query(Property.id).filter(
            Property.organization_id.in_(orgs),
            Property.deleted_at.is_(None),
        ).all()
    ]
    org_unit_ids = [
        r[0]
        for r in db.query(Unit.id).filter(
            Unit.property_id.in_(org_property_ids),
            Unit.deleted_at.is_(None),
        ).all()
    ]
    return (
        db.query(RepairOperation)
        .filter(
            (RepairOperation.unit_id.in_(org_unit_ids))
            | (RepairOperation.property_id.in_(org_property_ids))
        )
        .order_by(RepairOperation.id.desc())
        .all()
    )


# ---------------------------------------------------------------------------
# scoped_get_* — cross-org lookup -> LookupError (404); role gate if given
# ---------------------------------------------------------------------------


def scoped_get_property(
    db: Session,
    property_id: int,
    *,
    for_user_id: int,
    role: OrganizationRole | Iterable[OrganizationRole] | None = None,
) -> tuple[Property, Membership]:
    org_id = property_org_id(db, property_id)
    if org_id is None:
        raise LookupError(
            f"property {property_id} not found or has no organization"
        )
    membership = _resolve_scoped_membership(
        db,
        for_user_id=for_user_id,
        object_org_id=org_id,
        role=role,
        object_kind="property",
    )
    prop = (
        db.query(Property)
        .filter(Property.id == property_id, Property.deleted_at.is_(None))
        .first()
    )
    if prop is None:
        raise LookupError(f"property {property_id} not found")
    return prop, membership


def scoped_get_unit(
    db: Session,
    unit_id: int,
    *,
    for_user_id: int,
    role: OrganizationRole | Iterable[OrganizationRole] | None = None,
) -> tuple[Unit, Membership]:
    org_id = unit_org_id(db, unit_id)
    if org_id is None:
        raise LookupError(f"unit {unit_id} not found or property has no organization")
    membership = _resolve_scoped_membership(
        db,
        for_user_id=for_user_id,
        object_org_id=org_id,
        role=role,
        object_kind="unit",
    )
    unit = (
        db.query(Unit)
        .filter(Unit.id == unit_id, Unit.deleted_at.is_(None))
        .first()
    )
    if unit is None:
        raise LookupError(f"unit {unit_id} not found")
    return unit, membership


def scoped_get_tenant(
    db: Session,
    tenant_id: int,
    *,
    for_user_id: int,
    role: OrganizationRole | Iterable[OrganizationRole] | None = None,
) -> tuple[Tenant, Membership]:
    org_id = tenant_org_id(db, tenant_id)
    if org_id is None:
        raise LookupError(f"tenant {tenant_id} not found or has no organization")
    membership = _resolve_scoped_membership(
        db,
        for_user_id=for_user_id,
        object_org_id=org_id,
        role=role,
        object_kind="tenant",
    )
    tenant = (
        db.query(Tenant)
        .filter(Tenant.id == tenant_id, Tenant.deleted_at.is_(None))
        .first()
    )
    if tenant is None:
        raise LookupError(f"tenant {tenant_id} not found")
    return tenant, membership


def scoped_get_lease(
    db: Session,
    lease_id: int,
    *,
    for_user_id: int,
    role: OrganizationRole | Iterable[OrganizationRole] | None = None,
) -> tuple[Lease, Membership]:
    org_id = lease_org_id(db, lease_id)
    if org_id is None:
        raise LookupError(f"lease {lease_id} not found or has no organization")
    membership = _resolve_scoped_membership(
        db,
        for_user_id=for_user_id,
        object_org_id=org_id,
        role=role,
        object_kind="lease",
    )
    lease = (
        db.query(Lease)
        .filter(Lease.id == lease_id, Lease.deleted_at.is_(None))
        .first()
    )
    if lease is None:
        raise LookupError(f"lease {lease_id} not found")
    return lease, membership


def scoped_get_income(
    db: Session,
    income_id: int,
    *,
    for_user_id: int,
    role: OrganizationRole | Iterable[OrganizationRole] | None = None,
) -> tuple[Income, Membership]:
    org_id = income_org_id(db, income_id)
    if org_id is None:
        raise LookupError(f"income {income_id} not found or has no organization")
    membership = _resolve_scoped_membership(
        db,
        for_user_id=for_user_id,
        object_org_id=org_id,
        role=role,
        object_kind="income",
    )
    obj = db.query(Income).filter(Income.id == income_id).first()
    if obj is None:
        raise LookupError(f"income {income_id} not found")
    return obj, membership


def scoped_get_expense(
    db: Session,
    expense_id: int,
    *,
    for_user_id: int,
    role: OrganizationRole | Iterable[OrganizationRole] | None = None,
) -> tuple[Expense, Membership]:
    org_id = expense_org_id(db, expense_id)
    if org_id is None:
        raise LookupError(f"expense {expense_id} not found or has no organization")
    membership = _resolve_scoped_membership(
        db,
        for_user_id=for_user_id,
        object_org_id=org_id,
        role=role,
        object_kind="expense",
    )
    obj = db.query(Expense).filter(Expense.id == expense_id).first()
    if obj is None:
        raise LookupError(f"expense {expense_id} not found")
    return obj, membership


def scoped_get_repair(
    db: Session,
    repair_id: int,
    *,
    for_user_id: int,
    role: OrganizationRole | Iterable[OrganizationRole] | None = None,
) -> tuple[RepairOperation, Membership]:
    org_id = repair_org_id(db, repair_id)
    if org_id is None:
        raise LookupError(f"repair {repair_id} not found or has no organization")
    membership = _resolve_scoped_membership(
        db,
        for_user_id=for_user_id,
        object_org_id=org_id,
        role=role,
        object_kind="repair",
    )
    obj = db.query(RepairOperation).filter(RepairOperation.id == repair_id).first()
    if obj is None:
        raise LookupError(f"repair {repair_id} not found")
    return obj, membership
