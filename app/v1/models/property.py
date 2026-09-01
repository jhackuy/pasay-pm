"""Property + Unit ORM."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.v1.models.base import BigPK, TimestampMixin, V1Base


UNIT_STATUSES = ("AVAILABLE", "OCCUPIED", "MAINTENANCE", "RETIRED")

UNIT_LIFECYCLE_KINDS = (
    "STATUS_CHANGE",
    "RENT_CHANGE",
    "ARCHIVED",
    "MAINTENANCE_START",
    "MAINTENANCE_END",
)


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
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

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
    lifecycle_events = relationship(
        "UnitLifecycleEvent", back_populates="unit", cascade="all, delete-orphan",
    )


class UnitLifecycleEvent(V1Base, TimestampMixin):
    """Append-only audit trail for unit-level state changes.

    Drives the "Unit detail with history" surface and the vacant/occupied
    notification trigger.
    """
    __tablename__ = "v1_unit_lifecycle_events"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('STATUS_CHANGE','RENT_CHANGE','ARCHIVED',"
            "'MAINTENANCE_START','MAINTENANCE_END')",
            name="ck_v1_unit_lifecycle_events_kind",
        ),
        Index("ix_v1_unit_lifecycle_events_unit_id", "unit_id"),
        Index("ix_v1_unit_lifecycle_events_org_id", "org_id"),
    )

    id: Mapped[BigPK]
    unit_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("v1_units.id", ondelete="RESTRICT"), nullable=False,
    )
    org_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("v1_organizations.id", ondelete="RESTRICT"), nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    actor_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("v1_users.id", ondelete="RESTRICT"), nullable=True,
    )

    unit = relationship("Unit", back_populates="lifecycle_events")