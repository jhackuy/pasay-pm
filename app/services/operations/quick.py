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


def build_unit_timeline(
    db: Session, unit_id: int, *, now: datetime | None = None
) -> dict:
    """AI-OPS-FOUNDATION-001 §15: the unit's digital file — a deterministic,
    time-ordered timeline of everything the business knows about this unit
    (rent/payment history, expenses, repairs, tasks, evidence, lease events).

    Returns ``{"unit": {...}, "events": [...]}`` with human-safe rows."""
    from app.models.evidence import Evidence
    from app.models.financial import Expense, Income, IncomeStatus
    from app.models.lease import Lease
    from app.models.operations import OperationalTask

    unit = db.query(Unit).filter(Unit.id == unit_id, Unit.deleted_at.is_(None)).first()
    if unit is None:
        return {"unit": None, "events": []}
    events: list[dict] = []

    leases = (
        db.query(Lease)
        .filter(Lease.unit_id == unit_id, Lease.deleted_at.is_(None))
        .all()
    )
    lease_ids = [l.id for l in leases]
    tenant_names = {
        t.id: t.full_name
        for t in db.query(Tenant).filter(Tenant.id.in_([l.tenant_id for l in leases])).all()
    }
    for lease in leases:
        events.append({
            "at": lease.start_date.isoformat(),
            "kind": "lease",
            "label": f"Lease started · {tenant_names.get(lease.tenant_id, '?')}",
            "detail": (
                f"₱{_d2(lease.monthly_rent)}/mo · deposit ₱{_d2(lease.deposit)}"
                f" · {lease.start_date} → {lease.end_date}"
            ),
        })
        events.append({
            "at": lease.end_date.isoformat(),
            "kind": "lease",
            "label": "Lease ends",
            "detail": tenant_names.get(lease.tenant_id, "?"),
        })

    if lease_ids:
        incomes = (
            db.query(Income)
            .filter(Income.lease_id.in_(lease_ids), Income.status == IncomeStatus.confirmed)
            .order_by(Income.received_date, Income.id)
            .all()
        )
        for inc in incomes:
            events.append({
                "at": inc.received_date.isoformat(),
                "kind": "rent",
                "label": f"Rent paid · ₱{_d2(inc.amount)}",
                "detail": (inc.description or "")[:120],
            })

        tasks = (
            db.query(OperationalTask)
            .filter(OperationalTask.lease_id.in_(lease_ids))
            .order_by(OperationalTask.due_at, OperationalTask.id)
            .all()
        )
        for task in tasks:
            events.append({
                "at": (task.due_at or task.created_at).isoformat(),
                "kind": "task",
                "label": f"{task.task_type.value} · {task.title}",
                "detail": f"{task.status.value}"[:120],
            })

    expenses = (
        db.query(Expense)
        .filter(Expense.unit_id == unit_id)
        .order_by(Expense.expense_date, Expense.id)
        .all()
    )
    for exp in expenses:
        events.append({
            "at": exp.expense_date.isoformat(),
            "kind": "expense",
            "label": f"Expense · {exp.category} · ₱{_d2(exp.amount)}",
            "detail": f"{exp.status.value} · {exp.payee}"[:120],
        })

    evidence_rows = (
        db.query(Evidence)
        .filter(Evidence.unit_id == unit_id)
        .order_by(Evidence.created_at, Evidence.id)
        .all()
    )
    for ev in evidence_rows:
        events.append({
            "at": ev.created_at.isoformat() if ev.created_at else "",
            "kind": "evidence",
            "label": f"Evidence · {ev.category.value if ev.category else 'other'}",
            "detail": (ev.filename or "")[:120],
        })

    events.sort(key=lambda e: (e["at"] or "", e["kind"]))
    return {
        "unit": {
            "unit_number": unit.unit_number,
            "monthly_rent": str(unit.monthly_rent),
            "status": unit.status.value,
            "unit_state": unit.unit_state,
        },
        "events": events,
    }


def _clean_text(value: str | None) -> str | None:
    """Trim free text and drop placeholder/empty sentinels so the Quick View
    never ships `??`, `None`, `null`, an empty string, or a bare dash as a
    real purpose (PASAY-V2-EXPENSE-UX-AUDIT-005 §2). Returns None when no
    meaningful text remains."""
    if not value:
        return None
    text = " ".join(str(value).split())
    if not text or text.lower() in {"none", "null", "??", "-"}:
        return None
    return text


def _expense_purpose(expense) -> str | None:
    """Smallest existing-data purpose fallback chain for a Quick View row:
    category -> description/memo -> payee/vendor. A placeholder category such
    as `??` is dropped, so an incomplete record (e.g. E7/E8) still resolves to
    truthful existing facts (here: the 'Repair' payee) before the renderer's
    neutral unspecified label. The final locale-aware `Other / 其他` fallback
    lives in the renderer, not here (P1-...-008 A3)."""
    for field in (
        _clean_text(expense.category),
        _clean_text(expense.description),
        _clean_text(getattr(expense, "payee", None)),
    ):
        if field:
            return field
    return None


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


def _payable_expense_rows(
    db: Session, *, now: datetime | None = None
) -> list[dict]:
    """Owner-actionable payable expenses: every APPROVED (not yet PAID)
    expense, which the product rule `PENDING -> APPROVED -> PAID` treats as an
    unfinished task the Owner still must pay.

    Only PAID is financially completed; APPROVED is approved-but-unpaid, so
    each row carries the stable business identity (E{id}, plain text — never
    the Telegram `#E{id}` hashtag) and the strong
    matching fields (unit, purpose, amount, expense_date) the bot needs to
    distinguish same-day/same-amount expenses and to run its advisory
    possible-duplicate warning."""
    expenses = (
        db.query(Expense)
        .filter(Expense.status == ExpenseStatus.approved)
        .order_by(Expense.expense_date, Expense.id)
        .all()
    )
    unit_ids = {e.unit_id for e in expenses if e.unit_id is not None}
    label_by_unit: dict[int, str] = {}
    if unit_ids:
        units = {
            u.id: u for u in db.query(Unit).filter(Unit.id.in_(unit_ids)).all()
        }
        for u in units.values():
            label = _unit_label(db, u)
            if label:
                label_by_unit[u.id] = label
    rows = []
    for e in expenses:
        rows.append(
            {
                "kind": "payable_expense",
                "expense_id": e.id,
                "unit": label_by_unit.get(e.unit_id, ""),
                "purpose": _expense_purpose(e) or "",
                "amount": _d2(e.amount),
                "status": e.status.value,
                "expense_date": e.expense_date.isoformat(),
                "has_receipt": e.receipt_attachment_id is not None,
            }
        )
    return rows


def build_quick_tasks(
    db: Session, user: User, *, now: datetime | None = None,
    owner_only: bool = False,
) -> list[dict]:
    """Active tasks (PENDING + IN_PROGRESS) for the caller's scope.

    For the Owner (admin) the actionable set also includes every APPROVED
    unpaid Expense as a payable task row (``kind == "payable_expense"``), so
    the `✅ Tasks` Quick View never reports "Nothing here" while an approved
    expense still awaits payment (PASAY-V2-EXPENSE-PAYABLE-TASK-006).

    AI-OPS-FOUNDATION-001 §5: ``owner_only=True`` applies the Owner attention
    filter — the Owner queue holds only approvals, their payments, decisions
    and escalations; routine operational work stays out."""
    now = now or datetime.now(timezone.utc)
    query = _agent_scope(db.query(OperationalTask), user).filter(
        OperationalTask.status.in_(
            [OperationalTaskStatus.PENDING, OperationalTaskStatus.IN_PROGRESS]
        )
    )
    tasks = query.order_by(OperationalTask.due_at, OperationalTask.id).all()
    if owner_only:
        from app.services.operations.owner_scope import is_owner_actionable

        tasks = [t for t in tasks if is_owner_actionable(t, user)]
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
    if user.role == UserRole.admin:
        rows.extend(_payable_expense_rows(db, now=now))
    return rows


def _similar_date_window(expense_date: date, *, now: datetime | None = None) -> tuple[date, date]:
    """Relevant date window for a possible-duplicate check: centers on the
    expense date (same relevant day ± one day) so a payment recorded a day or
    two apart on the same unit/amount/purpose still raises the advisory
    warning. Date only narrows the window — it is never the sole signal."""
    window_start = expense_date - timedelta(days=1)
    window_end = expense_date + timedelta(days=1)
    return window_start, window_end


def find_similar_paid_expenses(
    db: Session, expense: Expense, *, now: datetime | None = None
) -> list[dict]:
    """Strong-attribute possible-duplicate matcher (advisory only, never a
    delete/reject) — PASAY-V2-EXPENSE-PAYABLE-TASK-006 §7/§8.

    A different but highly similar PAID expense signals a possible business
    duplicate. Matching requires MULTIPLE strong fields: same unit identity,
    same amount, same purpose/category, all within a relevant date window.
    Amount alone is never enough. Returns similar PAID rows (existing-ID +
    display fields) so the bot can show
    ``Existing: E{old} / Current: E{new}`` and let the Owner Continue /
    Cancel / View-existing without deleting business records."""
    if expense.unit_id is None:
        return []
    window_start, window_end = _similar_date_window(expense.expense_date, now=now)
    candidate_purpose = _clean_text(expense.category)
    paid = (
        db.query(Expense)
        .filter(
            Expense.id != expense.id,
            Expense.status == ExpenseStatus.paid,
            Expense.unit_id == expense.unit_id,
            Expense.amount == expense.amount,
            Expense.expense_date >= window_start,
            Expense.expense_date <= window_end,
        )
        .all()
    )
    label = ""
    if expense.unit_id is not None:
        unit = db.query(Unit).filter(Unit.id == expense.unit_id).first()
        label = _unit_label(db, unit) if unit is not None else ""
    rows = []
    for other in paid:
        other_purpose = _clean_text(other.category)
        if candidate_purpose and other_purpose and candidate_purpose == other_purpose:
            rows.append(
                {
                    "expense_id": other.id,
                    "status": other.status.value,
                    "unit": label,
                    "purpose": _expense_purpose(other) or "",
                    "amount": _d2(other.amount),
                    "expense_date": other.expense_date.isoformat(),
                }
            )
    rows.sort(key=lambda r: (r["expense_date"], r["expense_id"]), reverse=True)
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


def _month_from_income(income: Income) -> str:
    """The rent period (YYYY-MM) a confirmed income maps to. Mirrors the
    financial summary: an explicit YYYY-MM in the description is authoritative;
    the received-date month is the fallback (never both)."""
    return _month_from_description(income.description) or income.received_date.strftime("%Y-%m")


def _current_month_collected(confirmed_by_lease: dict[int, list[Income]], lease_id: int, month: str) -> Decimal:
    """Cash attributed to ONE month for a lease: sum of confirmed incomes whose
    period resolves to ``month`` (description YYYY-MM else received-date month).
    Partial payments reduce outstanding correctly (they add to collected)."""
    total = Decimal("0.00")
    for income in confirmed_by_lease.get(lease_id, []):
        if _month_from_income(income) == month:
            total += _d2(income.amount)
    return total


def build_quick_rent(db: Session, *, now: datetime | None = None) -> dict:
    """Overdue units + outstanding total, plus the CURRENT month's rent
    statistics (expected / collected / outstanding / collection rate / unpaid
    unit count). Same period semantics as /overdue-rents and the financial
    summary: an income covers a month when its description YYYY-MM (else its
    received-date month) matches it. ``outstanding_rent`` = expected - valid
    collected; partial payments reduce it — a partial payment is never treated
    as fully paid. ``unpaid_unit_count`` counts active leases whose current
    period is not yet fully covered."""
    now = now or datetime.now(timezone.utc)
    today = now.date()
    month = today.strftime("%Y-%m")
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
    expected_rent = Decimal("0.00")
    collected_rent = Decimal("0.00")
    unpaid_unit_count = 0
    for lease in leases:
        unit = units.get(lease.unit_id)
        if unit is None:
            continue
        periods = _lease_periods(lease)
        # Does the lease cover the current month at all?
        month_due = next((due for m, due in periods if m == month), None)
        # Expected rent only counts months the lease actually covers.
        covers_current = any(m == month for m, _ in periods)
        if covers_current:
            expected_rent += _d2(lease.monthly_rent)
            cur_collected = _current_month_collected(confirmed_by_lease, lease.id, month)
            collected_rent += cur_collected
            if cur_collected < _d2(lease.monthly_rent) and month_due is not None and month_due <= today:
                unpaid_unit_count += 1
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
    outstanding_rent = _d2(expected_rent - collected_rent)
    if expected_rent > 0:
        collection_rate = (collected_rent / expected_rent * Decimal("100")).quantize(_TWO_PLACES)
    else:
        collection_rate = Decimal("0.00")
    return {
        "overdue": overdue_rows,
        "outstanding_total": outstanding,
        "month": month,
        "expected_rent_total": _d2(expected_rent),
        "collected_rent": _d2(collected_rent),
        "outstanding_rent": outstanding_rent,
        "collection_rate": collection_rate,
        "unpaid_unit_count": unpaid_unit_count,
    }


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
            "expense_id": e.id,
            "unit": label_by_unit.get(e.unit_id, ""),
            "unit_code": label_by_unit.get(e.unit_id, ""),
            "purpose": _expense_purpose(e) or "",
            "amount": _d2(e.amount),
            "expense_date": e.expense_date.isoformat(),
            "status": e.status.value,
        }
        for e in month_records
    ]
    # EXPENSE-UX-FIX-001: the pending-payment section is built from the REAL
    # expense records (APPROVED, not PAID), never from operational-task titles
    # that used to embed a raw `??` category. `paid_records` is this month's
    # PAID spend so an APPROVED expense appears exactly once per page.
    payable = _payable_expense_rows(db, now=now)
    paid_records = [r for r in records if r["status"] == "paid"]
    return {
        "month_total": month_total,
        "pending_approval_count": len(pending_rows),
        "pending_approval_amount": pending_amount,
        "unresolved_expense_tasks": [
            _task_row(db, t, unit_number_by_lease) for t in unresolved
        ],
        "records": records,
        "payable": payable,
        "paid_records": paid_records,
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
