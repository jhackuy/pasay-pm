"""Operations summary (V1.2) — shared by /operations/summary and the Copilot
context builder so both always use the same counting semantics.

- Agents are scoped to their own assigned tasks (mirrors the router's
  ``_agent_scope``).
- Snoozed tasks count toward ``pending_total`` but are skipped from the
  overdue / due buckets while ``snoozed_until`` is in the future.
- Organization scope (``org_id``): non-agent callers only see tasks linked
  to the caller's organization via property / lease / tenant 3-channel OR,
  following the canonical ``_scoped_task_query`` pattern in operations.py.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.lease import Lease
from app.models.operations import OperationalTask, OperationalTaskStatus
from app.models.property import Property, Unit
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.operations import OperationsSummary


def _org_property_ids(db: Session, org_id: int) -> set[int]:
    rows = db.execute(
        select(Property.id).where(
            Property.organization_id == org_id,
            Property.deleted_at.is_(None),
        )
    ).all()
    return {r[0] for r in rows}


def _org_lease_ids(db: Session, org_id: int) -> set[int]:
    rows = db.execute(
        select(Lease.id)
        .join(Unit, Unit.id == Lease.unit_id)
        .join(Property, Property.id == Unit.property_id)
        .where(
            Property.organization_id == org_id,
            Lease.deleted_at.is_(None),
        )
    ).all()
    return {r[0] for r in rows}


def _org_tenant_ids(db: Session, org_id: int) -> set[int]:
    rows = db.execute(
        select(Tenant.id).where(
            Tenant.organization_id == org_id,
            Tenant.deleted_at.is_(None),
        )
    ).all()
    return {r[0] for r in rows}


def _scoped_task_query(db: Session, org_id: int):
    pids = _org_property_ids(db, org_id)
    lids = _org_lease_ids(db, org_id)
    tids = _org_tenant_ids(db, org_id)
    if not pids and not lids and not tids:
        return OperationalTask.id == -1
    or_terms = []
    if pids:
        or_terms.append(OperationalTask.property_id.in_(list(pids)))
    if lids:
        or_terms.append(OperationalTask.lease_id.in_(list(lids)))
    if tids:
        or_terms.append(OperationalTask.tenant_id.in_(list(tids)))
    return or_(*or_terms)


def build_operations_summary(
    db: Session, user: User, *, now: datetime | None = None,
    owner_only: bool = False,
    org_id: int | None = None,
) -> OperationsSummary:
    """Count pending operational tasks visible to ``user`` at ``now``.

    ``owner_only=True`` applies the AI-OPS-FOUNDATION-001 §5 Owner attention
    filter (approvals, Owner payments, decisions, escalations only).
    ``org_id`` fail-closes the payload to the resolved organization scope
    when the caller is not a SystemReader / agent; agents are still bounded
    by the ``assigned_user_id`` self-scope.
    """
    query = db.query(OperationalTask).filter(
        OperationalTask.status == OperationalTaskStatus.PENDING
    )
    if user.role == UserRole.agent:
        query = query.filter(OperationalTask.assigned_user_id == user.id)
    elif org_id is not None:
        query = query.filter(_scoped_task_query(db, org_id))
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
