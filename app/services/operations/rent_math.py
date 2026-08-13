"""Shared rent-period / coverage / balance math (mirrors /reports/overdue-rents)."""
from __future__ import annotations

import calendar
import re
from datetime import date
from decimal import Decimal

from app.models.financial import Income, IncomeStatus
from app.models.lease import Lease

_PERIOD_IN_DESC = re.compile(r"(?<!\d)(\d{4})(?:[-/.])?(\d{1,2})(?!\d)")


def default_due_day(lease: Lease) -> int:
    return lease.due_day if lease.due_day is not None else lease.start_date.day


def accounting_start(lease: Lease) -> date:
    if lease.accounting_start_date is None:
        return lease.start_date
    return max(lease.start_date, lease.accounting_start_date)


def lease_periods(lease: Lease) -> list[tuple[str, date]]:
    """(YYYY-MM, due_date) for every full rent month from accounting start.

    A trailing partial final month does not generate a new full rent period;
    the start month always does (overdue-rents semantics).
    """
    due_day = default_due_day(lease)
    periods: list[tuple[str, date]] = []
    start = accounting_start(lease)
    year, month = start.year, start.month
    end_year, end_month = lease.end_date.year, lease.end_date.month
    end_is_fully_covered = lease.end_date.day >= calendar.monthrange(end_year, end_month)[1]
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


def month_from_description(description: str | None) -> str | None:
    if not description:
        return None
    match = _PERIOD_IN_DESC.search(description)
    if match is None:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        return None
    return f"{year:04d}-{month:02d}"


def covered_periods(
    lease: Lease,
    lease_periods_list: list[tuple[str, date]],
    incomes: list[Income],
) -> set[str]:
    """Months covered by confirmed income (description period, else received
    month), mirroring the overdue-rents coverage rule."""
    receivable = {month for month, _ in lease_periods_list}
    covered: set[str] = set()
    for income in incomes:
        month = month_from_description(income.description)
        if month is None:
            month = income.received_date.strftime("%Y-%m")
        if month in receivable:
            covered.add(month)
    return covered


def confirmed_paid_by_period(
    lease_periods_list: list[tuple[str, date]],
    incomes: list[Income],
) -> dict[str, Decimal]:
    """Total confirmed income amount per rent period (SLICE2-RENT-005).

    One period can now be paid by several partial payments, so the amount
    matters, not just "is there a confirmed income". The period attribution
    reuses the exact coverage rule (description period, else received month)
    so a partial payment never silently covers a different month.
    """
    receivable = {month for month, _ in lease_periods_list}
    paid: dict[str, Decimal] = {}
    for income in incomes:
        if income.status != IncomeStatus.confirmed:
            continue
        month = month_from_description(income.description)
        if month is None:
            month = income.received_date.strftime("%Y-%m")
        if month in receivable:
            paid[month] = paid.get(month, Decimal("0")) + income.amount
    return paid


def period_remaining(due_amount: Decimal, paid_amount: Decimal) -> Decimal:
    """Remaining receivable for one period; never negative (balance safety)."""
    return max(due_amount - paid_amount, Decimal("0.00"))
