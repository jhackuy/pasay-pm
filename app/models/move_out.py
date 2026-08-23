from datetime import datetime
from enum import Enum

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import AuditMixin, Base, pg_enum


class MoveOutInspectionStatus(str, Enum):
    SCHEDULED = "SCHEDULED"
    INSPECTED = "INSPECTED"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"


class MoveOutInspection(AuditMixin, Base):
    __tablename__ = "move_out_inspections"
    __table_args__ = (
        Index(
            "uq_move_out_inspections_active_per_lease",
            "lease_id",
            unique=True,
            postgresql_where=text("status IN ('SCHEDULED','INSPECTED')"),
        ),
        CheckConstraint(
            "status IN ('SCHEDULED','INSPECTED','CONFIRMED','CANCELLED')",
            name="ck_move_out_inspections_status",
        ),
    )

    lease_id: Mapped[int] = mapped_column(ForeignKey("leases.id"), nullable=False, index=True)
    unit_id: Mapped[int | None] = mapped_column(ForeignKey("units.id"), nullable=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenants.id"), nullable=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    inspected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[MoveOutInspectionStatus] = mapped_column(
        pg_enum(MoveOutInspectionStatus, "move_out_inspection_status"),
        nullable=False,
        default=MoveOutInspectionStatus.SCHEDULED,
    )
    findings: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    evidence_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cancelled_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
