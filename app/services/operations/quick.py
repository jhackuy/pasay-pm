"""PASAY-V2-FOUNDATION-001 deterministic Quick Views + Daily Digest.

One query set per view, no LLM, no writes. The shapes are the shared
contract for the bot's Quick View cards (``cards.properties_quick_card``,
``tasks_quick_card``, ``rent_quick_card``, ``expense_quick_card``,
``active_tasks_digest_card``). Rent period semantics are copied from the
reports router so the Quick View never disagrees with /overdue-rents.
"""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.financial import Expense, ExpenseStatus, Income, IncomeStatus
from app.models.lease import Lease, LeaseStatus
from app.models.operations import (
    OperationalTask,
    OperationalTaskStatus,
    OperationalTaskType,
)
from app.models.property import Property, Unit, UnitStatus
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.dates import month_range

_TWO_PLACES = Decimal("0.01")
_PERIOD_IN_DESC = re.compile(r"(?<!\d)(\d{4})(?:[-/.])?(\d{1,2})(?!\d)")


def _d2(value) -> Decimal:
    return Decimal(value).quantize(_TWO_PLACES)


def _default_due_day(lease: Lease) -> int:
    return lease.due_day if lease.due_day is not None else lease.start_date.day


def _accounting_start(lease: Lease) -> date:
    if lease.accounting_start_date is None:
        return lease.start_date
    return max(lease.start_date, lease.accounting_start_date)


def _lease_periods(lease: Lease) -> list[tuple[str, date]]:
    """(YYYY-MM, due_date) for every full rent month of the lease."""
    due_day = _default_due_day(lease)
    periods: list[tuple[str, date]] = []
    accounting_start = _accounting_start(lease)
    year, month = accounting_start.year, accounting_start.month
    end_year, end_month = lease.end_date.year, lease.end_date.month
    end_is_fully_covered = (
        lease.end_date.day >= calendar.monthrange(end_year, end_month)[1]
    )
    while (year, month) <= (end_year, end_month):
        if (year, month) == (end_year, end_month) and not end_is_fully_covered:
            break
        day = min(due_day, calendar.monthrange(year, month)[1])
        periods.append((f"{year:04d}-{month:02d}", date(year, month, day)))
        month += 1
        if month > 12:
            month = 1
            year += 1
    return periods


def _month_from_description(description: str | None) -> str | None:
    if not description:
        return None
    match = _PERIOD_IN_DESC.search(description)
    if match is None:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        return None
    return f"{year:04d}-{month:02d}"


def _covered_periods(
    lease: Lease,
    lease_periods: list[tuple[str, date]],
    incomes: list[Income],
) -> set[str]:
    receivable = {month for month, _ in lease_periods}
    covered: set[str] = set()
    for income in incomes:
        month = _month_from_description(income.description)
        if month is None:
            month = income.received_date.strftime("%Y-%m")
        if month in receivable:
            covered.add(month)
    return covered


def _short_property_code(prop: Property) -> str:
    """Deterministic short prefix for duplicate unit numbers (e.g. BAY-1680)."""
    token = re.sub(r"[^A-Za-z0-9]", "", (prop.name or "").split()[0] if prop.name else "")
    return (token[:4] or str(prop.id)).upper()


def _unit_label(db: Session, unit: Unit | None) -> str | None:
    """Unit code, with a property prefix ONLY when the unit number repeats."""
    if unit is None:
        return None
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
    prop = db.query(Property).filter(Property.id == unit.property_id).first()
    return f"{_short_property_code(prop) if prop else unit.property_id}-{unit.unit_number}"


def _agent_scope(query, user: User):
    if user.role == UserRole.agent:
        return query.filter(OperationalTask.assigned_user_id == user.id)
    return query


def _task_row(db: Session, task: OperationalTask, unit_number_by_lease: dict[int, str]) -> dict:
    """One active-task row in the shape the bot cards expect."""
    unit = unit_number_by_lease.get(task.lease_id)
    return {
        "id": task.id,
        "task_type": task.task_type.value,
        "title": task.title,
        "status": task.status.value,
        "property_code": unit or (task.details or {}).get("unit_number"),
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "next_action": task.next_action,
        "next_check_at": task.next_check_at.isoformat() if task.next_check_at else None,
        "completion_condition": task.completion_condition,
        "context": task.context,
        "source_event": task.source_event,
    }


def build_quick_tasks(db: Session, user: User, *, now: datetime | None = None) -> list[dict]:
    """Active tasks (PENDING + IN_PROGRESS) for the caller's scope."""
    now = now or datetime.now(timezone.utc)
    query = _agent_scope(db.query(OperationalTask), user).filter(
        OperationalTask.status.in_(
            [OperationalTaskStatus.PENDING, OperationalTaskStatus.IN_PROGRESS]
        )
    )
    tasks = query.order_by(OperationalTask.due_at, OperationalTask.id).all()
    lease_ids = {t.lease_id for t in tasks if t.lease_id is not None}
    unit_number_by_lease: dict[int, str] = {}
    if lease_ids:
        leases = db.query(Lease).filter(Lease.id.in_(lease_ids)).all()
        units = {
            u.id: u
            for u in db.query(Unit)
            .filter(Unit.id.in_([l.unit_id for l in leases]))
            .all()
        }
        for lease in leases:
            unit = units.get(lease.unit_id)
            label = _unit_label(db, unit)
            if label:
                unit_number_by_lease[lease.id] = label
    rows = [_task_row(db, t, unit_number_by_lease) for t in tasks]
    for row in rows:
        due = row.get("due_at")
        if due:
            due_dt = datetime.fromisoformat(due)
            row["overdue_days"] = max((now - due_dt).days, 0) if due_dt < now else None
            row["due_in_days"] = (due_dt - now).days if due_dt >= now else None
    return rows


def build_quick_properties(db: Session, *, now: datetime | None = None) -> list[dict]:
    """One row per active unit: abnormal first (overdue > expiring > vacant)."""
    now = now or datetime.now(timezone.utc)
    today = now.date()
    units = (
        db.query(Unit)
        .filter(Unit.deleted_at.is_(None), Unit.is_active.is_(True))
        .order_by(Unit.property_id, Unit.unit_number)
        .all()
    )
    unit_ids = [u.id for u in units]
    leases = (
        db.query(Lease)
        .filter(
            Lease.status == LeaseStatus.active,
            Lease.deleted_at.is_(None),
            Lease.unit_id.in_(unit_ids) if unit_ids else False,
        )
        .all()
    )
    lease_by_unit: dict[int, Lease] = {lease.unit_id: lease for lease in leases}
    confirmed_by_lease: dict[int, list[Income]] = {}
    if leases:
        for income in (
            db.query(Income)
            .filter(
                Income.lease_id.in_([l.id for l in leases]),
                Income.status == IncomeStatus.confirmed,
            )
            .all()
        ):
            confirmed_by_lease.setdefault(income.lease_id, []).append(income)

    rows: list[dict] = []
    for unit in units:
        label = _unit_label(db, unit) or unit.unit_number
        lease = lease_by_unit.get(unit.id)
        if lease is None:
            status = "vacant" if unit.status == UnitStatus.vacant else "normal"
            rows.append({"unit_code": label, "status": status, "amount": None, "days": None})
            continue
        periods = _lease_periods(lease)
        due_periods = [(m, due) for m, due in periods if due <= today]
        covered = _covered_periods(lease, periods, confirmed_by_lease.get(lease.id, []))
        overdue = [(m, due) for m, due in due_periods if m not in covered]
        if overdue:
            oldest_due = overdue[0][1]
            rows.append(
                {
                    "unit_code": label,
                    "status": "overdue_rent",
                    "amount": _d2(lease.monthly_rent) * len(overdue),
                    "days": max((today - oldest_due).days, 0),
                }
            )
            continue
        days_to_end = (lease.end_date - today).days
        if 0 <= days_to_end <= 30:
            rows.append(
                {
                    "unit_code": label,
                    "status": "lease_expiring",
                    "amount": None,
                    "days": days_to_end,
                }
            )
            continue
        rows.append(
            {
                "unit_code": label,
                "status": "paid",
                "amount": None,
                "days": None,
            }
        )
    order = {"overdue_rent": 0, "lease_expiring": 1, "vacant": 2, "normal": 3, "paid": 4}
    rows.sort(key=lambda r: (order.get(r["status"], 9), r["unit_code"]))
    return rows


def build_quick_rent(db: Session, *, now: datetime | None = None) -> dict:
    """Overdue units + outstanding total (same semantics as /overdue-rents)."""
    now = now or datetime.now(timezone.utc)
    today = now.date()
    leases = (
        db.query(Lease)
        .filter(Lease.status == LeaseStatus.active, Lease.deleted_at.is_(None))
        .all()
    )
    confirmed_by_lease: dict[int, list[Income]] = {}
    if leases:
        for income in (
            db.query(Income)
            .filter(
                Income.lease_id.in_([l.id for l in leases]),
                Income.status == IncomeStatus.confirmed,
            )
            .all()
        ):
            confirmed_by_lease.setdefault(income.lease_id, []).append(income)
    units = {
        u.id: u
        for u in db.query(Unit)
        .filter(Unit.id.in_([l.unit_id for l in leases]))
        .all()
    }
    overdue_rows: list[dict] = []
    outstanding = Decimal("0.00")
    for lease in leases:
        unit = units.get(lease.unit_id)
        if unit is None:
            continue
        periods = _lease_periods(lease)
        due_periods = [(m, due) for m, due in periods if due <= today]
        covered = _covered_periods(lease, periods, confirmed_by_lease.get(lease.id, []))
        overdue = [(m, due) for m, due in due_periods if m not in covered]
        if not overdue:
            continue
        total = _d2(lease.monthly_rent) * len(overdue)
        outstanding += total
        oldest_due = overdue[0][1]
        overdue_rows.append(
            {
                "unit": _unit_label(db, unit) or unit.unit_number,
                "unit_code": _unit_label(db, unit) or unit.unit_number,
                "amount": total,
                "overdue_days": max((today - oldest_due).days, 0),
            }
        )
    overdue_rows.sort(key=lambda r: r["overdue_days"], reverse=True)
    return {"overdue": overdue_rows, "outstanding_total": outstanding}


def build_quick_expense(db: Session, *, now: datetime | None = None) -> dict:
    """Current-month spend + pending approval + unresolved expense tasks."""
    now = now or datetime.now(timezone.utc)
    start, end = month_range(now.date().strftime("%Y-%m"))
    month_total = Decimal("0.00")
    for amount, in (
        db.query(Expense.amount)
        .filter(
            Expense.status.in_([ExpenseStatus.approved, ExpenseStatus.paid]),
            Expense.expense_date >= start,
            Expense.expense_date <= end,
        )
        .all()
    ):
        month_total += _d2(amount)
    pending_rows = (
        db.query(Expense)
        .filter(Expense.status == ExpenseStatus.pending)
        .order_by(Expense.expense_date)
        .all()
    )
    pending_amount = sum((_d2(e.amount) for e in pending_rows), Decimal("0.00"))
    unresolved = (
        db.query(OperationalTask)
        .filter(
            OperationalTask.task_type.in_(
                [OperationalTaskType.APPROVAL_PENDING, OperationalTaskType.PAYMENT_PENDING]
            ),
            OperationalTask.status.in_(
                [OperationalTaskStatus.PENDING, OperationalTaskStatus.IN_PROGRESS]
            ),
        )
        .order_by(OperationalTask.due_at)
        .all()
    )
    unit_number_by_lease: dict[int, str] = {}
    # P1-EXPENSE-QUICKVIEW-LIST-001: this month's real expense records so the
    # quick view shows actual spend, not only unresolved items.
    # PENDING/APPROVED/PAID are real spend; REJECTED (cancelled) and REVERSED
    # records are not normal expenses and never appear. The month_total
    # semantics (approved + paid) are unchanged.
    month_records = (
        db.query(Expense)
        .filter(
            Expense.status.in_(
                [ExpenseStatus.pending, ExpenseStatus.approved, ExpenseStatus.paid]
            ),
            Expense.expense_date >= start,
            Expense.expense_date <= end,
        )
        .order_by(Expense.expense_date.desc(), Expense.id.desc())
        .limit(20)
        .all()
    )
    unit_ids = {e.unit_id for e in month_records if e.unit_id is not None}
    units = {}
    if unit_ids:
        units = {
            u.id: u
            for u in db.query(Unit).filter(Unit.id.in_(unit_ids)).all()
        }
    label_by_unit = {
        u.id: (_unit_label(db, u) or u.unit_number)
        for u in units.values()
    }
    records = [
        {
            "unit": label_by_unit.get(e.unit_id, ""),
            "unit_code": label_by_unit.get(e.unit_id, ""),
            "purpose": e.category,
            "amount": _d2(e.amount),
            "expense_date": e.expense_date.isoformat(),
            "status": e.status.value,
        }
        for e in month_records
    ]
    return {
        "month_total": month_total,
        "pending_approval_count": len(pending_rows),
        "pending_approval_amount": pending_amount,
        "unresolved_expense_tasks": [
            _task_row(db, t, unit_number_by_lease) for t in unresolved
        ],
        "records": records,
    }


def build_digest(db: Session, user: User, *, now: datetime | None = None) -> dict:
    """Daily Active Tasks Digest: pending / in_progress / recently_completed."""
    now = now or datetime.now(timezone.utc)
    query = _agent_scope(db.query(OperationalTask), user)
    pending = (
        query.filter(OperationalTask.status == OperationalTaskStatus.PENDING)
        .order_by(OperationalTask.due_at)
        .all()
    )
    in_progress = (
        query.filter(OperationalTask.status == OperationalTaskStatus.IN_PROGRESS)
        .order_by(OperationalTask.next_check_at, OperationalTask.due_at)
        .all()
    )
    recently = (
        query.filter(
            OperationalTask.status == OperationalTaskStatus.COMPLETED,
            OperationalTask.completed_at >= now - timedelta(days=1),
        )
        .order_by(OperationalTask.completed_at.desc())
        .limit(20)
        .all()
    )
    lease_ids = {
        t.lease_id
        for t in list(pending) + list(in_progress) + list(recently)
        if t.lease_id is not None
    }
    unit_number_by_lease: dict[int, str] = {}
    if lease_ids:
        leases = db.query(Lease).filter(Lease.id.in_(lease_ids)).all()
        units = {
            u.id: u
            for u in db.query(Unit)
            .filter(Unit.id.in_([l.unit_id for l in leases]))
            .all()
        }
        for lease in leases:
            unit = units.get(lease.unit_id)
            label = _unit_label(db, unit)
            if label:
                unit_number_by_lease[lease.id] = label
    return {
        "pending": [_task_row(db, t, unit_number_by_lease) for t in pending],
        "in_progress": [_task_row(db, t, unit_number_by_lease) for t in in_progress],
        "recently_completed": [
            _task_row(db, t, unit_number_by_lease) for t in recently
        ],
    }
