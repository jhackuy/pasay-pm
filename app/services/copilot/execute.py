"""V1.2.2 Phase C2 — CONFIRMED-action copilot executor.

The ONLY three executable actions (EXACT allowlist, enforced here against the
shared constants in ``app/services/operations/copilot.py``):

    create_followup_task / assign_task / snooze_task

Financial wall (ARCHITECTURAL, not lexical): the executor
- re-validates the action against ``EXECUTABLE_ACTIONS`` (no financial verb is
  executable) and re-rejects any proposal whose target is a financial entity;
- the strict payload schema carries no financial / irreversible fields;
- it ONLY calls the existing operations task service layer (generation /
  outbox / redelivery) — no financial service is imported or reachable here,
  and it never writes ``operational_tasks`` directly.

Execution semantics (single DB transaction, fail closed):
- ``execute_proposal`` re-locks the proposal row (SELECT ... FOR UPDATE), which
  must be exactly CONFIRMED; already-EXECUTED is an idempotent replay (no
  second effect); PENDING/CANCELLED/EXPIRED reject.
- EVERYTHING is re-validated against CURRENT state at execute time — the
  earlier confirm pass is NOT authority (actor, role, ownership, action x
  target allowlist, payload schema, target existence/scope/staleness, assignee
  eligibility, snooze window).
- The business mutation routes through the existing service layer, then the
  proposal is transitioned CONFIRMED -> EXECUTED with ``executed_at`` and the
  ``copilot_proposal_executing`` / ``copilot_proposal_executed`` audits.
- Any failure records ``copilot_proposal_execution_rejected`` with a stable
  ``error_code``, mutates nothing, and leaves the proposal CONFIRMED.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.copilot import CopilotActionProposal, CopilotActionStatus
from app.models.operations import (
    OperationalTask,
    OperationalTaskPriority,
    OperationalTaskStatus,
    OperationalTaskType,
)
from app.models.property import Unit
from app.models.user import User, UserRole
from app.schemas.copilot import CopilotExecuteResult
from app.services.audit import record_audit, serialize_row
from app.services.identity import eligible_human
from app.services.operations import copilot as copilot_svc
from app.services.operations import generation
from app.services.operations.config import NOTIFY_CHANNEL_TELEGRAM
from app.services.operations.outbox import enqueue_notification, resolve_recipient
from app.services.operations.redelivery import suppress_pending_redeliveries


ExecutionDisabledError = copilot_svc.ExecutionDisabledError


class ProposalExecuteRejectedError(copilot_svc.ProposalStateError):
    """Execute-time revalidation failed: nothing mutated, the proposal stays
    CONFIRMED, and a stable machine-readable ``error_code`` is recorded."""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


class _ActionValidationError(ValueError):
    """Typed action-specific validation failure (assignee / snooze window)."""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def execute_proposal(
    db: Session, *, actor: User, proposal_id: int, now: datetime | None = None
) -> CopilotActionProposal:
    """CONFIRMED -> EXECUTED in ONE transaction with execute-time revalidation.

    Idempotent replay when already EXECUTED (returns the existing proposal, no
    second business effect). Fail closed: any rejection records a
    ``copilot_proposal_execution_rejected`` audit and raises
    ``ProposalExecuteRejectedError``; nothing mutates.
    """
    copilot_svc._guard_execution_disabled()
    now = now or datetime.now(timezone.utc)
    proposal = (
        db.query(CopilotActionProposal)
        .filter(CopilotActionProposal.id == proposal_id)
        .with_for_update()
        .first()
    )
    if proposal is None:
        raise copilot_svc.ProposalStateError("proposal not found")
    if proposal.status == CopilotActionStatus.EXECUTED:
        copilot_svc.assert_executed_invariant(proposal)
        return proposal  # replay: exactly one logical effect ever
    if proposal.status != CopilotActionStatus.CONFIRMED:
        raise copilot_svc.ProposalStateError(
            f"cannot execute a {proposal.status.value} proposal"
        )
    if proposal.expires_at is not None and proposal.expires_at <= now:
        _expire_confirmed(db, proposal, now=now)
        _reject_audit(
            db, actor, proposal,
            copilot_svc.ERR_PROPOSAL_EXPIRED, "proposal has expired",
        )

    subject = _revalidate_for_execute(db, actor=actor, proposal=proposal, now=now)

    record_audit(
        db,
        table_name="copilot_action_proposals",
        record_id=proposal.id,
        action="copilot_proposal_executing",
        actor_id=actor.id,
        changed_fields={"execution_started_at": now.isoformat()},
        old_value=serialize_row(proposal),
    )
    _apply(db, actor=actor, proposal=proposal, now=now)

    old = serialize_row(proposal)
    result = db.execute(
        update(CopilotActionProposal)
        .where(
            CopilotActionProposal.id == proposal.id,
            CopilotActionProposal.status == CopilotActionStatus.CONFIRMED,
        )
        .values(
            status=CopilotActionStatus.EXECUTED,
            executed_at=now,
            updated_at=now,
            updated_by=actor.id,
            executed_principal_id=subject.id if subject is not None else None,
        ),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount != 1:
        raise copilot_svc.ProposalStateError(
            "proposal was changed concurrently; refresh and retry"
        )
    proposal.status = CopilotActionStatus.EXECUTED
    proposal.executed_at = now
    proposal.executed_principal_id = subject.id if subject is not None else None
    record_audit(
        db,
        table_name="copilot_action_proposals",
        record_id=proposal.id,
        action="copilot_proposal_executed",
        actor_id=actor.id,
        changed_fields={
            "status": [CopilotActionStatus.CONFIRMED.value, CopilotActionStatus.EXECUTED.value],
            "executed_at": [None, now.isoformat()],
        },
        old_value=old,
        new_value=serialize_row(proposal),
    )
    copilot_svc.assert_executed_invariant(proposal)
    return proposal


def execution_result(
    db: Session, proposal: CopilotActionProposal, *, replay: bool = False
) -> CopilotExecuteResult:
    """Render-friendly outcome block for the bot (role-aware text upstream).

    Derives the created task id deterministically (dedupe key) — the executor
    writes no transient state.
    """
    action = copilot_svc.canonicalize(proposal.action_type)
    payload = proposal.payload_json or {}
    assignee_id = payload.get("assignee_user_id")
    due_at = None
    task_id = None
    detail = (
        "Proposal already executed (idempotent replay)"
        if replay
        else "Proposal executed"
    )
    if action == "create_followup_task":
        reason = str(payload.get("reason_code") or "")
        dedupe_key = (
            f"followup:{copilot_svc.canonicalize(proposal.target_type)}:"
            f"{proposal.target_id}:{reason}"
        )
        task = (
            db.query(OperationalTask)
            .filter(OperationalTask.dedupe_key == dedupe_key)
            .first()
        )
        if task is not None:
            task_id = task.id
        else:
            detail = "Followup task already active (dedupe replay)"
        due_at = _parse_dt(payload.get("due_at"))
    elif action == "assign_task":
        task_id = proposal.target_id
    elif action == "snooze_task":
        task_id = proposal.target_id
        due_at = _parse_dt(payload.get("until"))
    return CopilotExecuteResult(
        action_type=action,
        target_type=proposal.target_type,
        target_id=proposal.target_id,
        task_id=task_id,
        assignee_user_id=assignee_id,
        due_at=due_at,
        executed_at=proposal.executed_at,
        status=proposal.status.value,
        replay=replay,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# execute-time revalidation (FAIL CLOSED — the confirm pass is NOT authority)
# ---------------------------------------------------------------------------

def _revalidate_for_execute(
    db: Session, *, actor: User, proposal: CopilotActionProposal, now: datetime
):
    def _reject(error_code: str, reason: str) -> None:
        _reject_audit(db, actor, proposal, error_code, reason)

    # 1) actor still exists, active, and holds manager/admin
    actor_row = copilot_svc._fresh_user(db, actor.id)
    if actor_row is None:
        _reject(copilot_svc.ERR_ACTOR_NOT_FOUND, "actor user no longer exists")
    if not actor_row.is_active:
        _reject(copilot_svc.ERR_ACTOR_INACTIVE, "actor user is deactivated")
    if actor_row.role not in (UserRole.manager, UserRole.admin):
        _reject(copilot_svc.ERR_ACTOR_PERMISSION,
                "actor no longer has permission to execute proposals")
    if proposal.actor_user_id != actor_row.id:
        _reject(copilot_svc.ERR_ACTOR_PERMISSION, "actor does not own this proposal")
    try:
        subject = copilot_svc._validate_proposal_principals(
            db,
            actor=actor_row,
            proposal=proposal,
            require_confirmed_subject=True,
        )
    except copilot_svc._PrincipalValidationError as exc:
        _reject(exc.error_code, str(exc))

    # 2) EXACT executable allowlist + target allowlist (current constants)
    action_type = copilot_svc.canonicalize(proposal.action_type)
    target_type = copilot_svc.canonicalize(proposal.target_type)
    if action_type not in copilot_svc.EXECUTABLE_ACTIONS:
        _reject(
            copilot_svc.ERR_ACTION_NOT_EXECUTABLE,
            f"action '{action_type}' is not executable by the copilot",
        )
    if target_type not in copilot_svc.TARGET_TYPES:
        _reject(copilot_svc.ERR_TARGET_TYPE_UNKNOWN,
                f"unknown target_type '{target_type}'")
    # Financial wall: no executable action may target a financial entity.
    if target_type in copilot_svc.FINANCIAL_TARGET_TYPES:
        _reject(
            copilot_svc.ERR_ACTION_TARGET_ILLEGAL,
            f"copilot execution may not target financial entity '{target_type}'",
        )

    # 3) payload strict schema (same rules as create + confirm)
    try:
        copilot_svc._validate_payload(proposal.payload_json)
        copilot_svc.validate_action_payload(action_type, proposal.payload_json)
    except copilot_svc.ProposalValidationError as exc:
        _reject(copilot_svc.ERR_PAYLOAD_INVALID, str(exc))

    # 4) target still exists, in scope, business not stale
    target = copilot_svc._resolve_target(db, target_type, proposal.target_id)
    if target is None:
        _reject(
            copilot_svc.ERR_TARGET_MISSING,
            f"target {target_type}:{proposal.target_id} no longer exists",
        )
    if not copilot_svc._target_in_actor_scope(actor_row, target_type, target):
        _reject(
            copilot_svc.ERR_TARGET_OUT_OF_SCOPE,
            f"target {target_type}:{proposal.target_id} is outside the actor's scope",
        )
    stale = copilot_svc._business_stale_reason(target_type, target)
    if stale is not None:
        _reject(copilot_svc.ERR_BUSINESS_STALE, stale)

    # 5) action-specific validity (assignee eligibility / snooze window)
    try:
        t_org_id = _target_org_id(db, target_type, target)
        _validate_action_specific(
            db, action_type, proposal.payload_json, now=now,
            target_org_id=t_org_id,
        )
    except _ActionValidationError as exc:
        _reject(exc.error_code, str(exc))
    return subject


def _validate_action_specific(
    db: Session, action_type: str, payload: dict, *, now: datetime,
    target_org_id: int | None = None,
) -> None:
    if action_type in ("create_followup_task", "assign_task"):
        assignee_id = int(payload["assignee_user_id"])
        _require_assignee(db, assignee_id, org_id=target_org_id)
    if action_type == "create_followup_task":
        _parse_payload_dt(db, payload["due_at"])  # must be parseable + aware
    if action_type == "snooze_task":
        until = _parse_payload_dt(db, payload["until"])
        if until <= now:
            raise _ActionValidationError(
                copilot_svc.ERR_SNOOZE_WINDOW_INVALID,
                "snooze window is no longer valid (until is in the past)",
            )


def _require_assignee(
    db: Session, user_id: int, *, org_id: int | None = None,
) -> User:
    user = db.get(User, user_id)
    eligible = {UserRole.agent, UserRole.manager, UserRole.admin}
    if user is None or not eligible_human(user) or user.role not in eligible:
        raise _ActionValidationError(
            copilot_svc.ERR_ASSIGNEE_INVALID,
            f"assignee user {user_id} is not an active, eligible assignee",
        )
    if org_id is not None:
        from app.services.membership import has_active_membership
        if not has_active_membership(db, user.id, org_id):
            raise _ActionValidationError(
                copilot_svc.ERR_ASSIGNEE_INVALID,
                f"assignee user {user_id} has no active membership in target org",
            )
    return user


def _target_org_id(db: Session, target_type: str, target) -> int | None:
    """Resolve the owning organization_id for a copilot proposal target.

    Used to assert that any chosen assignee holds an ACTIVE Membership in the
    same organization (fail-closed — cross-org assignment is rejected).
    Returns ``None`` when ownership cannot be determined (assignee check is
    skipped conservatively for such targets).
    """
    try:
        if target_type == "property":
            return getattr(target, "organization_id", None)
        if target_type == "lease":
            unit = db.get(Unit, getattr(target, "unit_id", None))
            if unit is None:
                return None
            from app.models.property import Property as _P
            row = db.query(_P.organization_id).filter(
                _P.id == unit.property_id, _P.deleted_at.is_(None)
            ).one_or_none()
            return row[0] if row else None
        if target_type == "task":
            pid = getattr(target, "property_id", None)
            if pid is not None:
                from app.models.property import Property as _P
                row = db.query(_P.organization_id).filter(
                    _P.id == pid, _P.deleted_at.is_(None)
                ).one_or_none()
                if row:
                    return row[0]
            lid = getattr(target, "lease_id", None)
            if lid is not None:
                from app.models.lease import Lease as _L
                lease = db.get(_L, lid)
                if lease is not None:
                    unit = db.get(Unit, lease.unit_id)
                    if unit is not None:
                        from app.models.property import Property as _P
                        row = db.query(_P.organization_id).filter(
                            _P.id == unit.property_id, _P.deleted_at.is_(None)
                        ).one_or_none()
                        if row:
                            return row[0]
            tid = getattr(target, "tenant_id", None)
            if tid is not None:
                from app.models.tenant import Tenant as _T
                t_row = db.query(_T.organization_id).filter(
                    _T.id == tid, _T.deleted_at.is_(None)
                ).one_or_none()
                if t_row:
                    return t_row[0]
        return None
    except Exception:
        return None


def _parse_payload_dt(db: Session, value) -> datetime:
    if not isinstance(value, str):
        raise _ActionValidationError(
            copilot_svc.ERR_PAYLOAD_INVALID, "payload datetime must be a string"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise _ActionValidationError(
            copilot_svc.ERR_PAYLOAD_INVALID, "payload datetime is not ISO-8601"
        ) from exc
    if parsed.tzinfo is None:
        raise _ActionValidationError(
            copilot_svc.ERR_PAYLOAD_INVALID, "payload datetime must be timezone-aware"
        )
    return parsed


def _parse_dt(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# secretary-facing (English) outbox cards
# ---------------------------------------------------------------------------
# The confirmed followup/assign outbox message is role-reorganized per the
# receiver (UX design §3): the English-speaking secretary gets a card built
# from the backend-resolved display_context — Unit / Issue / Action / Due /
# Tenant — never a translation of the owner's Chinese text. Scheduler
# business tasks keep the Chinese default (this override is opt-in).

_FOLLOWUP_REASON_LABELS = {
    "RENT_OVERDUE": "Rent overdue",
    "RENT_DUE": "Rent due",
    "LEASE_EXPIRING": "Lease expiring",
    "PROPERTY_FEE_DUE": "Property fee due",
    "APPROVAL_PENDING": "Approval pending",
    "PAYMENT_PENDING": "Payment pending",
    "FOLLOWUP": "Follow-up",
}

_FOLLOWUP_ACTION_LABELS = {
    "RENT_OVERDUE": "Contact tenant and confirm payment date.",
    "RENT_DUE": "Contact tenant to confirm payment date.",
}


def _human_reason(reason_code: str) -> str:
    """Snake-case reason code -> short English label."""
    return _FOLLOWUP_REASON_LABELS.get(
        str(reason_code).upper(),
        str(reason_code).replace("_", " ").strip().title() or "Follow-up",
    )


def _followup_action(reason_code: str) -> str:
    return _FOLLOWUP_ACTION_LABELS.get(
        str(reason_code).upper(), "Follow up and confirm resolution."
    )


def _secretary_followup_message(
    *, due_at: datetime, reason_code: str, display_context: dict, note: str | None
) -> str:
    """English secretary card for a confirmed follow-up (one outbox row)."""
    ctx = display_context or {}
    lines = ["📋 Follow-up Required"]
    unit = ctx.get("unit")
    if unit:
        lines.append(f"Unit: {unit}")
    if ctx.get("property"):
        lines.append(f"Property: {ctx['property']}")
    lines.append(f"Issue: {_human_reason(reason_code)}")
    if note and str(note).strip():
        lines.append(f"Note: {str(note).strip()[:200]}")
    lines.append(f"Action: {_followup_action(reason_code)}")
    lines.append(f"Due: {due_at:%Y-%m-%d %H:%M}")
    if ctx.get("tenant"):
        lines.append(f"Tenant: {ctx['tenant']}")
    return "\n".join(lines)


def _secretary_assign_message(task: OperationalTask, *, display_context: dict) -> str:
    """English secretary card for a reassignment (one outbox row)."""
    ctx = display_context or {}
    lines = ["📋 Task Assigned"]
    title = ctx.get("title") or task.title
    if title:
        lines.append(str(title))
    lines.append(f"Due: {task.due_at:%Y-%m-%d %H:%M}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# business mutation — routes ONLY through the existing operations service layer
# ---------------------------------------------------------------------------

def _apply(db: Session, *, actor: User, proposal: CopilotActionProposal, now: datetime) -> None:
    action = copilot_svc.canonicalize(proposal.action_type)
    if action == "create_followup_task":
        _apply_followup(db, actor=actor, proposal=proposal, now=now)
    elif action == "assign_task":
        _apply_assign(db, actor=actor, proposal=proposal, now=now)
    else:
        _apply_snooze(db, actor=actor, proposal=proposal, now=now)


def _apply_followup(
    db: Session, *, actor: User, proposal: CopilotActionProposal, now: datetime
) -> int | None:
    """CREATE_FOLLOWUP_TASK -> generation.create_operational_task (the EXISTING
    atomic create + audit + outbox path). Returns the task id or None when the
    DB dedupe boundary already holds an active followup (at-most-one effect)."""
    payload = proposal.payload_json
    target_type = copilot_svc.canonicalize(proposal.target_type)
    target = copilot_svc._resolve_target(db, target_type, proposal.target_id)
    t_org_id = _target_org_id(db, target_type, target)
    assignee_id = int(payload["assignee_user_id"])
    _require_assignee(db, assignee_id, org_id=t_org_id)
    reason_code = str(payload["reason_code"])
    due_at = _parse_payload_dt(db, payload["due_at"])
    prop_id, tenant_id, lease_id = _inherit_task_context(db, target_type, target)
    note = payload.get("note")
    title = (note or "").strip()[:120] or f"跟进 {reason_code}"
    dedupe_key = f"followup:{target_type}:{proposal.target_id}:{reason_code}"
    display_context = payload.get("display_context") or {}
    task, _enqueued = generation.create_operational_task(
        db,
        now=now,
        actor_id=actor.id,
        notification_message=_secretary_followup_message(
            due_at=due_at,
            reason_code=reason_code,
            display_context=display_context,
            note=note,
        ),
        fields={
            "task_type": OperationalTaskType.FOLLOWUP,
            "title": title,
            "description": note,
            "property_id": prop_id,
            "tenant_id": tenant_id,
            "lease_id": lease_id,
            "source_type": target_type,
            "source_id": proposal.target_id,
            "assigned_user_id": assignee_id,
            "priority": OperationalTaskPriority.medium,
            "status": OperationalTaskStatus.PENDING,
            "due_at": due_at,
            "dedupe_key": dedupe_key,
            "details": {
                "copilot_reason_code": reason_code,
                "copilot_proposal_id": proposal.id,
                "display_context": display_context,
            },
        },
    )
    return task.id if task is not None else None


def _apply_assign(
    db: Session, *, actor: User, proposal: CopilotActionProposal, now: datetime
) -> int | None:
    """ASSIGN_TASK -> update the task + outbox to the NEW assignee (same
    transaction; no second reminder path). No-op when already assigned."""
    payload = proposal.payload_json
    target_type = copilot_svc.canonicalize(proposal.target_type)
    target = copilot_svc._resolve_target(db, target_type, proposal.target_id)
    t_org_id = _target_org_id(db, target_type, target)
    assignee_id = int(payload["assignee_user_id"])
    _require_assignee(db, assignee_id, org_id=t_org_id)
    task = db.get(OperationalTask, proposal.target_id)
    if task is None or task.status != OperationalTaskStatus.PENDING:
        raise _ActionValidationError(
            copilot_svc.ERR_BUSINESS_STALE, "task is no longer pending"
        )
    if task.assigned_user_id == assignee_id:
        return task.id  # logical effect already present
    old = serialize_row(task)
    task.assigned_user_id = assignee_id
    task.reminder_generation = (task.reminder_generation or 0) + 1
    task.updated_at = now
    task.updated_by = actor.id
    db.flush()
    record_audit(
        db,
        table_name="operational_tasks",
        record_id=task.id,
        action="task_reassigned",
        actor_id=actor.id,
        changed_fields={
            "assigned_user_id": [old.get("assigned_user_id"), assignee_id],
            "reminder_generation": [
                old.get("reminder_generation", 0),
                task.reminder_generation,
            ],
        },
        old_value=old,
        new_value=serialize_row(task),
    )
    recipient = resolve_recipient(db, assignee_id)
    if recipient is not None:
        enqueue_notification(
            db,
            task_id=task.id,
            channel=NOTIFY_CHANNEL_TELEGRAM,
            recipient=recipient,
            payload={
                "task_id": task.id,
                "task_type": task.task_type.value,
                "title": task.title,
                "due_at": task.due_at.isoformat(),
                "message": _secretary_assign_message(
                    task, display_context=payload.get("display_context") or {}
                ),
            },
            dedupe_key=f"task:{task.id}:{NOTIFY_CHANNEL_TELEGRAM}:{recipient}",
        )
    return task.id


def _apply_snooze(
    db: Session, *, actor: User, proposal: CopilotActionProposal, now: datetime
) -> int | None:
    """SNOOZE_TASK -> set snoozed_until + bump reminder_generation; the EXISTING
    redeliver_due_snoozes -> outbox -> notifier path fires the due reminder.
    No second reminder path is created here."""
    payload = proposal.payload_json
    until = _parse_payload_dt(db, payload["until"])
    if until <= now:
        raise _ActionValidationError(
            copilot_svc.ERR_SNOOZE_WINDOW_INVALID,
            "snooze window is no longer valid (until is in the past)",
        )
    task = db.get(OperationalTask, proposal.target_id)
    if task is None or task.status != OperationalTaskStatus.PENDING:
        raise _ActionValidationError(
            copilot_svc.ERR_BUSINESS_STALE, "task is no longer pending"
        )
    if task.snoozed_until == until:
        return task.id  # logical effect already present
    old = serialize_row(task)
    task.snoozed_until = until
    task.reminder_generation = (task.reminder_generation or 0) + 1
    task.updated_at = now
    task.updated_by = actor.id
    db.flush()
    record_audit(
        db,
        table_name="operational_tasks",
        record_id=task.id,
        action="task_snoozed",
        actor_id=actor.id,
        changed_fields={
            "snoozed_until": [old.get("snoozed_until"), until.isoformat()],
            "reminder_generation": [
                old.get("reminder_generation", 0),
                task.reminder_generation,
            ],
        },
        old_value=old,
        new_value=serialize_row(task),
    )
    suppress_pending_redeliveries(
        db, task.id, actor_id=actor.id, reason="copilot_snooze", now=now
    )
    return task.id


def _inherit_task_context(db: Session, target_type: str, target):
    """Property/tenant/lease scope inherited deterministically from the target."""
    if target_type == "lease":
        unit = db.get(Unit, target.unit_id)
        return (unit.property_id if unit else None), target.tenant_id, target.id
    if target_type == "property":
        return target.id, None, None
    if target_type == "task":
        return target.property_id, target.tenant_id, target.lease_id
    return None, None, None


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------

def _expire_confirmed(
    db: Session, proposal: CopilotActionProposal, *, now: datetime
) -> bool:
    """CONFIRMED -> EXPIRED (execute-time expiry; the shared _expire_one only
    handles PENDING proposals)."""
    old = serialize_row(proposal)
    result = db.execute(
        update(CopilotActionProposal)
        .where(
            CopilotActionProposal.id == proposal.id,
            CopilotActionProposal.status == CopilotActionStatus.CONFIRMED,
        )
        .values(status=CopilotActionStatus.EXPIRED, updated_at=now),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount != 1:
        return False
    proposal.status = CopilotActionStatus.EXPIRED
    proposal.updated_at = now
    record_audit(
        db,
        table_name="copilot_action_proposals",
        record_id=proposal.id,
        action="copilot_proposal_expired",
        actor_id=None,  # time-driven
        changed_fields={"status": [CopilotActionStatus.CONFIRMED.value, CopilotActionStatus.EXPIRED.value]},
        old_value=old,
        new_value=serialize_row(proposal),
    )
    return True


def _reject_audit(
    db: Session, actor: User, proposal: CopilotActionProposal,
    error_code: str, reason: str,
) -> None:
    """Record the fail-closed rejection audit and raise (nothing mutates)."""
    record_audit(
        db,
        table_name="copilot_action_proposals",
        record_id=proposal.id,
        action="copilot_proposal_execution_rejected",
        actor_id=actor.id,
        changed_fields={"error_code": error_code, "reason": reason},
    )
    raise ProposalExecuteRejectedError(error_code, reason)
