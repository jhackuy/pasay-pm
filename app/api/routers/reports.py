"""High-level reports: server-computed aggregates for Hermes (no client-side math)."""
import calendar
import re
from collections.abc import Iterable
from datetime import date, datetime, time, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Integer, func, or_
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.database import get_db
from app.models.commission import CommissionRule, CommissionSettlement
from app.models.financial import Expense, ExpenseStatus, Income, IncomeStatus
from app.models.lease import Lease, LeaseStatus
from app.models.membership import Membership, MembershipState, OrganizationRole
from app.models.operations import OperationalTask, OperationalTaskPriority, OperationalTaskStatus
from app.models.property import Property, Unit, UnitStatus
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
from app.schemas.common import Paginated
from app.services.dates import add_months, month_range

router = APIRouter(prefix="/reports", tags=["reports"])


def resolve_org_membership(
    role: OrganizationRole | Iterable[OrganizationRole] | None = None,
):
    def _dep(
        db: Session = Depends(get_db),
        user: User = Depends(get_current_user),
    ) -> Membership:
        query = db.query(Membership).filter(
            Membership.user_id == user.id,
            Membership.state == MembershipState.ACTIVE,
            Membership.removed_at.is_(None),
        )
        if role is not None:
            roles = list(role) if not isinstance(role, OrganizationRole) else [role]
            query = query.filter(Membership.role.in_(roles))
        small_batch = query.limit(2).all()
        if len(small_batch) == 0:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )
        if len(small_batch) == 1:
            return small_batch[0]
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Organization context required",
        )
    return _dep

MONTH_PATTERN = r"^\d{4}-\d{2}$"
_TWO_PLACES = Decimal("0.01")


def _d2(value) -> Decimal:
    """Normalize an aggregate amount to NUMERIC(14,2) scale."""
    return Decimal(value).quantize(_TWO_PLACES)


def _today_utc() -> date:
    """Single source of UTC today; deterministic for tests via monkeypatch."""
    return datetime.now(timezone.utc).date()


def _current_month() -> str:
    return _today_utc().strftime("%Y-%m")


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


def _income_period(income: Income) -> str | None:
    """The rent period (YYYY-MM) an income payment should be attributed to.

    Prefers the explicit YYYY-MM in the description (strict period matching);
    falls back to the received-date month. Returns None if the income is not
    linked to a lease (non-rent income).
    """
    if income.lease_id is None:
        return None
    month = _month_from_description(income.description)
    if month is None:
        month = income.received_date.strftime("%Y-%m")
    return month


@router.get("/financial-summary", response_model=FinancialSummary)
def financial_summary(
    month: str | None = Query(default=None, pattern=MONTH_PATTERN),
    unit_id: int | None = None,
    db: Session = Depends(get_db),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    org_id = membership.organization_id
    resolved = _resolve_month(month)
    start, end = month_range(resolved)

    lease_query = (
        db.query(Lease)
        .join(Unit, Lease.unit_id == Unit.id)
        .join(Property, Unit.property_id == Property.id)
        .filter(
            Lease.deleted_at.is_(None),
            Lease.status == LeaseStatus.active,
            func.coalesce(Lease.accounting_start_date, Lease.start_date) <= end,
            Lease.end_date >= start,
            Property.organization_id == org_id,
        )
    )
    if unit_id is not None:
        lease_query = lease_query.filter(Lease.unit_id == unit_id)
    expected_rent_total = lease_query.with_entities(
        func.coalesce(func.sum(Lease.monthly_rent), 0)
    ).scalar()

    # cash-basis income actually received during the month (all confirmed income)
    cash_income_query = (
        db.query(Income)
        .join(Lease, Income.lease_id == Lease.id)
        .join(Unit, Lease.unit_id == Unit.id)
        .join(Property, Unit.property_id == Property.id)
        .filter(
            Income.status == IncomeStatus.confirmed,
            Income.received_date >= start,
            Income.received_date <= end,
            Property.organization_id == org_id,
        )
    )
    if unit_id is not None:
        cash_income_query = cash_income_query.filter(Lease.unit_id == unit_id)
    total_income = cash_income_query.with_entities(
        func.coalesce(func.sum(Income.amount), 0)
    ).scalar()

    # period-accurate rent collected for THIS month: match confirmed income to its
    # rent period (YYYY-MM in description, else received-date month). This is
    # consistent with /overdue-rents and avoids late/advance/backdated payments
    # inflating a month's collected above its expected rent (which made
    # outstanding_rent negative and inconsistent with overdue-rents).
    active_leases = lease_query.all()
    lease_ids = [l.id for l in active_leases]
    confirmed_by_lease: dict[int, list[Income]] = {}
    if lease_ids:
        inc_q = (
            db.query(Income)
            .join(Lease, Income.lease_id == Lease.id)
            .join(Unit, Lease.unit_id == Unit.id)
            .join(Property, Unit.property_id == Property.id)
            .filter(
                Income.lease_id.in_(lease_ids),
                Income.status == IncomeStatus.confirmed,
                Property.organization_id == org_id,
            )
        )
        for inc in inc_q.all():
            lid = inc.lease_id
            if lid is not None:
                confirmed_by_lease.setdefault(lid, []).append(inc)
    period_collected = Decimal("0.00")
    for lease in active_leases:
        for inc in confirmed_by_lease.get(lease.id, []):
            if _income_period(inc) == resolved:
                period_collected += inc.amount
    collected_rent = _d2(period_collected)
    outstanding_rent = _d2(expected_rent_total - collected_rent)

    expense_query = (
        db.query(Expense)
        .join(Property, Expense.property_id == Property.id)
        .filter(
            Expense.status.in_([ExpenseStatus.approved, ExpenseStatus.paid]),
            Expense.expense_date >= start,
            Expense.expense_date <= end,
            Property.organization_id == org_id,
        )
    )
    if unit_id is not None:
        expense_query = expense_query.filter(Expense.unit_id == unit_id)
    total_expense = expense_query.with_entities(
        func.coalesce(func.sum(Expense.amount), 0)
    ).scalar()

    unit_query = (
        db.query(Unit)
        .join(Property, Unit.property_id == Property.id)
        .filter(Unit.deleted_at.is_(None), Property.organization_id == org_id)
    )
    if unit_id is not None:
        unit_query = unit_query.filter(Unit.id == unit_id)
    units_count = unit_query.count()
    occupied_units = unit_query.filter(Unit.status == UnitStatus.occupied).count()
    vacant_units = unit_query.filter(Unit.status == UnitStatus.vacant).count()

    return FinancialSummary(
        month=resolved,
        expected_rent_total=_d2(expected_rent_total),
        collected_rent=_d2(collected_rent),
        outstanding_rent=outstanding_rent,
        total_income=_d2(total_income),
        total_expense=_d2(total_expense),
        net_income=_d2(total_income - total_expense),
        units_count=units_count,
        occupied_units=occupied_units,
        vacant_units=vacant_units,
    )


@router.get("/overdue-rents", response_model=Paginated[OverdueRent])
def overdue_rents(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    org_id = membership.organization_id
    today = _today_utc()
    leases = (
        db.query(Lease)
        .join(Unit, Lease.unit_id == Unit.id)
        .join(Property, Unit.property_id == Property.id)
        .filter(
            Lease.status == LeaseStatus.active,
            Lease.deleted_at.is_(None),
            Property.organization_id == org_id,
        )
        .all()
    )
    lease_ids = [lease.id for lease in leases]
    confirmed_by_lease: dict[int, list[Income]] = {}
    if lease_ids:
        for income in (
            db.query(Income)
            .join(Lease, Income.lease_id == Lease.id)
            .join(Unit, Lease.unit_id == Unit.id)
            .join(Property, Unit.property_id == Property.id)
            .filter(
                Income.lease_id.in_(lease_ids),
                Income.status == IncomeStatus.confirmed,
                Property.organization_id == org_id,
            )
            .all()
        ):
            confirmed_by_lease.setdefault(income.lease_id, []).append(income)

    rows: list[OverdueRent] = []
    for lease in leases:
        unit = (
            db.query(Unit)
            .join(Property, Unit.property_id == Property.id)
            .filter(Unit.id == lease.unit_id, Property.organization_id == org_id)
            .first()
        )
        tenant = (
            db.query(Tenant)
            .filter(Tenant.id == lease.tenant_id, Tenant.organization_id == org_id)
            .first()
        )
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
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    total = len(rows)
    paged = rows[offset:offset + limit]
    return Paginated(items=paged, total=total, limit=limit, offset=offset)


@router.get("/monthly", response_model=Paginated[MonthlyLeaseSummary])
def monthly_report(
    month: str | None = Query(default=None, pattern=MONTH_PATTERN),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    org_id = membership.organization_id
    resolved = _resolve_month(month)
    start, end = month_range(resolved)
    leases = (
        db.query(Lease)
        .join(Unit, Lease.unit_id == Unit.id)
        .join(Property, Unit.property_id == Property.id)
        .filter(
            Lease.deleted_at.is_(None),
            Lease.status == LeaseStatus.active,
            func.coalesce(Lease.accounting_start_date, Lease.start_date) <= end,
            Lease.end_date >= start,
            Property.organization_id == org_id,
        )
        .order_by(Lease.id)
        .all()
    )
    rows: list[MonthlyLeaseSummary] = []
    for lease in leases:
        unit = (
            db.query(Unit)
            .join(Property, Unit.property_id == Property.id)
            .filter(Unit.id == lease.unit_id, Property.organization_id == org_id)
            .first()
        )
        tenant = (
            db.query(Tenant)
            .filter(Tenant.id == lease.tenant_id, Tenant.organization_id == org_id)
            .first()
        )
        if unit is None or tenant is None:
            continue
        confirmed_incomes = (
            db.query(Income)
            .join(Lease, Income.lease_id == Lease.id)
            .join(Unit, Lease.unit_id == Unit.id)
            .join(Property, Unit.property_id == Property.id)
            .filter(
                Income.lease_id == lease.id,
                Income.status == IncomeStatus.confirmed,
                Property.organization_id == org_id,
            )
            .all()
        )
        collected = _d2(
            sum(
                (inc.amount for inc in confirmed_incomes if _income_period(inc) == resolved),
                Decimal("0.00"),
            )
        )
        rows.append(
            MonthlyLeaseSummary(
                lease_id=lease.id,
                unit_id=lease.unit_id,
                tenant_id=lease.tenant_id,
                unit=unit.unit_number,
                tenant=tenant.full_name,
                expected=_d2(lease.monthly_rent),
                collected=collected,
                outstanding=_d2(lease.monthly_rent - collected),
            )
        )
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    total = len(rows)
    paged = rows[offset:offset + limit]
    return Paginated(items=paged, total=total, limit=limit, offset=offset)


@router.get("/commission", response_model=Paginated[CommissionSummaryRow])
def commission_report(
    month: str | None = Query(default=None, pattern=MONTH_PATTERN),
    agent_id: int | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    org_id = membership.organization_id
    resolved = _resolve_month(month)
    start, _ = month_range(resolved)
    next_start = add_months(start, 1)
    start_dt = datetime.combine(start, time.min, tzinfo=timezone.utc)
    next_start_dt = datetime.combine(next_start, time.min, tzinfo=timezone.utc)

    query = (
        db.query(CommissionSettlement)
        .join(Lease, CommissionSettlement.lease_id == Lease.id)
        .join(Unit, Lease.unit_id == Unit.id)
        .join(Property, Unit.property_id == Property.id)
        .filter(
            CommissionSettlement.created_at >= start_dt,
            CommissionSettlement.created_at < next_start_dt,
            Property.organization_id == org_id,
        )
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
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    total = len(rows)
    paged = rows[offset:offset + limit]
    return Paginated(items=paged, total=total, limit=limit, offset=offset)


@router.get("/tasks", response_model=Paginated[ReportTask])
def tasks_report(
    status: str | None = Query(
        default=None,
        pattern=r"^(pending|scheduled|completed|open|in_progress)$",
    ),
    overdue: bool = False,
    within_days: int | None = Query(
        default=None, ge=1,
        description="Only tasks with due_date within the next N days (future window).",
    ),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    org_id = membership.organization_id
    org_property_ids = (
        db.query(Property.id).filter(Property.organization_id == org_id, Property.deleted_at.is_(None))
    )
    org_unit_ids = (
        db.query(Unit.id).join(Property, Unit.property_id == Property.id)
        .filter(Property.organization_id == org_id, Unit.deleted_at.is_(None))
    )
    org_lease_ids = (
        db.query(Lease.id).join(Unit, Lease.unit_id == Unit.id)
        .join(Property, Unit.property_id == Property.id)
        .filter(Property.organization_id == org_id, Lease.deleted_at.is_(None))
    )
    org_tenant_ids = (
        db.query(Tenant.id).filter(Tenant.organization_id == org_id, Tenant.deleted_at.is_(None))
    )
    from sqlalchemy.dialects.postgresql import JSONB
    query = db.query(OperationalTask).filter(
        or_(
            OperationalTask.property_id.in_(org_property_ids),
            OperationalTask.lease_id.in_(org_lease_ids),
            OperationalTask.tenant_id.in_(org_tenant_ids),
            OperationalTask.details.op("->>")("organization_id").cast(Integer) == org_id,
        )
    )
    today = _today_utc()
    if status is not None:
        if status == "pending":
            query = query.filter(
                OperationalTask.status.in_([OperationalTaskStatus.PENDING, OperationalTaskStatus.IN_PROGRESS])
            )
        elif status == "open":
            query = query.filter(OperationalTask.status == OperationalTaskStatus.PENDING)
        elif status == "in_progress":
            query = query.filter(OperationalTask.status == OperationalTaskStatus.IN_PROGRESS)
        elif status == "completed":
            query = query.filter(OperationalTask.status == OperationalTaskStatus.COMPLETED)
        elif status == "scheduled":
            query = query.filter(
                OperationalTask.status.in_([
                    OperationalTaskStatus.PENDING,
                    OperationalTaskStatus.IN_PROGRESS,
                ]),
                OperationalTask.due_at.isnot(None),
                func.date(OperationalTask.due_at) > today,
            )
    if overdue:
        query = query.filter(
            OperationalTask.due_at.isnot(None),
            func.date(OperationalTask.due_at) < today,
            OperationalTask.status != OperationalTaskStatus.COMPLETED,
        )
    if within_days is not None:
        from datetime import timedelta
        horizon = today + timedelta(days=within_days)
        query = query.filter(
            OperationalTask.due_at.isnot(None),
            func.date(OperationalTask.due_at) >= today,
            func.date(OperationalTask.due_at) <= horizon,
        )
    tasks = (
        query.order_by(OperationalTask.due_at.is_(None), OperationalTask.due_at, OperationalTask.id)
        .all()
    )

    lease_unit_ids: dict[int, int] = {}
    lease_ids = {t.lease_id for t in tasks if t.lease_id is not None}
    if lease_ids:
        for lid, uid in (
            db.query(Lease.id, Lease.unit_id)
            .join(Unit, Unit.id == Lease.unit_id)
            .join(Property, Unit.property_id == Property.id)
            .filter(
                Lease.id.in_(list(lease_ids)),
                Property.organization_id == org_id,
                Lease.deleted_at.is_(None),
                Unit.deleted_at.is_(None),
            )
            .all()
        ):
            lease_unit_ids[lid] = uid

    unit_ids_from_leases = set(lease_unit_ids.values())
    unit_numbers: dict[int, str] = {}
    if unit_ids_from_leases:
        unit_numbers = {
            u.id: u.unit_number
            for u in (
                db.query(Unit)
                .join(Property, Unit.property_id == Property.id)
                .filter(Unit.id.in_(unit_ids_from_leases), Property.organization_id == org_id)
                .all()
            )
        }

    def _map_status(s: OperationalTaskStatus) -> str:
        if s == OperationalTaskStatus.PENDING:
            return "open"
        if s == OperationalTaskStatus.IN_PROGRESS:
            return "in_progress"
        if s == OperationalTaskStatus.COMPLETED:
            return "completed"
        if s == OperationalTaskStatus.CANCELLED:
            return "cancelled"
        return "scheduled"

    def _map_priority(p: OperationalTaskPriority) -> str:
        if p == OperationalTaskPriority.critical:
            return "high"
        return p.value

    rows = []
    for t in tasks:
        unit_id = lease_unit_ids.get(t.lease_id) if t.lease_id is not None else None
        rows.append(
            ReportTask(
                id=t.id,
                title=t.title,
                unit_id=unit_id,
                unit=unit_numbers.get(unit_id) if unit_id is not None else None,
                status=_map_status(t.status),
                priority=_map_priority(t.priority),
                due_date=t.due_at.date() if t.due_at is not None else None,
                assigned_to=t.assigned_user_id,
                recurring=False,
                interval_months=None,
                next_due_date=None,
            )
        )
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    total = len(rows)
    paged = rows[offset:offset + limit]
    return Paginated(items=paged, total=total, limit=limit, offset=offset)


@router.get("/expenses", response_model=ExpenseSummary)
def expenses_report(
    category: str | None = None,
    unit_id: int | None = None,
    month: str | None = Query(default=None, pattern=MONTH_PATTERN),
    db: Session = Depends(get_db),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    org_id = membership.organization_id
    resolved = _resolve_month(month)
    start, end = month_range(resolved)
    query = (
        db.query(Expense)
        .join(Property, Expense.property_id == Property.id)
        .filter(
            Expense.status.in_([ExpenseStatus.approved, ExpenseStatus.paid]),
            Expense.expense_date >= start,
            Expense.expense_date <= end,
            Property.organization_id == org_id,
        )
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
            for u in (
                db.query(Unit)
                .join(Property, Unit.property_id == Property.id)
                .filter(Unit.id.in_(unit_ids), Property.organization_id == org_id)
                .all()
            )
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
