"""V1.2 PROACTIVE OPERATIONS API router (/api/v1/operations).

RBAC (re-checked on EVERY request, including task transitions):
- admin: everything
- manager: view + process operational tasks, manage recurring rules
- agent: only view / process tasks assigned to themselves (403 otherwise)

Task handlers NEVER write incomes/expenses/commission settlements — the
V1.1 financial state machine is the only writer for those tables.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, manager_or_admin
from app.database import get_db
from app.models.operations import (
    OperationalTask,
    OperationalTaskStatus,
    OperationalTaskType,
    RecurringRule,
)
from app.models.user import User, UserRole
from app.schemas.operations import (
    OperationsSummary,
    OperationalTaskRead,
    RecurringRuleCreate,
    RecurringRuleRead,
    RecurringRuleUpdate,
    SchedulerRunResult,
    TaskActionOut,
    TaskSnoozeIn,
)
from app.services.audit import record_audit, serialize_row
from app.services.operations.scheduler import run_scheduler_once

router = APIRouter(prefix="/operations", tags=["operations"])

SNOOZE_PRESETS = {"1h", "today_afternoon", "tomorrow_morning", "3d"}


def _agent_scope(query, user: User):
    """Agents only ever see their own assigned tasks."""
    if user.role == UserRole.agent:
        return query.filter(OperationalTask.assigned_user_id == user.id)
    return query


def _get_task_or_404(db: Session, task_id: int) -> OperationalTask:
    task = db.query(OperationalTask).filter(OperationalTask.id == task_id).first()
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Operational task not found")
    return task


def _require_access(task: OperationalTask, user: User) -> None:
    if user.role == UserRole.agent and task.assigned_user_id != user.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Cannot access a task assigned to another user"
        )


def _check_assignee(db: Session, user_id: int | None) -> None:
    if user_id is None:
        return
    user = db.query(User).filter(User.id == user_id, User.is_active.is_(True)).first()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Assigned user not found")


def _check_property(db: Session, property_id: int | None) -> None:
    if property_id is None:
        return
    from app.models.property import Property
    prop = db.query(Property).filter(Property.id == property_id).first()
    if prop is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Property not found")


def _resolve_snooze_until(payload: TaskSnoozeIn, now: datetime) -> datetime:
    if payload.until is not None:
        if payload.until <= now:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "Snooze until must be in the future"
            )
        return payload.until
    if payload.preset is None or payload.preset not in SNOOZE_PRESETS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"preset must be one of {sorted(SNOOZE_PRESETS)} or provide until",
        )
    if payload.preset == "1h":
        return now + timedelta(hours=1)
    if payload.preset == "today_afternoon":
        target = now.replace(hour=17, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target
    if payload.preset == "tomorrow_morning":
        target = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        return target
    return now + timedelta(days=3)  # "3d"


def _replay_or_conflict(db: Session, task_id: int, want_status, conflict_detail: str):
    current = _get_task_or_404(db, task_id)
    if current.status == want_status:
        return current
    raise HTTPException(status.HTTP_409_CONFLICT, conflict_detail)


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------

@router.get("/tasks", response_model=list[OperationalTaskRead])
def list_tasks(
    property_id: int | None = Query(default=None),
    assignee: int | None = Query(default=None),
    task_type: OperationalTaskType | None = Query(default=None),
    status: OperationalTaskStatus | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = _agent_scope(db.query(OperationalTask), user)
    if user.role != UserRole.agent and assignee is not None:
        query = query.filter(OperationalTask.assigned_user_id == assignee)
    if property_id is not None:
        query = query.filter(OperationalTask.property_id == property_id)
    if task_type is not None:
        query = query.filter(OperationalTask.task_type == task_type)
    if status is not None:
        query = query.filter(OperationalTask.status == status)
    return query.order_by(OperationalTask.due_at, OperationalTask.id).all()


@router.get("/tasks/{task_id}", response_model=OperationalTaskRead)
def get_task(
    task_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    task = _get_task_or_404(db, task_id)
    _require_access(task, user)
    return task


@router.post("/tasks/{task_id}/complete", response_model=TaskActionOut)
def complete_task(
    task_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    task = _get_task_or_404(db, task_id)
    _require_access(task, user)
    now = datetime.now(timezone.utc)
    old = serialize_row(task)
    result = db.execute(
        update(OperationalTask)
        .where(OperationalTask.id == task_id, OperationalTask.status == OperationalTaskStatus.PENDING)
        .values(
            status=OperationalTaskStatus.COMPLETED,
            completed_at=now,
            completed_by=user.id,
            updated_by=user.id,
            updated_at=now,
        ),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount == 1:
        db.refresh(task)
        record_audit(
            db,
            table_name="operational_tasks",
            record_id=task.id,
            action="task_completed",
            actor_id=user.id,
            changed_fields={"status": ["PENDING", "COMPLETED"]},
            old_value=old,
            new_value=serialize_row(task),
        )
        db.commit()
        db.refresh(task)
        return TaskActionOut(task=task, detail="Task completed")
    db.rollback()
    current = _replay_or_conflict(
        db, task_id, OperationalTaskStatus.COMPLETED, "Cannot complete a cancelled task"
    )
    return TaskActionOut(task=current, detail="Task already completed")


@router.post("/tasks/{task_id}/snooze", response_model=TaskActionOut)
def snooze_task(
    task_id: int,
    payload: TaskSnoozeIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    task = _get_task_or_404(db, task_id)
    _require_access(task, user)
    now = datetime.now(timezone.utc)
    until = _resolve_snooze_until(payload, now)
    old = serialize_row(task)
    result = db.execute(
        update(OperationalTask)
        .where(OperationalTask.id == task_id, OperationalTask.status == OperationalTaskStatus.PENDING)
        .values(
            snoozed_until=until,
            updated_by=user.id,
            updated_at=now,
        ),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount == 1:
        db.refresh(task)
        record_audit(
            db,
            table_name="operational_tasks",
            record_id=task.id,
            action="task_snoozed",
            actor_id=user.id,
            changed_fields={"snoozed_until": [None, until.isoformat()]},
            old_value=old,
            new_value=serialize_row(task),
        )
        db.commit()
        db.refresh(task)
        return TaskActionOut(task=task, detail="Task snoozed")
    db.rollback()
    current = _get_task_or_404(db, task_id)
    if current.status != OperationalTaskStatus.PENDING:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only pending tasks can be snoozed")
    if current.snoozed_until == until:
        return TaskActionOut(task=current, detail="Task already snoozed")
    raise HTTPException(status.HTTP_409_CONFLICT, "Task was changed; refresh and retry")


@router.post("/tasks/{task_id}/cancel", response_model=TaskActionOut)
def cancel_task(
    task_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    task = _get_task_or_404(db, task_id)
    _require_access(task, user)
    now = datetime.now(timezone.utc)
    old = serialize_row(task)
    result = db.execute(
        update(OperationalTask)
        .where(OperationalTask.id == task_id, OperationalTask.status == OperationalTaskStatus.PENDING)
        .values(
            status=OperationalTaskStatus.CANCELLED,
            updated_by=user.id,
            updated_at=now,
        ),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount == 1:
        db.refresh(task)
        record_audit(
            db,
            table_name="operational_tasks",
            record_id=task.id,
            action="task_cancelled",
            actor_id=user.id,
            changed_fields={"status": ["PENDING", "CANCELLED"]},
            old_value=old,
            new_value=serialize_row(task),
        )
        db.commit()
        db.refresh(task)
        return TaskActionOut(task=task, detail="Task cancelled")
    db.rollback()
    current = _replay_or_conflict(
        db, task_id, OperationalTaskStatus.CANCELLED, "Cannot cancel a completed task"
    )
    return TaskActionOut(task=current, detail="Task already cancelled")


@router.get("/summary", response_model=OperationsSummary)
def operations_summary(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = _agent_scope(db.query(OperationalTask), user).filter(
        OperationalTask.status == OperationalTaskStatus.PENDING
    )
    tasks = query.all()
    now = datetime.now(timezone.utc)
    start_of_today = datetime.combine(now.date(), time.min, tzinfo=now.tzinfo)
    end_of_today = start_of_today + timedelta(days=1)
    end_of_7_days = start_of_today + timedelta(days=7)
    overdue = due_today = due_7_days = 0
    for task in tasks:
        if task.snoozed_until is not None and task.snoozed_until > now:
            continue  # deferred by snooze
        if task.due_at < start_of_today:
            overdue += 1
        elif task.due_at < end_of_today:
            due_today += 1
        if start_of_today <= task.due_at < end_of_7_days:
            due_7_days += 1
    return OperationsSummary(
        overdue=overdue,
        due_today=due_today,
        due_7_days=due_7_days,
        pending_total=len(tasks),
    )


# ---------------------------------------------------------------------------
# recurring rules
# ---------------------------------------------------------------------------

def _get_rule_or_404(db: Session, rule_id: int) -> RecurringRule:
    rule = (
        db.query(RecurringRule)
        .filter(RecurringRule.id == rule_id, RecurringRule.deleted_at.is_(None))
        .first()
    )
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recurring rule not found")
    return rule


@router.get("/rules", response_model=list[RecurringRuleRead])
def list_rules(
    enabled: bool | None = Query(default=None),
    db: Session = Depends(get_db),
    _: User = Depends(manager_or_admin),
):
    query = db.query(RecurringRule).filter(RecurringRule.deleted_at.is_(None))
    if enabled is not None:
        query = query.filter(RecurringRule.enabled.is_(enabled))
    return query.order_by(RecurringRule.id).all()


@router.post("/rules", response_model=RecurringRuleRead, status_code=status.HTTP_201_CREATED)
def create_rule(
    payload: RecurringRuleCreate,
    db: Session = Depends(get_db),
    user: User = Depends(manager_or_admin),
):
    _check_assignee(db, payload.assigned_user_id)
    _check_property(db, payload.property_id)
    obj = RecurringRule(**payload.model_dump(exclude={"next_run_at"}))
    obj.next_run_at = payload.next_run_at or datetime.now(timezone.utc)
    obj.created_by = user.id
    obj.updated_by = user.id
    db.add(obj)
    db.flush()
    record_audit(
        db,
        table_name="recurring_rules",
        record_id=obj.id,
        action="rule_created",
        actor_id=user.id,
        new_value=serialize_row(obj),
    )
    db.commit()
    db.refresh(obj)
    return obj


@router.patch("/rules/{rule_id}", response_model=RecurringRuleRead)
def update_rule(
    rule_id: int,
    payload: RecurringRuleUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(manager_or_admin),
):
    obj = _get_rule_or_404(db, rule_id)
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return obj
    _check_assignee(db, updates.get("assigned_user_id"))
    _check_property(db, updates.get("property_id"))
    old = serialize_row(obj)
    for field, value in updates.items():
        setattr(obj, field, value)
    obj.updated_by = user.id
    record_audit(
        db,
        table_name="recurring_rules",
        record_id=obj.id,
        action="rule_updated",
        actor_id=user.id,
        old_value=old,
        new_value=serialize_row(obj),
    )
    db.commit()
    db.refresh(obj)
    return obj


@router.post("/rules/{rule_id}/disable", response_model=RecurringRuleRead)
def disable_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(manager_or_admin),
):
    obj = _get_rule_or_404(db, rule_id)
    if not obj.enabled:
        return obj
    old = serialize_row(obj)
    obj.enabled = False
    obj.updated_by = user.id
    record_audit(
        db,
        table_name="recurring_rules",
        record_id=obj.id,
        action="rule_disabled",
        actor_id=user.id,
        old_value=old,
        new_value=serialize_row(obj),
    )
    db.commit()
    db.refresh(obj)
    return obj


# ---------------------------------------------------------------------------
# scheduler trigger
# ---------------------------------------------------------------------------

@router.post("/scheduler/run", response_model=SchedulerRunResult)
def trigger_scheduler(
    db: Session = Depends(get_db),
    _: User = Depends(manager_or_admin),
):
    """Run one scheduler pass on demand (same code path as the worker loop).

    Fail fast if the default assignee is misconfigured, so an operator-triggered
    pass can never silently create un-notifiable business tasks (mirrors the
    worker's startup guard — worker.main() validates at boot; this endpoint
    validates on hit).
    """
    from app.services.operations.config import DEFAULT_ASSIGNED_USER_ID
    from app.services.operations.assignee import validate_default_assignee

    validate_default_assignee(db, DEFAULT_ASSIGNED_USER_ID)
    return run_scheduler_once(db)
