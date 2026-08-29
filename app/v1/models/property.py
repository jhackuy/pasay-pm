"""Property + Unit ORM.

AGENTS.md §4: org-scope enforced via org_id column + index.
Money is NUMERIC(14,2) — never float (AGENTS.md §4).
"""
from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.orm import relationship

from app.v1.models.base import TimestampMixin, V1Base, big_pk


UNIT_STATUSES = ("AVAILABLE", "OCCUPIED", "MAINTENANCE", "RETIRED")


class Property(V1Base, TimestampMixin):
    __tablename__ = "v1_properties"
    __table_args__ = (
        Index("ix_v1_properties_org_id", "org_id"),
    )

    id = big_pk()
    org_id = Column(
        BigInteger,
        ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    name = Column(String(120), nullable=False)
    address_line1 = Column(String(200), nullable=True)
    address_line2 = Column(String(200), nullable=True)
    city = Column(String(80), nullable=True)
    region = Column(String(80), nullable=True)
    postal_code = Column(String(20), nullable=True)

    organization = relationship("Organization", back_populates="properties")
    units = relationship(
        "Unit", back_populates="property",
        cascade="all, delete-orphan",
    )


class Unit(V1Base, TimestampMixin):
    __tablename__ = "v1_units"
    __table_args__ = (
        CheckConstraint(
            "status IN ('AVAILABLE','OCCUPIED','MAINTENANCE','RETIRED')",
            name="ck_v1_units_status",
        ),
        Index("ix_v1_units_property_id", "property_id"),
        Index("ix_v1_units_org_id", "org_id"),
    )

    id = big_pk()
    property_id = Column(
        BigInteger,
        ForeignKey("v1_properties.id", ondelete="RESTRICT"),
        nullable=False,
    )
    org_id = Column(
        BigInteger,
        ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    label = Column(String(80), nullable=False)
    bedrooms = Column(Integer, nullable=False, default=0)
    bathrooms = Column(Integer, nullable=False, default=0)
    monthly_rent = Column(Numeric(14, 2), nullable=False)
    status = Column(String(16), nullable=False, default="AVAILABLE")

    property_ = relationship("Property", back_populates="units")