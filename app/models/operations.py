"""V1.2 PROACTIVE OPERATIONS models.

Three new tables (semantically separate from the V1.1 ``tasks`` table):
- ``operational_tasks``: business-event driven reminders with a DB-level
  partial unique dedupe index (one active task per dedupe_key).
- ``recurring_rules``: rule-driven task generation (monthly/quarterly/...).
- ``notification_outbox``: at-least-once outbox for the notifier worker.
"""
from datetime import datetime
from enum import Enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import AuditMixin, Base, SoftDeleteMixin, pg_enum


class OperationalTaskType(str, Enum):
    RENT_DUE = "RENT_DUE"
    RENT_OVERDUE = "RENT_OVERDUE"
    LEASE_EXPIRING = "LEASE_EXPIRING"
    PROPERTY_FEE_DUE = "PROPERTY_FEE_DUE"
    AC_MAINTENANCE = "AC_MAINTENANCE"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    PAYMENT_PENDING = "PAYMENT_PENDING"
    SETTLEMENT_PENDING = "SETTLEMENT_PENDING"
    # V1.2.2 Phase C2: human-confirmed copilot follow-up tasks (text/tracking
    # only — never a financial mutation).
    FOLLOWUP = "FOLLOWUP"
    MOVE_OUT_INSPECTION = "MOVE_OUT_INSPECTION"
    DEPOSIT_SETTLEMENT = "DEPOSIT_SETTLEMENT"


class OperationalTaskStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class OperationalTaskPriority(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class OperationalTask(AuditMixin, Base):
    """One actionable reminder derived from a business source record."""

    __tablename__ = "operational_tasks"
    __table_args__ = (
        # DB-level dedupe boundary: at most one ACTIVE (PENDING) task per
        # dedupe_key. NULL dedupe_key (manual tasks) is skipped by the
        # partial index. Generation uses INSERT ... ON CONFLICT DO NOTHING.
        Index(
            "uq_operational_tasks_active_dedupe",
            "dedupe_key",
            unique=True,
            postgresql_where=text("status = 'PENDING'"),
        ),
        Index("ix_operational_tasks_task_type_status", "task_type", "status"),
        Index("ix_operational_tasks_due_at", "due_at"),
        Index("ix_operational_tasks_status_due_at", "status", "due_at"),
        Index("ix_operational_tasks_assigned_status", "assigned_user_id", "status"),
        CheckConstraint(
            "task_type IN ('RENT_DUE','RENT_OVERDUE','LEASE_EXPIRING',"
            "'PROPERTY_FEE_DUE','AC_MAINTENANCE','APPROVAL_PENDING',"
            "'PAYMENT_PENDING','SETTLEMENT_PENDING','FOLLOWUP',"
            "'MOVE_OUT_INSPECTION','DEPOSIT_SETTLEMENT')",
            name="ck_operational_tasks_task_type",
        ),
        CheckConstraint(
            "status IN ('PENDING','IN_PROGRESS','COMPLETED','CANCELLED')",
            name="ck_operational_tasks_status",
        ),
        CheckConstraint(
            "priority IN ('low','medium','high','critical')",
            name="ck_operational_tasks_priority",
        ),
    )

    task_type: Mapped[OperationalTaskType] = mapped_column(
        pg_enum(OperationalTaskType, "operational_task_type"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    property_id: Mapped[int | None] = mapped_column(
        ForeignKey("properties.id"), nullable=True, index=True
    )
    tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenants.id"), nullable=True, index=True
    )
    lease_id: Mapped[int | None] = mapped_column(
        ForeignKey("leases.id"), nullable=True, index=True
    )
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    assigned_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    priority: Mapped[OperationalTaskPriority] = mapped_column(
        pg_enum(OperationalTaskPriority, "operational_task_priority", length=20),
        nullable=False,
        default=OperationalTaskPriority.medium,
    )
    status: Mapped[OperationalTaskStatus] = mapped_column(
        pg_enum(OperationalTaskStatus, "operational_task_status"),
        nullable=False,
        default=OperationalTaskStatus.PENDING,
    )
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    remind_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # V1.2.2 A+B.1: bumped on every snooze / complete / cancel so the snooze
    # redelivery logical identity is (task, generation, window) — a DROPPED
    # old-generation reminder can never block a new-generation enqueue.
    reminder_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # PASAY-V2-FOUNDATION-001: V2 task core fields (next action / next check /
    # context / completion condition / source event). All nullable so existing
    # scheduler-generated tasks and row insert paths remain back-compatible.
    next_action: Mapped[str | None] = mapped_column(String(300), nullable=True)
    next_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    context: Mapped[str | None] = mapped_column(Text, nullable=True)
    completion_condition: Mapped[str | None] = mapped_column(String(300), nullable=True)
    source_event: Mapped[str | None] = mapped_column(String(500), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Named `details` (not `metadata`) because `metadata` is reserved by the
    # SQLAlchemy Declarative API.
    details: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)


class Recurrence(str, Enum):
    monthly = "monthly"
    quarterly = "quarterly"
    yearly = "yearly"
    fixed_interval = "fixed_interval"


class RecurringRule(AuditMixin, SoftDeleteMixin, Base):
    """A rule that periodically generates an operational_task."""

    __tablename__ = "recurring_rules"
    __table_args__ = (
        Index("ix_recurring_rules_enabled_next_run", "enabled", "next_run_at"),
        CheckConstraint(
            "rule_type IN ('RENT_DUE','RENT_OVERDUE','LEASE_EXPIRING',"
            "'PROPERTY_FEE_DUE','AC_MAINTENANCE','APPROVAL_PENDING',"
            "'PAYMENT_PENDING','SETTLEMENT_PENDING','FOLLOWUP',"
            "'MOVE_OUT_INSPECTION','DEPOSIT_SETTLEMENT')",
            name="ck_recurring_rules_rule_type",
        ),
        CheckConstraint(
            "recurrence IN ('monthly','quarterly','yearly','fixed_interval')",
            name="ck_recurring_rules_recurrence",
        ),
    )

    rule_type: Mapped[OperationalTaskType] = mapped_column(
        pg_enum(OperationalTaskType, "recurring_rule_type"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    property_id: Mapped[int | None] = mapped_column(
        ForeignKey("properties.id"), nullable=True, index=True
    )
    recurrence: Mapped[Recurrence] = mapped_column(
        pg_enum(Recurrence, "recurrence", length=20), nullable=False
    )
    interval_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    assigned_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    # Named `details` (not `metadata`) because `metadata` is reserved by the
    # SQLAlchemy Declarative API.
    details: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)


class ReminderDailyDedup(AuditMixin, Base):
    """Persistent same-day reminder dedupe (TELEGRAM-OPS-UX-CONVERGENCE-003 §1.4).

    One row per ``(business key, recipient, PH local date, reminder type)``:
    the unique ``dedupe_key`` index makes the enqueue atomic
    (INSERT ... ON CONFLICT DO NOTHING), survives runtime restarts and is safe
    under concurrent workers — the DB is the only source of truth, never
    Python memory.

    The business key is the task's ``dedupe_key`` (e.g. ``lease:3:RENT_DUE:
    2026-08``) — the STABLE business identity — so even a task row that is
    re-created by a later scheduler pass cannot re-send within the same
    Philippines natural day.
    """

    __tablename__ = "reminder_daily_dedup"
    __table_args__ = (
        Index("uq_reminder_daily_dedup_key", "dedupe_key", unique=True),
        Index("ix_reminder_daily_dedup_date", "local_date"),
    )

    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("operational_tasks.id"), nullable=True, index=True
    )
    recipient: Mapped[str] = mapped_column(String(200), nullable=False)
    # Philippines operational local date "YYYY-MM-DD" (UTC+8, no DST).
    local_date: Mapped[str] = mapped_column(String(10), nullable=False)
    reminder_type: Mapped[str] = mapped_column(String(50), nullable=False)


class NotificationStatus(str, Enum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    DROPPED = "DROPPED"


class NotificationOutbox(AuditMixin, Base):
    """At-least-once outbox: claimed by the notifier with SKIP LOCKED."""

    __tablename__ = "notification_outbox"
    __table_args__ = (
        Index("uq_notification_outbox_dedupe", "dedupe_key", unique=True),
        Index("ix_notification_outbox_status_next_attempt", "status", "next_attempt_at"),
        CheckConstraint(
            "status IN ('PENDING','SENT','FAILED','DROPPED')",
            name="ck_notification_outbox_status",
        ),
    )

    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("operational_tasks.id"), nullable=True, index=True
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    recipient: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[NotificationStatus] = mapped_column(
        pg_enum(NotificationStatus, "notification_status"),
        nullable=False,
        default=NotificationStatus.PENDING,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # V1.2.2 A+B.1: durable claim marker for the notifier's two-phase
    # claim -> send -> finalize flow (reclaimed after the lease expires).
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Optional Telegram message id returned by sendMessage; lets the bot edit
    # the same message later (editMessageText) instead of spamming new ones.
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
