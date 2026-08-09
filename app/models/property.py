from enum import Enum
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import AuditMixin, Base, SoftDeleteMixin, pg_enum


class Property(AuditMixin, SoftDeleteMixin, Base):
    __tablename__ = "properties"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    address: Mapped[str] = mapped_column(String(500), nullable=False)
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    total_units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class UnitStatus(str, Enum):
    vacant = "vacant"
    occupied = "occupied"
    maintenance = "maintenance"


class Unit(AuditMixin, SoftDeleteMixin, Base):
    __tablename__ = "units"

    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"), nullable=False, index=True)
    unit_number: Mapped[str] = mapped_column(String(50), nullable=False)
    floor: Mapped[str | None] = mapped_column(String(20), nullable=True)
    size_sqm: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    monthly_rent: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    status: Mapped[UnitStatus] = mapped_column(
        pg_enum(UnitStatus, "unit_status"), nullable=False, default=UnitStatus.vacant
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
