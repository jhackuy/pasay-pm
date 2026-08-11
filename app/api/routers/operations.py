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
    TaskSnoozeIn,
)
from app.schemas.copilot import (
    CopilotProposalActionOut,
    CopilotProposalCreate,
    CopilotProposalRead,
    CopilotTodayIn,
    CopilotTodayOut,
)
from app.services.audit import record_audit, serialize_row
from app.services.copilot import llm as copilot_llm
from app.services.copilot import today as copilot_today_svc
from app.services.operations import copilot as copilot_svc
from app.services.operations.redelivery import suppress_pending_redeliveries
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


@router.get("/summary", response_model=OperationsSummary)
def operations_summary(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Operational counts for the current user (agents scoped to their own)."""
    from app.services.operations.summary import build_operations_summary

    return build_operations_summary(db, user)


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
    """Read-only TODAY brief grounded to the deterministic context (C1).

    Grounds from ``build_copilot_context`` (same RBAC scope as A+B), renders
    it as injection-safe fenced data, calls the configured/requested provider,
    and post-validates server-side: item refs must be grounded and within the
    deterministic top-K, at most 3 items, summary at most 2 sentences.
    Fail-closed: provider errors (unreachable/timeout/5xx) return 503 with a
    clear reason — never a fabricated answer. The only DB write is the
    ``copilot_runs`` audit row.
    """
    body = payload or CopilotTodayIn()
    provider = body.provider
    if provider is not None and provider not in copilot_llm.list_providers():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"unknown copilot LLM provider {provider!r}; "
            f"known providers: {', '.join(copilot_llm.list_providers())}",
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
                "flags": result.flags,
                "deterministic_top_refs": result.deterministic_top_refs,
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
