from datetime import datetime
from enum import Enum
from decimal import Decimal

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Numeric, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import AuditMixin, Base, pg_enum


class DepositSettlementStatus(str, Enum):
    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    RECONCILED = "RECONCILED"


class DepositSettlement(AuditMixin, Base):
    __tablename__ = "deposit_settlements"
    __table_args__ = (
        CheckConstraint(
            "deposit_received >= 0",
            name="ck_deposit_settlements_deposit_received_non_negative",
        ),
        CheckConstraint(
            "total_deductions >= 0",
            name="ck_deposit_settlements_total_deductions_non_negative",
        ),
        CheckConstraint(
            "refund_amount >= 0",
            name="ck_deposit_settlements_refund_amount_non_negative",
        ),
        CheckConstraint(
            "status IN ('DRAFT','CONFIRMED','RECONCILED')",
            name="ck_deposit_settlements_status",
        ),
    )

    lease_id: Mapped[int] = mapped_column(
        ForeignKey("leases.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    move_out_inspection_id: Mapped[int] = mapped_column(
        ForeignKey("move_out_inspections.id"), nullable=False, unique=True
    )
    deposit_received: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    deductions: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    total_deductions: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    refund_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    status: Mapped[DepositSettlementStatus] = mapped_column(
        pg_enum(DepositSettlementStatus, "deposit_settlement_status"),
        nullable=False,
        default=DepositSettlementStatus.DRAFT,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
