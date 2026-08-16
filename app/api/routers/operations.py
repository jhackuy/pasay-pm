"""V1.2 PROACTIVE OPERATIONS API router (/api/v1/operations).

RBAC (re-checked on EVERY request, including task transitions):
- admin: everything
- manager: view + process operational tasks, manage recurring rules
- agent: only view / process tasks assigned to themselves (403 otherwise)

Task handlers NEVER write incomes/expenses/commission settlements — the
V1.1 financial state machine is the only writer for those tables.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, manager_or_admin
from app.database import get_db
from app.models.copilot import CopilotActionProposal, CopilotActionStatus
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
    TaskCreateIn,
    TaskSnoozeIn,
    TaskUpdateIn,
)
from app.schemas.copilot import (
    CopilotAskIn,
    CopilotAskOut,
    CopilotExecuteOut,
    CopilotNlParseIn,
    CopilotNlParseOut,
    CopilotProposalActionOut,
    CopilotProposalCard,
    CopilotProposalCreate,
    CopilotProposalRead,
    CopilotRecommendIn,
    CopilotRecommendOut,
    CopilotTodayIn,
    CopilotTodayOut,
    CopilotWhyIn,
    CopilotWhyOut,
    LatencyOut,
)
from app.services.audit import record_audit, serialize_row
from app.services.copilot import ask as copilot_ask_svc
from app.services.copilot import llm as copilot_llm
from app.services.copilot import nl_parse as copilot_nl_parse_svc
from app.services.copilot import today as copilot_today_svc
from app.services.copilot import today_fast as copilot_today_fast_svc
from app.services.copilot import why as copilot_why_svc
from app.services.copilot import execute as copilot_execute
from app.services.operations import copilot as copilot_svc
from app.services.operations import proposals as copilot_proposals
from app.services.operations import quick as quick_svc
from app.services.operations.redelivery import suppress_pending_redeliveries
from app.services.operations.scheduler import run_scheduler_once
from app.services.operations.generation import create_operational_task
from app.services.operations.owner_scope import is_owner_actionable

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


def _task_property_code(db: Session, task: OperationalTask) -> str | None:
    """Derived display unit code for a task ("1680", or "BAY-1680" only when
    the unit number repeats across properties). Never an internal UUID."""
    if task.lease_id is not None:
        from app.models.lease import Lease
        from app.models.property import Unit
        lease = db.query(Lease).filter(Lease.id == task.lease_id).first()
        if lease is not None:
            unit = db.query(Unit).filter(Unit.id == lease.unit_id).first()
            if unit is not None:
                dupes = (
                    db.query(Unit.id)
                    .filter(
                        Unit.unit_number == unit.unit_number,
                        Unit.deleted_at.is_(None),
                        Unit.is_active.is_(True),
                    )
                    .count()
                )
                if dupes <= 1:
                    return unit.unit_number
                from app.models.property import Property
                prop = db.query(Property).filter(Property.id == unit.property_id).first()
                prefix = (prop.name or str(unit.property_id)).split()[0][:4].upper() if prop else str(unit.property_id)
                return f"{prefix}-{unit.unit_number}"
    details = task.details or {}
    if details.get("unit_number"):
        return str(details["unit_number"])
    return None


def _task_read(db: Session, task: OperationalTask) -> OperationalTaskRead:
    """Serialize one task with the derived property_code for the V2 cards."""
    data = serialize_row(task)
    if "metadata" in data and "details" not in data:
        data["details"] = data.pop("metadata")
    data["property_code"] = _task_property_code(db, task)
    return OperationalTaskRead(**data)


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
    scope: str | None = Query(default=None, description="'owner' = Owner attention filter"),
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
    rows = query.order_by(OperationalTask.due_at, OperationalTask.id).all()
    if scope == "owner":
        # AI-OPS-FOUNDATION-001 §5: Owner queue contains ONLY tasks needing
        # the Owner (approvals, payments they owe, decisions, escalations).
        rows = [t for t in rows if is_owner_actionable(t, user)]
    return rows


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
            reminder_generation=OperationalTask.reminder_generation + 1,
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
            changed_fields={
                "status": ["PENDING", "COMPLETED"],
                "reminder_generation": [old.get("reminder_generation", 0), task.reminder_generation],
            },
            old_value=old,
            new_value=serialize_row(task),
        )
        suppress_pending_redeliveries(
            db, task_id, actor_id=user.id, reason="task_completed", now=now
        )
        # AI-OPS-FOUNDATION-001 §13: a repair that completes without minimal
        # completion evidence gets a SECRETARY follow-up (never the Owner).
        if task.task_type == OperationalTaskType.AC_MAINTENANCE:
            from app.services.operations.repair_flow import ensure_evidence_followup

            ensure_evidence_followup(db, task, now=now, actor_id=user.id)
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
            reminder_generation=OperationalTask.reminder_generation + 1,
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
            changed_fields={
                "snoozed_until": [None, until.isoformat()],
                "reminder_generation": [old.get("reminder_generation", 0), task.reminder_generation],
            },
            old_value=old,
            new_value=serialize_row(task),
        )
        suppress_pending_redeliveries(
            db, task_id, actor_id=user.id, reason="task_snoozed", now=now
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
            reminder_generation=OperationalTask.reminder_generation + 1,
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
            changed_fields={
                "status": ["PENDING", "CANCELLED"],
                "reminder_generation": [old.get("reminder_generation", 0), task.reminder_generation],
            },
            old_value=old,
            new_value=serialize_row(task),
        )
        suppress_pending_redeliveries(
            db, task_id, actor_id=user.id, reason="task_cancelled", now=now
        )
        db.commit()
        db.refresh(task)
        return TaskActionOut(task=task, detail="Task cancelled")
    db.rollback()
    current = _replay_or_conflict(
        db, task_id, OperationalTaskStatus.CANCELLED, "Cannot cancel a completed task"
    )
    return TaskActionOut(task=current, detail="Task already cancelled")


# ---------------------------------------------------------------------------
# PASAY-V2-FOUNDATION-001: conversation-driven task updates / creation
# ---------------------------------------------------------------------------

def _validate_transition(
    task: OperationalTask,
    want_status: OperationalTaskStatus,
    payload: TaskUpdateIn,
) -> None:
    """V2 transition rules:
    - PENDING -> IN_PROGRESS requires next_action AND next_check_at.
    - IN_PROGRESS -> COMPLETED is the only forward path (completed_at set).
    - CANCELLED is terminal.
    """
    if task.status == OperationalTaskStatus.CANCELLED:
        raise HTTPException(status.HTTP_409_CONFLICT, "Cannot update a cancelled task")
    if want_status == task.status:
        return
    if task.status == OperationalTaskStatus.PENDING and want_status == OperationalTaskStatus.IN_PROGRESS:
        next_action = payload.next_action if payload.next_action is not None else task.next_action
        next_check_at = payload.next_check_at if payload.next_check_at is not None else task.next_check_at
        if not next_action or not next_check_at:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "IN_PROGRESS requires next_action and next_check_at",
            )
        return
    if task.status == OperationalTaskStatus.IN_PROGRESS and want_status == OperationalTaskStatus.COMPLETED:
        return
    if want_status in (
        OperationalTaskStatus.PENDING,
        OperationalTaskStatus.IN_PROGRESS,
        OperationalTaskStatus.COMPLETED,
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Invalid transition {task.status.value} -> {want_status.value}",
        )


@router.patch("/tasks/{task_id}", response_model=TaskActionOut)
def update_task(
    task_id: int,
    payload: TaskUpdateIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    task = _get_task_or_404(db, task_id)
    _require_access(task, user)
    now = datetime.now(timezone.utc)
    want_status = payload.status if payload.status is not None else task.status
    _validate_transition(task, want_status, payload)
    old = serialize_row(task)

    updates: dict = {}
    if payload.title is not None:
        updates["title"] = payload.title
    if payload.due_at is not None:
        updates["due_at"] = payload.due_at
    if payload.next_action is not None:
        updates["next_action"] = payload.next_action
    if payload.next_check_at is not None:
        updates["next_check_at"] = payload.next_check_at
    if payload.context is not None:
        updates["context"] = payload.context
    if payload.completion_condition is not None:
        updates["completion_condition"] = payload.completion_condition
    if payload.details is not None:
        # AI-OPS-FOUNDATION-001 §8: structured details (promise/escalation)
        # are MERGED into the JSONB so a follow-up update never wipes other
        # task metadata.
        merged_details = dict(task.details or {})
        for key, value in payload.details.items():
            if isinstance(value, dict) and isinstance(merged_details.get(key), dict):
                merged_details[key] = {**merged_details[key], **value}
            else:
                merged_details[key] = value
        updates["details"] = merged_details
    if payload.status is not None:
        updates["status"] = payload.status
        if payload.status == OperationalTaskStatus.COMPLETED:
            updates["completed_at"] = now
            updates["completed_by"] = user.id
    if not updates:
        return TaskActionOut(task=_task_read(db, task), detail="No changes")

    updates["updated_by"] = user.id
    updates["updated_at"] = now
    updates["reminder_generation"] = (task.reminder_generation or 0) + 1
    for key, value in updates.items():
        setattr(task, key, value)
    db.flush()
    record_audit(
        db,
        table_name="operational_tasks",
        record_id=task.id,
        action="task_updated",
        actor_id=user.id,
        old_value=old,
        new_value=serialize_row(task),
    )
    if task.status == OperationalTaskStatus.COMPLETED:
        suppress_pending_redeliveries(db, task.id, actor_id=user.id, reason="task_completed", now=now)
        # AI-OPS-FOUNDATION-001 §13: a repair that completes without minimal
        # completion evidence gets a SECRETARY follow-up (never the Owner).
        if task.task_type == OperationalTaskType.AC_MAINTENANCE:
            from app.services.operations.repair_flow import ensure_evidence_followup

            ensure_evidence_followup(db, task, now=now, actor_id=user.id)
    db.commit()
    db.refresh(task)
    detail = "Task completed" if task.status == OperationalTaskStatus.COMPLETED else "Task updated"
    return TaskActionOut(task=_task_read(db, task), detail=detail)


@router.post("/tasks", response_model=TaskActionOut, status_code=status.HTTP_201_CREATED)
def create_task(
    payload: TaskCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Create a V2 task from a conversation event. Dedupes on dedupe_key when
    supplied (at most one active task per key); otherwise a fresh row."""
    if user.role == UserRole.agent:
        if payload.assigned_user_id not in (None, user.id):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Agents may only assign tasks to themselves",
            )
    _check_assignee(db, payload.assigned_user_id)
    _check_property(db, payload.property_id)
    now = datetime.now(timezone.utc)
    fields: dict = {
        "task_type": payload.task_type,
        "title": payload.title,
        "description": payload.description,
        "property_id": payload.property_id,
        "priority": payload.priority,
        "due_at": payload.due_at or (now + timedelta(days=1)),
        "next_action": payload.next_action,
        "next_check_at": payload.next_check_at,
        "context": payload.context,
        "completion_condition": payload.completion_condition,
        "source_event": payload.source_event,
        "source_type": "conversation",
        "assigned_user_id": payload.assigned_user_id,
        "status": (
            OperationalTaskStatus.IN_PROGRESS
            if payload.status == OperationalTaskStatus.IN_PROGRESS
            else OperationalTaskStatus.PENDING
        ),
    }
    if payload.dedupe_key:
        fields["dedupe_key"] = payload.dedupe_key
    if fields["status"] == OperationalTaskStatus.IN_PROGRESS and (
        not payload.next_action or not payload.next_check_at
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "IN_PROGRESS requires next_action and next_check_at",
        )
    task, _ = create_operational_task(
        db, fields=fields, now=now, actor_id=user.id,
    )
    db.commit()
    if task is None:
        # dedupe hit: return the existing active task as a 200-style read
        existing = (
            db.query(OperationalTask)
            .filter(
                OperationalTask.dedupe_key == payload.dedupe_key,
                OperationalTask.status == OperationalTaskStatus.PENDING,
            )
            .first()
        )
        if existing is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "Task dedupe conflict")
        return TaskActionOut(task=_task_read(db, existing), detail="Task already exists")
    db.refresh(task)
    return TaskActionOut(task=_task_read(db, task), detail="Task created")


# ---------------------------------------------------------------------------
# PASAY-V2-FOUNDATION-001: deterministic Quick Views + Daily Digest (no LLM)
# ---------------------------------------------------------------------------

@router.get("/quick/tasks")
def quick_tasks(
    scope: str | None = Query(default=None, description="'owner' = Owner attention filter"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return quick_svc.build_quick_tasks(db, user, owner_only=(scope == "owner"))


@router.get("/quick/properties")
def quick_properties(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return quick_svc.build_quick_properties(db)


@router.get("/quick/rent")
def quick_rent(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return quick_svc.build_quick_rent(db)


@router.get("/quick/expense")
def quick_expense(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return quick_svc.build_quick_expense(db)


@router.get("/quick/expense-duplicates")
def quick_expense_duplicates(
    expense_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Possible-duplicate matcher for one payable expense (PASAY-V2
    -EXPENSE-PAYABLE-TASK-006 §7/§8).

    Advisory only: returns OTHER highly similar PAID expenses (same unit,
    amount, purpose/category and a relevant date window) so the bot can warn
    the Owner before finalizing payment. Amount alone is never a match, and
    no business record is ever deleted or rejected here."""
    from app.models.financial import Expense

    expense = db.query(Expense).filter(Expense.id == expense_id).first()
    if expense is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Expense not found")
    return quick_svc.find_similar_paid_expenses(db, expense)


@router.get("/quick/unit-timeline")
def quick_unit_timeline(
    unit_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """AI-OPS-FOUNDATION-001 §15: the unit's digital file — deterministic
    time-ordered timeline (rent/payment history, expenses, repairs/tasks,
    evidence, lease events) for NL queries like 'Give me the history of 1608'."""
    return quick_svc.build_unit_timeline(db, unit_id)


@router.get("/digest")
def daily_digest(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return quick_svc.build_digest(db, user)


@router.get("/summary", response_model=OperationsSummary)
def operations_summary(
    scope: str | None = Query(default=None, description="'owner' = Owner attention filter"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Operational counts for the current user (agents scoped to their own)."""
    from app.services.operations.summary import build_operations_summary

    return build_operations_summary(db, user, owner_only=(scope == "owner"))


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


# ---------------------------------------------------------------------------
# copilot context + action proposals (V1.2.2 Phase B — NO execution)
# ---------------------------------------------------------------------------

@router.get("/copilot/context")
def copilot_context(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Deterministic, RBAC-scoped Copilot context.

    Admin/manager see the full operational picture; agents see only their own
    tasks and the entities reachable through them. Read-only: the only write is
    the ``copilot_runs`` audit row for this build. No LLM is involved and
    nothing executes any action in Phase A+B.
    """
    context = copilot_svc.build_copilot_context(db, user)
    copilot_svc.log_context_run(db, actor=user, context=context)
    db.commit()
    return context


@router.post(
    "/copilot/today",
    response_model=CopilotTodayOut,
    status_code=status.HTTP_200_OK,
)
def copilot_today(
    payload: CopilotTodayIn | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(manager_or_admin),
):
    """Read-only TODAY brief (C1.1 deterministic-first).

    DEFAULT (no ``provider``): deterministic fast path — the grounded A+B
    context + the existing ``ranking.rank_items`` engine, top-3 items, and a
    versioned deterministic summary. NO LLM on the critical path: no provider
    call, no timeout, no failure path; returns in milliseconds.

    Explicit ``provider`` (eval/measurement): LLM enrichment via
    ``build_today``, post-validated server-side (grounded refs, top-K
    restriction, <=3 items, <=2 sentences). Fail-closed: provider errors
    (unreachable/timeout/5xx) return 503 — never a fabricated answer.

    The only DB write is the optional ``copilot_runs`` audit row.
    """
    body = payload or CopilotTodayIn()
    provider = body.provider
    if provider is not None and provider not in copilot_llm.list_providers():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"unknown copilot LLM provider {provider!r}; "
            f"known providers: {', '.join(copilot_llm.list_providers())}",
        )

    if provider is None:
        # Deterministic-first: this is the immediate Telegram response.
        result = copilot_today_fast_svc.build_today_deterministic(db, user)
        copilot_svc.log_context_run(
            db,
            actor=user,
            context={
                "context": result.context,
                "today": {
                    "top_items": [item.to_dict() for item in result.top_items],
                    "summary": result.summary,
                    "summary_version": result.summary_version,
                    "provider": result.provider,
                    "model": result.model,
                    "latency_ms": result.latency_ms,
                    "enriched": result.enriched,
                    "flags": result.flags,
                    "deterministic_top_refs": result.deterministic_top_refs,
                    "latency": result.latency.to_dict(),
                },
                "intent_note": body.intent_note,
            },
            intent="copilot_today",
        )
        db.commit()
        return CopilotTodayOut(
            top_items=[item.to_dict() for item in result.top_items],
            summary=result.summary,
            context_schema_version=result.context_schema_version,
            provider=result.provider,
            model=result.model,
            latency_ms=result.latency_ms,
            enriched=result.enriched,
            summary_version=result.summary_version,
            flags=result.flags,
            deterministic_top_refs=result.deterministic_top_refs,
            latency=LatencyOut(**result.latency.to_dict()),
        )

    try:
        result = copilot_today_svc.build_today(db, user, provider=provider)
    except copilot_llm.LLMProviderError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "message": f"copilot provider unavailable: {exc}",
                "error_code": "llm_provider_error",
            },
        ) from exc
    except copilot_today_svc.TodayError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "message": f"copilot produced no usable brief: {exc}",
                "error_code": "copilot_today_unavailable",
            },
        ) from exc
    # Optional audit row (the only C1 write): what the copilot was shown + the
    # validated brief + internal flags (hallucinated/dropped/backfilled refs).
    copilot_svc.log_context_run(
        db,
        actor=user,
        context={
            "context": result.context,
            "today": {
                "top_items": [item.to_dict() for item in result.top_items],
                "summary": result.summary,
                "provider": result.provider,
                "model": result.model,
                "latency_ms": result.latency_ms,
                "enriched": result.enriched,
                "flags": result.flags,
                "deterministic_top_refs": result.deterministic_top_refs,
                "latency": result.latency.to_dict(),
            },
            "intent_note": body.intent_note,
        },
        intent="copilot_today",
    )
    db.commit()
    return CopilotTodayOut(
        top_items=[item.to_dict() for item in result.top_items],
        summary=result.summary,
        context_schema_version=result.context_schema_version,
        provider=result.provider,
        model=result.model,
        latency_ms=result.latency.total_ms,
        enriched=result.enriched,
        flags=result.flags,
        deterministic_top_refs=result.deterministic_top_refs,
        latency=LatencyOut(**result.latency.to_dict()),
    )


@router.post(
    "/copilot/why",
    response_model=CopilotWhyOut,
    status_code=status.HTTP_200_OK,
)
def copilot_why(
    payload: CopilotWhyIn,
    db: Session = Depends(get_db),
    user: User = Depends(manager_or_admin),
):
    """Per-item WHY enrichment (on-demand LLM, deterministic fail-closed).

    Grounds from ``build_copilot_context``, validates ``item_ref`` is in the
    grounded set, and calls the EXPLAIN provider profile (fast non-reasoning
    by default) with a scoped WHY prompt. On provider error/timeout/malformed
    output returns HTTP 200 with ``fallback=True`` and the DETERMINISTIC
    reason + suggested action from the priority engine — never fabricated.
    LLM amounts/dates are post-validated against the grounded facts and
    stripped + flagged when invented. Read-only; the only write is the
    optional ``copilot_runs`` audit row.
    """
    provider = payload.provider
    if provider is not None and provider not in copilot_llm.list_providers():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"unknown copilot LLM provider {provider!r}; "
            f"known providers: {', '.join(copilot_llm.list_providers())}",
        )
    try:
        result = copilot_why_svc.explain_item(
            db, user, payload.item_ref, provider=provider
        )
    except copilot_why_svc.WhyItemNotGrounded as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    copilot_svc.log_context_run(
        db,
        actor=user,
        context={
            "context": result.context,
            "why": {
                "item_ref": result.item_ref,
                "explanation": result.explanation,
                "recommendation": result.recommendation,
                "provider": result.provider,
                "model": result.model,
                "latency_ms": result.latency_ms,
                "fallback": result.fallback,
                "flags": result.flags,
                "latency": result.latency.to_dict(),
            },
        },
        intent="copilot_why",
    )
    db.commit()
    return CopilotWhyOut(
        item_ref=result.item_ref,
        explanation=result.explanation,
        recommendation=result.recommendation,
        grounded_refs=result.grounded_refs,
        provider=result.provider,
        model=result.model,
        latency_ms=result.latency.total_ms,
        fallback=result.fallback,
        flags=result.flags,
        latency=LatencyOut(**result.latency.to_dict()),
    )


@router.post(
    "/copilot/ask",
    response_model=CopilotAskOut,
    status_code=status.HTTP_200_OK,
)
def copilot_ask(
    payload: CopilotAskIn,
    db: Session = Depends(get_db),
    user: User = Depends(manager_or_admin),
):
    """On-demand Q&A enrichment (grounded, deterministic fail-closed).

    Grounds to the current A+B context and calls the ASK provider profile
    (strong model by default). LLM output is post-validated server-side: any
    amount/date not resolvable in the grounded context is stripped + flagged,
    backend refs are removed, nothing is executed or written. On provider-down
    returns HTTP 200 with ``fallback=True`` and a friendly deterministic
    answer — never fabricated. Read-only; the only write is the optional
    ``copilot_runs`` audit row.
    """
    provider = payload.provider
    if provider is not None and provider not in copilot_llm.list_providers():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"unknown copilot LLM provider {provider!r}; "
            f"known providers: {', '.join(copilot_llm.list_providers())}",
        )
    try:
        result = copilot_ask_svc.ask_question(
            db, user, payload.question, provider=provider
        )
    except copilot_ask_svc.AskQuestionRequired as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    copilot_svc.log_context_run(
        db,
        actor=user,
        context={
            "context": result.context,
            "ask": {
                "question": payload.question,
                "answer": result.answer,
                "provider": result.provider,
                "model": result.model,
                "latency_ms": result.latency_ms,
                "fallback": result.fallback,
                "flags": result.flags,
                "latency": result.latency.to_dict(),
            },
        },
        intent="copilot_ask",
    )
    db.commit()
    return CopilotAskOut(
        answer=result.answer,
        provider=result.provider,
        model=result.model,
        latency_ms=result.latency.total_ms,
        fallback=result.fallback,
        flags=result.flags,
        latency=LatencyOut(**result.latency.to_dict()),
    )


@router.post(
    "/copilot/nl-parse",
    response_model=CopilotNlParseOut,
    status_code=status.HTTP_200_OK,
)
def copilot_nl_parse(
    payload: CopilotNlParseIn,
    db: Session = Depends(get_db),
    user: User = Depends(manager_or_admin),
):
    """BOT-V1-USABLE-001 P0-5: grounded NL intent parsing for the bot's AI
    fallback lane.

    The text is grounded to the real catalog (units / tenants / categories /
    current month) and parsed into a STRUCTURED intent. Nothing is executed
    or written here; the bot maps the intent to its existing deterministic
    business paths. Provider-down returns HTTP 200 with ``fallback=True`` and
    a deterministic classification / clarification — never a fabricated
    write action. Read-only; the only write is the optional ``copilot_runs``
    audit row.
    """
    provider = payload.provider
    if provider is not None and provider not in copilot_llm.list_providers():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"unknown copilot LLM provider {provider!r}; "
            f"known providers: {', '.join(copilot_llm.list_providers())}",
        )
    result = copilot_nl_parse_svc.parse_nl_intent(
        db, user, payload.text, provider=provider
    )
    copilot_svc.log_context_run(
        db,
        actor=user,
        context={
            "text": payload.text,
            "nl_parse": {
                "intent": result.intent,
                "unit": result.unit,
                "unit_id": result.unit_id,
                "amount": str(result.amount) if result.amount is not None else None,
                "category": result.category,
                "month": result.month,
                "missing": result.missing,
                "options": result.options,
                "provider": result.provider,
                "model": result.model,
                "latency_ms": result.latency_ms,
                "fallback": result.fallback,
                "flags": result.flags,
            },
        },
        intent="copilot_nl_parse",
    )
    db.commit()
    return CopilotNlParseOut(
        intent=result.intent,
        message=result.message,
        unit=result.unit,
        unit_id=result.unit_id,
        amount=str(result.amount) if result.amount is not None else None,
        category=result.category,
        month=result.month,
        missing=result.missing,
        options=result.options,
        provider=result.provider,
        model=result.model,
        fallback=result.fallback,
        flags=result.flags,
        latency_ms=result.latency_ms,
    )




def _target_label(db: Session, target_type: str, target_id: int) -> str:
    """Human label for the confirmation card (rendering only)."""
    from app.models.tenant import Tenant
    from app.models.property import Unit
    target = copilot_svc._resolve_target(db, target_type, target_id)
    if target is None:
        return ""
    if target_type == "lease":
        unit = db.get(Unit, target.unit_id)
        tenant = db.get(Tenant, target.tenant_id)
        return (
            f"Lease #{target.id} · {unit.unit_number if unit else '?'} · "
            f"{tenant.full_name if tenant else '?'}"
        )
    if target_type == "property":
        return f"{target.name} (#{target.id})"
    if target_type == "task":
        return f"#{target.id} {target.title}"
    return f"{target_type} #{target_id}"


def _build_recommend_card(db: Session, proposal: CopilotActionProposal) -> CopilotProposalCard:
    """Render-safe card data (the bot must not display the raw proposal id)."""
    payload = proposal.payload_json or {}
    assignee_id = payload.get("assignee_user_id")
    assignee_name = None
    if assignee_id is not None:
        assignee = db.get(User, assignee_id)
        assignee_name = assignee.username if assignee else None
    due = None
    for key in ("due_at", "until"):
        raw = payload.get(key)
        if isinstance(raw, str):
            try:
                due = datetime.fromisoformat(raw)
            except ValueError:
                due = None
            break
    return CopilotProposalCard(
        action_type=proposal.action_type,
        target_type=proposal.target_type,
        target_id=proposal.target_id,
        target_label=_target_label(db, proposal.target_type, proposal.target_id),
        reason_code=payload.get("reason_code"),
        assignee_user_id=assignee_id,
        assignee_name=assignee_name,
        due_at=due,
        note=payload.get("note"),
        display_context=payload.get("display_context") or {},
    )


@router.post(
    "/copilot/recommend",
    response_model=CopilotRecommendOut,
    status_code=status.HTTP_201_CREATED,
)
def copilot_recommend(
    payload: CopilotRecommendIn,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(manager_or_admin),
):
    """Canonical proposal builder endpoint (V1.2.2 Phase C2).

    The bot posts an intent + resolved refs; the backend deterministically
    resolves EVERYTHING (action code, target, assignee, due time, property
    scope, idempotency key) and returns the canonical PENDING proposal card.
    Nothing executes here — the owner must tap the confirmation card, then
    POST /copilot/proposals/{id}/execute.
    """
    try:
        proposal, created, _payload = copilot_proposals.build_proposal_from_intent(
            db,
            actor=user,
            intent=payload.intent,
            source_type=payload.source_type,
            source_id=payload.source_id,
            task_ref=payload.task_ref,
            reason_code=payload.reason_code,
            assignee_user_id=payload.assignee_user_id,
            due_at=payload.due_at,
            preset=payload.preset,
            note=payload.note,
        )
    except copilot_proposals.ProposalNeedsClarification as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except copilot_svc.ProposalValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    db.commit()
    db.refresh(proposal)
    if not created:
        response.status_code = status.HTTP_200_OK
    return CopilotRecommendOut(
        proposal_id=proposal.id,
        action_type=proposal.action_type,
        status=proposal.status.value,
        target_type=proposal.target_type,
        target_id=proposal.target_id,
        idempotency_key=proposal.idempotency_key,
        expires_at=proposal.expires_at,
        card=_build_recommend_card(db, proposal),
        detail=(
            "Proposal already exists (idempotent replay)"
            if not created
            else "Proposal created"
        ),
        created=created,
    )


@router.post(
    "/copilot/proposals/{proposal_id}/execute",
    response_model=CopilotExecuteOut,
)
def execute_copilot_proposal(
    proposal_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(manager_or_admin),
):
    """CONFIRMED -> EXECUTED (V1.2.2 Phase C2).

    Fail-closed, in ONE DB transaction: the proposal row is locked and
    EVERYTHING is revalidated against CURRENT state (kill-switch, actor
    existence/activity/permission/ownership, EXACT executable allowlist,
    payload schema, target existence/scope/staleness, assignee eligibility,
    snooze window), then the mutation routes through the EXISTING operations
    service layer and the proposal is marked EXECUTED. Replay when already
    EXECUTED returns the existing result with ``replay=true`` and creates no
    second effect. Rejections return a structured 409
    ``{"message", "error_code"}`` with a ``copilot_proposal_execution_rejected``
    audit and NO mutation.
    """
    before = db.get(CopilotActionProposal, proposal_id)
    before_status = before.status if before is not None else None
    try:
        proposal = copilot_execute.execute_proposal(
            db, actor=user, proposal_id=proposal_id
        )
    except copilot_execute.ExecutionDisabledError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except copilot_execute.ProposalExecuteRejectedError as exc:
        db.commit()  # persist the execution_rejected audit (+ EXPIRED transition)
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"message": str(exc), "error_code": exc.error_code},
        ) from exc
    except copilot_svc.ProposalStateError as exc:
        raise _proposal_state_error(exc) from exc
    db.commit()
    db.refresh(proposal)
    was_replay = before_status == CopilotActionStatus.EXECUTED
    return CopilotExecuteOut(
        proposal=CopilotProposalRead.model_validate(proposal),
        result=copilot_execute.execution_result(db, proposal, replay=was_replay),
    )


def _proposal_state_error(exc: Exception) -> HTTPException:
    if "not found" in str(exc):
        return HTTPException(status.HTTP_404_NOT_FOUND, "Copilot proposal not found")
    return HTTPException(status.HTTP_409_CONFLICT, str(exc))


@router.post(
    "/copilot/proposals",
    response_model=CopilotProposalActionOut,
    status_code=status.HTTP_201_CREATED,
)
def create_copilot_proposal(
    payload: CopilotProposalCreate,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(manager_or_admin),
):
    """Create a PENDING action proposal.

    Idempotency is actor-scoped (``UNIQUE(actor_user_id, idempotency_key)``):
    a duplicate submission by the SAME actor returns the existing proposal with
    HTTP 200 instead of creating a second row; a different actor using the same
    key is an independent request. ``action_type`` / ``target_type`` /
    ``idempotency_key`` are canonicalized (NFC + invisible-character removal)
    at this boundary. Nothing is executed — proposals only record intent for
    Phase C.
    """
    try:
        proposal, created = copilot_svc.create_proposal(
            db,
            actor=user,
            action_type=payload.action_type,
            target_type=payload.target_type,
            target_id=payload.target_id,
            payload=payload.payload,
            idempotency_key=payload.idempotency_key,
            expires_at=payload.expires_at,
        )
    except copilot_svc.ProposalValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    db.commit()
    db.refresh(proposal)
    if not created:
        response.status_code = status.HTTP_200_OK
    return CopilotProposalActionOut(
        proposal=CopilotProposalRead.model_validate(proposal),
        detail=(
            "Proposal already exists (idempotent replay)"
            if not created
            else "Proposal created"
        ),
    )


@router.post(
    "/copilot/proposals/{proposal_id}/confirm",
    response_model=CopilotProposalActionOut,
)
def confirm_copilot_proposal(
    proposal_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(manager_or_admin),
):
    """Confirm a PENDING proposal (PENDING -> CONFIRMED).

    Fail-closed: in one DB transaction the proposal row is locked and
    EVERYTHING is revalidated against current state (actor existence/activity/
    permission, proposal state + expiry, target allowlist/existence/scope,
    action x target legality, payload schema, business staleness). Failures
    return a structured 409 ``{"message", "error_code"}``, write a
    ``copilot_proposal_confirm_rejected`` audit, and execute nothing. Idempotent
    replay when already CONFIRMED; expired proposals are atomically marked
    EXPIRED and rejected. Phase A+B never transitions to EXECUTED and never
    sets ``executed_at``.
    """
    before = db.get(CopilotActionProposal, proposal_id)
    before_status = (
        before.status if before is not None else None
    )  # snapshot value BEFORE the service mutates the same ORM instance
    try:
        proposal = copilot_svc.confirm_proposal(db, actor=user, proposal_id=proposal_id)
    except copilot_svc.ProposalExpiredError as exc:
        db.commit()  # persist the EXPIRED transition before rejecting
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except copilot_svc.ProposalConfirmRejectedError as exc:
        db.commit()  # persist the copilot_proposal_confirm_rejected audit
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"message": str(exc), "error_code": exc.error_code},
        ) from exc
    except copilot_svc.ProposalStateError as exc:
        raise _proposal_state_error(exc) from exc
    db.commit()
    db.refresh(proposal)
    was_replay = before_status == CopilotActionStatus.CONFIRMED
    return CopilotProposalActionOut(
        proposal=CopilotProposalRead.model_validate(proposal),
        detail=(
            "Proposal already confirmed (idempotent replay)"
            if was_replay
            else "Proposal confirmed"
        ),
    )


@router.post(
    "/copilot/proposals/{proposal_id}/cancel",
    response_model=CopilotProposalActionOut,
)
def cancel_copilot_proposal(
    proposal_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(manager_or_admin),
):
    """Cancel a PENDING proposal (PENDING -> CANCELLED, idempotent replay)."""
    before = db.get(CopilotActionProposal, proposal_id)
    before_status = (
        before.status if before is not None else None
    )  # snapshot value BEFORE the service mutates the same ORM instance
    try:
        proposal = copilot_svc.cancel_proposal(db, actor=user, proposal_id=proposal_id)
    except copilot_svc.ProposalExpiredError as exc:
        db.commit()  # persist the EXPIRED transition before rejecting
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except copilot_svc.ProposalStateError as exc:
        raise _proposal_state_error(exc) from exc
    db.commit()
    db.refresh(proposal)
    was_replay = before_status == CopilotActionStatus.CANCELLED
    return CopilotProposalActionOut(
        proposal=CopilotProposalRead.model_validate(proposal),
        detail=(
            "Proposal already cancelled (idempotent replay)"
            if was_replay
            else "Proposal cancelled"
        ),
    )
