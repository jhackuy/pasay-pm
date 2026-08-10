"""High-level reports: server-computed aggregates for Hermes (no client-side math)."""
import calendar
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps import manager_or_admin
from app.database import get_db
from app.models.commission import CommissionRule, CommissionSettlement
from app.models.financial import Expense, ExpenseStatus, Income, IncomeStatus
from app.models.lease import Lease, LeaseStatus
from app.models.property import Unit, UnitStatus
from app.models.task import Task, TaskStatus
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.reports import (
    CommissionSummaryRow,
    ExpenseCategoryRow,
    ExpenseSummary,
    ExpenseUnitRow,
    FinancialSummary,
    MonthlyLeaseSummary,
    OverdueRent,
    ReportTask,
)
from app.services.dates import add_months, month_range

router = APIRouter(prefix="/reports", tags=["reports"])

MONTH_PATTERN = r"^\d{4}-\d{2}$"
_TWO_PLACES = Decimal("0.01")


def _d2(value) -> Decimal:
    """Normalize an aggregate amount to NUMERIC(14,2) scale."""
    return Decimal(value).quantize(_TWO_PLACES)


def _current_month() -> str:
    return date.today().strftime("%Y-%m")


def _resolve_month(month: str | None) -> str:
    return month or _current_month()


def _default_due_day(lease: Lease) -> int:
    return lease.due_day if lease.due_day is not None else lease.start_date.day


def _months_expected(lease: Lease, today: date) -> int:
    """Monthly rent periods whose due day has passed since the lease started."""
    elapsed = (
        (today.year - lease.start_date.year) * 12
        + (today.month - lease.start_date.month)
    )
    if elapsed < 0:
        return 0
    due_day = _default_due_day(lease)
    count = elapsed
    cur_due = date(
        today.year,
        today.month,
        min(due_day, calendar.monthrange(today.year, today.month)[1]),
    )
    if today >= cur_due:
        count += 1
    return count


def _last_due_date(today: date, due_day: int) -> date:
    day = min(due_day, calendar.monthrange(today.year, today.month)[1])
    candidate = date(today.year, today.month, day)
    if candidate <= today:
        return candidate
    prev = candidate.replace(day=1) - timedelta(days=1)
    prev_day = min(due_day, calendar.monthrange(prev.year, prev.month)[1])
    return date(prev.year, prev.month, prev_day)


@router.get("/financial-summary", response_model=FinancialSummary)
def financial_summary(
    month: str | None = Query(default=None, pattern=MONTH_PATTERN),
    unit_id: int | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(manager_or_admin),
):
    resolved = _resolve_month(month)
    start, end = month_range(resolved)

    lease_query = db.query(Lease).filter(
        Lease.deleted_at.is_(None),
        Lease.status == LeaseStatus.active,
        Lease.start_date <= end,
        Lease.end_date >= start,
    )
    if unit_id is not None:
        lease_query = lease_query.filter(Lease.unit_id == unit_id)
    expected_rent_total = lease_query.with_entities(
        func.coalesce(func.sum(Lease.monthly_rent), 0)
    ).scalar()

    income_query = db.query(Income).filter(
        Income.status == IncomeStatus.confirmed,
        Income.received_date >= start,
        Income.received_date <= end,
    )
    if unit_id is not None:
        income_query = income_query.join(Lease, Income.lease_id == Lease.id).filter(
            Lease.unit_id == unit_id
        )
    total_income = income_query.with_entities(
        func.coalesce(func.sum(Income.amount), 0)
    ).scalar()
    collected_rent = income_query.filter(Income.lease_id.isnot(None)).with_entities(
        func.coalesce(func.sum(Income.amount), 0)
    ).scalar()

    expense_query = db.query(Expense).filter(
        Expense.status.in_([ExpenseStatus.approved, ExpenseStatus.paid]),
        Expense.expense_date >= start,
        Expense.expense_date <= end,
    )
    if unit_id is not None:
        expense_query = expense_query.filter(Expense.unit_id == unit_id)
    total_expense = expense_query.with_entities(
        func.coalesce(func.sum(Expense.amount), 0)
    ).scalar()

    unit_query = db.query(Unit).filter(Unit.deleted_at.is_(None))
    if unit_id is not None:
        unit_query = unit_query.filter(Unit.id == unit_id)
    units_count = unit_query.count()
    occupied_units = unit_query.filter(Unit.status == UnitStatus.occupied).count()
    vacant_units = unit_query.filter(Unit.status == UnitStatus.vacant).count()

    return FinancialSummary(
        month=resolved,
        expected_rent_total=_d2(expected_rent_total),
        collected_rent=_d2(collected_rent),
        outstanding_rent=_d2(expected_rent_total - collected_rent),
        total_income=_d2(total_income),
        total_expense=_d2(total_expense),
        net_income=_d2(total_income - total_expense),
        units_count=units_count,
        occupied_units=occupied_units,
        vacant_units=vacant_units,
    )


@router.get("/overdue-rents", response_model=list[OverdueRent])
def overdue_rents(
    db: Session = Depends(get_db),
    _: User = Depends(manager_or_admin),
):
    today = date.today()
    rows: list[OverdueRent] = []
    leases = (
        db.query(Lease)
        .filter(Lease.status == LeaseStatus.active, Lease.deleted_at.is_(None))
        .all()
    )
    for lease in leases:
        unit = db.query(Unit).filter(Unit.id == lease.unit_id).first()
        tenant = db.query(Tenant).filter(Tenant.id == lease.tenant_id).first()
        if unit is None or tenant is None:
            continue
        months = _months_expected(lease, today)
        if months <= 0:
            continue
        expected = _d2(lease.monthly_rent * months)
        collected = (
            db.query(func.coalesce(func.sum(Income.amount), 0))
            .filter(
                Income.lease_id == lease.id,
                Income.status == IncomeStatus.confirmed,
            )
            .scalar()
        )
        outstanding = _d2(expected - collected)
        if outstanding <= 0:
            continue
        last_due = _last_due_date(today, _default_due_day(lease))
        rows.append(
            OverdueRent(
                lease_id=lease.id,
                unit_id=lease.unit_id,
                tenant_id=lease.tenant_id,
                unit=unit.unit_number,
                tenant=tenant.full_name,
                outstanding=outstanding,
                days_overdue=max((today - last_due).days, 0),
            )
        )
    rows.sort(key=lambda r: r.days_overdue, reverse=True)
    return rows


@router.get("/monthly", response_model=list[MonthlyLeaseSummary])
def monthly_report(
    month: str | None = Query(default=None, pattern=MONTH_PATTERN),
    db: Session = Depends(get_db),
    _: User = Depends(manager_or_admin),
):
    resolved = _resolve_month(month)
    start, end = month_range(resolved)
    leases = (
        db.query(Lease)
        .filter(
            Lease.deleted_at.is_(None),
            Lease.status == LeaseStatus.active,
            Lease.start_date <= end,
            Lease.end_date >= start,
        )
        .order_by(Lease.id)
        .all()
    )
    rows: list[MonthlyLeaseSummary] = []
    for lease in leases:
        unit = db.query(Unit).filter(Unit.id == lease.unit_id).first()
        tenant = db.query(Tenant).filter(Tenant.id == lease.tenant_id).first()
        if unit is None or tenant is None:
            continue
        collected = (
            db.query(func.coalesce(func.sum(Income.amount), 0))
            .filter(
                Income.lease_id == lease.id,
                Income.status == IncomeStatus.confirmed,
                Income.received_date >= start,
                Income.received_date <= end,
            )
            .scalar()
        )
        rows.append(
            MonthlyLeaseSummary(
                lease_id=lease.id,
                unit_id=lease.unit_id,
                tenant_id=lease.tenant_id,
                unit=unit.unit_number,
                tenant=tenant.full_name,
                expected=_d2(lease.monthly_rent),
                collected=_d2(collected),
                outstanding=_d2(lease.monthly_rent - collected),
            )
        )
    return rows


@router.get("/commission", response_model=list[CommissionSummaryRow])
def commission_report(
    month: str | None = Query(default=None, pattern=MONTH_PATTERN),
    agent_id: int | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(manager_or_admin),
):
    resolved = _resolve_month(month)
    start, _ = month_range(resolved)
    next_start = add_months(start, 1)
    start_dt = datetime.combine(start, time.min, tzinfo=timezone.utc)
    next_start_dt = datetime.combine(next_start, time.min, tzinfo=timezone.utc)

    query = db.query(CommissionSettlement).filter(
        CommissionSettlement.created_at >= start_dt,
        CommissionSettlement.created_at < next_start_dt,
    )
    if agent_id is not None:
        query = query.filter(CommissionSettlement.agent_id == agent_id)
    settlements = query.all()

    agent_ids = {s.agent_id for s in settlements}
    rule_ids = {s.rule_id for s in settlements}
    agents = {
        u.id: u.username
        for u in db.query(User).filter(User.id.in_(agent_ids)).all()
    } if agent_ids else {}
    rules = {
        r.id: r.name
        for r in db.query(CommissionRule).filter(CommissionRule.id.in_(rule_ids)).all()
    } if rule_ids else {}

    grouped: dict[tuple[int, int], list[CommissionSettlement]] = {}
    for s in settlements:
        grouped.setdefault((s.agent_id, s.rule_id), []).append(s)

    rows = []
    for (aid, rid), items in grouped.items():
        rows.append(
            CommissionSummaryRow(
                agent_id=aid,
                agent=agents.get(aid, "?"),
                rule_id=rid,
                rule=rules.get(rid, "?"),
                computed_total=_d2(sum((i.computed_amount for i in items), Decimal("0.00"))),
                settlements=len(items),
            )
        )
    rows.sort(key=lambda r: (r.agent, r.rule))
    return rows


@router.get("/tasks", response_model=list[ReportTask])
def tasks_report(
    status: str | None = Query(
        default=None,
        pattern=r"^(pending|scheduled|completed|open|in_progress)$",
    ),
    overdue: bool = False,
    db: Session = Depends(get_db),
    _: User = Depends(manager_or_admin),
):
    query = db.query(Task).filter(Task.deleted_at.is_(None))
    if status is not None:
        if status == "pending":
            query = query.filter(
                Task.status.in_([TaskStatus.open, TaskStatus.in_progress])
            )
        else:
            query = query.filter(Task.status == TaskStatus(status))
    if overdue:
        query = query.filter(
            Task.due_date.isnot(None),
            Task.due_date < date.today(),
            Task.status != TaskStatus.completed,
        )
    tasks = (
        query.order_by(Task.due_date.is_(None), Task.due_date, Task.id)
        .all()
    )

    unit_numbers: dict[int, str] = {}
    unit_ids = {t.unit_id for t in tasks if t.unit_id is not None}
    if unit_ids:
        unit_numbers = {
            u.id: u.unit_number
            for u in db.query(Unit).filter(Unit.id.in_(unit_ids)).all()
        }

    return [
        ReportTask(
            id=t.id,
            title=t.title,
            unit_id=t.unit_id,
            unit=unit_numbers.get(t.unit_id) if t.unit_id is not None else None,
            status=t.status.value,
            priority=t.priority.value,
            due_date=t.due_date,
            assigned_to=t.assigned_to,
            recurring=t.recurring,
            interval_months=t.interval_months,
            next_due_date=t.next_due_date,
        )
        for t in tasks
    ]


@router.get("/expenses", response_model=ExpenseSummary)
def expenses_report(
    category: str | None = None,
    unit_id: int | None = None,
    month: str | None = Query(default=None, pattern=MONTH_PATTERN),
    db: Session = Depends(get_db),
    _: User = Depends(manager_or_admin),
):
    resolved = _resolve_month(month)
    start, end = month_range(resolved)
    query = db.query(Expense).filter(
        Expense.status.in_([ExpenseStatus.approved, ExpenseStatus.paid]),
        Expense.expense_date >= start,
        Expense.expense_date <= end,
    )
    if category is not None:
        query = query.filter(Expense.category == category)
    if unit_id is not None:
        query = query.filter(Expense.unit_id == unit_id)
    expenses = query.all()

    by_category: dict[str, list[Expense]] = {}
    by_unit: dict[int | None, list[Expense]] = {}
    for e in expenses:
        by_category.setdefault(e.category, []).append(e)
        by_unit.setdefault(e.unit_id, []).append(e)

    unit_numbers: dict[int, str] = {}
    unit_ids = {uid for uid in by_unit if uid is not None}
    if unit_ids:
        unit_numbers = {
            u.id: u.unit_number
            for u in db.query(Unit).filter(Unit.id.in_(unit_ids)).all()
        }

    total = _d2(sum((e.amount for e in expenses), Decimal("0.00")))
    return ExpenseSummary(
        month=resolved,
        total_amount=total,
        by_category=[
            ExpenseCategoryRow(
                category=cat,
                amount=_d2(sum((e.amount for e in items), Decimal("0.00"))),
                count=len(items),
            )
            for cat, items in sorted(by_category.items())
        ],
        by_unit=[
            ExpenseUnitRow(
                unit_id=uid,
                unit=unit_numbers.get(uid) if uid is not None else None,
                amount=_d2(sum((e.amount for e in items), Decimal("0.00"))),
                count=len(items),
            )
            for uid, items in sorted(by_unit.items(), key=lambda kv: (kv[0] is None, kv[0]))
        ],
    )
