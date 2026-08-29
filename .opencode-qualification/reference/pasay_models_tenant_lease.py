"""PASAY reference implementation — Tenant / Lease / Renewal / MoveOut ORM models.

Issue #99 / PR #100 /oc continuation. Staged under
``.opencode-qualification/reference/`` pending Owner-side promotion to
``app/models/tenant.py`` and ``app/models/lease.py``.

Entities in this file:
    * Tenant        — a person who may lease units within an Organization.
    * Lease         — binding agreement between a Tenant and a Unit.
    * LeaseTerms    — key/value terms attached to a Lease.
    * LeaseRenewal  — proposal to extend a Lease.
    * MoveOut       — end-of-lease workflow with deductions and refund.

All money columns are ``Numeric(14, 2)``. All timestamps are
``DateTime(timezone=True)``. Every business row carries ``org_id``.
"""
from __future__ import annotations

import enum
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    Date,
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


class TenantStatusEnum(str, enum.Enum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"
    BLACKLISTED = "BLACKLISTED"


class LeaseStatusEnum(str, enum.Enum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    ENDED = "ENDED"
    TERMINATED = "TERMINATED"


class RenewalStatusEnum(str, enum.Enum):
    PROPOSED = "PROPOSED"
    ACCEPTED = "ACCEPTED"
    DECLINED = "DECLINED"
    WITHDRAWN = "WITHDRAWN"
    EXPIRED = "EXPIRED"


class MoveOutStatusEnum(str, enum.Enum):
    NOTICE_FILED = "NOTICE_FILED"
    KEYS_RETURNED = "KEYS_RETURNED"
    INSPECTION_DONE = "INSPECTION_DONE"
    DEDUCTIONS_CALCULATED = "DEDUCTIONS_CALCULATED"
    REFUND_ISSUED = "REFUND_ISSUED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class Tenant(Base, AuditMixin, OrgScopedMixin):
    """A Tenant is a person who may lease Units within an Organization."""

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    telegram_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )
    id_document_ref: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    status: Mapped[TenantStatusEnum] = mapped_column(
        SAEnum(
            TenantStatusEnum,
            name="tenant_status_enum",
            native_enum=False,
            length=16,
        ),
        nullable=False,
        server_default=text("'ACTIVE'"),
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    leases: Mapped[list["Lease"]] = relationship(back_populates="tenant")

    __table_args__ = (
        CheckConstraint("length(full_name) > 0", name="tenants_name_nonempty"),
    )


class Lease(Base, AuditMixin, OrgScopedMixin):
    """Binding agreement between a Tenant and a Unit."""

    __tablename__ = "leases"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    unit_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("units.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    start_date: Mapped[object] = mapped_column(
        Date, nullable=False
    )
    end_date: Mapped[object] = mapped_column(Date, nullable=False)
    monthly_rent_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False
    )
    monthly_rent_currency: Mapped[str] = mapped_column(
        String(3), nullable=False, server_default=text("'USD'")
    )
    deposit_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, server_default=text("0")
    )
    billing_day: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    status: Mapped[LeaseStatusEnum] = mapped_column(
        SAEnum(
            LeaseStatusEnum,
            name="lease_status_enum",
            native_enum=False,
            length=16,
        ),
        nullable=False,
        server_default=text("'DRAFT'"),
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="leases")
    terms: Mapped[list["LeaseTerms"]] = relationship(
        back_populates="lease", cascade="all, delete-orphan"
    )
    renewals: Mapped[list["LeaseRenewal"]] = relationship(
        back_populates="lease", cascade="all, delete-orphan"
    )
    move_out: Mapped[Optional["MoveOut"]] = relationship(
        back_populates="lease", uselist=False
    )

    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="lease_dates_ordered"),
        CheckConstraint("monthly_rent_amount >= 0", name="lease_rent_nonneg"),
        CheckConstraint("deposit_amount >= 0", name="lease_deposit_nonneg"),
        CheckConstraint(
            "billing_day BETWEEN 1 AND 28", name="lease_billing_day_range"
        ),
    )


class LeaseTerms(Base, AuditMixin, OrgScopedMixin):
    """Free-form key/value terms attached to a Lease (late-fee rules, pet policy, etc.)."""

    __tablename__ = "lease_terms"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    lease_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("leases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    key: Mapped[str] = mapped_column(String(80), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)

    lease: Mapped["Lease"] = relationship(back_populates="terms")

    __table_args__ = (
        UniqueConstraint("lease_id", "key", name="uq_lease_terms_key"),
    )


class LeaseRenewal(Base, AuditMixin, OrgScopedMixin):
    """Proposal to extend an existing Lease."""

    __tablename__ = "lease_renewals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    lease_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("leases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    proposed_end_date: Mapped[object] = mapped_column(Date, nullable=False)
    proposed_monthly_rent_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False
    )
    status: Mapped[RenewalStatusEnum] = mapped_column(
        SAEnum(
            RenewalStatusEnum,
            name="renewal_status_enum",
            native_enum=False,
            length=16,
        ),
        nullable=False,
        server_default=text("'PROPOSED'"),
    )
    responded_at: Mapped[Optional[object]] = mapped_column(
        DateTime(timezone=True), nullable=True  # type: ignore[arg-type]
    )

    lease: Mapped["Lease"] = relationship(back_populates="renewals")

    __table_args__ = (
        CheckConstraint(
            "proposed_monthly_rent_amount >= 0", name="renewal_rent_nonneg"
        ),
    )


class MoveOut(Base, AuditMixin, OrgScopedMixin):
    """End-of-lease workflow: notice, inspection, deductions, refund."""

    __tablename__ = "move_outs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    lease_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("leases.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    notice_date: Mapped[object] = mapped_column(Date, nullable=False)
    actual_move_date: Mapped[Optional[object]] = mapped_column(Date, nullable=True)
    keys_returned_at: Mapped[Optional[object]] = mapped_column(
        DateTime(timezone=True), nullable=True  # type: ignore[arg-type]
    )
    inspection_at: Mapped[Optional[object]] = mapped_column(
        DateTime(timezone=True), nullable=True  # type: ignore[arg-type]
    )
    deductions_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, server_default=text("0")
    )
    deductions_currency: Mapped[str] = mapped_column(
        String(3), nullable=False, server_default=text("'USD'")
    )
    refund_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, server_default=text("0")
    )
    status: Mapped[MoveOutStatusEnum] = mapped_column(
        SAEnum(
            MoveOutStatusEnum,
            name="moveout_status_enum",
            native_enum=False,
            length=32,
        ),
        nullable=False,
        server_default=text("'NOTICE_FILED'"),
    )

    lease: Mapped["Lease"] = relationship(back_populates="move_out")

    __table_args__ = (
        CheckConstraint("deductions_amount >= 0", name="moveout_deductions_nonneg"),
        CheckConstraint("refund_amount >= 0", name="moveout_refund_nonneg"),
    )


__all__ = [
    "TenantStatusEnum",
    "LeaseStatusEnum",
    "RenewalStatusEnum",
    "MoveOutStatusEnum",
    "Tenant",
    "Lease",
    "LeaseTerms",
    "LeaseRenewal",
    "MoveOut",
]
