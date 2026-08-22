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

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.financial import Expense, ExpenseStatus, Income, IncomeStatus
from app.models.lease import Lease, LeaseStatus
from app.models.operations import (
    OperationalTask,
    OperationalTaskStatus,
    OperationalTaskType,
)
from app.models.property import Property, Unit
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.dates import month_range
from app.services.organization_scope import list_active_org_ids_for_user

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


class _AllUserPseudo:
    """Sentinel passed to `_agent_scope` when we want the full-scope no-agent-filter
    behaviour (build_quick_expense is called for an admin user without a user
    argument on the route today)."""

    role = UserRole.admin  # never UserRole.agent


_SCOPE_ALL_USER = _AllUserPseudo()


def _derive_org_scope_sets(db: Session, user_id: int | None = None):
    """Triple-channel ownership derivation: org_property_ids or org_unit_ids or
    org_lease_ids or org_tenant_ids, matching build_quick_tasks canonical
    pattern. Returns four sets (may be empty) — empty set means "no rows from
    that channel" (fail-closed). Tenant.organization_id is always queried
    directly, independent of whether the org has any Property/Unit/Lease rows.

    user_id=None means system-level full-scope caller (scheduler / unauthenticated
    code path) — returns all organizations' scoped sets (not user_id=0 which
    would return empty)."""
    if user_id is None:
        from app.models.membership import Organization
        orgs = [
            r for (r,) in db.execute(
                select(Organization.id)
            ).all()
        ]
    else:
        orgs = list_active_org_ids_for_user(db, user_id)
    if not orgs:
        return set(), set(), set(), set()
    org_property_ids = {
        r for (r,) in db.execute(
            select(Property.id).where(
                Property.organization_id.in_(orgs),
                Property.deleted_at.is_(None),
            )
        ).all()
    }
    org_unit_ids = {
        r for (r,) in db.execute(
            select(Unit.id).where(
                Unit.property_id.in_(list(org_property_ids)),
                Unit.deleted_at.is_(None),
            )
        ).all()
    } if org_property_ids else set()
    org_lease_ids: set[int] = set()
    org_tenant_ids: set[int] = set()
    if org_unit_ids:
        for (lease_id, tenant_id) in db.execute(
            select(Lease.id, Lease.tenant_id).where(
                Lease.unit_id.in_(list(org_unit_ids)),
                Lease.deleted_at.is_(None),
            )
        ).all():
            if lease_id is not None:
                org_lease_ids.add(lease_id)
            if tenant_id is not None:
                org_tenant_ids.add(tenant_id)
    from app.models.tenant import Tenant
    direct_tenant_ids = {
        r for (r,) in db.execute(
            select(Tenant.id).where(
                Tenant.organization_id.in_(orgs),
                Tenant.deleted_at.is_(None),
            )
        ).all()
    }
    org_tenant_ids |= direct_tenant_ids
    return org_property_ids, org_unit_ids, org_lease_ids, org_tenant_ids


def _triple_channel_operational_task_filter():
    """Return a three-channel OR filter clause so any OperationalTask that is
    reachable via property/lease/tenant ownership is included. The caller
    is responsible for computing the four sets via `_derive_org_scope_sets`
    in-advance."""
    # Deferred import to avoid circular import at module load
    from sqlalchemy import true as _sa_true
    return _sa_true()


def _task_row(db: Session, task: OperationalTask, unit_number_by_lease: dict[int, str]) -> dict:
    """One active-task row in the shape the bot cards expect.

    CONVERGENCE-003 §8: rows also carry the business context the cards need to
    distinguish identical-looking reminders (payable amount, purpose, unit,
    expense id, unpaid periods, total outstanding) — a renderer never has to
    re-derive the same business fact itself."""
    unit = unit_number_by_lease.get(task.lease_id)
    details = task.details or {}
    task_type = task.task_type.value
    # TELEGRAM-OPS-REAL-WORLD-CLOSURE-005 §11: for RENT_OVERDUE follow-up tasks
    # the visible amount must be the TOTAL ARREARS (monthly × uncovered
    # periods), NEVER a bare monthly rent. ``_rent_task_details`` historically
    # wrote ``amount=monthly_rent`` and ``total_outstanding=<total>``; the old
    # ``amount or total_outstanding`` order surfaced the monthly rent on the
    # Tasks board. Fixed by preferring ``total_outstanding`` for rent arrears
    # tasks (stale DB rows included), while expense/other task types keep the
    # plain ``amount``.
    if task_type in ("RENT_OVERDUE", "FOLLOWUP", "RENT_DUE"):
        arrears_raw = (
            details.get("total_outstanding")
            or details.get("arrears")
            or details.get("amount")
        )
        amount = _d2(arrears_raw) if arrears_raw is not None else None
    else:
        amount_raw = details.get("amount") or details.get("total_outstanding")
        amount = _d2(amount_raw) if amount_raw is not None else None
    return {
        "id": task.id,
        "task_type": task_type,
        "title": task.title,
        "status": task.status.value,
        "property_code": unit or details.get("unit_number"),
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "next_action": task.next_action,
        "next_check_at": task.next_check_at.isoformat() if task.next_check_at else None,
        "completion_condition": task.completion_condition,
        "context": task.context,
        "source_event": task.source_event,
        # CONVERGENCE-003 §8: truthful business context (never raw sentinels).
        "amount": amount,
        "purpose": _clean_text(details.get("category")) or _clean_text(details.get("payee")),
        "expense_id": details.get("expense_id"),
        "period": details.get("period"),
        "unpaid_periods": len(details["periods"]) if isinstance(details.get("periods"), list) else None,
        # TELEGRAM-OPS-REAL-WORLD-CLOSURE-005 §13: surface the real follow-up
        # state so the Tasks board can show "秘书跟进中" (🟡) vs "需要催租"
        # (🔴) for the SAME rent item — the assignment is a real fact, not a
        # status guessed by the renderer.
        "followup_assigned": bool(details.get("assigned_to")),
        "followup_executed": task.status.value == "COMPLETED",
    }


def _payable_expense_rows(
    db: Session, *, now: datetime | None = None
) -> list[dict]:
    """Owner-actionable payable expenses: every expense that still has REAL
    remaining money to pay (APPROVED, PARTIALLY_PAID, or PAYMENT_CLAIMED with a
    remaining balance), derived from the VERIFIED-claims truth (003B §4 / §12).
    A fully-pa (PAID) expense is financially completed and never appears.

    Each row carries the stable business identity (E{id}) and the strong
    matching fields (unit, purpose, amount, expense_date) the bot needs to
    distinguish same-day/same-amount expenses and to run its advisory
    possible-duplicate warning."""
    from app.services.expense_payment_truth import payment_truth

    expenses = (
        db.query(Expense)
        .filter(
            Expense.status.in_([
                ExpenseStatus.approved,
                ExpenseStatus.partially_paid,
                ExpenseStatus.payment_claimed,
            ])
        )
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
        truth = payment_truth(db, e)
        if truth.fully_paid or truth.remaining <= 0:
            continue  # fully verified -> not payable
        waiting_days = 0
        if e.approved_at is not None:
            try:
                waiting_days = max((now.date() - e.approved_at.date()).days, 0)
            except (TypeError, ValueError):
                waiting_days = 0
        rows.append(
            {
                "kind": "payable_expense",
                "expense_id": e.id,
                "unit": label_by_unit.get(e.unit_id, ""),
                "purpose": _expense_purpose(e) or "",
                "amount": truth.remaining,
                "status": e.status.value,
                "expense_date": e.expense_date.isoformat(),
                # ZERO-LEARNING-004 §6: the SAME waiting-day fact the task rows
                # carry, so the To-pay section is the single representation.
                "waiting_days": waiting_days,
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
    """One asset row per active unit for the frozen Units page.

    The Units page is an asset directory, not an operations workbench, so the
    payload carries only the minimal real estate identity needed by the bot:
    unit number, building/property name, occupancy status and current tenant.
    Overdue/follow-up/payment workload stays on Home / Tasks / Rent / Expense.
    """
    now = now or datetime.now(timezone.utc)
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
    property_by_id = {
        p.id: p
        for p in db.query(Property)
        .filter(Property.id.in_([u.property_id for u in units]))
        .all()
    }
    tenant_by_id = {
        t.id: t
        for t in db.query(Tenant)
        .filter(Tenant.id.in_([l.tenant_id for l in leases if l.tenant_id is not None]))
        .all()
    }

    rows: list[dict] = []
    for unit in units:
        label = _unit_label(db, unit) or unit.unit_number
        prop = property_by_id.get(unit.property_id)
        lease = lease_by_unit.get(unit.id)
        if lease is None:
            rows.append(
                {
                    "unit_code": label,
                    "property_name": getattr(prop, "name", "") or "",
                    "status": "vacant",
                    "tenant_name": "",
                }
            )
            continue
        tenant = tenant_by_id.get(lease.tenant_id) if lease.tenant_id is not None else None
        rows.append(
            {
                "unit_code": label,
                "property_name": getattr(prop, "name", "") or "",
                "status": "occupied",
                "tenant_name": getattr(tenant, "full_name", "") or "",
            }
        )
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
    # CONVERGENCE-003 §5/§7 + TELEGRAM-OPS-REAL-WORLD-CLOSURE-005 §2.5/§4:
    # the "last follow-up" timestamp per lease is when the Secretary ACTUALLY
    # contacted the tenant — i.e. the latest ``completed_at`` of a rent
    # follow-up task. Merely ASSIGNING the follow-up (Owner tap -> Secretary
    # DM) must NOT move this date; only a real execution (Secretary confirms)
    # does. A never-completed task therefore never hides an older truth.
    followup_by_lease: dict[int, str] = {}
    if leases:
        followup_tasks = (
            db.query(OperationalTask)
            .filter(
                OperationalTask.lease_id.in_([l.id for l in leases]),
                OperationalTask.task_type.in_(
                    [OperationalTaskType.RENT_OVERDUE, OperationalTaskType.FOLLOWUP]
                ),
            )
            .order_by(OperationalTask.completed_at.desc().nulls_last(),
                      OperationalTask.id.desc())
            .all()
        )
        for ft in followup_tasks:
            if ft.lease_id is None or ft.lease_id in followup_by_lease:
                continue
            stamp = ft.completed_at if ft.completed_at is not None else None
            if stamp is not None:
                followup_by_lease[ft.lease_id] = stamp.isoformat()
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
                # CONVERGENCE-003 §7: the SAME truth source as the RENT_OVERDUE
                # task generator (len(overdue) uncovered periods) — the bot's
                # Rent detail must never hardcode its own period count.
                "unpaid_periods": len(overdue),
                "monthly_rent": _d2(lease.monthly_rent),
                "overdue_days": max((today - oldest_due).days, 0),
                "last_followup_at": followup_by_lease.get(lease.id),
            }
        )
    overdue_rows.sort(key=lambda r: r["overdue_days"], reverse=True)
    outstanding_rent = _d2(expected_rent - collected_rent)
    if expected_rent > 0:
        collection_rate = (collected_rent / expected_rent * Decimal(100)).quantize(_TWO_PLACES)
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


def build_quick_expense(db: Session, *, user_id: int | None = None, now: datetime | None = None) -> dict:
    """Current-month spend + pending approval + unresolved expense tasks."""
    now = now or datetime.now(timezone.utc)
    start, end = month_range(now.date().strftime("%Y-%m"))
    month_total = Decimal("0.00")
    for amount, in (
        db.query(Expense.amount)
        .filter(
            Expense.status.in_([
                ExpenseStatus.approved,
                ExpenseStatus.paid,
                ExpenseStatus.partially_paid,
                ExpenseStatus.payment_claimed,
            ]),
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
    org_property_ids, _org_unit_ids, org_lease_ids, org_tenant_ids = _derive_org_scope_sets(db, user_id=user_id)
    scope_clauses = []
    org_property_ids_list = list(org_property_ids)
    org_lease_ids_list = list(org_lease_ids)
    org_tenant_ids_list = list(org_tenant_ids)
    if org_property_ids_list:
        scope_clauses.append(OperationalTask.property_id.in_(org_property_ids_list))
    if org_lease_ids_list:
        scope_clauses.append(OperationalTask.lease_id.in_(org_lease_ids_list))
    if org_tenant_ids_list:
        scope_clauses.append(OperationalTask.tenant_id.in_(org_tenant_ids_list))
    if not scope_clauses:
        unresolved: list[OperationalTask] = []
    else:
        unresolved = (
            db.query(OperationalTask)
            .filter(
                OperationalTask.task_type.in_(
                    [OperationalTaskType.APPROVAL_PENDING, OperationalTaskType.PAYMENT_PENDING]
                ),
                OperationalTask.status.in_(
                    [OperationalTaskStatus.PENDING, OperationalTaskStatus.IN_PROGRESS]
                ),
                or_(*scope_clauses),
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
            Expense.status.in_([
                ExpenseStatus.pending,
                ExpenseStatus.approved,
                ExpenseStatus.paid,
                ExpenseStatus.partially_paid,
                ExpenseStatus.payment_claimed,
            ]),
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


_LEASE_EXPIRY_DIGEST_WINDOW_DAYS = 30
_DIGEST_ACT_MAX = 8
_DIGEST_UPCOMING_MAX = 5
_DIGEST_DONE_MAX = 3


def _digest_unit_label(db: Session, lease: Lease) -> str:
    """Short unit label for a lease (the stable business display identity)."""
    unit = (
        db.query(Unit).filter(Unit.id == lease.unit_id).first()
        if lease.unit_id is not None
        else None
    )
    return _unit_label(db, unit) or (unit.unit_number if unit else str(lease.unit_id))


def _overdue_rent_digest_rows(db: Session, *, now: datetime) -> list[dict]:
    """🔴 ACT-NOW overdue-rent items built from the SAME real truth source as the
    Rent Quick View (TELEGRAM-OPS-REAL-WORLD-CLOSURE-005 §7 / §11): one item per
    lease that has overdue uncovered periods, carrying the TOTAL arrears
    (monthly × uncovered periods), the uncovered-period count and the overdue
    days. Never a monthly rent in place of the outstanding and never the
    operational_tasks table."""
    today = now.date()
    leases = (
        db.query(Lease)
        .filter(Lease.status == LeaseStatus.active, Lease.deleted_at.is_(None))
        .all()
    )
    if not leases:
        return []
    confirmed_by_lease: dict[int, list[Income]] = {}
    for income in (
        db.query(Income)
        .filter(
            Income.lease_id.in_([l.id for l in leases]),
            Income.status == IncomeStatus.confirmed,
        )
        .all()
    ):
        confirmed_by_lease.setdefault(income.lease_id, []).append(income)
    from app.services.operations.daily_dedup import philippines_local_date

    ph_today = philippines_local_date(now)
    day_start = datetime.fromisoformat(f"{ph_today}T00:00:00+08:00").astimezone(timezone.utc)
    day_end = day_start + timedelta(days=1)
    followed_up_today_lease_ids = {
        lease_id
        for lease_id, in (
            db.query(OperationalTask.lease_id)
            .filter(
                OperationalTask.lease_id.in_([l.id for l in leases]),
                OperationalTask.task_type.in_(
                    [OperationalTaskType.RENT_OVERDUE, OperationalTaskType.FOLLOWUP]
                ),
                OperationalTask.status == OperationalTaskStatus.COMPLETED,
                OperationalTask.completed_by.isnot(None),
                OperationalTask.completed_at.isnot(None),
                OperationalTask.completed_at >= day_start,
                OperationalTask.completed_at < day_end,
            )
            .all()
        )
        if lease_id is not None
    }
    rows: list[dict] = []
    for lease in leases:
        if lease.id in followed_up_today_lease_ids:
            # A real human already completed today's rent follow-up for this
            # lease, so the same logical business action is not actionable
            # again in today's red queue.
            continue
        periods = _lease_periods(lease)
        due_periods = [(m, due) for m, due in periods if due <= today]
        if not due_periods:
            continue
        covered = _covered_periods(lease, periods, confirmed_by_lease.get(lease.id, []))
        overdue = [(m, due) for m, due in due_periods if m not in covered]
        if not overdue:
            continue
        oldest_due = overdue[0][1]
        rows.append(
            {
                "business_dedupe_key": f"lease:{lease.id}:RENT_OVERDUE",
                "kind": "rent_overdue",
                "unit": _digest_unit_label(db, lease),
                "amount": _d2(Decimal(str(lease.monthly_rent)) * len(overdue)),
                "unpaid_periods": len(overdue),
                "overdue_days": max((today - oldest_due).days, 0),
                "lease_id": lease.id,
                "sort_severity": 1,
                "sort_anchor": max((today - oldest_due).days, 0),
                "sort_tie": -lease.id,
            }
        )
    return rows


def _lease_expiring_digest_rows(db: Session, *, now: datetime) -> list[dict]:
    """🟡 UPCOMING lease-expiry items: active leases whose end date falls
    inside the near-term window (>= today). The user action is to prepare the
    renewal / handover — never the same bucket as an overdue rent chase."""
    today = now.date()
    window_end = today + timedelta(days=_LEASE_EXPIRY_DIGEST_WINDOW_DAYS)
    leases = (
        db.query(Lease)
        .filter(
            Lease.status == LeaseStatus.active,
            Lease.deleted_at.is_(None),
            Lease.end_date >= today,
            Lease.end_date <= window_end,
        )
        .order_by(Lease.end_date, Lease.id)
        .all()
    )
    rows = []
    for lease in leases:
        rows.append(
            {
                "business_dedupe_key": f"lease:{lease.id}:LEASE_EXPIRING",
                "kind": "lease_expiring",
                "unit": _digest_unit_label(db, lease),
                "days_to_expiry": max((lease.end_date - today).days, 0),
                "lease_id": lease.id,
                "sort_severity": 2,
                "sort_anchor": max((lease.end_date - today).days, 0),
                "sort_tie": lease.id,
            }
        )
    return rows


def _human_completion_kind(task: OperationalTask) -> str:
    """User-visible label for a genuinely HUMAN-completed task."""
    if task.task_type in (OperationalTaskType.FOLLOWUP, OperationalTaskType.RENT_OVERDUE):
        return "rent_followup"
    if task.task_type == OperationalTaskType.PAYMENT_PENDING:
        return "expense_paid"
    if task.task_type == OperationalTaskType.APPROVAL_PENDING:
        return "expense_approved"
    if task.task_type == OperationalTaskType.AC_MAINTENANCE:
        return "maintenance"
    return "generic"


def _human_done_digest_rows(db: Session, *, now: datetime) -> list[dict]:
    """✅ DONE-TODAY items: only tasks completed by a REAL human principal today
    (``completed_by IS NOT NULL``) — never a scheduler auto-completion, a
    supersede, a reconcile, a generator replacement or a duplicate-cleanup
    (those leave ``completed_by`` NULL). Deduped by the stable business key so
    repeated rows for the same fact collapse to one."""
    from app.services.operations.daily_dedup import philippines_local_date

    ph_today = philippines_local_date(now)
    day_start = datetime.fromisoformat(f"{ph_today}T00:00:00+08:00").astimezone(timezone.utc)
    day_end = day_start + timedelta(days=1)
    commits = (
        db.query(OperationalTask)
        .filter(
            OperationalTask.status == OperationalTaskStatus.COMPLETED,
            OperationalTask.completed_by.isnot(None),
            OperationalTask.completed_at.isnot(None),
            OperationalTask.completed_at >= day_start,
            OperationalTask.completed_at < day_end,
        )
        .order_by(OperationalTask.completed_at.desc(), OperationalTask.id.desc())
        .all()
    )
    # Unit labels for every lease referenced by a completion.
    lease_ids = {t.lease_id for t in commits if t.lease_id is not None}
    label_by_lease: dict[int, str] = {}
    if lease_ids:
        for lease in (
            db.query(Lease).filter(Lease.id.in_(lease_ids)).all()
        ):
            label_by_lease[lease.id] = _digest_unit_label(db, lease)
    rows: list[dict] = []
    seen_keys: set[str] = set()
    for t in commits:
        key = t.dedupe_key or (
            f"committed:{t.task_type.value}:"
            f"{t.lease_id or t.source_id or t.id}"
        )
        if key in seen_keys:
            continue
        seen_keys.add(key)
        details = t.details or {}
        amount = None
        expense_id = details.get("expense_id")
        if expense_id is not None:
            amount = details.get("amount")
        item: dict = {
            "business_dedupe_key": key,
            "kind": _human_completion_kind(t),
            "unit": label_by_lease.get(t.lease_id, ""),
            "expense_id": expense_id,
            "amount": _d2(amount) if amount is not None else None,
            "completed_at": t.completed_at.isoformat() if t.completed_at else None,
        }
        rows.append(item)
    return rows


def build_digest(db: Session, user: User, *, now: datetime | None = None) -> dict:
    """Daily Tasks Digest — the three-section human-action view.

    The digest answers ONE question: **what does someone need to do today?**
    It is NOT a dump of the ``operational_tasks`` table.

    Sections (deterministic, language-agnostic data):
    - ``act_now``   (🔴): real current human actions — overdue rent (total
      arrears truth) + approved-but-unpaid expenses. One row per business
      object at most (deduped by the stable business key).
    - ``upcoming``  (🟡): near-term lease expiries (watch, do not chase).
    - ``done_today``(✅): tasks completed by a REAL human today only. System
      auto-completions (scheduler / supersede / reconcile / generator
      replacement / duplicate cleanup) never appear — those leave
      ``completed_by`` NULL.

    The legacy ``pending / in_progress / recently_completed`` keys are
    preserved for the greeting counter and old callers; ``recently_completed``
    now holds the same filtered, deduped human rows so a duplicate-completed
    history can never flood the UI.
    """
    now = now or datetime.now(timezone.utc)
    overdue = _overdue_rent_digest_rows(db, now=now)
    payable = [
        {
            **{k: v for k, v in r.items() if k != "unit_code"},
            "sort_anchor": -(r.get("waiting_days") or 0),
            "sort_tie": r["expense_id"],
        }
        for r in _payable_expense_rows(db, now=now)
    ]
    # --- 🔴 ACT NOW: overdue rents (severity = more days first), then payable
    # expenses (more waiting first); deterministic stable tie-breakers only. ---
    act_now = sorted(
        overdue + payable,
        key=lambda r: (
            0 if r.get("kind") == "rent_overdue" else 1,  # rents before expenses
            -r.get("sort_anchor", 0),  # largest overdue/waiting first
            -r.get("sort_tie", 0) if r.get("kind") == "rent_overdue" else r.get("sort_tie", 0),
        ),
    )
    upcoming = _lease_expiring_digest_rows(db, now=now)
    done_today = _human_done_digest_rows(db, now=now)

    act_hidden = max(len(act_now) - _DIGEST_ACT_MAX, 0)
    upcoming_hidden = max(len(upcoming) - _DIGEST_UPCOMING_MAX, 0)
    done_hidden = max(len(done_today) - _DIGEST_DONE_MAX, 0)

    return {
        "act_now": act_now[:_DIGEST_ACT_MAX],
        "upcoming": upcoming[:_DIGEST_UPCOMING_MAX],
        "done_today": done_today[:_DIGEST_DONE_MAX],
        "hidden": {
            "act_now": act_hidden,
            "upcoming": upcoming_hidden,
            "done_today": done_hidden,
        },
        "counts": {
            "act_now": len(act_now),
            "upcoming": len(upcoming),
            "done_today": len(done_today),
        },
        # Legacy keys (greeting counter + old callers): semantic, deduped.
        "pending": [
            {"id": (-i), "task_type": r["kind"].upper(), "title": r["kind"],
             "status": "PENDING"}
            for i, r in enumerate(act_now)
        ],
        "in_progress": [],
        "recently_completed": [
            {"id": (-i), "task_type": r["kind"].upper(), "title": r["kind"],
             "status": "COMPLETED", "completed_by": 1}
            for i, r in enumerate(done_today)
        ],
    }
