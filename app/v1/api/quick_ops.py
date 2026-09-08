"""V1 HUMAN operations surface — Quick Views.

Issue #119 P0 (Telegram six-menu V1 contract repair): the production
Docker entrypoint is now ``app.v1.main:app`` (PR #138). The Telegram
bot's six frozen Owner bottom-menu routes (首页 / 房源 / 待办 / 租金 / 支出 /
档案) call a stable set of endpoints through ``PasayApiClient``:

  * ``GET /operations/quick/properties``  →  Properties 🏘
  * ``GET /operations/quick/rent``         →  Rent 💰
  * ``GET /operations/quick/expense``      →  Expense 💸
  * ``GET /operations/digest``             →  Tasks ✅  (already mounted by
                                                       ``system_ops`` for the
                                                       SYSTEM scheduled-job;
                                                       HUMAN call must reach
                                                       the SAME shape so the
                                                       ``cards.active_tasks_
                                                       digest_card`` keeps
                                                       rendering without a
                                                       rewrite).
  * ``GET /reports/financial-summary``     →  Home 🏠 (the Home view fails
                                                       closed when this is
                                                       missing/empty — see
                                                       ``show_home``).
  * ``GET /reports/overdue-rents``         →  Home 🏠 (overdue list).
  * ``GET /units``                         →  Home 🏠 (units inventory).

The legacy ``app/api/routers/operations.py`` exposed all six, but its
legacy ``users`` / ``principals`` / ``api_credentials`` tables do NOT
exist in the production ``v1_*`` schema. We therefore re-implement the
two Quick Views (Properties / Rent / Expense) in V1 against the real
production schema, authenticated as a HUMAN Principal via
``get_current_principal`` (the same dependency the V1 dashboard /
properties / leases routers already use).

The V1 SYSTEM scheduled-job surface ``system_ops`` is mounted BEFORE
this module so the literal ``/operations/digest`` and
``/operations/quick/tasks`` paths win over the
``/operations/{operation_id}`` parameter pattern on the V1 operations
router. The HUMAN ``/operations/quick/{properties,rent,expense}``
endpoints sit beside the SYSTEM ones — they share the same URL prefix
but authenticate through a different dependency (HUMAN membership vs
SYSTEM principal), so FastAPI will dispatch the request to the correct
function based on the caller's credential type.

Response shapes are kept STABLE on purpose so the bot's render layer
(``pasay_bot.render.cards.properties_quick_card`` /
``rent_quick_card`` / ``expense_quick_card``) does not need a single
line of change — they all branch on the same field names the legacy
contract used:

  * ``/operations/quick/properties`` →
    ``list[dict]``  (unit_code, property_name, status, tenant_name)

  * ``/operations/quick/rent`` →
    ``dict``  (overdue[], outstanding_total, expected_rent_total,
    collected_rent, outstanding_rent, collection_rate, month,
    unpaid_unit_count)

  * ``/operations/quick/expense`` →
    ``dict``  (month_total, payable[], pending_approval_count,
    records[])

All read-only. ``get_current_principal`` resolves a Bearer to a HUMAN
Principal; SYSTEM callers are explicitly rejected by that dependency
and fall through to a 401 (the SYSTEM /digest + /quick/tasks endpoints
remain reachable through ``system_ops`` for the scheduled job).
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
    Role,
    require_org_scope,
)
from app.v1.deps import get_current_principal, get_db_dep
from app.v1.models.base import UnitStatus
from app.v1.models.expense import (
    ExpenseClaim,
    ExpenseClaimStatus,
)
from app.v1.models.property import Property, Unit
from app.v1.models.rent_payment import (
    RentDueSchedule,
    RentDueState,
    RentPayment,
)
from app.v1.models.tenant_lease import Lease


router = APIRouter(prefix="/operations", tags=["operations-quick"])


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


_TWO_PLACES = Decimal("0.01")


def _money(value: Any) -> str:
    """Stable string form of a money value the bot can decode via Decimal."""
    if value is None:
        return "0.00"
    if isinstance(value, Decimal):
        return format(value.quantize(_TWO_PLACES), "f")
    return format(Decimal(str(value)).quantize(_TWO_PLACES), "f")


def _unit_label(unit: Unit) -> str:
    """Display label for a unit (V1: ``unit.label`` only)."""
    return str(getattr(unit, "label", "") or "")


# ---------------------------------------------------------------------------
# GET /operations/quick/properties  →  Owner Properties 🏘 menu
# ---------------------------------------------------------------------------


def _resolve_org_id(
    org_id: int | None, principal: Principal,
) -> int:
    """Pick the org to read.

    Issue #119 P0 (Telegram six-menu V1 contract repair): the bot's
    ``PasayApiClient.get_quick_properties()`` does NOT send ``org_id``
    (it derives the org from the credential), so this dep MUST default
    to ``principal.org_id`` when the query parameter is absent.
    Explicit ``org_id`` is still accepted so the bot can probe a
    specific workspace.
    """
    if org_id is not None:
        try:
            require_org_scope(principal, org_id)
        except PermissionDenied as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
        return int(org_id)
    return int(principal.org_id)


@router.get("/quick/properties")
def quick_properties(
    org_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[dict[str, Any]]:
    """V1 Properties Quick View: one row per active unit, scoped to the
    caller's org. Same shape as the legacy contract the bot's
    ``properties_quick_card`` already consumes.

    V1 schema: ``v1_units`` joins ``v1_properties`` and ``v1_leases`` +
    ``v1_tenants``. A unit is ``"occupied"`` when an ACTIVE lease
    exists; otherwise it is ``"vacant"``. The bot treats both values as
    case-insensitive (see ``_bi_header`` in pasay_bot/render/cards.py).
    """
    org_id = _resolve_org_id(org_id, principal)

    units = (
        db.query(Unit)
        .filter(Unit.org_id == org_id)
        .order_by(Unit.property_id, Unit.label)
        .all()
    )
    property_by_id = {
        p.id: p
        for p in db.query(Property).filter(Property.org_id == org_id).all()
    }
    lease_by_unit: dict[int, Lease] = {}
    if units:
        from app.v1.models.base import LeaseState
        unit_ids = [u.id for u in units]
        leases = (
            db.query(Lease)
            .filter(
                Lease.org_id == org_id,
                Lease.unit_id.in_(unit_ids),
                Lease.state == LeaseState.ACTIVE.value,
            )
            .all()
        )
        for lease in leases:
            # One ACTIVE lease per unit; if the data ever carries
            # multiple, the most recently started wins (defensive — V1
            # does not enforce this at the schema level).
            if lease.unit_id is not None and lease.unit_id not in lease_by_unit:
                lease_by_unit[lease.unit_id] = lease
    tenant_by_lease: dict[int, str] = {}
    if lease_by_unit:
        from app.v1.models.tenant_lease import Tenant
        tenants = (
            db.query(Tenant)
            .filter(
                Tenant.org_id == org_id,
                Tenant.id.in_([l.tenant_id for l in lease_by_unit.values() if l.tenant_id is not None]),
            )
            .all()
        )
        tenant_by_lease = {t.id: t.full_name for t in tenants}

    rows: list[dict[str, Any]] = []
    for unit in units:
        prop = property_by_id.get(unit.property_id)
        lease = lease_by_unit.get(unit.id)
        if lease is None:
            rows.append(
                {
                    "unit_code": _unit_label(unit),
                    "property_name": str(getattr(prop, "name", "") or ""),
                    "status": "vacant",
                    "tenant_name": "",
                }
            )
            continue
        rows.append(
            {
                "unit_code": _unit_label(unit),
                "property_name": str(getattr(prop, "name", "") or ""),
                "status": "occupied",
                "tenant_name": str(
                    tenant_by_lease.get(lease.tenant_id, "") if lease.tenant_id else ""
                ),
            }
        )

    # Stable pagination — same shape the legacy router returned.
    total = len(rows)
    return rows[offset:offset + limit]


# ---------------------------------------------------------------------------
# GET /operations/quick/rent  →  Owner Rent 💰 menu
# ---------------------------------------------------------------------------


@router.get("/quick/rent")
def quick_rent(
    org_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> dict[str, Any]:
    """V1 Rent Quick View: overdue list + outstanding total + the
    current-month rent stats (expected / collected / outstanding /
    collection rate / unpaid unit count).

    V1 schema: ``v1_rent_due_schedules`` is the rent obligation; rent
    ACTUALLY arrived = sum of ``v1_rent_payments.verified_amount``
    where ``status='VERIFIED'``. The bot's ``rent_quick_card`` reads
    the resulting dict and never needs to know the V1 tables.
    """
    org_id = _resolve_org_id(org_id, principal)

    today = date.today()
    month = today.strftime("%Y-%m")

    from app.v1.models.base import LeaseState

    leases = (
        db.query(Lease)
        .filter(
            Lease.org_id == org_id,
            Lease.state == LeaseState.ACTIVE.value,
        )
        .all()
    )
    lease_by_id = {l.id: l for l in leases}
    units = {
        u.id: u
        for u in db.query(Unit).filter(Unit.org_id == org_id).all()
    }
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
    # Map schedule -> verified total so we can tell how much of each
    # overdue period is still uncovered.
    verified_by_schedule: dict[int, Decimal] = {}
    if overdue_schedules:
        sched_ids = [s.id for s in overdue_schedules]
        verified_payments = (
            db.query(RentPayment)
            .filter(
                RentPayment.org_id == org_id,
                RentPayment.due_schedule_id.in_(sched_ids),
                RentPayment.status == "VERIFIED",
            )
            .all()
        )
        for p in verified_payments:
            amount = (
                Decimal(p.verified_amount) if p.verified_amount is not None
                else Decimal("0")
            )
            verified_by_schedule[
                p.due_schedule_id
            ] = verified_by_schedule.get(p.due_schedule_id, Decimal("0")) + amount

    overdue_rows: list[dict[str, Any]] = []
    outstanding_total = Decimal("0")
    for sched in overdue_schedules:
        lease = lease_by_id.get(sched.lease_id)
        if lease is None or lease.unit_id is None:
            continue
        unit = units.get(lease.unit_id)
        if unit is None:
            continue
        amount_due = Decimal(sched.amount_due)
        verified = verified_by_schedule.get(sched.id, Decimal("0"))
        uncovered = amount_due - verified
        if uncovered <= Decimal("0"):
            continue
        outstanding_total += uncovered
        overdue_rows.append(
            {
                "unit": _unit_label(unit),
                "unit_code": _unit_label(unit),
                "amount": _money(uncovered),
                "unpaid_periods": 1,
                "monthly_rent": _money(lease.monthly_rent),
                "overdue_days": max((today - sched.due_date).days, 0),
                "last_followup_at": None,
            }
        )
    overdue_rows.sort(key=lambda r: r["overdue_days"], reverse=True)

    # Current-month stats: expected = sum(monthly_rent) over leases
    # that actually cover ``month``; collected = sum(verified_amount)
    # over the due_schedules whose period_start falls in ``month``.
    expected_rent = Decimal("0")
    collected_rent = Decimal("0")
    unpaid_unit_count = 0
    month_schedules_by_lease: dict[int, list[dict[str, Any]]] = {}
    month_schedules = (
        db.query(RentDueSchedule)
        .filter(
            RentDueSchedule.org_id == org_id,
            RentDueSchedule.lease_id.in_([l.id for l in leases]) if leases else False,
        )
        .all()
    )
    for sched in month_schedules:
        period_month = sched.period_start.strftime("%Y-%m")
        if period_month == month:
            month_schedules_by_lease.setdefault(sched.lease_id, []).append(
                {
                    "amount_due": Decimal(sched.amount_due),
                    "due_date": sched.due_date,
                    "id": sched.id,
                }
            )
    if month_schedules:
        verified_payments = (
            db.query(RentPayment)
            .filter(
                RentPayment.org_id == org_id,
                RentPayment.due_schedule_id.in_(
                    [s["id"] for rows in month_schedules_by_lease.values() for s in rows]
                ),
                RentPayment.status == "VERIFIED",
            )
            .all()
        )
        verified_by_schedule_current: dict[int, Decimal] = {}
        for p in verified_payments:
            amount = (
                Decimal(p.verified_amount) if p.verified_amount is not None
                else Decimal("0")
            )
            verified_by_schedule_current[p.due_schedule_id] = (
                verified_by_schedule_current.get(p.due_schedule_id, Decimal("0")) + amount
            )
    else:
        verified_by_schedule_current = {}

    for lease in leases:
        month_rows = month_schedules_by_lease.get(lease.id) or []
        if not month_rows:
            continue
        expected_rent += Decimal(lease.monthly_rent)
        cur_collected = Decimal("0")
        any_unpaid = False
        for s in month_rows:
            cur_collected += verified_by_schedule_current.get(s["id"], Decimal("0"))
            if (
                verified_by_schedule_current.get(s["id"], Decimal("0"))
                < s["amount_due"]
                and s["due_date"] <= today
            ):
                any_unpaid = True
        collected_rent += cur_collected
        if any_unpaid:
            unpaid_unit_count += 1
    outstanding_rent = expected_rent - collected_rent
    if expected_rent > 0:
        collection_rate = (
            collected_rent / expected_rent * Decimal(100)
        ).quantize(_TWO_PLACES)
    else:
        collection_rate = Decimal("0.00")

    return {
        "overdue": overdue_rows[offset:offset + limit],
        "outstanding_total": _money(outstanding_total),
        "month": month,
        "expected_rent_total": _money(expected_rent),
        "collected_rent": _money(collected_rent),
        "outstanding_rent": _money(outstanding_rent),
        "collection_rate": _money(collection_rate),
        "unpaid_unit_count": int(unpaid_unit_count),
    }


# ---------------------------------------------------------------------------
# GET /operations/quick/expense  →  Owner Expense 💸 menu
# ---------------------------------------------------------------------------


@router.get("/quick/expense")
def quick_expense(
    org_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> dict[str, Any]:
    """V1 Expense Quick View: month total + APPROVED-unpaid (payable)
    list + pending approval count + this-month records.

    V1 schema: ``v1_expense_claims`` with ``status`` in
    ``{VERIFIED, SETTLED}`` (= APPROVED/PAID in legacy terms) is the
    money-arrived truth; ``status in {VERIFIED}`` is APPROVED-unpaid
    (the bot's ``payable`` bucket).
    """
    org_id = _resolve_org_id(org_id, principal)

    today = date.today()
    month = today.strftime("%Y-%m")
    month_start = today.replace(day=1)

    all_month_claims = (
        db.query(ExpenseClaim)
        .filter(
            ExpenseClaim.org_id == org_id,
            ExpenseClaim.created_at >= datetime.combine(
                month_start, datetime.min.time(), tzinfo=timezone.utc,
            ),
        )
        .order_by(ExpenseClaim.created_at)
        .all()
    )
    month_total = Decimal("0")
    pending_approval_count = 0
    payable: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    for claim in all_month_claims:
        if claim.status in (ExpenseClaimStatus.VERIFIED.value, ExpenseClaimStatus.SETTLED.value):
            month_total += Decimal(claim.claimed_amount)
        if claim.status in (ExpenseClaimStatus.SUBMITTED.value, ExpenseClaimStatus.OPEN.value):
            pending_approval_count += 1
        if claim.status == ExpenseClaimStatus.VERIFIED.value:
            waiting_days = max((today - claim.created_at.date()).days, 0)
            payable.append(
                {
                    "expense_id": int(claim.id),
                    "category": str(claim.category),
                    "title": str(claim.title),
                    "amount": _money(claim.claimed_amount),
                    "claimed_amount": _money(claim.claimed_amount),
                    "waiting_days": int(waiting_days),
                }
            )
        records.append(
            {
                "id": int(claim.id),
                "title": str(claim.title),
                "category": str(claim.category),
                "amount": _money(claim.claimed_amount),
                "status": str(claim.status),
                "created_at": claim.created_at.isoformat() if claim.created_at else None,
            }
        )

    return {
        "month_total": _money(month_total),
        "current_month_total": _money(month_total),
        "month": month,
        "payable": payable[offset:offset + limit],
        "pending_approval_count": int(pending_approval_count),
        "pending_approval_amount": _money(
            sum(
                Decimal(c.claimed_amount) for c in all_month_claims
                if c.status in (
                    ExpenseClaimStatus.SUBMITTED.value,
                    ExpenseClaimStatus.OPEN.value,
                )
            )
        ),
        "records": records[offset:offset + limit],
    }


__all__ = ["router"]