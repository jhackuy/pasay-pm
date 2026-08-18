"""REPAIR-AI-EMPLOYEE-WORKFLOW-008A — Repair Operation core service.

Create/fetch repair Operations and compute the derived AI-employee state
(``next_action`` / ``waiting_on`` / ``blocked_reason``). The Repair Operation
must answer, for both Telegram and the Mini App:

- What happened?            (status + timeline)
- Where is it stuck?        (blocked_reason)
- Who are we waiting on?    (waiting_on)
- What is the next step?    (next_action)
- What has the AI already done?   (repair_actions)
- What must a human do now? (next active repair_action owner)

All derived state is stored on the row (never only in Telegram copy) so the
Mini App and bot read the same real business status.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.repair import (
    RepairAction,
    RepairActionStatus,
    RepairOperation,
    RepairOperationStatus,
)
from app.models.user import User
from app.services.repairs.state import default_waiting_on

# Human-facing next-action text keyed off the derived state.
_NEXT_ACTION_BY_STATUS = {
    RepairOperationStatus.OPEN: "Awaiting a solution proposal or a work plan.",
    RepairOperationStatus.IN_PROGRESS: "Repair work is underway; wait to verify the outcome.",
    RepairOperationStatus.WAITING_HUMAN: "A human must take the next step now.",
    RepairOperationStatus.WAITING_VENDOR: "Waiting on the vendor to respond / deliver a quote.",
    RepairOperationStatus.WAITING_APPROVAL: "A quote is awaiting owner approval.",
    RepairOperationStatus.WAITING_PAYMENT: "An approved quote is awaiting payment.",
    RepairOperationStatus.VERIFYING: "The repair needs real-world verification before closing.",
    RepairOperationStatus.CLOSED: "Repair closed.",
    RepairOperationStatus.CANCELLED: "Repair cancelled.",
}


def create_repair(
    db: Session,
    *,
    issue: str,
    issue_description: str | None = None,
    merchant_id: int | None = None,
    property_id: int | None = None,
    unit_id: int | None = None,
    created_source: str = "manual",
    reported_by: int | None = None,
    assignee_user_id: int | None = None,
    closure_criteria: str | None = None,
    operational_task_id: int | None = None,
    details: dict | None = None,
    now: datetime | None = None,
) -> RepairOperation:
    """Create a new OPEN Repair Operation (the REAL-world problem)."""
    now = now or datetime.now(timezone.utc)
    op = RepairOperation(
        merchant_id=merchant_id,
        property_id=property_id,
        unit_id=unit_id,
        issue=issue,
        issue_description=issue_description,
        created_source=created_source,
        reported_by=reported_by,
        assignee_user_id=assignee_user_id,
        status=RepairOperationStatus.OPEN,
        closure_criteria=closure_criteria,
        operational_task_id=operational_task_id,
        details=details or {},
        created_by=reported_by,
    )
    db.add(op)
    db.flush()
    _apply_derived_state(op)
    db.flush()
    return op


def get_repair(db: Session, repair_id: int) -> RepairOperation | None:
    return db.get(RepairOperation, repair_id)


def get_repair_or_raise(db: Session, repair_id: int) -> RepairOperation:
    op = get_repair(db, repair_id)
    if op is None:
        raise KeyError(f"Repair operation {repair_id} does not exist")
    return op


def set_status(
    op: RepairOperation,
    new_status: RepairOperationStatus,
    *,
    now: datetime | None = None,
    reason: str | None = None,
) -> str:
    """Apply a validated transition to the row (service-level)."""
    from app.services.repairs.state import transition_to

    now = now or datetime.now(timezone.utc)
    event, _next = transition_to(
        op.status.value, new_status.value, reason=reason, now=now
    )
    op.status = new_status
    op.updated_at = now
    _apply_derived_state(op)
    return event


def _apply_derived_state(op: RepairOperation) -> None:
    """(Re)derive next_action / waiting_on / blocked_reason from status."""
    status = op.status
    if isinstance(status, str):
        status = RepairOperationStatus(status)
    op.next_action = _NEXT_ACTION_BY_STATUS.get(status, "Unknown.")
    op.waiting_on = op.waiting_on or default_waiting_on(status.value)
    if status == RepairOperationStatus.VERIFYING:
        op.next_action = (
            "The repair needs real verification: confirm it is actually fixed "
            "before it can be closed."
        )
        op.blocked_reason = "Closure requires real-world verification of the outcome."


def active_action(db: Session, repair_id: int) -> RepairAction | None:
    """The single ACTIVE human action for a repair (the thing a human must do
    now). Dedup guarantees at most one per dedupe_key. Returns the first."""
    return (
        db.query(RepairAction)
        .filter(
            RepairAction.repair_id == repair_id,
            RepairAction.status.in_(
                [RepairActionStatus.PENDING, RepairActionStatus.IN_PROGRESS]
            ),
        )
        .order_by(RepairAction.id.asc())
        .first()
    )


def assignee_email_name(db: Session, user_id: int | None) -> str | None:
    if user_id is None:
        return None
    user = db.get(User, user_id)
    return user.username if user else None


def cancel_repair(
    op: RepairOperation,
    *,
    actor_id: int | None = None,
    reason: str | None = None,
    now: datetime | None = None,
) -> str:
    """State-machine-validated cancel of a Repair Operation.

    Conformance: the only canonical way for callers (routers, tests, scripts)
    to cancel a Repair. Routers MUST route through here so the explicit
    state-machine guard runs; raw ``op.status = CANCELLED`` writes are no
    longer required (the static guard forbids them in canonical modules).

    Returns the audit action label (e.g. ``"cancelled"``) so callers can
    record the same event in their audit log without re-deriving it.
    """
    from app.services.repairs.state import transition_to

    now = now or datetime.now(timezone.utc)
    event, _next = transition_to(
        op.status.value,
        RepairOperationStatus.CANCELLED.value,
        reason=reason,
        now=now,
    )
    op.status = RepairOperationStatus.CANCELLED
    op.next_action = "Repair cancelled."
    op.waiting_on = None
    op.updated_at = now
    op.updated_by = actor_id
    _apply_derived_state(op)
    return event
