"""REPAIR-AI-EMPLOYEE-WORKFLOW-008A — AI Employee Repair Operation model.

The Repair flow is promoted to a FIRST-CLASS Operation: a real-world problem
(``repair_operations``) with one business status, plus decoupled versioned
solution candidates (``repair_proposals``) and an idempotent action stream
(``repair_actions``) that drives AI continuation.

Object semantics (task 008A core):
- Repair Operation == the REAL problem. CLOSED means the problem was verified
  resolved in the real world — NEVER just because a proposal was made,
  approved, an expense was paid, a reminder sent, or a vendor contacted.
- Proposal == one solution candidate for the repair. Rejecting a proposal
  does NOT reject the repair; it only marks that candidate REJECTED and the
  AI creates the next action (e.g. requote).
- Expense == a financial record. Expense PAID does NOT close the repair.
- Verification == human/evidence confirmation the problem is actually fixed;
  it is the ONLY path into CLOSED.
- Action == a discrete step (requote, contact vendor, verify, ...). Its
  creation is idempotent via ``(repair_id, dedupe_key, active status)``.

The existing AC_MAINTENANCE ``operational_tasks`` row remains the operational /
notification carrier for back-compat (linked via ``operational_task_id``), but
the operational_tasks table is NOT the source of repair truth anymore.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import AuditMixin, Base, pg_enum

# ---------------------------------------------------------------------------
# Repair Operation status (business state machine).
# ---------------------------------------------------------------------------


class RepairOperationStatus(str, Enum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    WAITING_HUMAN = "WAITING_HUMAN"
    WAITING_VENDOR = "WAITING_VENDOR"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    WAITING_PAYMENT = "WAITING_PAYMENT"
    VERIFYING = "VERIFYING"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class RepairProposalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


class RepairActionStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class RepairOperation(AuditMixin, Base):
    """The real-world repair problem (AI Employee Operation)."""

    __tablename__ = "repair_operations"
    __table_args__ = (
        Index("ix_repair_operations_status", "status"),
        Index("ix_repair_operations_property_id", "property_id"),
        Index("ix_repair_operations_unit_id", "unit_id"),
        Index(
            "ix_repair_operations_assignee_status",
            "assignee_user_id",
            "status",
        ),
        CheckConstraint(
            "status IN ('OPEN','IN_PROGRESS','WAITING_HUMAN','WAITING_VENDOR',"
            "'WAITING_APPROVAL','WAITING_PAYMENT','VERIFYING','CLOSED','CANCELLED')",
            name="ck_repair_operations_status",
        ),
    )

    # --- problem identity ---------------------------------------------------
    merchant_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    property_id: Mapped[int | None] = mapped_column(
        ForeignKey("properties.id"), nullable=True
    )
    unit_id: Mapped[int | None] = mapped_column(
        ForeignKey("units.id"), nullable=True
    )
    issue: Mapped[str] = mapped_column(String(200), nullable=False)
    issue_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_source: Mapped[str] = mapped_column(
        String(50), nullable=False, default="manual"
    )
    reported_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # --- responsible human + derived AI employee state ---------------------
    assignee_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    status: Mapped[RepairOperationStatus] = mapped_column(
        pg_enum(RepairOperationStatus, "repair_operation_status"),
        nullable=False,
        default=RepairOperationStatus.OPEN,
    )
    # AI Employee derived state: what happens next + who blocks it.
    next_action: Mapped[str | None] = mapped_column(String(400), nullable=True)
    waiting_on: Mapped[str | None] = mapped_column(String(50), nullable=True)
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- closure / verification gate ---------------------------------------
    closure_criteria: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verification_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- evidence + related records ----------------------------------------
    # Media/evidence ids (Evidence index entity_type='repair', entity_id=id).
    evidence: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    details: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    # Back-compat bridge to the existing AC_MAINTENANCE operational task.
    operational_task_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )


class RepairProposal(AuditMixin, Base):
    """One versioned solution candidate for a repair (decoupled from the
    Repair Operation: rejecting a proposal never rejects the repair)."""

    __tablename__ = "repair_proposals"
    __table_args__ = (
        Index("uq_repair_proposals_repair_version", "repair_id", "version", unique=True),
        Index("ix_repair_proposals_repair_id", "repair_id"),
        CheckConstraint(
            "status IN ('PENDING','APPROVED','REJECTED','SUPERSEDED')",
            name="ck_repair_proposals_status",
        ),
    )

    repair_id: Mapped[int] = mapped_column(
        ForeignKey("repair_operations.id"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    vendor: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source: Mapped[str | None] = mapped_column(String(50), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    submitted_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[RepairProposalStatus] = mapped_column(
        pg_enum(RepairProposalStatus, "repair_proposal_status"),
        nullable=False,
        default=RepairProposalStatus.PENDING,
    )
    decision_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    decision_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # When a proposal becomes an Expense (approved quote -> spend), link the
    # created expense so the Expense is NEVER conflated with the Repair.
    expense_id: Mapped[int | None] = mapped_column(
        ForeignKey("expenses.id"), nullable=True, index=True
    )


class RepairAction(AuditMixin, Base):
    """One idempotent AI-employee action that advances a repair.

    Every action carries a deterministic ``dedupe_key`` scoped to the repair.
    The DB partial unique index on ``(repair_id, dedupe_key) WHERE status IN
    ('PENDING','IN_PROGRESS')`` makes repeated worker ticks / retries /
    page refreshes unable to create more than one ACTIVE action for the same
    logical step (008A §4 requote dedup).
    """

    __tablename__ = "repair_actions"
    __table_args__ = (
        Index(
            "uq_repair_actions_active_dedupe",
            "repair_id",
            "dedupe_key",
            unique=True,
            postgresql_where=text("status IN ('PENDING','IN_PROGRESS')"),
        ),
        Index("ix_repair_actions_repair_id", "repair_id"),
        Index("ix_repair_actions_assignee_status", "assigned_user_id", "status"),
        CheckConstraint(
            "status IN ('PENDING','IN_PROGRESS','COMPLETED','CANCELLED')",
            name="ck_repair_actions_status",
        ),
    )

    repair_id: Mapped[int] = mapped_column(
        ForeignKey("repair_operations.id"), nullable=False
    )
    action_kind: Mapped[str] = mapped_column(String(50), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[RepairActionStatus] = mapped_column(
        pg_enum(RepairActionStatus, "repair_action_status"),
        nullable=False,
        default=RepairActionStatus.PENDING,
    )
    assigned_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    # The business event that created this action (for provenance / history).
    source_event: Mapped[str | None] = mapped_column(String(160), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Links to the negotiation point (e.g. the rejected proposal that caused it).
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


_REPAIR_OPERATION_CHECK_FIELDS = (
    "status", "next_action", "waiting_on", "blocked_reason",
    "closure_criteria", "verified_by", "verified_at", "verification_result",
    "closed_at", "closure_reason",
)
_REPAIR_PROPOSAL_CHECK_FIELDS = (
    "vendor", "source", "description", "amount", "status",
    "rejection_reason", "decision_by", "decision_at", "expense_id",
)
_REPAIR_ACTION_CHECK_FIELDS = (
    "action_kind", "title", "description", "status", "dedupe_key",
    "source_event", "resolved_at", "resolved_by",
)
