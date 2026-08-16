"""Operations summary (V1.2) — shared by /operations/summary and the Copilot
context builder so both always use the same counting semantics.

- Agents are scoped to their own assigned tasks (mirrors the router's
  ``_agent_scope``).
- Snoozed tasks count toward ``pending_total`` but are skipped from the
  overdue / due buckets while ``snoozed_until`` is in the future.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from sqlalchemy.orm import Session

from app.models.operations import OperationalTask, OperationalTaskStatus
from app.models.user import User, UserRole
from app.schemas.operations import OperationsSummary


def build_operations_summary(
    db: Session, user: User, *, now: datetime | None = None,
    owner_only: bool = False,
) -> OperationsSummary:
    """Count pending operational tasks visible to ``user`` at ``now``.

    ``owner_only=True`` applies the AI-OPS-FOUNDATION-001 §5 Owner attention
    filter (approvals, Owner payments, decisions, escalations only)."""
    query = db.query(OperationalTask).filter(
        OperationalTask.status == OperationalTaskStatus.PENDING
    )
    if user.role == UserRole.agent:
        query = query.filter(OperationalTask.assigned_user_id == user.id)
    tasks = query.all()
    if owner_only:
        from app.services.operations.owner_scope import is_owner_actionable

        tasks = [t for t in tasks if is_owner_actionable(t, user)]
    now = now or datetime.now(timezone.utc)
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
