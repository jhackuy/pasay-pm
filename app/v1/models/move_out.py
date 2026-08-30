"""Move-out / Settlement ORM.

AGENTS.md §4 invariants encoded here:

- Money is ``NUMERIC(14, 2)``; never float. Every settlement amount is
  stored as a separate column with the matching CHECK ``>= 0``; the
  net refund / owed is derived.
- Timestamps are ``timestamptz``; never naive.
- Every table is org-scoped (``org_id`` FK RESTRICT + index).
- ``MoveOut`` is the typed inspection request attached to a lease that
  has reached end-of-term or has been terminated. It is a single
  business truth: there is a pending move-out to settle. State machine:
  ``REQUESTED -> INSPECTED -> SETTLED`` or ``CANCELLED``. Closure
  (``SETTLED``) requires a recorded ``DepositSettlement`` whose
  ``disposition`` matches the policy.
- ``MoveOutInspection`` is the typed walk-through record. A single
  inspection can have multiple ``MoveOutDamage`` rows but at least one
  is required to drive a SETTLED transition.
- ``DepositSettlement`` is the OWNER-only typed accounting record.
  ``disposition`` is one of ``('FULL_REFUND','PARTIAL_REFUND','NO_REFUND','ADDITIONAL_OWED')``.
  The settlement is the SINGLE source of truth for "deposit cleared".
  It records ``deposit_held`` (what we kept), ``deductions_total``
  (sum of accepted damage charges), ``refund_amount`` (what we
  returned), and ``additional_owed`` (what the tenant still owes).
- ``(org_id, idempotency_key)`` is UNIQUE on ``v1_move_outs`` so a
  request can never be duplicated by a replay.
- ``Operation`` is reused polymorphically
  (``subject_type='move_out'``, ``kind='MOVE_OUT_SETTLEMENT'``). The
  Operation resolves ONLY when ``DepositSettlement`` is recorded with a
  terminal disposition AND the move-out transitions to ``SETTLED``.
- ``Task`` is the projection of a follow-up. A Task can never resolve
  an Operation by itself.
- ``MoveOutDamage`` is *not* a Repair — it is the financial liability
  recorded against the deposit. Repairs remain a separate lifecycle.
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


class MoveOutState(StrEnum):
    """Lifecycle of a single move-out inspection + settlement."""

    REQUESTED = "REQUESTED"
    INSPECTED = "INSPECTED"
    SETTLED = "SETTLED"
    CANCELLED = "CANCELLED"


class MoveOutDamageKind(StrEnum):
    """What kind of damage is being recorded against the deposit."""

    CLEANING = "CLEANING"
    REPAIR = "REPAIR"
    REPLACEMENT = "REPLACEMENT"
    UTILITIES = "UTILITIES"
    OTHER = "OTHER"


class DepositDisposition(StrEnum):
    """Terminal disposition of a deposit settlement."""

    FULL_REFUND = "FULL_REFUND"
    PARTIAL_REFUND = "PARTIAL_REFUND"
    NO_REFUND = "NO_REFUND"
    ADDITIONAL_OWED = "ADDITIONAL_OWED"


class MoveOutActivityKind(StrEnum):
    """Append-only move-out history / activity feed."""

    REQUESTED = "REQUESTED"
    REQUEST_REPLAYED = "REQUEST_REPLAYED"
    INSPECTED = "INSPECTED"
    DAMAGE_RECORDED = "DAMAGE_RECORDED"
    SETTLED = "SETTLED"
    CANCELLED = "CANCELLED"
    REOPENED = "REOPENED"
    FOLLOW_UP_CREATED = "FOLLOW_UP_CREATED"
    FOLLOW_UP_DONE = "FOLLOW_UP_DONE"


MOVE_OUT_STATES = tuple(s.value for s in MoveOutState)
MOVE_OUT_DAMAGE_KINDS = tuple(k.value for k in MoveOutDamageKind)
DEPOSIT_DISPOSITIONS = tuple(d.value for d in DepositDisposition)
MOVE_OUT_ACTIVITY_KINDS = tuple(k.value for k in MoveOutActivityKind)

OPERATION_KIND_MOVE_OUT = "MOVE_OUT_SETTLEMENT"
OPERATION_SUBJECT_MOVE_OUT = "move_out"
TASK_KIND_MOVE_OUT_FOLLOW_UP = "MOVE_OUT_FOLLOW_UP"


# --- tables -----------------------------------------------------------


class MoveOut(V1Base, TimestampMixin):
    """A typed move-out request attached to a lease.

    Closure gate: ``state=SETTLED`` requires a ``DepositSettlement`` with
    a terminal disposition recorded on or after this move-out's
    inspection date.
    """

    __tablename__ = "v1_move_outs"
    __table_args__ = (
        CheckConstraint(
            "state IN ('REQUESTED','INSPECTED','SETTLED','CANCELLED')",
            name="ck_v1_move_outs_state",
        ),
        UniqueConstraint(
            "org_id",
            "idempotency_key",
            name="uq_v1_move_outs_org_idempotency_key",
        ),
        Index("ix_v1_move_outs_org_id", "org_id"),
        Index(
            "ix_v1_move_outs_org_state", "org_id", "state",
        ),
        Index("ix_v1_move_outs_lease_id", "lease_id"),
        Index("ix_v1_move_outs_settlement_id", "settlement_id"),
    )

    id: Mapped[BigPK]
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    lease_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_leases.id", ondelete="RESTRICT"),
        nullable=False,
    )
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default=MoveOutState.REQUESTED.value,
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    requested_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    planned_move_out_date: Mapped[date | None] = mapped_column(
        Date, nullable=True,
    )
    inspected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    inspected_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    inspection_notes: Mapped[str | None] = mapped_column(
        String(4000), nullable=True,
    )
    settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # NOTE: settlement_id logically points at v1_deposit_settlements.id,
    # but a hard FK there creates a cycle with v1_deposit_settlements.move_out_id.
    # The DB-level FK is created via ALTER TABLE after both tables exist
    # (see alembic/versions/0001_baseline.py). ORM-side referential
    # integrity is enforced by the closure-gate flow in MoveOutService.
    settlement_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True,
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    cancel_reason: Mapped[str | None] = mapped_column(
        String(500), nullable=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class MoveOutInspection(V1Base, TimestampMixin):
    """A single walk-through record attached to a MoveOut.

    At least one ``MoveOutInspection`` row is required to transition a
    MoveOut from ``REQUESTED`` to ``INSPECTED``.
    """

    __tablename__ = "v1_move_out_inspections"
    __table_args__ = (
        Index("ix_v1_move_out_inspections_org_id", "org_id"),
        Index(
            "ix_v1_move_out_inspections_move_out_id", "move_out_id",
        ),
    )

    id: Mapped[BigPK]
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    move_out_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_move_outs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    inspected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    inspected_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    summary: Mapped[str] = mapped_column(String(4000), nullable=False)


class MoveOutDamage(V1Base, TimestampMixin):
    """A single damage / charge item recorded against the deposit.

    ``amount`` is the gross charge for this item. ``accepted_amount``
    is what the OWNER actually applied to the deposit (may be 0 if
    rejected, may equal ``amount`` if accepted in full).
    """

    __tablename__ = "v1_move_out_damages"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('CLEANING','REPAIR','REPLACEMENT','UTILITIES','OTHER')",
            name="ck_v1_move_out_damages_kind",
        ),
        CheckConstraint(
            "amount >= 0", name="ck_v1_move_out_damages_amount_nonneg",
        ),
        CheckConstraint(
            "accepted_amount >= 0",
            name="ck_v1_move_out_damages_accepted_nonneg",
        ),
        CheckConstraint(
            "accepted_amount <= amount",
            name="ck_v1_move_out_damages_accepted_le_amount",
        ),
        Index("ix_v1_move_out_damages_org_id", "org_id"),
        Index(
            "ix_v1_move_out_damages_move_out_id", "move_out_id",
        ),
    )

    id: Mapped[BigPK]
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    move_out_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_move_outs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str] = mapped_column(String(2000), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    accepted_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0"),
    )
    recorded_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_users.id", ondelete="SET NULL"),
        nullable=True,
    )


class DepositSettlement(V1Base, TimestampMixin):
    """OWNER-only deposit settlement record.

    Terminal disposition. The single source of truth for "the deposit
    has been cleared". Once recorded against a MoveOut, the MoveOut
    transitions to ``SETTLED`` and the linked Operation resolves.
    """

    __tablename__ = "v1_deposit_settlements"
    __table_args__ = (
        CheckConstraint(
            "disposition IN ("
            "'FULL_REFUND','PARTIAL_REFUND','NO_REFUND','ADDITIONAL_OWED'"
            ")",
            name="ck_v1_deposit_settlements_disposition",
        ),
        CheckConstraint(
            "deposit_held >= 0",
            name="ck_v1_deposit_settlements_deposit_held_nonneg",
        ),
        CheckConstraint(
            "deductions_total >= 0",
            name="ck_v1_deposit_settlements_deductions_nonneg",
        ),
        CheckConstraint(
            "refund_amount >= 0",
            name="ck_v1_deposit_settlements_refund_nonneg",
        ),
        CheckConstraint(
            "additional_owed >= 0",
            name="ck_v1_deposit_settlements_additional_owed_nonneg",
        ),
        CheckConstraint(
            "(disposition = 'FULL_REFUND' AND refund_amount = deposit_held "
            "AND additional_owed = 0) OR disposition <> 'FULL_REFUND'",
            name="ck_v1_deposit_settlements_full_refund_amounts",
        ),
        CheckConstraint(
            "(disposition = 'NO_REFUND' AND refund_amount = 0 "
            "AND additional_owed = 0) OR disposition <> 'NO_REFUND'",
            name="ck_v1_deposit_settlements_no_refund_amounts",
        ),
        Index("ix_v1_deposit_settlements_org_id", "org_id"),
        Index(
            "ix_v1_deposit_settlements_move_out_id", "move_out_id",
        ),
    )

    id: Mapped[BigPK]
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    move_out_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_move_outs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    disposition: Mapped[str] = mapped_column(String(20), nullable=False)
    deposit_held: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False,
    )
    deductions_total: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0"),
    )
    refund_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0"),
    )
    additional_owed: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0"),
    )
    notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    settled_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    settled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class MoveOutActivity(V1Base, TimestampMixin):
    """Append-only move-out history / activity feed."""

    __tablename__ = "v1_move_out_activities"
    __table_args__ = (
        Index("ix_v1_move_out_activities_org_id", "org_id"),
        Index(
            "ix_v1_move_out_activities_move_out_id", "move_out_id",
        ),
    )

    id: Mapped[BigPK]
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    move_out_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_move_outs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
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
    "DEPOSIT_DISPOSITIONS",
    "MOVE_OUT_ACTIVITY_KINDS",
    "MOVE_OUT_DAMAGE_KINDS",
    "MOVE_OUT_STATES",
    "DepositDisposition",
    "DepositSettlement",
    "MoveOut",
    "MoveOutActivity",
    "MoveOutActivityKind",
    "MoveOutDamage",
    "MoveOutDamageKind",
    "MoveOutInspection",
    "MoveOutState",
    "OPERATION_KIND_MOVE_OUT",
    "OPERATION_SUBJECT_MOVE_OUT",
    "TASK_KIND_MOVE_OUT_FOLLOW_UP",
]
