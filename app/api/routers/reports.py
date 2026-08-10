"""High-level reports: server-computed aggregates for Hermes (no client-side math)."""
import calendar
import re
from datetime import date, datetime, time, timezone
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
    OverdueRentPeriod,
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


def _accounting_start(lease: Lease) -> date:
    """Earliest month the lease accrues rent: max(start_date, accounting_start_date)."""
    if lease.accounting_start_date is None:
        return lease.start_date
    return max(lease.start_date, lease.accounting_start_date)


_PERIOD_IN_DESC = re.compile(r"(?<!\d)(\d{4})(?:[-/.])?(\d{1,2})(?!\d)")


def _lease_periods(lease: Lease) -> list[tuple[str, date]]:
    """(YYYY-MM, due_date) for every rent month from accounting start through end.

    A trailing end month is only included when the lease covers it fully
    (i.e. the lease end date is the last day of that month). If the lease ends
    mid-month, the partial final month does not generate a new full rent
    period (principle: periods after the lease effectively stop are not owed).
    The start month is always included.
    """
    due_day = _default_due_day(lease)
    periods: list[tuple[str, date]] = []
    accounting_start = _accounting_start(lease)
    year, month = accounting_start.year, accounting_start.month
    end_year, end_month = lease.end_date.year, lease.end_date.month
    end_is_fully_covered = lease.end_date.day >= calendar.monthrange(end_year, end_month)[1]
    while (year, month) <= (end_year, end_month):
        if (year, month) == (end_year, end_month) and not end_is_fully_covered:
            # final month is partial (lease ends mid-month) -> skip
            break
        day = min(due_day, calendar.monthrange(year, month)[1])
        periods.append((f"{year:04d}-{month:02d}", date(year, month, day)))
        month += 1
        if month > 12:
            month = 1
            year += 1
    return periods


def _month_from_description(description: str | None) -> str | None:
    """Extract a YYYY-MM rent period from an income description, if present."""
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
    """Months of this lease covered by confirmed income.

    Match by an explicit YYYY-MM in the description when present; otherwise
    fall back to the month of the received date. Payments landing outside the
    lease's receivable months (before start / after end) cover nothing.
    """
    receivable = {month for month, _ in lease_periods}
    covered: set[str] = set()
    for income in incomes:
        month = _month_from_description(income.description)
        if month is None:
            month = income.received_date.strftime("%Y-%m")
        if month in receivable:
            covered.add(month)
    return covered


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
        func.coalesce(Lease.accounting_start_date, Lease.start_date) <= end,
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
    leases = (
        db.query(Lease)
        .filter(Lease.status == LeaseStatus.active, Lease.deleted_at.is_(None))
        .all()
    )
    lease_ids = [lease.id for lease in leases]
    confirmed_by_lease: dict[int, list[Income]] = {}
    if lease_ids:
        for income in (
            db.query(Income)
            .filter(
                Income.lease_id.in_(lease_ids),
                Income.status == IncomeStatus.confirmed,
            )
            .all()
        ):
            confirmed_by_lease.setdefault(income.lease_id, []).append(income)

    rows: list[OverdueRent] = []
    for lease in leases:
        unit = db.query(Unit).filter(Unit.id == lease.unit_id).first()
        tenant = db.query(Tenant).filter(Tenant.id == lease.tenant_id).first()
        if unit is None or tenant is None:
            continue
        periods = _lease_periods(lease)
        due_periods = [(month, due) for month, due in periods if due <= today]
        if not due_periods:
            continue
        covered = _covered_periods(lease, periods, confirmed_by_lease.get(lease.id, []))
        overdue = [(month, due) for month, due in due_periods if month not in covered]
        if not overdue:
            continue
        oldest_due_date = overdue[0][1]
        overdue_days = max((today - oldest_due_date).days, 0)
        total_outstanding = _d2(lease.monthly_rent * len(overdue))
        rows.append(
            OverdueRent(
                lease_id=lease.id,
                unit_id=lease.unit_id,
                tenant_id=lease.tenant_id,
                unit=unit.unit_number,
                tenant=tenant.full_name,
                overdue_months=len(overdue),
                overdue_periods=[
                    OverdueRentPeriod(month=month, amount=_d2(lease.monthly_rent))
                    for month, _ in overdue
                ],
                amount_per_month=_d2(lease.monthly_rent),
                total_outstanding=total_outstanding,
                oldest_due_date=oldest_due_date,
                overdue_days=overdue_days,
                outstanding=total_outstanding,
                days_overdue=overdue_days,
            )
        )
    rows.sort(key=lambda r: r.overdue_days, reverse=True)
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
            func.coalesce(Lease.accounting_start_date, Lease.start_date) <= end,
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
