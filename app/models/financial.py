from datetime import date, datetime
from enum import Enum
from decimal import Decimal

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Index, Numeric, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import AuditMixin, Base, pg_enum


class IncomeStatus(str, Enum):
    pending = "pending"
    confirmed = "confirmed"
    reversed = "reversed"


class Income(AuditMixin, Base):
    __tablename__ = "incomes"
    __table_args__ = (
        Index(
            "uq_incomes_idempotency_key",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    lease_id: Mapped[int | None] = mapped_column(ForeignKey("leases.id"), nullable=True, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    received_date: Mapped[date] = mapped_column(Date, nullable=False)
    payment_method: Mapped[str | None] = mapped_column(String(50), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[IncomeStatus] = mapped_column(
        pg_enum(IncomeStatus, "income_status"), nullable=False, default=IncomeStatus.pending
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExpenseStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    paid = "paid"
    reversed = "reversed"
    # PASAY-EXPENSE-OPERATION-003B: distinct truth states between APPROVED and
    # PAID. A payment claim has been reported but is NOT yet verified
    # (payment_claimed = "payment reported / awaiting verification"); a partial
    # verified amount makes the expense PARTIALLY_PAID until the full amount is
    # VERIFIED. `paid` is reached ONLY via verified-claims aggregation.
    payment_claimed = "payment_claimed"
    partially_paid = "partially_paid"


class Expense(AuditMixin, Base):
    __tablename__ = "expenses"

    expense_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    payee: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    unit_id: Mapped[int | None] = mapped_column(
        ForeignKey("units.id"), nullable=True, index=True
    )
    property_id: Mapped[int] = mapped_column(
        ForeignKey("properties.id"), nullable=False, index=True
    )
    status: Mapped[ExpenseStatus] = mapped_column(
        pg_enum(ExpenseStatus, "expense_status"), nullable=False, default=ExpenseStatus.pending
    )
    approved_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    receipt_attachment_id: Mapped[int | None] = mapped_column(
        ForeignKey("attachments.id"), nullable=True
    )
    # AI-OPS-FOUNDATION-001 §4/§8: the ACTUAL human responsible for payment.
    # After approval the PAYMENT_PENDING task routes to this payer, not always
    # the Owner; None falls back to the Owner at generation time.
    payer_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    # PASAY-EXPENSE-OPERATION-003B: approve-vs-pay, reject->resubmit, and
    # critical-field reapproval continuity.
    #
    # rejection_reason: Owner's reason when the CURRENT version was rejected
    # (section 8) — preserved so the next version + Mini App/timeline can show
    # why V1 was rejected.
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # reapproval_reason: why a previously-approved expense was demoted back to
    # PENDING because a critical financial field changed (section 9).
    reapproval_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # version: an increasing version counter for the SAME logical expense need.
    # Reject->edit->resubmit and critical-field reapproval bump this; the OLD
    # rejected state is never overwritten in place (section 8/9).
    version: Mapped[int | None] = mapped_column(BigInteger, nullable=True, default=1)
    # parent_expense_id: points to the immediately-previous version row, so the
    # family (V1 REJECTED -> V2 PENDING) is preserved without a separate table.
    parent_expense_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True
    )
