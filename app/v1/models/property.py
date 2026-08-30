"""Property + Unit ORM."""
from __future__ import annotations

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.v1.models.base import BigPK, TimestampMixin, V1Base


UNIT_STATUSES = ("AVAILABLE", "OCCUPIED", "MAINTENANCE", "RETIRED")


class Property(V1Base, TimestampMixin):
    __tablename__ = "v1_properties"
    __table_args__ = (Index("ix_v1_properties_org_id", "org_id"),)

    id: Mapped[BigPK]
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    address_line1: Mapped[str | None] = mapped_column(String(200), nullable=True)
    address_line2: Mapped[str | None] = mapped_column(String(200), nullable=True)
    city: Mapped[str | None] = mapped_column(String(80), nullable=True)
    region: Mapped[str | None] = mapped_column(String(80), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(20), nullable=True)

    organization = relationship("Organization", back_populates="properties")
    units = relationship("Unit", back_populates="property_", cascade="all, delete-orphan")


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

    id: Mapped[BigPK]
    property_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_properties.id", ondelete="RESTRICT"),
        nullable=False,
    )
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    bedrooms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bathrooms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    monthly_rent = mapped_column(Numeric(14, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="AVAILABLE")

    property_ = relationship("Property", back_populates="units")