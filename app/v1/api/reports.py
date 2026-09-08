"""V1 reports surface — Owner Home financial / overdue reads.

Issue #119 P0 (Telegram six-menu V1 contract repair): the Telegram bot
``show_home`` calls ``get_financial_summary``, ``get_overdue_rents``
and ``get_units`` in parallel (see
``pasay-telegram-bot/pasay_bot/handlers/commands.py::show_home``).
When ANY of those 404s the Home view fails closed with the
``⚠️⚠️ 获取数据失败`` card. The legacy ``app/api/routers/reports.py``
exposed ``/reports/financial-summary`` and ``/reports/overdue-rents``,
but its legacy schema does not exist in production.

This module re-implements the same two endpoints against the real
``v1_*`` production schema, preserving the response shapes the bot's
``FinancialSummary.from_dict`` / ``OverdueRent.from_dict`` already
consume.

Auth: HUMAN Principal via ``get_current_principal`` (mirrors the rest
of the V1 surface — Owner / Secretary membership required, SYSTEM
credentials rejected by the dependency).

Read-only. Never touches write paths.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.permissions import (
    PermissionDenied,
    Principal,
    require_org_scope,
)
from app.v1.deps import get_current_principal, get_db_dep
from app.v1.models.expense import ExpenseClaim, ExpenseClaimStatus
from app.v1.models.property import Property, Unit
from app.v1.models.base import UnitStatus
from app.v1.models.rent_payment import (
    RentDueSchedule,
    RentDueState,
    RentPayment,
)
from app.v1.models.tenant_lease import Lease, Tenant
from app.v1.models.base import LeaseState


router = APIRouter(prefix="/reports", tags=["reports"])


_TWO_PLACES = Decimal("0.01")


def _money(value: Any) -> str:
    if value is None:
        return "0.00"
    if isinstance(value, Decimal):
        return format(value.quantize(_TWO_PLACES), "f")
    return format(Decimal(str(value)).quantize(_TWO_PLACES), "f")


def _month_range(month: str) -> tuple[date, datetime]:
    """Inclusive [start, end) for the given YYYY-MM month, in UTC."""
    year, mon = month.split("-")
    start = date(int(year), int(mon), 1)
    if int(mon) == 12:
        end = date(int(year) + 1, 1, 1)
    else:
        end = date(int(year), int(mon) + 1, 1)
    return start, datetime.combine(end, datetime.min.time(), tzinfo=timezone.utc)


@router.get("/financial-summary")
def financial_summary(
    org_id: int | None = Query(default=None, gt=0),
    month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> dict[str, Any]:
    """V1 Financial Summary: expected / collected / outstanding rent +
    total income + total expense + net income + unit occupancy counts.

    V1 schema:
      * ``v1_leases`` (state='ACTIVE') covers the month → expected rent
      * ``v1_rent_payments`` (status='VERIFIED') for the month → collected
      * ``v1_expense_claims`` (status in {VERIFIED,SETTLED}) for the month
        → total expense

    Money fields are emitted as Decimal strings (matches the legacy
    contract + ``FinancialSummary.from_dict``).

    Issue #119 P0 (Telegram six-menu V1 contract repair): the bot's
    ``PasayApiClient.get_financial_summary()`` does NOT send
    ``org_id`` (it derives the org from the credential), so this
    endpoint defaults to ``principal.org_id`` when the query
    parameter is absent.
    """
    if org_id is not None:
        try:
            require_org_scope(principal, org_id)
        except PermissionDenied as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    else:
        org_id = principal.org_id

    resolved = month or date.today().strftime("%Y-%m")
    start, end = _month_range(resolved)

    leases = (
        db.query(Lease)
        .filter(
            Lease.org_id == org_id,
            Lease.state == LeaseState.ACTIVE.value,
            Lease.start_date < end.date(),
            Lease.end_date >= start,
        )
        .all()
    )
    expected_rent_total = sum(
        (Decimal(l.monthly_rent) for l in leases), Decimal("0"),
    )

    collected_rent = Decimal("0")
    if leases:
        due_schedules = (
            db.query(RentDueSchedule)
            .filter(
                RentDueSchedule.org_id == org_id,
                RentDueSchedule.lease_id.in_([l.id for l in leases]),
                RentDueSchedule.period_start >= start,
                RentDueSchedule.period_start < end.date(),
            )
            .all()
        )
        if due_schedules:
            payments = (
                db.query(RentPayment)
                .filter(
                    RentPayment.org_id == org_id,
                    RentPayment.due_schedule_id.in_([s.id for s in due_schedules]),
                    RentPayment.status == "VERIFIED",
                )
                .all()
            )
            for p in payments:
                if p.verified_amount is not None:
                    collected_rent += Decimal(p.verified_amount)
    outstanding_rent = expected_rent_total - collected_rent

    total_income = collected_rent  # V1 truth: VERIFIED rent IS the income.

    expense_claims = (
        db.query(ExpenseClaim)
        .filter(
            ExpenseClaim.org_id == org_id,
            ExpenseClaim.created_at >= datetime.combine(
                start, datetime.min.time(), tzinfo=timezone.utc,
            ),
            ExpenseClaim.created_at < end,
            ExpenseClaim.status.in_(
                (
                    ExpenseClaimStatus.VERIFIED.value,
                    ExpenseClaimStatus.SETTLED.value,
                )
            ),
        )
        .all()
    )
    total_expense = sum(
        (Decimal(c.claimed_amount) for c in expense_claims), Decimal("0"),
    )

    units = db.query(Unit).filter(Unit.org_id == org_id).all()
    units_count = len(units)
    occupied_units = sum(1 for u in units if u.status == UnitStatus.OCCUPIED.value)
    vacant_units = sum(1 for u in units if u.status == UnitStatus.AVAILABLE.value)

    return {
        "month": resolved,
        "expected_rent_total": _money(expected_rent_total),
        "collected_rent": _money(collected_rent),
        "outstanding_rent": _money(outstanding_rent),
        "total_income": _money(total_income),
        "total_expense": _money(total_expense),
        "net_income": _money(total_income - total_expense),
        "units_count": int(units_count),
        "occupied_units": int(occupied_units),
        "vacant_units": int(vacant_units),
    }


@router.get("/overdue-rents")
def overdue_rents(
    org_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[dict[str, Any]]:
    """V1 Overdue Rents: list of leases with at least one uncovered
    DUE/OVERDUE schedule, paginated.

    Each row carries the legacy shape ``OverdueRent.from_dict``
    expects:
      ``lease_id``, ``unit_id``, ``tenant_id``, ``unit``, ``tenant``,
      ``overdue_months`` (= number of overdue periods), list of
      ``overdue_periods`` (each ``{month, amount}``), ``amount_per_month``,
      ``total_outstanding``, ``oldest_due_date``, ``overdue_days``,
      ``outstanding`` (= total_outstanding), ``days_overdue``.

    Issue #119 P0 (Telegram six-menu V1 contract repair): the bot's
    ``PasayApiClient.get_overdue_rents()`` does NOT send ``org_id``
    (it derives the org from the credential), so this endpoint
    defaults to ``principal.org_id`` when the query parameter is
    absent.
    """
    if org_id is not None:
        try:
            require_org_scope(principal, org_id)
        except PermissionDenied as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    else:
        org_id = principal.org_id

    today = date.today()
    overdue_schedules = (
        db.query(RentDueSchedule)
        .filter(
            RentDueSchedule.org_id == org_id,
            RentDueSchedule.state.in_(
                (RentDueState.DUE.value, RentDueState.OVERDUE.value)
            ),
            RentDueSchedule.due_date < today,
        )
        .order_by(RentDueSchedule.due_date)
        .all()
    )
    lease_ids = sorted({s.lease_id for s in overdue_schedules if s.lease_id is not None})
    leases = (
        db.query(Lease).filter(Lease.id.in_(lease_ids)).all() if lease_ids else []
    )
    lease_by_id = {l.id: l for l in leases}
    units = (
        db.query(Unit).filter(Unit.org_id == org_id).all()
    )
    unit_by_id = {u.id: u for u in units}
    tenants = (
        db.query(Tenant).filter(Tenant.org_id == org_id).all()
    )
    tenant_by_id = {t.id: t for t in tenants}

    verified_by_schedule: dict[int, Decimal] = {}
    if overdue_schedules:
        payments = (
            db.query(RentPayment)
            .filter(
                RentPayment.org_id == org_id,
                RentPayment.due_schedule_id.in_(
                    [s.id for s in overdue_schedules]
                ),
                RentPayment.status == "VERIFIED",
            )
            .all()
        )
        for p in payments:
            amount = (
                Decimal(p.verified_amount) if p.verified_amount is not None
                else Decimal("0")
            )
            verified_by_schedule[p.due_schedule_id] = (
                verified_by_schedule.get(p.due_schedule_id, Decimal("0")) + amount
            )

    # Bucket overdue schedules by lease.
    overdue_by_lease: dict[int, list[RentDueSchedule]] = {}
    for sched in overdue_schedules:
        if sched.lease_id is None:
            continue
        if (
            verified_by_schedule.get(sched.id, Decimal("0"))
            >= Decimal(sched.amount_due)
        ):
            continue  # period is fully covered → not "overdue"
        overdue_by_lease.setdefault(sched.lease_id, []).append(sched)

    items: list[dict[str, Any]] = []
    for lease_id, scheds in overdue_by_lease.items():
        lease = lease_by_id.get(lease_id)
        if lease is None or lease.unit_id is None:
            continue
        unit = unit_by_id.get(lease.unit_id)
        tenant = (
            tenant_by_id.get(lease.tenant_id) if lease.tenant_id is not None else None
        )
        unit_label = str(getattr(unit, "label", "") or "") if unit else ""
        tenant_label = str(getattr(tenant, "full_name", "") or "") if tenant else ""
        monthly_rent = Decimal(lease.monthly_rent)

        # Issue #119 P0 (Telegram six-menu V1 contract repair — partial-
        # payment truth fix): each overdue schedule contributes only its
        # UNCOVERED amount (``max(amount_due - verified, 0)``) to the
        # totals + per-period ``amount``. ``quick_rent()`` already uses
        # this exact rule (see ``app/v1/api/quick_ops.py``); the Home
        # overdue card must agree — otherwise the Owner sees VERIFIED
        # partial payments re-counted as outstanding, which is a real
        # data-truth bug (e.g. 18,000 due / 10,000 verified must show
        # 8,000 outstanding, not 18,000).
        total_outstanding = Decimal("0")
        period_rows: list[tuple[RentDueSchedule, Decimal]] = []
        for sched in scheds:
            amount_due = Decimal(sched.amount_due)
            verified = verified_by_schedule.get(sched.id, Decimal("0"))
            uncovered = amount_due - verified
            total_outstanding += uncovered
            period_rows.append((sched, uncovered))
        oldest = min(s.due_date for s, _ in period_rows)
        periods = [
            {
                "month": s.period_start.strftime("%Y-%m"),
                "amount": _money(uncovered),
            }
            for s, uncovered in sorted(
                period_rows, key=lambda r: r[0].period_start,
            )
        ]
        items.append(
            {
                "lease_id": int(lease_id),
                "unit_id": int(lease.unit_id) if lease.unit_id is not None else 0,
                "tenant_id": int(lease.tenant_id) if lease.tenant_id is not None else 0,
                "unit": unit_label,
                "tenant": tenant_label,
                "overdue_months": len(period_rows),
                "overdue_periods": periods,
                "amount_per_month": _money(monthly_rent),
                "total_outstanding": _money(total_outstanding),
                "oldest_due_date": oldest.isoformat(),
                "overdue_days": max((today - oldest).days, 0),
                # Backward-compatible aliases (legacy / bot readers).
                "outstanding": _money(total_outstanding),
                "days_overdue": max((today - oldest).days, 0),
            }
        )
    items.sort(key=lambda r: r["overdue_days"], reverse=True)
    total = len(items)
    paged = items[offset:offset + limit]
    # Issue #119 P0 (Telegram six-menu V1 contract repair): the bot's
    # ``PasayApiClient.get_overdue_rents()`` iterates the raw response
    # as a flat list (``for d in data`` → ``OverdueRent.from_dict(d)``)
    # and ``len(overdue_list)`` for the home overdue counter. We
    # therefore return a FLAT LIST (the legacy V1.3 contract before
    # ``Paginated[OverdueRent]`` was introduced) so the bot keeps
    # working without a single line of change.
    return paged


__all__ = ["router"]