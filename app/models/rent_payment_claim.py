"""PASAY-MILESTONE-002 — Rent Payment Claim truth model.

One ``rent_payment_claim`` row = ONE reported rent payment for a specific
lease period (YYYY-MM) with its own lifecycle and idempotency boundary:

    PENDING  -- payment reported (tenant/secretary claims paid), awaiting
                real-world verification (bank slip / GCash ref / landlord
                confirmation of the deposit).
    VERIFIED -- verification succeeded; only then does the claimed amount
                enter the verified-paid aggregate for the period.
    FAILED   -- verification failed (bad ref, amount mismatch, not found).
                NEVER enters the aggregate.
    REVERSED -- a previously VERIFIED payment was legitimately reversed
                (e.g. bounced check).  Its verified amount is removed from
                the aggregate.

The claim is the authoritative record of "who claimed to have paid what
period and when". Payment evidence (``Evidence`` rows with
``entity_type='rent_payment_claim'`` / ``entity_id=<claim.id>``) binds to a
specific claim so each separate payment is traced independently.

A deterministic ``idempotency_key`` gives the DB-level boundary for
duplicate submission / retry / double-click.
"""
from __future__ import annotations

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


class RentClaimStatus(str, Enum):
    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    REVERSED = "REVERSED"


class RentPaymentClaim(AuditMixin, Base):
    __tablename__ = "rent_payment_claims"
    __table_args__ = (
        Index(
            "uq_rent_payment_claims_idempotency_key",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index(
            "ix_rent_payment_claims_lease_period_status",
            "lease_id",
            "period",
            "status",
        ),
        Index(
            "ix_rent_payment_claims_period_status",
            "period",
            "status",
        ),
        Index("ix_rent_payment_claims_status", "status"),
    )

    lease_id: Mapped[int] = mapped_column(
        ForeignKey("leases.id"), nullable=False, index=True
    )
    period: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    income_id: Mapped[int | None] = mapped_column(
        ForeignKey("incomes.id"), nullable=True, index=True
    )

    claimed_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    claimed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    received_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[RentClaimStatus] = mapped_column(
        pg_enum(RentClaimStatus, "rent_claim_status"),
        nullable=False,
        default=RentClaimStatus.PENDING,
    )
    evidence_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    verification_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2), nullable=True
    )
    verified_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    mismatch: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    mismatch_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
