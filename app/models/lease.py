from datetime import date, datetime
from enum import Enum
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import AuditMixin, Base, SoftDeleteMixin, pg_enum


class LeaseStatus(str, Enum):
    active = "active"
    expired = "expired"
    terminated = "terminated"


class Lease(AuditMixin, SoftDeleteMixin, Base):
    __tablename__ = "leases"

    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id"), nullable=False, index=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    accounting_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    monthly_rent: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    deposit: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0.00"))
    status: Mapped[LeaseStatus] = mapped_column(
        pg_enum(LeaseStatus, "lease_status"), nullable=False, default=LeaseStatus.active
    )
    due_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # PASAY-AI-EMPLOYEE-FOUNDATION-007 §4: structured lease truth (a contract is
    # NEVER just a PDF — these are the operational fields the AI reads instead
    # of re-deriving from the document).
    renewal_notice_period_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    management_fee_included: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    special_terms: Mapped[str | None] = mapped_column(Text, nullable=True)
    # AI-OPS-FOUNDATION-001 §18 deposit foundation: required deposit is
    # ``deposit``; the accounting columns below represent received / held /
    # deductions / refund without inventing a complex accounting UI.
    deposit_received: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    deposit_refund: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    deposit_deductions: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    move_out_inspection_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    deposit_settlement_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    moved_out_settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    renewal_metadata: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    superseded_by_lease_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ['move_out_inspection_id', 'id'],
            ['move_out_inspections.id', 'move_out_inspections.lease_id'],
            name='fk_leases_moi_id_lease',
        ),
        ForeignKeyConstraint(
            ['deposit_settlement_id', 'id'],
            ['deposit_settlements.id', 'deposit_settlements.lease_id'],
            name='fk_leases_ds_id_lease',
        ),
        Index(
            'uq_leases_id_unit_tenant',
            'id', 'unit_id', 'tenant_id',
            unique=True,
        ),
        ForeignKeyConstraint(
            ['superseded_by_lease_id', 'unit_id', 'tenant_id'],
            ['leases.id', 'leases.unit_id', 'leases.tenant_id'],
            name='fk_leases_superseded_same_party',
            ondelete='RESTRICT',
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ['superseded_by_lease_id'],
            ['leases.id'],
            name='fk_leases_superseded_by',
            ondelete='RESTRICT',
            use_alter=True,
        ),
        CheckConstraint(
            "(superseded_by_lease_id IS NULL AND superseded_at IS NULL) OR "
            "(superseded_by_lease_id IS NOT NULL AND superseded_at IS NOT NULL AND status = 'expired')",
            name='ck_leases_superseded_pair',
        ),
        CheckConstraint(
            "superseded_by_lease_id IS NULL OR superseded_by_lease_id != id",
            name='ck_leases_superseded_not_self',
        ),
        Index(
            'uq_leases_superseded_by_one_predecessor',
            'superseded_by_lease_id',
            unique=True,
            postgresql_where=text("superseded_by_lease_id IS NOT NULL"),
        ),
    )
