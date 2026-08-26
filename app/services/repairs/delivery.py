"""REPAIR-AI-EMPLOYEE-WORKFLOW-008A — Repair Action → Secretary work-entry projection.

The authoritative business fact is the ``repair_action`` (requote requested after
a proposal rejection). For a REAL human to act without knowing repair/proposal
internals, the action is **projected into the existing ``operational_tasks``
work queue** assigned to the Secretary — the same Tasks / Telegram channel she
already uses. We do NOT create a second task system; ``operational_tasks`` is
the human work-entry projection, ``repair_actions`` is the source of truth.

Convergence boundary (PASAY-VNEXT-FOUNDATION-LEGACY-001): this module reads
``OperationalTask`` rows (back-compat / display) but NEVER creates or mutates
them. Both creation and active-projection completion are routed through
``app.services.operations.projection``. The adapter owns:
- validation (active RepairAction, same ``repair_id``);
- task creation through the canonical operational-task seam;
- the close seam (status + reminder_generation + audit + redelivery drop).

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

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.operations import (
    OperationalTask,
    OperationalTaskStatus,
)
from app.models.repair import (
    RepairAction,
    RepairActionStatus,
    RepairOperation,
)
from app.models.property import Unit
from app.services.operations import projection
from app.services.operations.generation import secretary_assignee_id


# Re-export so legacy callers (e.g. tests that ``monkeypatch.setattr`` this
# name on the delivery module) keep working — projection tasks resolve the
# assignee through this function.



_REPAIR_PROJECTION_DOMAIN = "repairs.delivery"

_TWO = Decimal("0.01")


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
        d = Decimal(str(value or 0)).quantize(_TWO)
        return f"₱{d:,.2f}"
    except (TypeError, ValueError):
        return ""


def _rejected_quote_context(db: Session, repair: RepairOperation) -> dict | None:
    """The rejected quote details for the requote card (latest REJECTED proposal)."""
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


def _requote_payload(db: Session, repair: RepairOperation, action: RepairAction) -> dict:
    """Build the canonical ``fields`` payload for the projection adapter.

    Title + description (Secretary-facing card text) + details context.
    Status / dedupe / source / priority / dates are set by the adapter; callers
    MAY supply a custom ``dedupe_key`` for non-REQUOTE kinds, otherwise the
    adapter computes one.
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
    description = "\n".join(lines)
    details = {
        "repair_id": repair.id,
        "repair_action_id": action.id,
        "action_kind": action.action_kind,
        "unit_number": unit,
        "issue": repair.issue,
        "requote_context": ctx,
    }
    return {
        "title": title,
        "description": description,
        "property_id": repair.property_id,
        "details": details,
        "dedupe_key": requote_task_dedupe_key(repair.id),
        "notification_message": description,
    }


def project_requote_to_task(
    db: Session,
    repair: RepairOperation,
    action: RepairAction,
    *,
    now: datetime | None = None,
    actor_id: int | None = None,
) -> tuple[OperationalTask | None, bool]:
    """Thin shim kept for back-compat: delegates the actual create /
    validation to the projection adapter. Returns ``(task_or_existing,
    created_flag)``; ``(None, False)`` for inactive / wrong-repair / already
    projected actions.

    The Secretary assignee is resolved HERE (against
    ``delivery.secretary_assignee_id``) so tests that monkeypatch this
    module-level name pick it up via the projection layer.
    """
    if action.action_kind != "REQUOTE":
        return None, False
    payload = _requote_payload(db, repair, action)
    payload["assigned_user_id"] = _resolve_assignee(db)
    return projection.project_active_repair_action(
        db,
        repair,
        action,
        fields=payload,
        actor_id=actor_id,
        now=now,
    )


def _resolve_assignee(db: Session) -> int | None:
    """Resolve the Secretary assignee through the module-level ``secretary_assignee_id``
    so ``monkeypatch.setattr`` on this module is honored by the projection
    pipeline."""
    candidate = secretary_assignee_id()
    if candidate is None:
        return None
    from app.models.user import User

    user = db.get(User, candidate)
    return candidate if (user is not None and user.is_active) else None


def _active_tasks_for(
    db: Session,
    *,
    dedupe_key: str,
    repair_id: int | None = None,
) -> list[OperationalTask]:
    """Read ACTIVE projected tasks for a dedupe key; filter to ``repair_id``
    when supplied (defense in depth against wrong-repair rows)."""
    tasks = (
        db.query(OperationalTask)
        .filter(
            OperationalTask.dedupe_key == dedupe_key,
            OperationalTask.status.in_(
                [OperationalTaskStatus.PENDING, OperationalTaskStatus.IN_PROGRESS]
            ),
        )
        .all()
    )
    if repair_id is None:
        return tasks
    out: list[OperationalTask] = []
    for task in tasks:
        details = task.details or {}
        if details.get("repair_id") in (None, repair_id):
            out.append(task)
    return out


def close_requote_projection(
    db: Session,
    repair: RepairOperation,
    *,
    actor_id: int | None = None,
    now: datetime | None = None,
) -> int:
    """Complete the projected requote task(s) once the Secretary submits the
    next proposal (Gate B §3.3). Routed through the projection adapter."""
    now = now or datetime.now(timezone.utc)
    tasks = _active_tasks_for(
        db, dedupe_key=requote_task_dedupe_key(repair.id), repair_id=repair.id
    )
    return projection.close_active_projections(
        db,
        tasks=tasks,
        actor_id=actor_id,
        reason="next_proposal_submitted",
        source_domain=_REPAIR_PROJECTION_DOMAIN,
        now=now,
    )


def close_result_projection(
    db: Session,
    repair: RepairOperation,
    *,
    actor_id: int | None = None,
    now: datetime | None = None,
) -> int:
    """Complete any projected result / verification task when the Repair
    reaches CLOSED. Wrong-repair tasks are FILTERED OUT of the close call
    (not flagged and completed) so they stay ACTIVE for the right repair.
    """
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
    closed_total = 0
    for action in actions:
        projection_key = (
            requote_task_dedupe_key(repair.id)
            if action.action_kind == "REQUOTE"
            else f"repair-{action.action_kind.lower()}:{repair.id}"
        )
        valid_tasks = _active_tasks_for(
            db, dedupe_key=projection_key, repair_id=repair.id
        )
        if not valid_tasks:
            continue
        closed_total += projection.close_active_projections(
            db,
            tasks=valid_tasks,
            actor_id=actor_id,
            reason="repair_closed",
            source_domain=_REPAIR_PROJECTION_DOMAIN,
            now=now,
        )
    return closed_total
