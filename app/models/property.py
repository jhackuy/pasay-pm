from datetime import datetime
from enum import Enum
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import AuditMixin, Base, SoftDeleteMixin, pg_enum


class Property(AuditMixin, SoftDeleteMixin, Base):
    __tablename__ = "properties"
    __table_args__ = (
        Index(
            "ix_properties_organization_id_active",
            "organization_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    organization_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("organizations.id"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    address: Mapped[str] = mapped_column(String(500), nullable=False)
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    total_units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    management_company: Mapped[str | None] = mapped_column(String(200), nullable=True)
    management_office_phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    management_contact_person: Mapped[str | None] = mapped_column(String(200), nullable=True)
    management_email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    management_office_location: Mapped[str | None] = mapped_column(String(300), nullable=True)
    operational_notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class UnitStatus(str, Enum):
    vacant = "vacant"
    occupied = "occupied"
    maintenance = "maintenance"


class Unit(AuditMixin, SoftDeleteMixin, Base):
    __tablename__ = "units"
    __table_args__ = (
        Index(
            "uq_units_active_property_unit_number",
            "property_id",
            "unit_number",
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND is_active = TRUE"),
        ),
        CheckConstraint("length(btrim(unit_number)) > 0", name="ck_units_unit_number_nonblank"),
    )

    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"), nullable=False, index=True)
    unit_number: Mapped[str] = mapped_column(String(50), nullable=False)
    floor: Mapped[str | None] = mapped_column(String(20), nullable=True)
    size_sqm: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    monthly_rent: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    status: Mapped[UnitStatus] = mapped_column(
        pg_enum(UnitStatus, "unit_status"), nullable=False, default=UnitStatus.vacant
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    unit_state: Mapped[str | None] = mapped_column(String(30), nullable=True)


class UnitLifecycleEvent(AuditMixin, Base):
    """Durable unit lifecycle timeline (AI-OPS-FOUNDATION-001 §16/§17)."""

    __tablename__ = "unit_lifecycle_events"

    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id"), nullable=False, index=True)
    from_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    to_status: Mapped[str] = mapped_column(String(30), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
