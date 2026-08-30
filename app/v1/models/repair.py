"""Repair ORM — Operation is Truth, separate Report/Quote/Work/Completion/Verification.

AGENTS.md §4 + DATA_CONTRACT invariants encoded in the schema:

- Money is ``NUMERIC(14, 2)``; never float.
- Timestamps are ``timestamptz``; never naive.
- Every table is org-scoped (``org_id`` FK RESTRICT + index).
- ``RepairReport`` is the typed report submitted by a tenant/secretary.
  ``RepairQuote`` is the technician quote. ``RepairWork`` is the work
  progress log. ``RepairCompletionClaim`` is the completion assertion.
  ``RepairVerification`` is the OWNER's decision log. Each is its own
  table: a quote cannot be confused with a work update, a completion
  claim cannot be confused with a verification.
- ``Operation`` is reused polymorphically (subject_type='repair_report',
  kind='REPAIR_RESOLUTION'). The Operation is the only business truth.
  A Repair is CLOSED only when the OWNER explicitly verifies a real
  completion claim with verified work.
- ``Task`` is the projection of a follow-up (e.g. "waiting for technician
  to arrive", "send quote to owner"). A Task can never resolve an
  Operation by itself.
- ``(org_id, idempotency_key)`` is UNIQUE on ``v1_repair_reports``:
  replaying a report can never create a second row.
- **Linked Expense/Payment does NOT close Repair.** The report carries
  an *advisory* ``linked_expense_payment_id`` that downstream code may
  use to record the related expense, but the closure gate is the
  OWNER's verification of a completion claim, NOT the verification of
  any linked expense or rent payment. (AGENTS.md §4 Business Truth
  First: Expense/Payment = "money moved" ≠ Repair = "physical problem
  actually fixed".)
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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.v1.models.base import (
    BigPK,
    TimestampMixin,
    V1Base,
    utcnow,
)


# --- domain vocabulary ------------------------------------------------


class RepairState(StrEnum):
    """9-state repair lifecycle plus CANCELLED.

    State transitions are owned by RepairService; the ORM only constrains
    the legal string set. Any state not in this set is rejected by the
    CHECK constraint at the DB layer.

    REPORTED                  -> tenant/secretary submitted a typed report
    CONFIRMED                 -> OWNER acknowledged the report is real
    AWAITING_TECHNICIAN       -> technician dispatched, waiting to arrive
    QUOTE_REQUESTED           -> asked technician to quote
    QUOTE_RECEIVED            -> technician quote submitted
    QUOTE_APPROVED            -> OWNER approved the quote
    IN_PROGRESS               -> work has started on-site
    COMPLETION_CLAIMED        -> technician says work is done
    COMPLETED                 -> OWNER verified real completion (closure)
    CANCELLED                 -> terminal, not resolved
    """

    REPORTED = "REPORTED"
    CONFIRMED = "CONFIRMED"
    AWAITING_TECHNICIAN = "AWAITING_TECHNICIAN"
    QUOTE_REQUESTED = "QUOTE_REQUESTED"
    QUOTE_RECEIVED = "QUOTE_RECEIVED"
    QUOTE_APPROVED = "QUOTE_APPROVED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETION_CLAIMED = "COMPLETION_CLAIMED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class RepairCategory(StrEnum):
    """What kind of physical problem the report describes."""

    PLUMBING = "PLUMBING"
    ELECTRICAL = "ELECTRICAL"
    APPLIANCE = "APPLIANCE"
    STRUCTURAL = "STRUCTURAL"
    PEST = "PEST"
    HVAC = "HVAC"
    OTHER = "OTHER"


class RepairSeverity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    URGENT = "URGENT"


class RepairTechnicianSource(StrEnum):
    INTERNAL = "INTERNAL"
    EXTERNAL = "EXTERNAL"


class RepairQuoteDecision(StrEnum):
    """Append-only decision log for a quote (sub-cycle of the 9-state)."""

    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class RepairWorkState(StrEnum):
    """Lifecycle of one work event / progress update."""

    STARTED = "STARTED"
    BLOCKED = "BLOCKED"
    PROGRESS = "PROGRESS"
    DONE_ON_SITE = "DONE_ON_SITE"


class RepairVerificationDecision(StrEnum):
    """OWNER-only verification decision log for a completion claim."""

    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    REVERSED = "REVERSED"


class RepairActivityKind(StrEnum):
    """Append-only repair history / activity feed."""

    REPORTED = "REPORTED"
    REPORT_REPLAYED = "REPORT_REPLAYED"
    CONFIRMED = "CONFIRMED"
    TECHNICIAN_ASSIGNED = "TECHNICIAN_ASSIGNED"
    TECHNICIAN_WAITING = "TECHNICIAN_WAITING"
    QUOTE_REQUESTED = "QUOTE_REQUESTED"
    QUOTE_SUBMITTED = "QUOTE_SUBMITTED"
    QUOTE_APPROVED = "QUOTE_APPROVED"
    QUOTE_REJECTED = "QUOTE_REJECTED"
    WORK_STARTED = "WORK_STARTED"
    WORK_PROGRESS = "WORK_PROGRESS"
    WORK_BLOCKED = "WORK_BLOCKED"
    WORK_DONE_ON_SITE = "WORK_DONE_ON_SITE"
    COMPLETION_CLAIMED = "COMPLETION_CLAIMED"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    REVERSED = "REVERSED"
    COMPLETED = "COMPLETED"
    REOPENED = "REOPENED"
    CANCELLED = "CANCELLED"
    FOLLOW_UP_CREATED = "FOLLOW_UP_CREATED"
    FOLLOW_UP_DONE = "FOLLOW_UP_DONE"


REPAIR_STATES = tuple(s.value for s in RepairState)
REPAIR_CATEGORIES = tuple(c.value for c in RepairCategory)
REPAIR_SEVERITIES = tuple(s.value for s in RepairSeverity)
REPAIR_TECHNICIAN_SOURCES = tuple(s.value for s in RepairTechnicianSource)
REPAIR_QUOTE_DECISIONS = tuple(d.value for d in RepairQuoteDecision)
REPAIR_WORK_STATES = tuple(s.value for s in RepairWorkState)
REPAIR_VERIFICATION_DECISIONS = tuple(
    d.value for d in RepairVerificationDecision
)

OPERATION_KIND_REPAIR = "REPAIR_RESOLUTION"
OPERATION_SUBJECT_REPAIR_REPORT = "repair_report"
TASK_KIND_REPAIR_FOLLOW_UP = "REPAIR_FOLLOW_UP"


# --- tables -----------------------------------------------------------


class RepairReport(V1Base, TimestampMixin):
    """A typed problem report. State machine is owned by RepairService.

    ``idempotency_key`` is opaque, case-preserving. ``linked_expense_payment_id``
    is an *advisory* pointer to a related expense/rent row (e.g. the rent
    period that overlapped the repair, or an expense that paid for the
    repair). It NEVER closes or uncloses the report; closure is gated
    exclusively by the OWNER's verification of a completion claim.
    """

    __tablename__ = "v1_repair_reports"
    __table_args__ = (
        CheckConstraint(
            "state IN ("
            "'REPORTED','CONFIRMED','AWAITING_TECHNICIAN','QUOTE_REQUESTED',"
            "'QUOTE_RECEIVED','QUOTE_APPROVED','IN_PROGRESS',"
            "'COMPLETION_CLAIMED','COMPLETED','CANCELLED'"
            ")",
            name="ck_v1_repair_reports_state",
        ),
        CheckConstraint(
            "category IN ("
            "'PLUMBING','ELECTRICAL','APPLIANCE','STRUCTURAL',"
            "'PEST','HVAC','OTHER'"
            ")",
            name="ck_v1_repair_reports_category",
        ),
        CheckConstraint(
            "severity IN ('LOW','MEDIUM','HIGH','URGENT')",
            name="ck_v1_repair_reports_severity",
        ),
        CheckConstraint(
            "linked_expense_payment_id IS NULL OR linked_expense_payment_id > 0",
            name="ck_v1_repair_reports_linked_expense_positive",
        ),
        UniqueConstraint(
            "org_id",
            "idempotency_key",
            name="uq_v1_repair_reports_org_idempotency_key",
        ),
        Index("ix_v1_repair_reports_org_id", "org_id"),
        Index(
            "ix_v1_repair_reports_org_state", "org_id", "state",
        ),
        Index(
            "ix_v1_repair_reports_unit_id", "unit_id",
        ),
    )

    id: Mapped[BigPK]
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    unit_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_units.id", ondelete="RESTRICT"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(String(2000), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(
        String(24), nullable=False, default=RepairState.REPORTED.value,
    )
    reported_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    reported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    technician_name: Mapped[str | None] = mapped_column(
        String(200), nullable=True,
    )
    technician_source: Mapped[str | None] = mapped_column(
        String(16), nullable=True,
    )
    technician_eta_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    quoted_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2), nullable=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Advisory pointer; NEVER closes the report (see module docstring).
    linked_expense_payment_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )


class RepairQuote(V1Base, TimestampMixin):
    """Technician quote attached to a report. Append-only decision log."""

    __tablename__ = "v1_repair_quotes"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('SUBMITTED','APPROVED','REJECTED')",
            name="ck_v1_repair_quotes_decision",
        ),
        CheckConstraint(
            "amount > 0", name="ck_v1_repair_quotes_amount_positive",
        ),
        UniqueConstraint(
            "org_id",
            "idempotency_key",
            name="uq_v1_repair_quotes_org_idempotency_key",
        ),
        Index("ix_v1_repair_quotes_org_id", "org_id"),
        Index(
            "ix_v1_repair_quotes_report_id", "report_id",
        ),
    )

    id: Mapped[BigPK]
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    report_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_repair_reports.id", ondelete="RESTRICT"),
        nullable=False,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    description: Mapped[str] = mapped_column(String(2000), nullable=False)
    decision: Mapped[str] = mapped_column(
        String(16), nullable=False, default=RepairQuoteDecision.SUBMITTED.value,
    )
    technician_name: Mapped[str] = mapped_column(String(200), nullable=False)
    submitted_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    decided_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class RepairWork(V1Base, TimestampMixin):
    """Work progress log. Append-only."""

    __tablename__ = "v1_repair_works"
    __table_args__ = (
        CheckConstraint(
            "state IN ('STARTED','BLOCKED','PROGRESS','DONE_ON_SITE')",
            name="ck_v1_repair_works_state",
        ),
        Index("ix_v1_repair_works_org_id", "org_id"),
        Index(
            "ix_v1_repair_works_report_id", "report_id",
        ),
    )

    id: Mapped[BigPK]
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    report_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_repair_reports.id", ondelete="RESTRICT"),
        nullable=False,
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    note: Mapped[str] = mapped_column(String(2000), nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class RepairCompletionClaim(V1Base, TimestampMixin):
    """Technician / secretary says the on-site work is done.

    This is NOT closure. Closure is the OWNER's verification of this
    claim. (Business Truth First: the problem is only solved when the
    OWNER confirms the work actually happened, not when someone says
    it did.)
    """

    __tablename__ = "v1_repair_completion_claims"
    __table_args__ = (
        Index("ix_v1_repair_completion_claims_org_id", "org_id"),
        Index(
            "ix_v1_repair_completion_claims_report_id", "report_id",
        ),
    )

    id: Mapped[BigPK]
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    report_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_repair_reports.id", ondelete="RESTRICT"),
        nullable=False,
    )
    summary: Mapped[str] = mapped_column(String(2000), nullable=False)
    claimed_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class RepairVerification(V1Base, TimestampMixin):
    """Append-only OWNER verification decision log.

    A VERIFIED row may be superseded by a later REVERSED row. The original
    VERIFIED row is preserved in the audit log, but its contribution to
    the closure gate is removed once the reversal is recorded. This keeps
    the table fully append-only (no UPDATE on existing rows).
    """

    __tablename__ = "v1_repair_verifications"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('VERIFIED','REJECTED','REVERSED')",
            name="ck_v1_repair_verifications_decision",
        ),
        Index("ix_v1_repair_verifications_org_id", "org_id"),
        Index(
            "ix_v1_repair_verifications_report_id", "report_id",
        ),
        Index(
            "ix_v1_repair_verifications_reversed_by",
            "reversed_by_verification_id",
        ),
    )

    id: Mapped[BigPK]
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    report_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_repair_reports.id", ondelete="RESTRICT"),
        nullable=False,
    )
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    verifier_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    reversed_by_verification_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_repair_verifications.id", ondelete="RESTRICT"),
        nullable=True,
    )


class RepairActivity(V1Base, TimestampMixin):
    """Append-only repair history / activity feed."""

    __tablename__ = "v1_repair_activities"
    __table_args__ = (
        Index("ix_v1_repair_activities_org_id", "org_id"),
        Index(
            "ix_v1_repair_activities_report_id", "report_id",
        ),
    )

    id: Mapped[BigPK]
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    report_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_repair_reports.id", ondelete="RESTRICT"),
        nullable=True,
    )
    quote_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_repair_quotes.id", ondelete="RESTRICT"),
        nullable=True,
    )
    work_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_repair_works.id", ondelete="RESTRICT"),
        nullable=True,
    )
    claim_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_repair_completion_claims.id", ondelete="RESTRICT"),
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
    "OPERATION_KIND_REPAIR",
    "OPERATION_SUBJECT_REPAIR_REPORT",
    "REPAIR_CATEGORIES",
    "REPAIR_QUOTE_DECISIONS",
    "REPAIR_SEVERITIES",
    "REPAIR_STATES",
    "REPAIR_TECHNICIAN_SOURCES",
    "REPAIR_VERIFICATION_DECISIONS",
    "REPAIR_WORK_STATES",
    "RepairActivity",
    "RepairActivityKind",
    "RepairCategory",
    "RepairCompletionClaim",
    "RepairQuote",
    "RepairQuoteDecision",
    "RepairReport",
    "RepairSeverity",
    "RepairState",
    "RepairTechnicianSource",
    "RepairVerification",
    "RepairVerificationDecision",
    "RepairWork",
    "RepairWorkState",
    "TASK_KIND_REPAIR_FOLLOW_UP",
]
