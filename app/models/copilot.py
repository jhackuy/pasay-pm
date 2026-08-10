"""V1.2.2 OPS COPILOT (Phase A+B) models.

Two tables — kept minimal, no execution wiring:
- ``copilot_runs``: one row per context build (audit log of what the Copilot
  was shown; the DB is the source of truth).
- ``copilot_action_proposals``: user-confirmed action intents. Nothing in
  Phase A+B may transition a proposal to EXECUTED or set ``executed_at``
  (that is Phase C). Financial mutation is structurally prevented at the
  service layer (action_type allowlist + target guard) and by the DB CHECKs.
"""
from datetime import datetime
from enum import Enum

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import AuditMixin, Base, pg_enum

# DB-level action/target allowlists (kept in sync with the service-layer
# constants in app/services/operations/copilot.py).
COPILOT_ACTION_TYPES = (
    "summarize", "analyze", "explain", "risk_scan",
    "create_task", "assign_task", "snooze_task", "follow_up",
)
COPILOT_TARGET_TYPES = ("property", "lease", "task", "expense", "income", "settlement")


class CopilotRunStatus(str, Enum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class CopilotActionStatus(str, Enum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    EXECUTED = "EXECUTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class CopilotRun(AuditMixin, Base):
    """One logged context build (audit): who asked, when, and what snapshot."""

    __tablename__ = "copilot_runs"
    __table_args__ = (
        Index("ix_copilot_runs_actor_intent", "actor_user_id", "intent"),
        CheckConstraint(
            "status IN ('COMPLETED','FAILED')",
            name="ck_copilot_runs_status",
        ),
    )

    actor_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    intent: Mapped[str] = mapped_column(String(100), nullable=False)
    context_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[CopilotRunStatus] = mapped_column(
        pg_enum(CopilotRunStatus, "copilot_run_status"), nullable=False
    )


class CopilotActionProposal(AuditMixin, Base):
    """A proposed Copilot action awaiting explicit user confirmation.

    State machine (app-layer conditional UPDATE + tests):
    PENDING -> CONFIRMED / CANCELLED / EXPIRED; CONFIRMED -> EXECUTED is
    reserved for Phase C and unreachable in Phase A+B.
    """

    __tablename__ = "copilot_action_proposals"
    __table_args__ = (
        # DB-level dedupe, scoped per actor: the same idempotency key used by
        # two different actors is two independent requests (V1.2.2 A+B.1).
        Index(
            "uq_copilot_action_proposals_actor_idempotency",
            "actor_user_id",
            "idempotency_key",
            unique=True,
        ),
        Index(
            "ix_copilot_action_proposals_actor_status",
            "actor_user_id",
            "status",
        ),
        CheckConstraint(
            "action_type IN ('summarize','analyze','explain','risk_scan',"
            "'create_task','assign_task','snooze_task','follow_up')",
            name="ck_copilot_action_proposals_action_type",
        ),
        CheckConstraint(
            "target_type IN ('property','lease','task','expense','income','settlement')",
            name="ck_copilot_action_proposals_target_type",
        ),
        CheckConstraint(
            "status IN ('PENDING','CONFIRMED','EXECUTED','CANCELLED','EXPIRED')",
            name="ck_copilot_action_proposals_status",
        ),
    )

    actor_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    action_type: Mapped[str] = mapped_column(String(50), nullable=False)
    target_type: Mapped[str] = mapped_column(String(50), nullable=False)
    target_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[CopilotActionStatus] = mapped_column(
        pg_enum(CopilotActionStatus, "copilot_action_status"),
        nullable=False,
        default=CopilotActionStatus.PENDING,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # NEVER set by any Phase A+B code path (execution is Phase C).
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
