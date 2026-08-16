"""AI-OPS-FOUNDATION-001 §5: Owner attention filter ("Needs You" / 需要您处理).

Owner Tasks must contain ONLY items requiring Owner action:
approval, payment when the Owner is the actual payer, business decisions,
high-risk / SLA escalation and exceptional issues. Routine operational work
(rent collection, lease follow-up, repair execution) never lands in the
Owner queue — the Secretary owns it.

The rule is deterministic and shared by every owner-scoped read path
(/operations/tasks?scope=owner, quick tasks, operations summary) so the
filter cannot drift between endpoints.
"""
from __future__ import annotations

from app.models.operations import (
    OperationalTask,
    OperationalTaskType,
)
from app.models.user import User


def task_escalation(task: OperationalTask) -> dict:
    """Structured escalation state stored on the task details.

    Shape: ``{"level": "none"|"owner", "reason": str, "at": iso}``. Kept in
    JSONB so no schema migration is needed to escalate a task.
    """
    details = task.details or {}
    escalation = details.get("escalation") or {}
    if not isinstance(escalation, dict):
        return {"level": "none"}
    return escalation


def is_owner_actionable(task: OperationalTask, user: User) -> bool:
    """True when this ACTIVE task belongs in the Owner's Needs-You queue."""
    if task_escalation(task).get("level") == "owner":
        return True
    if task.task_type == OperationalTaskType.APPROVAL_PENDING:
        # Approval is always the Owner's job (the approver).
        return True
    if task.task_type in (
        OperationalTaskType.PAYMENT_PENDING,
        OperationalTaskType.FOLLOWUP,
    ):
        # Payment/decision belongs to the ACTUAL payer/assignee — only the
        # Owner's own rows appear in the Owner queue.
        return task.assigned_user_id == user.id
    return False
