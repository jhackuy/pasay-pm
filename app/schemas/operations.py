"""Pydantic schemas for the /operations router (V1.2 + V2 Foundation)."""
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.operations import (
    OperationalTaskPriority,
    OperationalTaskStatus,
    OperationalTaskType,
    Recurrence,
)
from app.schemas.common import AuditFields


class OperationalTaskRead(AuditFields):
    id: int
    task_type: OperationalTaskType
    title: str
    description: str | None = None
    property_id: int | None = None
    # PASAY-V2-FOUNDATION-001: derived display unit code ("1680", or
    # "BAY-1680" / "SOL-1680" only when duplicate unit codes exist).
    property_code: str | None = None
    tenant_id: int | None = None
    lease_id: int | None = None
    source_type: str
    source_id: int | None = None
    source_event: str | None = None
    assigned_user_id: int | None = None
    priority: OperationalTaskPriority
    status: OperationalTaskStatus
    due_at: datetime
    remind_at: datetime | None = None
    snoozed_until: datetime | None = None
    next_action: str | None = None
    next_check_at: datetime | None = None
    context: str | None = None
    completion_condition: str | None = None
    completed_at: datetime | None = None
    completed_by: int | None = None
    dedupe_key: str | None = None
    details: dict | None = None


class TaskSnoozeIn(BaseModel):
    until: datetime | None = None
    preset: str | None = Field(default=None, max_length=32)


class TaskUpdateIn(BaseModel):
    """PASAY-V2-FOUNDATION-001: partial V2 task update (conversation-driven)."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    status: OperationalTaskStatus | None = None
    due_at: datetime | None = None
    next_action: str | None = Field(default=None, max_length=300)
    next_check_at: datetime | None = None
    context: str | None = None
    completion_condition: str | None = Field(default=None, max_length=300)
    # AI-OPS-FOUNDATION-001 §8: structured promise/follow-up state (merged
    # into the task's JSONB details — e.g. {"promise": {...}}).
    details: dict | None = None


class TaskCreateIn(BaseModel):
    """PASAY-V2-FOUNDATION-001: create a task from a conversation event."""

    task_type: OperationalTaskType
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    property_id: int | None = None
    priority: OperationalTaskPriority = OperationalTaskPriority.medium
    status: OperationalTaskStatus | None = None
    due_at: datetime | None = None
    next_action: str | None = Field(default=None, max_length=300)
    next_check_at: datetime | None = None
    context: str | None = None
    completion_condition: str | None = Field(default=None, max_length=300)
    source_event: str | None = Field(default=None, max_length=500)
    assigned_user_id: int | None = None
    dedupe_key: str | None = Field(default=None, max_length=255)


class TaskActionOut(BaseModel):
    task: OperationalTaskRead
    detail: str


class OperationsSummary(BaseModel):
    overdue: int = 0
    due_today: int = 0
    due_7_days: int = 0
    pending_total: int = 0


class RecurringRuleBase(BaseModel):
    rule_type: OperationalTaskType
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    property_id: int | None = None
    recurrence: Recurrence
    interval_months: int | None = Field(default=None, ge=1)
    next_run_at: datetime | None = None
    enabled: bool = True
    assigned_user_id: int | None = None
    details: dict | None = None


class RecurringRuleCreate(RecurringRuleBase):
    pass


class RecurringRuleUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    property_id: int | None = None
    recurrence: Recurrence | None = None
    interval_months: int | None = Field(default=None, ge=1)
    next_run_at: datetime | None = None
    enabled: bool | None = None
    assigned_user_id: int | None = None
    details: dict | None = None


class RecurringRuleRead(AuditFields):
    id: int
    rule_type: OperationalTaskType
    title: str
    description: str | None = None
    property_id: int | None = None
    recurrence: Recurrence
    interval_months: int | None = None
    next_run_at: datetime
    enabled: bool
    assigned_user_id: int | None = None
    details: dict | None = None


class SchedulerRunResult(BaseModel):
    tasks_created: int
    notifications_enqueued: int
    rules_claimed: int
    rules_advanced: int
    reconciled_completed: int
    reconciled_cancelled: int
    snooze_redelivered: int = 0
    # AI-OPS-FOUNDATION-001 §8: promise follow-up pass outcome.
    promises_escalated: int = 0
    promises_reminded: int = 0
    # AI-OPS-FOUNDATION-001 §19: deterministic exception findings this pass.
    exceptions_found: int = 0
