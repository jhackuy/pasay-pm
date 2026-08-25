"""AI-OPS-FOUNDATION-001 §19: deterministic exception detection hooks.

Simple, reliable proactive checks that feed the Owner's WARNING lane (the
Owner attention filter) without overengineering:

- repeated_repair: the same unit had >= 3 repairs (AC_MAINTENANCE tasks)
  within the window -> likely recurring failure.
- long_vacancy: a unit is VACANT and has had no active lease for >= 60 days.
- occupied_missing_lease: a unit is OCCUPIED but has no active lease.
- unusual_expense: an expense amount is >= 2x the unit's monthly rent.

Every finding is deduped per (kind, unit, date) so a daily scan can never
spam duplicates. Findings go to the OWNER (WARNING / exceptional issue),
never to the Secretary's routine queue.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models.financial import Expense, ExpenseStatus
from app.models.lease import Lease, LeaseStatus
from app.models.operations import OperationalTask, OperationalTaskStatus, OperationalTaskType
from app.models.property import Unit, UnitStatus
from app.services.operations.config import DEFAULT_ASSIGNED_USER_ID, NOTIFY_CHANNEL_TELEGRAM
from app.services.operations.outbox import enqueue_notification, resolve_recipient

logger = logging.getLogger(__name__)

REPEATED_REPAIR_MIN = 3
REPEATED_REPAIR_WINDOW_DAYS = 90
LONG_VACANCY_DAYS = 60
UNUSUAL_EXPENSE_MULTIPLIER = 2


def scan_exceptions(
    db: Session, *, now: datetime | None = None,
    org_id: int | None = None,
) -> list[dict]:
    """One deterministic scan pass; returns the findings list and enqueues
    Owner notifications (deduped per kind+unit+date).

    ``org_id`` fail-closes the unit/lease/task scan via canonical
    property→organization scoping; None preserves the global standalone-worker
    behavior (daemon owns all tenants).
    """
    now = now or datetime.now(timezone.utc)
    findings: list[dict] = []
    today = now.date().isoformat()

    unit_query = db.query(Unit).filter(Unit.deleted_at.is_(None))
    if org_id is not None:
        from app.services.operations.summary import _org_property_ids
        pids = _org_property_ids(db, org_id)
        if pids:
            unit_query = unit_query.filter(Unit.property_id.in_(list(pids)))
        else:
            unit_query = unit_query.filter(Unit.id == -1)
    units = unit_query.all()
    unit_by_id = {u.id: u for u in units}
    lease_query = db.query(Lease).filter(Lease.deleted_at.is_(None))
    if org_id is not None:
        from app.services.operations.summary import _org_lease_ids
        lids = _org_lease_ids(db, org_id)
        if lids:
            lease_query = lease_query.filter(Lease.id.in_(list(lids)))
        else:
            lease_query = lease_query.filter(Lease.id == -1)
    leases = lease_query.all()
    active_leases_by_unit: dict[int, list[Lease]] = {}
    for lease in leases:
        if lease.status == LeaseStatus.active:
            active_leases_by_unit.setdefault(lease.unit_id, []).append(lease)

    # --- repeated repair -----------------------------------------------------
    window_start = now - timedelta(days=REPEATED_REPAIR_WINDOW_DAYS)
    repair_tasks = (
        db.query(OperationalTask)
        .filter(
            OperationalTask.task_type == OperationalTaskType.AC_MAINTENANCE,
            OperationalTask.created_at >= window_start,
        )
        .all()
    )
    count_by_unit: dict[int, int] = {}
    for task in repair_tasks:
        if task.lease_id is not None:
            # lease_id -> unit via lease map
            for lease in leases:
                if lease.id == task.lease_id:
                    count_by_unit[lease.unit_id] = count_by_unit.get(lease.unit_id, 0) + 1
                    break
    for unit_id, count in count_by_unit.items():
        if count >= REPEATED_REPAIR_MIN:
            unit = unit_by_id.get(unit_id)
            findings.append({
                "kind": "repeated_repair",
                "unit_id": unit_id,
                "unit_number": unit.unit_number if unit else str(unit_id),
                "message": (
                    f"⚠️ 重复维修 / Repeated repair: Unit {unit.unit_number if unit else unit_id} "
                    f"had {count} repairs in {REPEATED_REPAIR_WINDOW_DAYS} days."
                ),
                "dedupe": f"exception:repeated_repair:{unit_id}:{today}",
            })

    # --- long vacancy / occupied-missing-lease --------------------------------
    for unit in units:
        has_active = bool(active_leases_by_unit.get(unit.id))
        if unit.status == UnitStatus.vacant and not has_active:
            # longest previous lease end (or unit created_at) = vacancy start
            unit_leases = [l for l in leases if l.unit_id == unit.id]
            last_end = max((l.end_date for l in unit_leases), default=None)
            reference = datetime.combine(last_end, datetime.min.time(), tzinfo=now.tzinfo) if last_end else (unit.created_at or now)
            vacant_days = (now - reference).days
            if vacant_days >= LONG_VACANCY_DAYS:
                findings.append({
                    "kind": "long_vacancy",
                    "unit_id": unit.id,
                    "unit_number": unit.unit_number,
                    "message": (
                        f"⚠️ 长期空置 / Long vacancy: Unit {unit.unit_number} "
                        f"vacant for {vacant_days} days."
                    ),
                    "dedupe": f"exception:long_vacancy:{unit.id}:{today}",
                })
        if unit.status == UnitStatus.occupied and not has_active:
            findings.append({
                "kind": "occupied_missing_lease",
                "unit_id": unit.id,
                "unit_number": unit.unit_number,
                "message": (
                    f"⚠️ 已住但无租约 / Occupied without lease: Unit {unit.unit_number}."
                ),
                "dedupe": f"exception:occupied_missing_lease:{unit.id}:{today}",
            })

    # --- unusual expense ------------------------------------------------------
    for expense in db.query(Expense).filter(Expense.status.in_(
        [ExpenseStatus.approved, ExpenseStatus.paid]
    )).all():
        if expense.unit_id is None:
            continue
        unit = unit_by_id.get(expense.unit_id)
        if unit is None or not unit.monthly_rent:
            continue
        if expense.amount >= UNUSUAL_EXPENSE_MULTIPLIER * unit.monthly_rent:
            findings.append({
                "kind": "unusual_expense",
                "unit_id": unit.id,
                "unit_number": unit.unit_number,
                "message": (
                    f"⚠️ 异常支出 / Unusual expense: Unit {unit.unit_number} "
                    f"₱{expense.amount:.2f} (≥2x rent)."
                ),
                "dedupe": f"exception:unusual_expense:{expense.id}:{today}",
            })

    _enqueue_findings(db, findings)
    return findings


def _enqueue_findings(db: Session, findings: list[dict]) -> None:
    """Enqueue one WARNING notification per finding to the OWNER (deduped)."""
    owner_id = DEFAULT_ASSIGNED_USER_ID
    if owner_id is None:
        return
    try:
        recipient = resolve_recipient(db, owner_id)
    except LookupError:
        return
    if recipient is None:
        return
    for finding in findings:
        enqueue_notification(
            db,
            task_id=None,
            channel=NOTIFY_CHANNEL_TELEGRAM,
            recipient=recipient,
            payload={"message": finding["message"]},
            dedupe_key=finding["dedupe"],
        )
