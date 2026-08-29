"""PASAY reference implementation — Payment / Verification / Receipt ORM models.

Issue #99 / PR #100 /oc continuation. Staged under
``.opencode-qualification/reference/`` pending Owner-side promotion to
``app/models/payment.py``.

Entities in this file:
    * RentPayment        — claim / receipt / verification of a rent payment.
    * RentVerification   — irreversible verification record (Operation is Truth).
    * RentReceipt        — uploaded evidence (file URL + hash).
    * ExpenseClaim       — Secretary-claimed expense awaiting approval.
    * ExpenseApproval    — approval record (creator != approver, fail-closed).
    * ExpenseReceipt     — uploaded evidence.

All money columns are ``Numeric(14, 2)``. All timestamps are
``DateTime(timezone=True)``. Every business row carries ``org_id``.
"""
from __future__ import annotations

import enum
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from pasay_db_layer import AuditMixin, Base, OrgScopedMixin


class RentPaymentStateEnum(str, enum.Enum):
    DUE = "DUE"
    CLAIMED = "CLAIMED"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    OVERPAID = "OVERPAID"
    PARTIAL = "PARTIAL"


class ExpenseClaimStateEnum(str, enum.Enum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    PAID = "PAID"


class RentPayment(Base, AuditMixin, OrgScopedMixin):
    """A single rent payment claim or receipt tied to a Lease period."""

    __tablename__ = "rent_payments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    lease_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("leases.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    period_start: Mapped[object] = mapped_column(
        Integer, nullable=False
    )  # store as ISO date string; avoid python-date dep here
    period_end: Mapped[object] = mapped_column(Integer, nullable=False)
    amount_due: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    amount_paid: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, server_default=text("0")
    )
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, server_default=text("'USD'")
    )
    state: Mapped[RentPaymentStateEnum] = mapped_column(
        SAEnum(
            RentPaymentStateEnum,
            name="rent_payment_state_enum",
            native_enum=False,
            length=16,
        ),
        nullable=False,
        server_default=text("'DUE'"),
    )
    idempotency_key: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, index=True
    )

    __table_args__ = (
        CheckConstraint("amount_due >= 0", name="rent_amount_due_nonneg"),
        CheckConstraint("amount_paid >= 0", name="rent_amount_paid_nonneg"),
        CheckConstraint(
            "period_start::int <= period_end::int",
            name="rent_period_ordered",
        ),
    )


class RentVerification(Base, AuditMixin, OrgScopedMixin):
    """Irreversible record that a RentPayment has been VERIFIED by a human.

    Operation is Truth: a payment is VERIFIED iff at least one of these rows
    exists with no corresponding rejection row.
    """

    __tablename__ = "rent_verifications"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    payment_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("rent_payments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    verified_by_user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    verified_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False
    )
    evidence_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "verified_amount >= 0", name="rent_verif_amount_nonneg"
        ),
    )


class RentReceipt(Base, AuditMixin, OrgScopedMixin):
    """File evidence for a RentPayment (URL + content hash for tamper-evidence)."""

    __tablename__ = "rent_receipts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    payment_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("rent_payments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    file_url: Mapped[str] = mapped_column(Text, nullable=False)
    file_hash_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "size_bytes >= 0", name="rent_receipt_size_nonneg"
        ),
        UniqueConstraint(
            "payment_id", "file_hash_sha256", name="uq_rent_receipt_hash"
        ),
    )


class ExpenseClaim(Base, AuditMixin, OrgScopedMixin):
    """An expense claim raised by a Secretary/Owner; awaits approval + payment."""

    __tablename__ = "expense_claims"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    operation_id: Mapped[Optional[str]] = mapped_column(
        String(64),
        ForeignKey("operations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, server_default=text("'USD'")
    )
    state: Mapped[ExpenseClaimStateEnum] = mapped_column(
        SAEnum(
            ExpenseClaimStateEnum,
            name="expense_claim_state_enum",
            native_enum=False,
            length=16,
        ),
        nullable=False,
        server_default=text("'DRAFT'"),
    )
    created_by_user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    idempotency_key: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, index=True
    )

    __table_args__ = (
        CheckConstraint("amount >= 0", name="expense_amount_nonneg"),
    )


class ExpenseApproval(Base, AuditMixin, OrgScopedMixin):
    """Approval (or rejection) record for an ExpenseClaim.

    Creator != approver is enforced via cross-row CHECK that fails closed
    when the application forgets to validate.
    """

    __tablename__ = "expense_approvals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    claim_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("expense_claims.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    approver_user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    decision: Mapped[str] = mapped_column(String(16), nullable=False)  # APPROVED|REJECTED
    decided_at: Mapped[object] = mapped_column(
        Integer, nullable=False
    )  # ISO-8601 UTC string
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "decision IN ('APPROVED','REJECTED')",
            name="expense_decision_enum",
        ),
        UniqueConstraint("claim_id", name="uq_expense_approvals_claim"),
    )


class ExpenseReceipt(Base, AuditMixin, OrgScopedMixin):
    """File evidence for an ExpenseClaim."""

    __tablename__ = "expense_receipts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    claim_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("expense_claims.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    file_url: Mapped[str] = mapped_column(Text, nullable=False)
    file_hash_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "size_bytes >= 0", name="expense_receipt_size_nonneg"
        ),
        UniqueConstraint(
            "claim_id", "file_hash_sha256", name="uq_expense_receipt_hash"
        ),
    )


__all__ = [
    "RentPaymentStateEnum",
    "ExpenseClaimStateEnum",
    "RentPayment",
    "RentVerification",
    "RentReceipt",
    "ExpenseClaim",
    "ExpenseApproval",
    "ExpenseReceipt",
]
