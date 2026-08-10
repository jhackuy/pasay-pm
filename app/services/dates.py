"""Small date helpers shared by routers (month arithmetic)."""
import calendar
from datetime import date


def add_months(value: date, months: int) -> date:
    """Return `value` shifted by `months`, clamping the day to month length."""
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def month_range(month: str) -> tuple[date, date]:
    """Return (first_day, last_day) for a 'YYYY-MM' string."""
    year, month_num = (int(part) for part in month.split("-"))
    last_day = calendar.monthrange(year, month_num)[1]
    return date(year, month_num, 1), date(year, month_num, last_day)
