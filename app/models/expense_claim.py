"""PASAY-EXPENSE-OPERATION-003B — Payment Claim model.

One ``expense_payment_claim`` row = ONE Secretary-reported payment for an
expense, with its own lifecycle and idempotency boundary:

    PENDING  -- payment reported, awaiting real-world verification
    VERIFIED -- verification succeeded; only then does the claimed amount
               enter the verified-payment aggregate (E1/E2/E3/E4/E7)
    FAILED   -- verification failed; NEVER enters the aggregate (E7)
    REVERSED -- a previously VERIFIED payment was legitimately reversed;
               its verified amount is removed from the aggregate (E13)

The claim is the authoritative record of "who claimed to have paid what and
when" (section 3). Payment evidence (`Evidence` rows with
``entity_type='expense_payment_claim'`` / ``entity_id=<claim.id>``) binds to a
specific claim so multiple payments are traced separately (Claim1->Evidence1->
Verification1, Claim2->Evidence2->Verification2 — section 10).

A deterministic ``idempotency_key`` gives the DB-level boundary for duplicate
submission/retry/double-click so replaying the same claim never double-counts
(section 6 / E5).
"""
from datetime import datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import AuditMixin, Base, pg_enum


class ClaimStatus(str, Enum):
    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    REVERSED = "REVERSED"


class ExpensePaymentClaim(AuditMixin, Base):
    __tablename__ = "expense_payment_claims"
    __table_args__ = (
        # DB-level idempotency boundary: replaying the same deterministic claim
        # key can never create a second row (section 6 / E5). NULL keys are
        # skipped by the partial index.
        Index(
            "uq_expense_payment_claims_idempotency_key",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index("ix_expense_payment_claims_expense_status", "expense_id", "status"),
        Index("ix_expense_payment_claims_status", "status"),
    )

    expense_id: Mapped[int] = mapped_column(
        ForeignKey("expenses.id"), nullable=False, index=True
    )
    # The amount the person *claims* was paid. This is the human claim, not yet
    # a financial truth — it must not be confused with ``verified_amount``.
    claimed_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    claimed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status: Mapped[ClaimStatus] = mapped_column(
        pg_enum(ClaimStatus, "claim_status"),
        nullable=False,
        default=ClaimStatus.PENDING,
    )
    # Evidence rows (entity_type='expense_payment_claim', entity_id=claim.id)
    # that prove THIS claim. Kept as ids for the Mini App detail grouped per
    # claim (section 10).
    evidence_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # Free-text proof (reference number / bank confirmation / GCash ref).
    verification_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Verified amount: set to 0 while PENDING; only a VERIFIED claim sets it to
    # the amount that actually enters the verified aggregate.
    verified_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=True
    )
    verified_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Amount-mismatch (over-claim / > remaining) surfaced instead of silently
    # truncated or auto-PAID (section 5 / E6). The full claimed amount is kept;
    # the mismatch is preserved in the record and requires resolution.
    mismatch: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    mismatch_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Failure/blocker reason for FAILED claims (E7) or reversal reason.
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Server-generated deterministic key for duplicate-submission protection.
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
