"""PropertyService: create/list properties and units; org-scope enforced.

Decimal money via parse_money() — never float.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.money import parse_money
from app.core.permissions import (
    PermissionDenied,
    Principal,
    Role,
    require_org_scope,
)
from app.v1.models.property import Property, Unit
from app.v1.services.errors import ConflictError, NotFoundError


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
            status="AVAILABLE",
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