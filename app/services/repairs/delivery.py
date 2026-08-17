"""REPAIR-AI-EMPLOYEE-WORKFLOW-008A — Repair Action → Secretary work-entry projection.

The authoritative business fact is the ``repair_action`` (requote requested after
a proposal rejection). For a REAL human to act without knowing repair/proposal
internals, the action is **projected into the existing ``operational_tasks``
work queue** assigned to the Secretary — the same Tasks / Telegram channel she
already uses. We do NOT create a second task system; ``operational_tasks`` is the
human work-entry projection, ``repair_actions`` is the source of truth.

The projection reuses ``create_operational_task`` so it inherits the existing:
- DB-level dedupe (one active task per ``dedupe_key``) — a repeated worker tick
  / reject callback / retry can never enqueue a duplicate task (Gate B §3.2);
- outbox + notifier delivery with the send-time ``task still PENDING`` guard and
  daily-dedup / idempotent reminder semantics (no reminder spam);
- audit of task_created.

Delivery semantics (Gate B §3.2):
- Same active REQUOTE → at most ONE active projected task. Re-running
  ``project_requote_to_task`` N times creates at most one task (+ one outbox row).
- The reminder fires while the task is PENDING (proactive cadence governed by the
  existing daily-dedup + notifier). It does NOT repeat per worker tick.
- Acknowledging / completing the task stops reminders (notifier drops pending
  rows for a non-PENDING task), while the underlying business REQUOTE stays
  active until the repair actually moves forward — Reminder ≠ Action completion.

Completion (Gate B §3.3): when the Secretary submits the next proposal (V2),
``close_requote_projection`` completes the projected requote task so it disappears
from the work queue and its pending reminders are dropped; the Repair moves to
``WAITING_APPROVAL`` and the Owner gets the approval next-step.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models.operations import (
    OperationalTask,
    OperationalTaskPriority,
    OperationalTaskStatus,
    OperationalTaskType,
)
from app.models.repair import RepairAction, RepairOperation
from app.models.property import Unit
from app.services.operations.generation import create_operational_task, secretary_assignee_id
from app.services.operations.redelivery import suppress_pending_redeliveries


def _task_assignee(db: Session) -> int | None:
    """Safe Secretary assignee for the projection task: only if the configured
    secretary user actually exists as an active human (else None → unassigned,
    still visible on the board). Never inserts a dangling FK."""
    candidate = secretary_assignee_id()
    if candidate is not None:
        from app.models.user import User

        user = db.get(User, candidate)
        if user is not None and user.is_active:
            return candidate
    return None

# The projection task's dedupe key (stable business identity for the work entry).
def requote_task_dedupe_key(repair_id: int) -> str:
    return f"repair-requote:{repair_id}"


def _unit_label(db: Session, repair: RepairOperation) -> str | None:
    if repair.unit_id is not None:
        unit = db.get(Unit, repair.unit_id)
        if unit is not None:
            return unit.unit_number
    if repair.property_id is not None:
        from app.models.property import Property

        prop = db.get(Property, repair.property_id)
        if prop is not None:
            return prop.name
    return None


def _money(value) -> str:
    try:
        d = round(float(value or 0), 2)
        return f"₱{d:,.2f}"
    except (TypeError, ValueError):
        return ""


def _rejected_quote_context(db: Session, repair: RepairOperation) -> dict | None:
    """The rejected quote details for the requote card (the latest REJECTED
    proposal's amount + reason)."""
    from app.models.repair import RepairProposal, RepairProposalStatus

    proposal = (
        db.query(RepairProposal)
        .filter(
            RepairProposal.repair_id == repair.id,
            RepairProposal.status == RepairProposalStatus.REJECTED,
        )
        .order_by(RepairProposal.version.desc())
        .first()
    )
    if proposal is None:
        return None
    return {
        "amount": _money(proposal.amount),
        "reason": proposal.rejection_reason,
        "version": proposal.version,
    }


def requote_card(
    db: Session, repair: RepairOperation, action: RepairAction
) -> tuple[str, str]:
    """Human task title + notification message for a requote (008A §3.1).

    English Secretary-facing; shows unit, issue, rejected amount + reason, the
    next step and that the repair remains open — never the internal ids.
    """
    unit = _unit_label(db, repair)
    title = f"Get another quote · {unit}" if unit else "Get another quote"
    ctx = _rejected_quote_context(db, repair)
    lines = [f"🔧 {repair.issue}"]
    if unit:
        lines.append(f"Unit / property: {unit}")
    if ctx is not None:
        lines.append(f"The {ctx['amount']} quote was rejected (V{ctx['version']}).")
        if ctx["reason"]:
            lines.append(f"Reason: {ctx['reason']}.")
    lines.append("")
    lines.append("Next: get another quote or propose an alternative.")
    lines.append("Repair remains open.")
    return title, "\n".join(lines)


def project_requote_to_task(
    db: Session,
    repair: RepairOperation,
    action: RepairAction,
    *,
    now: datetime | None = None,
    actor_id: int | None = None,
) -> tuple[OperationalTask | None, bool]:
    """Project an active REQUOTE repair_action into the Secretary's task queue.

    Idempotent: N calls create at most one active task (DB dedupe key). Returns
    ``(task_or_None_or_existing, created_flag)``. ``created_flag`` False when an
    active projection already exists.

    The task carries enough context that the Secretary never reads repair_action
    ids: unit, issue, rejected amount + reason, next step, repair still open.
    """
    now = now or datetime.now(timezone.utc)
    if action.action_kind != "REQUOTE":
        return None, False
    dedupe_key = requote_task_dedupe_key(repair.id)
    title, message = requote_card(db, repair, action)
    assignee = _task_assignee(db)
    fields = {
        "task_type": OperationalTaskType.FOLLOWUP,
        "title": title,
        "description": message,
        "property_id": repair.property_id,
        "source_type": "repair",
        "source_id": repair.id,
        "assigned_user_id": assignee,
        "priority": OperationalTaskPriority.high,
        "status": OperationalTaskStatus.PENDING,
        "due_at": now + timedelta(days=2),
        "next_action": "Get another quote / propose an alternative",
        "next_check_at": now + timedelta(days=2),
        "dedupe_key": dedupe_key,
        "details": {
            "repair_id": repair.id,
            "repair_action_id": action.id,
            "action_kind": action.action_kind,
            "unit_number": _unit_label(db, repair),
            "issue": repair.issue,
            "requote_context": _rejected_quote_context(db, repair),
        },
    }
    task, enqueued = create_operational_task(
        db,
        fields=fields,
        now=now,
        actor_id=actor_id,
        notification_message=message,
    )
    if task is None:
        # An active task with this dedupe_key already exists — refresh it so the
        # Secretary sees the current requote card.
        existing = (
            db.query(OperationalTask)
            .filter(
                OperationalTask.dedupe_key == dedupe_key,
                OperationalTask.status.in_([OperationalTaskStatus.PENDING, OperationalTaskStatus.IN_PROGRESS]),
            )
            .first()
        )
        return existing, False
    return task, enqueued and True


def close_requote_projection(
    db: Session,
    repair: RepairOperation,
    *,
    actor_id: int | None = None,
    now: datetime | None = None,
) -> int:
    """Complete the projected requote task(s) for a repair once the Secretary
    submits the next proposal (Gate B §3.3).

    Completing the task:
    - removes the "get another quote" entry from the Secretary work queue;
    - makes the notifier DROP any pending reminder for it (send-time guard),
      so it never re-reminds;
    - the underlying repair_action is completed separately by the continuation
      engine (or left for history); the source of truth is not deleted.
    Returns the number of tasks completed.
    """
    now = now or datetime.now(timezone.utc)
    tasks = (
        db.query(OperationalTask)
        .filter(
            OperationalTask.dedupe_key == requote_task_dedupe_key(repair.id),
            OperationalTask.status.in_(
                [OperationalTaskStatus.PENDING, OperationalTaskStatus.IN_PROGRESS]
            ),
        )
        .all()
    )
    for task in tasks:
        task.status = OperationalTaskStatus.COMPLETED
        task.completed_at = now
        task.completed_by = actor_id
        task.updated_at = now
        db.flush()
        suppress_pending_redeliveries(
            db, task.id, actor_id=actor_id, reason="next_proposal_submitted", now=now,
        )
    return len(tasks)


def close_result_projection(
    db: Session,
    repair: RepairOperation,
    *,
    actor_id: int | None = None,
    now: datetime | None = None,
) -> int:
    """Complete any projected repair-result / verification task when the Repair
    reaches CLOSED (so the work queue stops surfacing it)."""
    now = now or datetime.now(timezone.utc)
    actions = (
        db.query(RepairAction)
        .filter(
            RepairAction.repair_id == repair.id,
            RepairAction.status.in_(
                [RepairActionStatus.PENDING, RepairActionStatus.IN_PROGRESS]
            ),
        )
        .all()
    )
    for action in actions:
        projection_key = (
            requote_task_dedupe_key(repair.id)
            if action.action_kind == "REQUOTE"
            else f"repair-{action.action_kind.lower()}:{repair.id}"
        )
        tasks = (
            db.query(OperationalTask)
            .filter(
                OperationalTask.dedupe_key == projection_key,
                OperationalTask.status.in_(
                    [OperationalTaskStatus.PENDING, OperationalTaskStatus.IN_PROGRESS]
                ),
            )
            .all()
        )
        for task in tasks:
            task.status = OperationalTaskStatus.COMPLETED
            task.completed_at = now
            task.completed_by = actor_id
            task.updated_at = now
            db.flush()
            suppress_pending_redeliveries(
                db, task.id, actor_id=actor_id, reason="repair_closed", now=now,
            )
    return len(actions)
