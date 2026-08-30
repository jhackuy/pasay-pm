"""Expense ORM — Claim/Evidence/Verification separated, Operation is Truth.

Invariants encoded in the schema (AGENTS.md §4, DATA_CONTRACT):

- Money is ``NUMERIC(14, 2)``; never float.
- Timestamps are ``timestamptz``; never naive.
- Every table is org-scoped (``org_id`` FK RESTRICT + index) so the
  Organization/Membership boundary is enforced at the storage layer too.
- ``ExpenseClaim`` is a CLAIM. ``ExpenseReceipt`` (proof) and
  ``ExpenseVerification`` (decision) are separate rows: a claim can never
  masquerade as a verified payment.
- ``Operation`` is reused polymorphically (subject_type='expense_claim',
  kind='EXPENSE_SETTLEMENT'). The Operation is the only business truth and
  resolves only when the verified amount covers the claim.
- ``Task`` is only the projection of a human follow-up and never resolves
  an Operation by itself.
- ``(org_id, idempotency_key)`` is UNIQUE on ``v1_expense_claims``:
  replaying an ``open_claim`` can never create a second claim row.
- Approval (verify) is NOT Payment. A verified amount != claimed amount is
  recorded as an ``AMOUNT_MISMATCH`` activity, but the claim still settles
  with ``verified_total >= claimed_total`` (no fake-close).
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.v1.models.base import (
    BigPK,
    TimestampMixin,
    V1Base,
    utcnow,
)


# --- domain vocabulary ------------------------------------------------


class ExpenseClaimStatus(StrEnum):
    """Lifecycle of one expense claim."""

    OPEN = "OPEN"
    SUBMITTED = "SUBMITTED"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ExpenseCategory(StrEnum):
    """Category of an expense claim (separate from Property/Unit purpose)."""

    UTILITIES = "UTILITIES"
    REPAIRS = "REPAIRS"
    SUPPLIES = "SUPPLIES"
    TAX = "TAX"
    INSURANCE = "INSURANCE"
    SERVICE = "SERVICE"
    OTHER = "OTHER"


class ExpenseReceiptKind(StrEnum):
    """Proof kind attached to a claim."""

    PHOTO = "PHOTO"
    DOCUMENT = "DOCUMENT"
    TEXT = "TEXT"
    TELEGRAM_FILE = "TELEGRAM_FILE"


class ExpenseVerificationDecision(StrEnum):
    """Append-only verification decisions recorded against a claim."""

    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    REVERSED = "REVERSED"


class ExpenseActivityKind(StrEnum):
    """Append-only expense history / activity feed."""

    CLAIM_OPENED = "CLAIM_OPENED"
    CLAIM_REPLAYED = "CLAIM_REPLAYED"
    RECEIPT_ADDED = "RECEIPT_ADDED"
    SUBMITTED = "SUBMITTED"
    VERIFIED = "VERIFIED"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    REJECTED = "REJECTED"
    REVERSED = "REVERSED"
    SETTLED = "SETTLED"
    REOPENED = "REOPENED"
    CANCELLED = "CANCELLED"
    FOLLOW_UP_CREATED = "FOLLOW_UP_CREATED"
    FOLLOW_UP_DONE = "FOLLOW_UP_DONE"


EXPENSE_CLAIM_STATUSES = tuple(s.value for s in ExpenseClaimStatus)
EXPENSE_CATEGORIES = tuple(c.value for c in ExpenseCategory)
EXPENSE_RECEIPT_KINDS = tuple(k.value for k in ExpenseReceiptKind)
EXPENSE_VERIFICATION_DECISIONS = tuple(
    d.value for d in ExpenseVerificationDecision
)

OPERATION_KIND_EXPENSE = "EXPENSE_SETTLEMENT"
OPERATION_SUBJECT_EXPENSE_CLAIM = "expense_claim"
TASK_KIND_EXPENSE_FOLLOW_UP = "EXPENSE_FOLLOW_UP"


# --- tables -----------------------------------------------------------


class ExpenseClaim(V1Base, TimestampMixin):
    """A CLAIM that an expense was incurred. Not money truth on its own.

    ``claimed_amount`` is the amount asserted by the claimant.
    ``verified_amount`` is only ever set by the verification path (OWNER)
    and is cleared on REJECT/REVERSE; summing VERIFIED rows for a claim
    is the single source of truth for how much was actually approved.

    Status transitions:
    OPEN -> SUBMITTED (evidence added)
    OPEN/SUBMITTED -> VERIFIED (verified_amount set, _settle may close)
    OPEN/SUBMITTED -> FAILED (rejected)
    OPEN/SUBMITTED -> CANCELLED (claimant cancels)
    VERIFIED -> REOPENED via _reopen on REVERSED
    """

    __tablename__ = "v1_expense_claims"
    __table_args__ = (
        CheckConstraint(
            "status IN ('OPEN','SUBMITTED','VERIFIED','FAILED','CANCELLED')",
            name="ck_v1_expense_claims_status",
        ),
        CheckConstraint(
            "category IN ("
            "'UTILITIES','REPAIRS','SUPPLIES','TAX',"
            "'INSURANCE','SERVICE','OTHER'"
            ")",
            name="ck_v1_expense_claims_category",
        ),
        CheckConstraint(
            "claimed_amount > 0",
            name="ck_v1_expense_claims_claimed_positive",
        ),
        CheckConstraint(
            "verified_amount IS NULL OR verified_amount > 0",
            name="ck_v1_expense_claims_verified_positive",
        ),
        UniqueConstraint(
            "org_id",
            "idempotency_key",
            name="uq_v1_expense_claims_org_idempotency_key",
        ),
        Index("ix_v1_expense_claims_org_id", "org_id"),
        Index(
            "ix_v1_expense_claims_org_status", "org_id", "status",
        ),
    )

    id: Mapped[BigPK]
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    claimed_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False,
    )
    verified_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2), nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False,
        default=ExpenseClaimStatus.OPEN.value,
    )
    opened_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class ExpenseReceipt(V1Base, TimestampMixin):
    """Proof attached to a claim. Separate row: receipt is not a decision."""

    __tablename__ = "v1_expense_receipts"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('PHOTO','DOCUMENT','TEXT','TELEGRAM_FILE')",
            name="ck_v1_expense_receipts_kind",
        ),
        Index("ix_v1_expense_receipts_org_id", "org_id"),
        Index(
            "ix_v1_expense_receipts_claim_id", "claim_id",
        ),
    )

    id: Mapped[BigPK]
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    claim_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_expense_claims.id", ondelete="RESTRICT"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    reference: Mapped[str] = mapped_column(String(500), nullable=False)
    uploaded_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_users.id", ondelete="SET NULL"),
        nullable=True,
    )


class ExpenseVerification(V1Base, TimestampMixin):
    """Append-only verification decision log for a claim."""

    __tablename__ = "v1_expense_verifications"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('VERIFIED','REJECTED','REVERSED')",
            name="ck_v1_expense_verifications_decision",
        ),
        Index("ix_v1_expense_verifications_org_id", "org_id"),
        Index(
            "ix_v1_expense_verifications_claim_id", "claim_id",
        ),
    )

    id: Mapped[BigPK]
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    claim_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_expense_claims.id", ondelete="RESTRICT"),
        nullable=False,
    )
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    verified_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2), nullable=True,
    )
    verifier_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)


class ExpenseActivity(V1Base, TimestampMixin):
    """Append-only expense history / activity feed."""

    __tablename__ = "v1_expense_activities"
    __table_args__ = (
        Index("ix_v1_expense_activities_org_id", "org_id"),
        Index(
            "ix_v1_expense_activities_claim_id", "claim_id",
        ),
    )

    id: Mapped[BigPK]
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    claim_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_expense_claims.id", ondelete="RESTRICT"),
        nullable=True,
    )
    receipt_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_expense_receipts.id", ondelete="RESTRICT"),
        nullable=True,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
    actor_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


__all__ = [
    "EXPENSE_CATEGORIES",
    "EXPENSE_CLAIM_STATUSES",
    "EXPENSE_RECEIPT_KINDS",
    "EXPENSE_VERIFICATION_DECISIONS",
    "ExpenseActivity",
    "ExpenseActivityKind",
    "ExpenseCategory",
    "ExpenseClaim",
    "ExpenseClaimStatus",
    "ExpenseReceipt",
    "ExpenseReceiptKind",
    "ExpenseVerification",
    "ExpenseVerificationDecision",
    "OPERATION_KIND_EXPENSE",
    "OPERATION_SUBJECT_EXPENSE_CLAIM",
    "TASK_KIND_EXPENSE_FOLLOW_UP",
]