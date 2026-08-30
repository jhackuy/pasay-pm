"""Lease Renewal ORM.

AGENTS.md §4 invariants encoded here:

- Money is ``NUMERIC(14, 2)``; never float.
- Timestamps are ``timestamptz``; never naive.
- Every table is org-scoped (``org_id`` FK RESTRICT + index).
- ``LeaseRenewal`` is a typed proposal: someone proposes a renewal of an
  ACTIVE lease with new dates, new monthly rent, new deposit. The proposal
  goes through ``PROPOSED -> APPROVED -> EXECUTED`` (or REJECTED /
  CANCELLED). Only ``execute`` mutates the source lease and creates the
  new lease; this is the single transition path that touches
  ``v1_leases``.
- ``(org_id, idempotency_key)`` is UNIQUE on ``v1_lease_renewals`` so a
  proposal can never be duplicated by a replay.
- ``Operation`` is reused polymorphically (``subject_type='lease_renewal'``,
  ``kind='LEASE_RENEWAL'``). The Operation is the business truth: a
  renewal resolves ONLY when ``execute`` has produced the new lease and
  flipped the unit status to OCCUPIED for the new lease.
- ``Task`` is the projection of a follow-up (e.g. "send new terms to
  tenant for signature"). A Task can never resolve an Operation by
  itself.
- ``Approval != Execution``. A proposal may be APPROVED without ever
  being EXECUTED (e.g. tenant changes mind); the source lease keeps
  its original terms until ``execute`` is called.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.v1.models.base import BigPK, TimestampMixin, V1Base, utcnow


# --- domain vocabulary ------------------------------------------------


class RenewalState(StrEnum):
    """5-state renewal lifecycle + CANCELLED.

    PROPOSED   -> tenant/secretary submitted a typed renewal proposal
    APPROVED   -> OWNER accepted the proposed terms
    REJECTED   -> OWNER rejected the proposal (terminal, source lease unaffected)
    EXECUTED   -> source lease terminated + new lease activated + unit
                  status flipped (closure gate)
    CANCELLED  -> proposal withdrawn before execution (terminal, source
                  lease unaffected)
    """

    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTED = "EXECUTED"
    CANCELLED = "CANCELLED"


class RenewalActivityKind(StrEnum):
    """Append-only renewal history / activity feed."""

    PROPOSED = "PROPOSED"
    PROPOSAL_REPLAYED = "PROPOSAL_REPLAYED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTED = "EXECUTED"
    CANCELLED = "CANCELLED"
    SOURCE_LEASE_TERMINATED = "SOURCE_LEASE_TERMINATED"
    NEW_LEASE_CREATED = "NEW_LEASE_CREATED"
    NEW_LEASE_ACTIVATED = "NEW_LEASE_ACTIVATED"
    UNIT_REASSIGNED = "UNIT_REASSIGNED"
    REOPENED = "REOPENED"
    FOLLOW_UP_CREATED = "FOLLOW_UP_CREATED"
    FOLLOW_UP_DONE = "FOLLOW_UP_DONE"


RENEWAL_STATES = tuple(s.value for s in RenewalState)
RENEWAL_ACTIVITY_KINDS = tuple(k.value for k in RenewalActivityKind)

OPERATION_KIND_LEASE_RENEWAL = "LEASE_RENEWAL"
OPERATION_SUBJECT_LEASE_RENEWAL = "lease_renewal"
TASK_KIND_RENEWAL_FOLLOW_UP = "RENEWAL_FOLLOW_UP"


# --- tables -----------------------------------------------------------


class LeaseRenewal(V1Base, TimestampMixin):
    """A typed renewal proposal attached to an ACTIVE lease.

    ``idempotency_key`` is opaque, case-preserving.
    ``new_lease_id`` is populated only when ``execute`` has produced the
    replacement lease; until then it is NULL.
    """

    __tablename__ = "v1_lease_renewals"
    __table_args__ = (
        CheckConstraint(
            "state IN ('PROPOSED','APPROVED','REJECTED','EXECUTED','CANCELLED')",
            name="ck_v1_lease_renewals_state",
        ),
        CheckConstraint(
            "proposed_end_date > proposed_start_date",
            name="ck_v1_lease_renewals_dates",
        ),
        CheckConstraint(
            "proposed_monthly_rent > 0",
            name="ck_v1_lease_renewals_rent_positive",
        ),
        CheckConstraint(
            "proposed_deposit >= 0",
            name="ck_v1_lease_renewals_deposit_nonneg",
        ),
        UniqueConstraint(
            "org_id",
            "idempotency_key",
            name="uq_v1_lease_renewals_org_idempotency_key",
        ),
        Index("ix_v1_lease_renewals_org_id", "org_id"),
        Index(
            "ix_v1_lease_renewals_org_state", "org_id", "state",
        ),
        Index(
            "ix_v1_lease_renewals_source_lease_id", "source_lease_id",
        ),
        Index(
            "ix_v1_lease_renewals_new_lease_id", "new_lease_id",
        ),
    )

    id: Mapped[BigPK]
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_lease_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_leases.id", ondelete="RESTRICT"),
        nullable=False,
    )
    new_lease_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_leases.id", ondelete="RESTRICT"),
        nullable=True,
    )
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default=RenewalState.PROPOSED.value,
    )
    proposed_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    proposed_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    proposed_monthly_rent: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False,
    )
    proposed_deposit: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0"),
    )
    proposed_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    proposed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    decided_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    decision_reason: Mapped[str | None] = mapped_column(
        String(500), nullable=True,
    )
    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class RenewalActivity(V1Base, TimestampMixin):
    """Append-only renewal history / activity feed."""

    __tablename__ = "v1_renewal_activities"
    __table_args__ = (
        Index("ix_v1_renewal_activities_org_id", "org_id"),
        Index(
            "ix_v1_renewal_activities_renewal_id", "renewal_id",
        ),
    )

    id: Mapped[BigPK]
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    renewal_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_lease_renewals.id", ondelete="RESTRICT"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


__all__ = [
    "OPERATION_KIND_LEASE_RENEWAL",
    "OPERATION_SUBJECT_LEASE_RENEWAL",
    "RENEWAL_ACTIVITY_KINDS",
    "RENEWAL_STATES",
    "RenewalActivity",
    "RenewalActivityKind",
    "RenewalState",
    "TASK_KIND_RENEWAL_FOLLOW_UP",
]
