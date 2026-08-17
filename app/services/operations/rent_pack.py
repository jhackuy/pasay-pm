"""PASAY-AI-EMPLOYEE-FOUNDATION-007 §11-§15 — Rent Action Pack builder.

A Rent Action Pack is the FULL execution package the Secretary needs to call a
tenant — never just "去催租". Every fact comes from structured DB truth (§14:
LLM never fabricates tenant/amount/period/date/overdue/promise/method). The
call and message scripts are phrases injected with the SAME truth, so the
Secretary never re-organizes language.

``build_rent_action_pack`` returns an ``assignable`` flag: when the tenant
phone is missing/invalid the pack is NOT assignable and carries the resolver
hint (the Owner must supply the phone first — §12).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.financial import Income, IncomeStatus
from app.models.lease import Lease, LeaseStatus
from app.models.property import Unit
from app.models.tenant import Tenant
from app.services.operations.quick import (
    _covered_periods,
    _lease_periods,
    _month_from_income,
)
from app.services.operations.resolver import (
    RISK_LOW,
    suggested_fix_command,
)

logger = logging.getLogger(__name__)


def _harmonize(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def build_rent_action_pack(
    db: Session, unit_id: int, *, now: datetime | None = None
) -> dict:
    """Deterministic Rent Action Pack for one unit's lease."""
    now = now or datetime.now(timezone.utc)
    today = now.date()
    unit = db.query(Unit).filter(Unit.id == unit_id, Unit.deleted_at.is_(None)).first()
    if unit is None:
        return {"unit_id": unit_id, "assignable": False, "error": "unit_not_found"}
    lease = (
        db.query(Lease)
        .filter(Lease.unit_id == unit_id, Lease.status == LeaseStatus.active,
                Lease.deleted_at.is_(None))
        .first()
    )
    if lease is None:
        return {"unit_id": unit_id, "unit_number": unit.unit_number,
                "assignable": False, "error": "no_active_lease"}
    tenant = db.get(Tenant, lease.tenant_id)

    # --- structured rent truth (same source as Rent Quick View) ---
    periods = _lease_periods(lease)
    due_periods = [(m, due) for m, due in periods if due <= today]
    confirmed = (
        db.query(Income)
        .filter(Income.lease_id == lease.id, Income.status == IncomeStatus.confirmed)
        .all()
    )
    covered = _covered_periods(lease, periods, confirmed)
    overdue = [(m, due) for m, due in due_periods if m not in covered]
    outstanding = _harmonize(lease.monthly_rent) * len(overdue) if overdue else Decimal("0.00")
    oldest_due = overdue[0][1] if overdue else None
    overdue_days = max((today - oldest_due).days, 0) if oldest_due else 0

    # --- latest payment promise (from prior follow-up details) ---
    latest_promise, payment_method = _latest_promise_and_method(db, lease.id)

    phone = (tenant.phone or "").strip() if tenant else ""
    secondary = (tenant.secondary_phone or "").strip() if tenant else ""
    available_phone = phone or secondary
    contact_status = (tenant.contact_status.value if tenant and tenant.contact_status else "")

    assignable = bool(available_phone) and contact_status != "WRONG_NUMBER"
    blocked_hint = ""
    if not assignable:
        blocked_hint = suggested_fix_command(
            "TENANT_PHONE_MISSING", unit=unit.unit_number
        )

    pack = {
        "unit_id": unit.id,
        "unit_number": unit.unit_number,
        "tenant_name": tenant.full_name if tenant else "",
        "tenant_phone": available_phone or "",
        "contact_status": contact_status,
        "outstanding_total": str(outstanding),
        "outstanding_periods": len(overdue),
        "unpaid_periods": [m for m, _ in overdue],
        "overdue_days": overdue_days,
        "last_follow_up": _last_follow_up(db, lease.id),
        "latest_promise": latest_promise,
        "payment_method": payment_method,
        "assignable": assignable,
        "blocked_hint": blocked_hint,
        "call_script": _call_script(
            tenant.full_name if tenant else "",
            unit.unit_number,
            outstanding,
            len(overdue),
            overdue_days,
            latest_promise,
        ),
        "message_script": _message_script(
            tenant.full_name if tenant else "",
            unit.unit_number,
            outstanding,
            len(overdue),
        ),
    }
    return pack


def _latest_promise_and_method(db: Session, lease_id: int):
    from app.models.operations import OperationalTask, OperationalTaskType, OperationalTaskStatus

    tasks = (
        db.query(OperationalTask)
        .filter(
            OperationalTask.lease_id == lease_id,
            OperationalTask.task_type.in_(
                [OperationalTaskType.RENT_OVERDUE, OperationalTaskType.FOLLOWUP]
            ),
        )
        .order_by(OperationalTask.updated_at.desc(), OperationalTask.id.desc())
        .all()
    )
    for t in tasks:
        details = t.details or {}
        promise = details.get("promise")
        if isinstance(promise, dict):
            return {
                "amount": promise.get("amount"),
                "promised_date": promise.get("promised_date") or promise.get("follow_up_at"),
                "note": promise.get("note"),
                "status": promise.get("status"),
            }, details.get("payment_method")
        if details.get("payment_method"):
            return None, details.get("payment_method")
    return None, None


def _last_follow_up(db: Session, lease_id: int):
    from app.models.operations import OperationalTask, OperationalTaskType, OperationalTaskStatus

    executed = (
        db.query(OperationalTask)
        .filter(
            OperationalTask.lease_id == lease_id,
            OperationalTask.task_type.in_(
                [OperationalTaskType.RENT_OVERDUE, OperationalTaskType.FOLLOWUP]
            ),
            OperationalTask.status == OperationalTaskStatus.COMPLETED,
            OperationalTask.completed_at.isnot(None),
        )
        .order_by(OperationalTask.completed_at.desc(), OperationalTask.id.desc())
        .first()
    )
    return executed.completed_at.isoformat() if executed else None


def _money(value) -> str:
    v = _harmonize(value)
    return f"₱{v:,.2f}"


def _call_script(tenant, unit, outstanding, periods, days, promise) -> str:
    """Deterministic phone script injected ONLY with structured truth (§14)."""
    out = _money(outstanding)
    if periods == 1:
        period_str = "1 rental period"
    else:
        period_str = f"{periods} rental periods"
    line = (
        f"Hi {tenant}, this is a call about the rent for Unit {unit}. "
        f"Your current outstanding balance is {out} covering {period_str}."
    )
    if promise and promise.get("promised_date"):
        line += f" We have a promise on record for {promise.get('promised_date')}."
    if days:
        line += f" This has been pending for {days} days."
    line += " Can you confirm when we can expect the payment? Thank you."
    return line


def _message_script(tenant, unit, outstanding, periods) -> str:
    """Copy-paste SMS / Telegram text built from the same truth (§15)."""
    out = _money(outstanding)
    if periods == 1:
        period_str = "1 rental period"
    else:
        period_str = f"{periods} rental periods"
    return (
        f"Hi {tenant}, this is a reminder regarding the rent for Unit {unit}. "
        f"The current outstanding balance is {out} covering {period_str}. "
        "Please let us know when payment can be expected. Thank you."
    )
