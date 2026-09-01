"""Rent / Payment ORM — Operation is Truth, Task is Projection.

Invariants encoded in the schema itself (AGENTS.md §4, DATA_CONTRACT):

- Money is ``NUMERIC(14, 2)``; never float.
- Timestamps are ``timestamptz``; never naive.
- Every table is org-scoped (``org_id`` FK RESTRICT + index) so the
  Organization/Membership boundary is enforced at the storage layer too.
- ``Operation`` is the business truth for one rent-collection cycle.
  ``Task`` is only the projection of a human follow-up action and can
  never resolve an Operation by itself.
- ``RentPayment`` is a CLAIM. ``RentEvidence`` (proof) and
  ``RentVerification`` (decision) are separate rows, so a claim can never
  masquerade as a verified payment.
- ``(org_id, idempotency_key)`` is UNIQUE on ``v1_rent_payments``:
  replaying a claim can never create a second payment row.
- At most ONE open ``Task`` per ``Operation`` (partial unique index,
  expressed for both PostgreSQL and SQLite).

``Operation.subject_id`` intentionally carries no foreign key: Operation is
polymorphic across rent / expense / repair subjects. Integrity is
guaranteed by the ``(org_id, kind, subject_type, subject_id)`` unique
constraint plus the owning service, which is the only writer.
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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.v1.models.base import (
    BigPK,
    OperationState,
    TimestampMixin,
    V1Base,
    utcnow,
)


# --- domain vocabulary ------------------------------------------------


class RentDueState(StrEnum):
    """Lifecycle of one rent period for one lease."""

    DUE = "DUE"
    OVERDUE = "OVERDUE"
    PAID = "PAID"
    CANCELLED = "CANCELLED"


class VerificationDecision(StrEnum):
    """Append-only verification decisions recorded against a claim."""

    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    REVERSED = "REVERSED"


class EvidenceKind(StrEnum):
    PHOTO = "PHOTO"
    DOCUMENT = "DOCUMENT"
    TEXT = "TEXT"
    TELEGRAM_FILE = "TELEGRAM_FILE"


class RentActivityKind(StrEnum):
    """Append-only rent history entries."""

    DUE_CREATED = "DUE_CREATED"
    MARKED_OVERDUE = "MARKED_OVERDUE"
    FOLLOW_UP_CREATED = "FOLLOW_UP_CREATED"
    FOLLOW_UP_DONE = "FOLLOW_UP_DONE"
    CLAIM_CREATED = "CLAIM_CREATED"
    CLAIM_REPLAYED = "CLAIM_REPLAYED"
    EVIDENCE_ADDED = "EVIDENCE_ADDED"
    PARTIAL_VERIFIED = "PARTIAL_VERIFIED"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    REVERSED = "REVERSED"
    PAID = "PAID"
    REOPENED = "REOPENED"


RENT_DUE_STATES = tuple(s.value for s in RentDueState)
VERIFICATION_DECISIONS = tuple(d.value for d in VerificationDecision)
EVIDENCE_KINDS = tuple(k.value for k in EvidenceKind)

OPERATION_KIND_RENT = "RENT_COLLECTION"
OPERATION_SUBJECT_RENT_DUE = "rent_due_schedule"
TASK_KIND_RENT_FOLLOW_UP = "RENT_FOLLOW_UP"


# --- tables -----------------------------------------------------------


class Operation(V1Base, TimestampMixin):
    """Business truth: a real-world problem that must actually be resolved."""

    __tablename__ = "v1_operations"
    __table_args__ = (
        CheckConstraint(
            "state IN ('open','in_progress','resolved','cancelled')",
            name="ck_v1_operations_state",
        ),
        UniqueConstraint(
            "org_id",
            "kind",
            "subject_type",
            "subject_id",
            name="uq_v1_operations_org_kind_subject",
        ),
        Index("ix_v1_operations_org_id", "org_id"),
        Index(
            "ix_v1_operations_org_subject",
            "org_id",
            "subject_type",
            "subject_id",
        ),
    )

    id: Mapped[BigPK]
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default=OperationState.OPEN.value,
    )
    due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )


class Task(V1Base, TimestampMixin):
    """Projection of a human follow-up action. NEVER the business truth.

    The partial unique index guarantees at most one *open* Task per
    Operation, so a stale reminder can never be mistaken for a second
    live obligation.
    """

    __tablename__ = "v1_tasks"
    __table_args__ = (
        CheckConstraint(
            "state IN ('open','done','cancelled')", name="ck_v1_tasks_state",
        ),
        Index("ix_v1_tasks_org_id", "org_id"),
        Index("ix_v1_tasks_operation_id", "operation_id"),
        Index(
            "uq_v1_tasks_one_open_per_operation",
            "operation_id",
            unique=True,
            postgresql_where=text("state = 'open'"),
            sqlite_where=text("state = 'open'"),
        ),
    )

    id: Mapped[BigPK]
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    operation_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_operations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    done_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )


class RentDueSchedule(V1Base, TimestampMixin):
    """One rent period for one lease: how much is owed and by when."""

    __tablename__ = "v1_rent_due_schedules"
    __table_args__ = (
        CheckConstraint(
            "state IN ('DUE','OVERDUE','PAID','CANCELLED')",
            name="ck_v1_rent_due_schedules_state",
        ),
        CheckConstraint(
            "amount_due > 0", name="ck_v1_rent_due_schedules_amount_positive",
        ),
        CheckConstraint(
            "due_date >= period_start", name="ck_v1_rent_due_schedules_dates",
        ),
        UniqueConstraint(
            "lease_id",
            "period_start",
            name="uq_v1_rent_due_schedules_lease_period",
        ),
        Index("ix_v1_rent_due_schedules_org_id", "org_id"),
        Index(
            "ix_v1_rent_due_schedules_org_due_date", "org_id", "due_date",
        ),
        Index("ix_v1_rent_due_schedules_lease_id", "lease_id"),
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
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount_due: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default=RentDueState.DUE.value,
    )


class RentPayment(V1Base, TimestampMixin):
    """A CLAIM that rent was paid. Not money truth on its own.

    ``verified_amount`` is only ever set by the verification path and is
    cleared on rejection/reversal, so summing VERIFIED rows is the single
    source of truth for how much rent actually arrived.
    """

    __tablename__ = "v1_rent_payments"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','VERIFIED','FAILED','REVERSED')",
            name="ck_v1_rent_payments_status",
        ),
        CheckConstraint(
            "claimed_amount > 0", name="ck_v1_rent_payments_claimed_positive",
        ),
        CheckConstraint(
            "verified_amount IS NULL OR verified_amount > 0",
            name="ck_v1_rent_payments_verified_positive",
        ),
        UniqueConstraint(
            "org_id",
            "idempotency_key",
            name="uq_v1_rent_payments_org_idempotency_key",
        ),
        Index("ix_v1_rent_payments_org_id", "org_id"),
        Index(
            "ix_v1_rent_payments_due_schedule_id", "due_schedule_id",
        ),
    )

    id: Mapped[BigPK]
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    due_schedule_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_rent_due_schedules.id", ondelete="RESTRICT"),
        nullable=False,
    )
    claimed_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False,
    )
    verified_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2), nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="PENDING",
    )
    claimed_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class RentEvidence(V1Base, TimestampMixin):
    """Proof attached to a claim. Separate row: Evidence is not a decision."""

    __tablename__ = "v1_rent_evidences"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('PHOTO','DOCUMENT','TEXT','TELEGRAM_FILE')",
            name="ck_v1_rent_evidences_kind",
        ),
        Index("ix_v1_rent_evidences_org_id", "org_id"),
        Index(
            "ix_v1_rent_evidences_rent_payment_id", "rent_payment_id",
        ),
    )

    id: Mapped[BigPK]
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    rent_payment_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_rent_payments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    reference: Mapped[str] = mapped_column(String(500), nullable=False)
    uploaded_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_users.id", ondelete="SET NULL"),
        nullable=True,
    )


class RentVerification(V1Base, TimestampMixin):
    """Append-only verification decision log for a claim."""

    __tablename__ = "v1_rent_verifications"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('VERIFIED','REJECTED','REVERSED')",
            name="ck_v1_rent_verifications_decision",
        ),
        Index("ix_v1_rent_verifications_org_id", "org_id"),
        Index(
            "ix_v1_rent_verifications_rent_payment_id", "rent_payment_id",
        ),
    )

    id: Mapped[BigPK]
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    rent_payment_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_rent_payments.id", ondelete="RESTRICT"),
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


class RentActivity(V1Base, TimestampMixin):
    """Append-only rent history / activity feed."""

    __tablename__ = "v1_rent_activities"
    __table_args__ = (
        Index("ix_v1_rent_activities_org_id", "org_id"),
        Index(
            "ix_v1_rent_activities_due_schedule_id", "due_schedule_id",
        ),
    )

    id: Mapped[BigPK]
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    due_schedule_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_rent_due_schedules.id", ondelete="RESTRICT"),
        nullable=True,
    )
    rent_payment_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_rent_payments.id", ondelete="RESTRICT"),
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
    "EVIDENCE_KINDS",
    "EvidenceKind",
    "OPERATION_KIND_RENT",
    "OPERATION_SUBJECT_RENT_DUE",
    "Operation",
    "RENT_DUE_STATES",
    "RentActivity",
    "RentActivityKind",
    "RentDueSchedule",
    "RentDueState",
    "RentEvidence",
    "RentPayment",
    "RentVerification",
    "TASK_KIND_RENT_FOLLOW_UP",
    "Task",
    "VERIFICATION_DECISIONS",
    "VerificationDecision",
]
