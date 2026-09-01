"""PropertyService: create/list properties and units; org-scope enforced.

Decimal money via parse_money() — never float.

This module exposes BOTH:
- A class-based ``PropertyService`` for legacy routes.
- Module-level functions (``create_property``, ``get_property``, ``create_unit``,
  ``get_unit``, ``set_unit_status``) used by V1 handlers and by other
  services (e.g. ``lease.activate_lease`` calls ``set_unit_status`` to flip
  AVAILABLE ↔ OCCUPIED).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.core.money import parse_money
from app.core.permissions import (
    PermissionDenied,
    Principal,
    Role,
    UnknownRoleError,
    require_org_scope,
)
from app.core.time import utcnow
from app.v1.models.base import UnitStatus
from app.v1.models.property import Property, Unit, UnitLifecycleEvent
from app.v1.services.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)


def _principal_from(
    user_id: int, org_id: int, role: str | Role,
) -> Principal:
    """Build a Principal from raw kwargs for module-level functions.

    Module-level callers usually have user_id + org_id + role but no
    Principal object. This helper synthesizes one with
    membership_state=ACTIVE so ``require_org_scope`` works correctly.
    """
    parsed = role if isinstance(role, Role) else Role.parse(role)
    return Principal(
        user_id=user_id,
        org_id=org_id,
        role=parsed,
        membership_state="ACTIVE",
    )


def create_property(
    db: Session,
    *,
    org_id: int,
    name: str,
    address: str,
    owner_user_id: int,
    actor_role: str | Role,
) -> Property:
    """Create a new Property in ``org_id``.

    Raises ``ValidationError`` if ``name`` or ``address`` is empty/whitespace.
    Enforces org-scope via ``require_org_scope``.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValidationError("name is required and must be non-empty")
    if not isinstance(address, str) or not address.strip():
        raise ValidationError("address is required and must be non-empty")
    principal = _principal_from(owner_user_id, org_id, actor_role)
    require_org_scope(principal, org_id)
    p = Property(
        org_id=org_id,
        name=name.strip(),
        address_line1=address.strip(),
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def get_property(
    db: Session,
    *,
    org_id: int,
    property_id: int,
    actor_user_id: int,
    actor_role: str | Role,
) -> Property:
    """Look up a Property by id; raise NotFoundError on cross-org."""
    principal = _principal_from(actor_user_id, org_id, actor_role)
    require_org_scope(principal, org_id)
    p = db.get(Property, property_id)
    if p is None or p.org_id != org_id:
        raise NotFoundError(
            f"property {property_id} not found in org {org_id}",
        )
    return p


def create_unit(
    db: Session,
    *,
    org_id: int,
    property_id: int,
    unit_number: str,
    label: str | None = None,
    owner_user_id: int,
    actor_role: str | Role,
) -> Unit:
    """Create a new Unit in ``org_id`` under ``property_id``.

    The unit starts at ``UnitStatus.AVAILABLE``. ``unit_number`` is
    NOT NULL on the Unit model — it is stored in the ``label`` column
    (single-unit-per-row design). ``monthly_rent`` defaults to 0.
    """
    if not isinstance(unit_number, str) or not unit_number.strip():
        raise ValidationError(
            "unit_number is required and must be non-empty",
        )
    principal = _principal_from(owner_user_id, org_id, actor_role)
    require_org_scope(principal, org_id)
    if principal.role not in (Role.OWNER, Role.SECRETARY):
        raise PermissionDenied(
            "only OWNER/SECRETARY can create units",
        )
    prop = db.get(Property, property_id)
    if prop is None or prop.org_id != org_id:
        raise NotFoundError(
            f"property {property_id} not found in org {org_id}",
        )
    final_label = (label or unit_number).strip()
    u = Unit(
        property_id=property_id,
        org_id=org_id,
        label=final_label,
        monthly_rent=parse_money(0),
        status=UnitStatus.AVAILABLE.value,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def get_unit(
    db: Session,
    *,
    org_id: int,
    unit_id: int,
    actor_user_id: int,
    actor_role: str | Role,
) -> Unit:
    """Look up a Unit by id; raise NotFoundError on cross-org."""
    principal = _principal_from(actor_user_id, org_id, actor_role)
    require_org_scope(principal, org_id)
    u = db.get(Unit, unit_id)
    if u is None or u.org_id != org_id:
        raise NotFoundError(
            f"unit {unit_id} not found in org {org_id}",
        )
    return u


def set_unit_status(
    db: Session,
    *,
    org_id: int,
    unit_id: int,
    status: UnitStatus,
    actor_user_id: int,
    actor_role: str | Role,
) -> Unit:
    """Transition a Unit's status (e.g. AVAILABLE ↔ OCCUPIED).

    Used by the lease service: activate_lease flips the unit to
    ``UnitStatus.OCCUPIED``; terminate_lease flips it back to
    ``UnitStatus.AVAILABLE``. ``actor_user_id`` and ``actor_role`` are
    accepted so the org-scope guard is identical to the other module-
    level functions; we intentionally DO NOT gate the transition on
    role here — that is the lease service's responsibility.
    """
    if isinstance(status, str) and not isinstance(status, UnitStatus):
        try:
            status = UnitStatus(status)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
    if not isinstance(status, UnitStatus):
        raise ValidationError(
            f"status must be a UnitStatus enum member, "
            f"got {type(status).__name__}",
        )
    principal = _principal_from(actor_user_id, org_id, actor_role)
    require_org_scope(principal, org_id)
    u = db.get(Unit, unit_id)
    if u is None or u.org_id != org_id:
        raise NotFoundError(
            f"unit {unit_id} not found in org {org_id}",
        )
    u.status = status.value
    db.commit()
    db.refresh(u)
    return u


class PropertyService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create_property(
        self,
        principal: Principal,
        *,
        org_id: int,
        name: str,
        address_line1: str | None = None,
        address_line2: str | None = None,
        city: str | None = None,
        region: str | None = None,
        postal_code: str | None = None,
    ) -> Property:
        require_org_scope(principal, org_id)
        if principal.role not in (Role.OWNER, Role.SECRETARY):
            raise PermissionDenied(
                "only OWNER/SECRETARY can create properties",
            )
        p = Property(
            org_id=org_id,
            name=name,
            address_line1=address_line1,
            address_line2=address_line2,
            city=city,
            region=region,
            postal_code=postal_code,
        )
        self.db.add(p)
        self.db.commit()
        self.db.refresh(p)
        return p

    def list_properties(
        self, principal: Principal, *, org_id: int,
    ) -> list[Property]:
        require_org_scope(principal, org_id)
        return (
            self.db.query(Property)
            .filter(Property.org_id == org_id)
            .order_by(Property.id.asc())
            .all()
        )

    def create_unit(
        self,
        principal: Principal,
        *,
        org_id: int,
        property_id: int,
        label: str,
        bedrooms: int = 0,
        bathrooms: int = 0,
        monthly_rent: Decimal | str | int = 0,
    ) -> Unit:
        require_org_scope(principal, org_id)
        if principal.role not in (Role.OWNER, Role.SECRETARY):
            raise PermissionDenied(
                "only OWNER/SECRETARY can create units",
            )
        prop = self.db.get(Property, property_id)
        if prop is None:
            raise NotFoundError(f"property {property_id} not found")
        if prop.org_id != org_id:
            raise ConflictError(
                f"property {property_id} belongs to a different org",
            )
        rent = parse_money(monthly_rent)
        u = Unit(
            property_id=property_id,
            org_id=org_id,
            label=label,
            bedrooms=bedrooms,
            bathrooms=bathrooms,
            monthly_rent=rent,
            status=UnitStatus.AVAILABLE.value,
        )
        self.db.add(u)
        self.db.commit()
        self.db.refresh(u)
        return u

    def list_units(
        self,
        principal: Principal,
        *,
        org_id: int,
        property_id: int,
    ) -> list[Unit]:
        require_org_scope(principal, org_id)
        return (
            self.db.query(Unit)
            .filter(
                Unit.property_id == property_id,
                Unit.org_id == org_id,
            )
            .order_by(Unit.id.asc())
            .all()
        )

    def get_property_detail(
        self, principal: Principal, *, org_id: int, property_id: int,
    ) -> tuple[Property, list[Unit]]:
        require_org_scope(principal, org_id)
        prop = self.db.get(Property, property_id)
        if prop is None or prop.org_id != org_id:
            raise NotFoundError(
                f"property {property_id} not found in org {org_id}",
            )
        units = (
            self.db.query(Unit)
            .filter(Unit.property_id == property_id, Unit.org_id == org_id)
            .order_by(Unit.id.asc())
            .all()
        )
        return prop, units

    def get_unit_detail(
        self, principal: Principal, *, org_id: int, unit_id: int,
    ) -> tuple[Unit, list[UnitLifecycleEvent]]:
        """Unit + full lifecycle event history (newest first)."""
        require_org_scope(principal, org_id)
        unit = self.db.get(Unit, unit_id)
        if unit is None or unit.org_id != org_id:
            raise NotFoundError(f"unit {unit_id} not found in org {org_id}")
        events = (
            self.db.query(UnitLifecycleEvent)
            .filter(
                UnitLifecycleEvent.unit_id == unit_id,
                UnitLifecycleEvent.org_id == org_id,
            )
            .order_by(UnitLifecycleEvent.id.desc())
            .all()
        )
        return unit, events

    def archive_property(
        self, principal: Principal, *, org_id: int, property_id: int,
    ) -> Property:
        """Archive a property (sets archived_at = now, never deletes history).

        Cannot archive a property with any OCCUPIED unit.
        """
        require_org_scope(principal, org_id)
        if principal.role != Role.OWNER:
            raise PermissionDenied("only OWNER can archive properties")
        prop = self.db.get(Property, property_id)
        if prop is None or prop.org_id != org_id:
            raise NotFoundError(
                f"property {property_id} not found in org {org_id}",
            )
        occupied_count = (
            self.db.query(Unit)
            .filter(
                Unit.property_id == property_id,
                Unit.org_id == org_id,
                Unit.status == UnitStatus.OCCUPIED.value,
            )
            .count()
        )
        if occupied_count > 0:
            raise ConflictError(
                "cannot archive property with OCCUPIED units",
            )
        prop.archived_at = utcnow()
        # Record a lifecycle event for each unit.
        units = (
            self.db.query(Unit)
            .filter(Unit.property_id == property_id, Unit.org_id == org_id)
            .all()
        )
        for u in units:
            self.db.add(UnitLifecycleEvent(
                unit_id=u.id,
                org_id=org_id,
                kind="ARCHIVED",
                note=f"property archived",
                actor_user_id=principal.user_id,
            ))
        self.db.commit()
        self.db.refresh(prop)
        return prop

    def set_unit_status_v1(
        self,
        principal: Principal,
        *,
        org_id: int,
        unit_id: int,
        status: UnitStatus | str,
        note: str | None = None,
    ) -> Unit:
        """Flip a Unit's status with a recorded UnitLifecycleEvent.

        Drives the vacant/occupied notification truth.
        """
        require_org_scope(principal, org_id)
        if principal.role not in (Role.OWNER, Role.SECRETARY):
            raise PermissionDenied(
                "only OWNER/SECRETARY can change unit status",
            )
        if isinstance(status, str):
            try:
                status = UnitStatus(status)
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
        unit = self.db.get(Unit, unit_id)
        if unit is None or unit.org_id != org_id:
            raise NotFoundError(
                f"unit {unit_id} not found in org {org_id}",
            )
        from_state = unit.status
        unit.status = status.value
        self.db.add(UnitLifecycleEvent(
            unit_id=unit_id,
            org_id=org_id,
            kind="STATUS_CHANGE",
            from_state=from_state,
            to_state=status.value,
            note=note,
            actor_user_id=principal.user_id,
        ))
        self.db.commit()
        self.db.refresh(unit)
        return unit

    def record_unit_event(
        self,
        principal: Principal,
        *,
        org_id: int,
        unit_id: int,
        kind: str,
        note: str | None = None,
    ) -> UnitLifecycleEvent:
        """Append-only: record an arbitrary UnitLifecycleEvent (RENT_CHANGE, etc)."""
        require_org_scope(principal, org_id)
        if principal.role not in (Role.OWNER, Role.SECRETARY):
            raise PermissionDenied("only OWNER/SECRETARY can record unit events")
        if kind not in (
            "STATUS_CHANGE", "RENT_CHANGE", "ARCHIVED",
            "MAINTENANCE_START", "MAINTENANCE_END",
        ):
            raise ValidationError(f"unknown event kind {kind!r}")
        unit = self.db.get(Unit, unit_id)
        if unit is None or unit.org_id != org_id:
            raise NotFoundError(
                f"unit {unit_id} not found in org {org_id}",
            )
        event = UnitLifecycleEvent(
            unit_id=unit_id,
            org_id=org_id,
            kind=kind,
            note=note,
            actor_user_id=principal.user_id,
        )
        self.db.add(event)
        self.db.commit()
        self.db.refresh(event)
        return event


__all__ = [
    "PropertyService",
    "create_property",
    "create_unit",
    "get_property",
    "get_unit",
    "set_unit_status",
]