"""PASAY reference implementation — Property / Unit / Address ORM models.

Issue #99 / PR #100 /oc continuation. Staged under
``.opencode-qualification/reference/`` pending Owner-side promotion to
``app/models/property.py``.

Entities in this file:
    * Address      — normalised postal address; org-scoped for isolation.
    * Property     — top-level real-estate asset within an Organization.
    * Unit         — leasable space within a Property.

All money columns are ``Numeric(14, 2)``. All timestamps are
``DateTime(timezone=True)``. Every business row carries ``org_id``.
"""
from __future__ import annotations

import enum
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pasay_db_layer import AuditMixin, Base, OrgScopedMixin


class PropertyTypeEnum(str, enum.Enum):
    RESIDENTIAL = "RESIDENTIAL"
    COMMERCIAL = "COMMERCIAL"
    MIXED = "MIXED"


class PropertyStatusEnum(str, enum.Enum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class UnitStatusEnum(str, enum.Enum):
    VACANT = "VACANT"
    OCCUPIED = "OCCUPIED"
    UNDER_MAINTENANCE = "UNDER_MAINTENANCE"
    ARCHIVED = "ARCHIVED"


class Address(Base, AuditMixin, OrgScopedMixin):
    """Normalised postal address. Org-scoped for tenant isolation."""

    __tablename__ = "addresses"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    line1: Mapped[str] = mapped_column(String(200), nullable=False)
    line2: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    region: Mapped[str] = mapped_column(String(100), nullable=False)
    postal_code: Mapped[str] = mapped_column(String(40), nullable=False)
    country: Mapped[str] = mapped_column(
        String(2), nullable=False, server_default=text("'US'")
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint("length(line1) > 0", name="addr_line1_nonempty"),
        CheckConstraint("length(city) > 0", name="addr_city_nonempty"),
        CheckConstraint("length(postal_code) > 0", name="addr_postal_nonempty"),
    )


class Property(Base, AuditMixin, OrgScopedMixin):
    """Top-level real-estate asset within an Organization."""

    __tablename__ = "properties"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    address_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("addresses.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    property_type: Mapped[PropertyTypeEnum] = mapped_column(
        SAEnum(
            PropertyTypeEnum,
            name="property_type_enum",
            native_enum=False,
            length=32,
        ),
        nullable=False,
        server_default=text("'RESIDENTIAL'"),
    )
    status: Mapped[PropertyStatusEnum] = mapped_column(
        SAEnum(
            PropertyStatusEnum,
            name="property_status_enum",
            native_enum=False,
            length=16,
        ),
        nullable=False,
        server_default=text("'ACTIVE'"),
    )
    year_built: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    address: Mapped["Address"] = relationship()
    units: Mapped[list["Unit"]] = relationship(
        back_populates="property",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("org_id", "name", name="uq_properties_org_name"),
        CheckConstraint("length(name) > 0", name="properties_name_nonempty"),
    )


class Unit(Base, AuditMixin, OrgScopedMixin):
    """A leasable Unit within a Property."""

    __tablename__ = "units"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    property_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("properties.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    bedrooms: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    bathrooms: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    square_feet: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[UnitStatusEnum] = mapped_column(
        SAEnum(
            UnitStatusEnum,
            name="unit_status_enum",
            native_enum=False,
            length=32,
        ),
        nullable=False,
        server_default=text("'VACANT'"),
    )
    market_rent_amount: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(14, 2), nullable=True
    )
    market_rent_currency: Mapped[Optional[str]] = mapped_column(
        String(3), nullable=True
    )

    property: Mapped["Property"] = relationship(back_populates="units")

    __table_args__ = (
        UniqueConstraint("property_id", "label", name="uq_units_property_label"),
        CheckConstraint(
            "bedrooms >= 0 AND bathrooms >= 0",
            name="units_counts_nonneg",
        ),
        CheckConstraint(
            "market_rent_amount IS NULL OR market_rent_amount >= 0",
            name="units_rent_nonneg",
        ),
    )


__all__ = [
    "PropertyTypeEnum",
    "PropertyStatusEnum",
    "UnitStatusEnum",
    "Address",
    "Property",
    "Unit",
]
