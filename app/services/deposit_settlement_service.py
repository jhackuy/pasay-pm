"""M004 Lease & Move-out Truth Closure: Deposit Settlement Service.

Implements:
 1. Amount conservation (1c tolerance, aligned with M002 RentClaim)
 2. CONFIRMED -> write Income deductions + Expense refund rows with unique idempotency_key
 3. Forward projection sync: CONFIRMED -> complete linked DEPOSIT_SETTLEMENT OperationalTask
 4. Idempotent confirm (repeat request has no side effects beyond the first)
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.deposit_settlement import DepositSettlement, DepositSettlementStatus
from app.models.financial import Expense, ExpenseStatus, Income, IncomeStatus
from app.models.lease import Lease
from app.models.operations import OperationalTask, OperationalTaskStatus, OperationalTaskType
from app.services.audit import record_audit, serialize_row


_ONE_CENT = Decimal("0.01")


def _jsonb_safe_deductions(deductions):
    """Return JSONB-serializable deduction items (Decimal amounts → quantized str, round-trip safe)."""
    if not deductions:
        return deductions
    out = []
    for raw in deductions:
        item = dict(raw) if not isinstance(raw, dict) else raw.copy()
        amount = item.get("amount")
        if amount is not None and isinstance(amount, Decimal):
            item["amount"] = f"{amount.quantize(Decimal('0.01')):.2f}"
        elif amount is not None and not isinstance(amount, str):
            item["amount"] = f"{Decimal(str(amount)).quantize(Decimal('0.01')):.2f}"
        out.append(item)
    return out

_ALLOWED_SETTLEMENT_TRANSITIONS: dict[DepositSettlementStatus, set[DepositSettlementStatus]] = {
    DepositSettlementStatus.DRAFT: {DepositSettlementStatus.CONFIRMED},
    DepositSettlementStatus.CONFIRMED: {DepositSettlementStatus.RECONCILED},
    DepositSettlementStatus.RECONCILED: set(),
}


def validate_settlement_transition(
    current: DepositSettlementStatus, target: DepositSettlementStatus
) -> None:
    allowed = _ALLOWED_SETTLEMENT_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "settlement_transition_invalid",
                "from_status": current.value,
                "to_status": target.value,
                "hint": f"Cannot transition {current.value} -> {target.value}. Valid targets: {sorted(s.value for s in allowed)}",
            },
        )


def check_amount_conservation(settlement: DepositSettlement) -> tuple[bool, Decimal]:
    """Return (ok, gap). gap = |deposit_received - (total_deductions + refund_amount)|.

    ok == True when gap <= 1c.
    """
    deposit = Decimal(str(settlement.deposit_received))
    deductions = Decimal(str(settlement.total_deductions))
    refund = Decimal(str(settlement.refund_amount))
    gap = abs(deposit - (deductions + refund))
    return gap <= _ONE_CENT, gap


def update_settlement(
    db: Session,
    settlement: DepositSettlement,
    *,
    updates: dict,
    actor_id: int,
) -> DepositSettlement:
    """PATCH fields for DRAFT settlement; enforce CONFIRMED is immutable via this path."""
    if settlement.status != DepositSettlementStatus.DRAFT:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "settlement_not_editable",
                "status": settlement.status.value,
                "hint": "Settlement is no longer DRAFT; create a new DRAFT if adjustments are required.",
            },
        )
    if "deductions" in updates:
        updates["deductions"] = _jsonb_safe_deductions(updates["deductions"])
    old = serialize_row(settlement)
    changed: dict = {}
    for field, value in updates.items():
        current = getattr(settlement, field, None)
        if current != value:
            setattr(settlement, field, value)
            changed[field] = [str(current), str(value)]
    settlement.updated_by = actor_id
    if changed:
        record_audit(
            db,
            table_name="deposit_settlements",
            record_id=settlement.id,
            action="update",
            actor_id=actor_id,
            changed_fields=changed,
            old_value=old,
            new_value=serialize_row(settlement),
        )
    return settlement


def confirm_settlement(
    db: Session,
    settlement: DepositSettlement,
    *,
    confirmed_at: datetime,
    confirmed_by: int,
) -> DepositSettlement:
    """Idempotent: when already CONFIRMED/RECONCILED, return unchanged with no side effects."""
    if settlement.status in (DepositSettlementStatus.CONFIRMED, DepositSettlementStatus.RECONCILED):
        return settlement
    target = DepositSettlementStatus.CONFIRMED
    validate_settlement_transition(settlement.status, target)
    deductions = settlement.deductions or []
    if deductions:
        try:
            deductions_sum = sum(Decimal(str(item["amount"])) for item in deductions)
        except (KeyError, TypeError, ValueError):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "reason": "deposit_settlement_deduction_sum_mismatch",
                    "settlement_id": settlement.id,
                    "hint": "Each deduction item must have a numeric 'amount' field.",
                },
            )
        total_deductions = Decimal(str(settlement.total_deductions))
        gap_d = abs(deductions_sum - total_deductions)
        if gap_d > Decimal("0.01"):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "reason": "deposit_settlement_deduction_sum_mismatch",
                    "settlement_id": settlement.id,
                    "deductions_sum": str(deductions_sum),
                    "total_deductions": str(total_deductions),
                    "gap": str(gap_d),
                    "tolerance_cents": 1,
                },
            )
    ok, gap = check_amount_conservation(settlement)
    if not ok:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "deposit_settlement_not_conserved",
                "settlement_id": settlement.id,
                "deposit_received": str(settlement.deposit_received),
                "total_deductions": str(settlement.total_deductions),
                "refund_amount": str(settlement.refund_amount),
                "gap": str(gap),
                "tolerance_cents": 1,
                "hint": "Adjust total_deductions + refund_amount to equal deposit_received within 1c.",
            },
        )
    old = serialize_row(settlement)
    settlement.status = target
    settlement.confirmed_at = confirmed_at
    settlement.confirmed_by = confirmed_by
    settlement.updated_by = confirmed_by
    record_audit(
        db,
        table_name="deposit_settlements",
        record_id=settlement.id,
        action="confirm",
        actor_id=confirmed_by,
        changed_fields={
            "status": [old.get("status"), target.value],
            "confirmed_at": [None, confirmed_at.isoformat()],
        },
        old_value=old,
        new_value=serialize_row(settlement),
    )
    # --- Idempotent financial row generation ---
    _write_financial_rows_for_settlement(db, settlement, confirmed_at, confirmed_by)
    # --- Forward projection sync ---
    _close_projection_tasks_for_settlement(db, settlement, confirmed_by, confirmed_at)
    return settlement


def mark_reconciled(
    db: Session,
    settlement: DepositSettlement,
    *,
    actor_id: int,
    now: datetime,
) -> DepositSettlement:
    if settlement.status == DepositSettlementStatus.RECONCILED:
        return settlement
    target = DepositSettlementStatus.RECONCILED
    validate_settlement_transition(settlement.status, target)
    old = serialize_row(settlement)
    settlement.status = target
    settlement.updated_by = actor_id
    record_audit(
        db,
        table_name="deposit_settlements",
        record_id=settlement.id,
        action="reconcile",
        actor_id=actor_id,
        changed_fields={"status": [old.get("status"), target.value]},
        old_value=old,
        new_value=serialize_row(settlement),
    )
    return settlement


def _write_financial_rows_for_settlement(
    db: Session,
    settlement: DepositSettlement,
    confirmed_at: datetime,
    confirmed_by: int,
) -> None:
    """Create Income (deductions) and Expense (refund) rows with unique idempotency keys.

    Idempotency: if rows for this settlement already exist, skip entirely.
    Uses savepoints + IntegrityError handling to handle concurrent unique key races.
    """
    from app.models.property import Property, Unit

    unit = None
    property_id: int | None = None
    lease = db.get(Lease, settlement.lease_id)
    if lease is not None:
        unit = db.get(Unit, lease.unit_id)
    if unit is not None:
        property_id = unit.property_id

    deductions = list(settlement.deductions or [])
    if not deductions and Decimal(str(settlement.total_deductions)) > Decimal("0"):
        deductions = [{"description": f"押金扣款汇总 #{settlement.id}", "amount": str(settlement.total_deductions)}]
    processed_income_ids: dict[int, int] = {}
    for i, item in enumerate(deductions):
        amount = Decimal(str(item.get("amount", 0)))
        if amount <= Decimal("0"):
            continue
        ikey = f"deposit_settlement:{settlement.id}:deduction:{i}"
        existing = db.query(Income).filter(Income.idempotency_key == ikey).first()
        if existing is not None:
            processed_income_ids[i] = existing.id
            continue
        desc = item.get("description") or f"押金扣款 - Settlement #{settlement.id}"
        income = Income(
            lease_id=settlement.lease_id,
            amount=amount,
            received_date=confirmed_at.date() if isinstance(confirmed_at, datetime) else date.today(),
            idempotency_key=ikey,
            status=IncomeStatus.confirmed,
            description=f"[DepositSettlement #{settlement.id}] {desc}",
            confirmed_by=confirmed_by,
            confirmed_at=confirmed_at,
        )
        income.created_by = confirmed_by
        income.updated_by = confirmed_by
        try:
            tx = db.begin_nested()
            db.add(income)
            db.flush()
            tx.commit()
            processed_income_ids[i] = income.id
            record_audit(
                db,
                table_name="incomes",
                record_id=income.id,
                action="create",
                actor_id=confirmed_by,
                new_value=serialize_row(income),
            )
        except IntegrityError:
            tx.rollback()
            existing_row = db.query(Income).filter(Income.idempotency_key == ikey).first()
            if existing_row is not None:
                processed_income_ids[i] = existing_row.id

    if processed_income_ids and deductions:
        updated = []
        for j, item in enumerate(deductions):
            d = dict(item)
            if j in processed_income_ids and not d.get("income_id"):
                d["income_id"] = processed_income_ids[j]
            updated.append(d)
        settlement.deductions = updated
        settlement.updated_by = confirmed_by

    refund = Decimal(str(settlement.refund_amount))
    if refund > Decimal("0"):
        ekey = f"deposit_settlement:{settlement.id}:refund"
        existing_exp = db.query(Expense).filter(Expense.idempotency_key == ekey).first()
        if existing_exp is None:
            exp = Expense(
                expense_date=confirmed_at.date() if isinstance(confirmed_at, datetime) else date.today(),
                due_date=None,
                category="deposit_refund",
                amount=refund,
                payee="TENANT_REFUND",
                description=f"[DepositSettlement #{settlement.id}] 押金退款 - Lease #{settlement.lease_id}",
                property_id=property_id or 0,
                unit_id=unit.id if unit else None,
                status=ExpenseStatus.pending,
                payer_user_id=None,
                idempotency_key=ekey,
            )
            exp.created_by = confirmed_by
            exp.updated_by = confirmed_by
            if exp.property_id == 0:
                exp.property_id = property_id or exp.property_id
            try:
                tx = db.begin_nested()
                db.add(exp)
                db.flush()
                tx.commit()
                record_audit(
                    db,
                    table_name="expenses",
                    record_id=exp.id,
                    action="create",
                    actor_id=confirmed_by,
                    new_value=serialize_row(exp),
                )
            except IntegrityError:
                tx.rollback()


def _close_projection_tasks_for_settlement(
    db: Session,
    settlement: DepositSettlement,
    actor_id: int,
    now: datetime,
) -> None:
    tasks = (
        db.query(OperationalTask)
        .filter(
            OperationalTask.task_type == OperationalTaskType.DEPOSIT_SETTLEMENT,
            OperationalTask.status == OperationalTaskStatus.PENDING,
            (
                (OperationalTask.source_type == "deposit_settlement")
                & (OperationalTask.source_id == settlement.id)
            )
            | (OperationalTask.dedupe_key == f"deposit_settlement:{settlement.id}:DEPOSIT_SETTLEMENT"),
        )
        .all()
    )
    for t in tasks:
        old_row = serialize_row(t)
        if t.source_id is None:
            t.source_id = settlement.id
            t.source_type = "deposit_settlement"
        t.status = OperationalTaskStatus.COMPLETED
        t.updated_at = now
        t.completed_at = now
        t.completed_by = actor_id
        t.reminder_generation = t.reminder_generation + 1
        record_audit(
            db,
            table_name="operational_tasks",
            record_id=t.id,
            action="task_auto_completed",
            actor_id=None,
            changed_fields={
                "status": [OperationalTaskStatus.PENDING.value, OperationalTaskStatus.COMPLETED.value],
                "reason": "deposit_settlement_confirmed_forward_sync",
            },
            old_value=old_row,
            new_value=serialize_row(t),
        )
