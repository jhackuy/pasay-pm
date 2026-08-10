from datetime import date, datetime

from pydantic import BaseModel, Field

from app.models import TaskPriority, TaskStatus
from app.schemas.common import AuditFields


class TaskBase(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    unit_id: int | None = None
    status: TaskStatus = TaskStatus.open
    priority: TaskPriority = TaskPriority.medium
    due_date: date | None = None
    recurring: bool = False
    interval_months: int | None = Field(default=None, ge=1)
    assigned_to: int | None = None


class TaskCreate(TaskBase):
    pass


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    unit_id: int | None = None
    status: TaskStatus | None = None
    priority: TaskPriority | None = None
    due_date: date | None = None
    recurring: bool | None = None
    interval_months: int | None = Field(default=None, ge=1)
    assigned_to: int | None = None


class TaskRead(TaskBase, AuditFields):
    id: int
    completed_at: datetime | None = None
    last_completed_at: datetime | None = None
    next_due_date: date | None = None


class TaskCompleteResult(BaseModel):
    completed: TaskRead
    next: TaskRead | None = None
