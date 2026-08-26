"""Reconciliation: auto-complete / auto-cancel tasks whose source state no
longer warrants an active reminder. Writes task_auto_completed /
task_auto_cancelled audit rows with actor_id=None (system).

The system must never become an "expired-reminder factory": if the source
changed through any other entry point (API, another worker, another user),
the next reconcile pass settles the task.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.commission import CommissionSettlement, CommissionSettlementStatus
from app.models.deposit_settlement import DepositSettlement, DepositSettlementStatus
from app.models.financial import Expense, ExpenseStatus, Income, IncomeStatus
from app.models.lease import Lease, LeaseStatus
from app.models.move_out import MoveOutInspection, MoveOutInspectionStatus
from app.models.operations import (
    OperationalTask,
    OperationalTaskStatus,
    OperationalTaskType,
)
from app.models.repair import RepairOperation, RepairOperationStatus
from app.services.audit import record_audit, serialize_row
from app.services.identity import bind_internal_audit
from app.services.operations.redelivery import suppress_pending_redeliveries
from app.services.operations.rent_math import covered_periods, lease_periods


def auto_transition(db: Session, task: OperationalTask, *, to: OperationalTaskStatus,
                     now: datetime, reason: str) -> bool:
    """Atomic PENDING->target transition; returns False if someone else
    already transitioned the task (no duplicate audit)."""
    values = {"status": to, "updated_at": now}
    if to == OperationalTaskStatus.COMPLETED:
        values["completed_at"] = now
    # Bump the reminder generation so any in-flight / enqueued snooze reminder
    # for this task is invalidated at the notifier's claim-time validation.
    values["reminder_generation"] = OperationalTask.reminder_generation + 1
    old_row = serialize_row(task)
    result = db.execute(
        update(OperationalTask)
        .where(
            OperationalTask.id == task.id,
            OperationalTask.status == OperationalTaskStatus.PENDING,
        )
        .values(**values),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount != 1:
        return False
    task.status = to
    task.reminder_generation += 1
    if to == OperationalTaskStatus.COMPLETED:
        task.completed_at = now
    record_audit(
        db,
        table_name="operational_tasks",
        record_id=task.id,
        action=(
            "task_auto_completed" if to == OperationalTaskStatus.COMPLETED
            else "task_auto_cancelled"
        ),
        actor_id=None,
        changed_fields={
            "status": ["PENDING", to.value],
            "reason": reason,
            "reminder_generation": [old_row.get("reminder_generation", 0), task.reminder_generation],
        },
        old_value=old_row,
        new_value=serialize_row(task),
    )
    # Drop any already-enqueued-but-unsent snooze reminders for this task.
    suppress_pending_redeliveries(db, task.id, actor_id=None, reason=reason, now=now)
    return True


def reconcile_tasks(db: Session, *, now: datetime, org_id: int | None = None) -> tuple[int, int]:
    """Settle stale PENDING tasks. Returns (auto_completed, auto_cancelled).

    ``org_id`` scopes the scan via the canonical 3-channel OR (property / lease
    / tenant → organization) fail-closed pattern; None preserves the global
    worker-path behavior (standalone daemon owns all tenants).
    """
    bind_internal_audit(db, "reconcile")
    completed = 0
    cancelled = 0
    query = db.query(OperationalTask).filter(OperationalTask.status == OperationalTaskStatus.PENDING)
    if org_id is not None:
        from app.services.operations.summary import _scoped_task_query
        query = query.filter(_scoped_task_query(db, org_id))
    tasks = query.all()
    for task in tasks:
        outcome = _reconcile_one(db, task, now=now)
        if outcome == "completed":
            completed += 1
        elif outcome == "cancelled":
            cancelled += 1
    return completed, cancelled


def _reconcile_one(db: Session, task: OperationalTask, *, now: datetime) -> str | None:
    """Return 'completed' / 'cancelled' / None (stays PENDING)."""
    if task.source_type == "expense" and task.task_type in (
        OperationalTaskType.PAYMENT_PENDING,
        OperationalTaskType.APPROVAL_PENDING,
    ):
        expense = db.get(Expense, task.source_id)
        if expense is None:
            return None
        if task.task_type == OperationalTaskType.PAYMENT_PENDING:
            if expense.status == ExpenseStatus.paid:
                return _transition(db, task, OperationalTaskStatus.COMPLETED, now, "expense_paid")
            if expense.status in (ExpenseStatus.rejected, ExpenseStatus.reversed):
                return _transition(db, task, OperationalTaskStatus.CANCELLED, now, "expense_closed")
        else:  # APPROVAL_PENDING
            if expense.status in (ExpenseStatus.approved, ExpenseStatus.paid):
                return _transition(db, task, OperationalTaskStatus.COMPLETED, now, "expense_approved")
            if expense.status in (ExpenseStatus.rejected, ExpenseStatus.reversed):
                return _transition(db, task, OperationalTaskStatus.CANCELLED, now, "expense_closed")
        return None

    if task.task_type == OperationalTaskType.SETTLEMENT_PENDING:
        settlement = db.get(CommissionSettlement, task.source_id)
        if settlement is None:
            return None
        if settlement.status == CommissionSettlementStatus.confirmed:
            return _transition(db, task, OperationalTaskStatus.COMPLETED, now, "settlement_confirmed")
        return None

    if task.task_type in (
        OperationalTaskType.RENT_DUE,
        OperationalTaskType.RENT_OVERDUE,
        OperationalTaskType.LEASE_EXPIRING,
    ) and task.source_type == "lease":
        lease = db.get(Lease, task.source_id)
        if lease is None:
            return None
        if lease.status != LeaseStatus.active or lease.deleted_at is not None:
            renewed = _lease_renewed(db, lease)
            return _transition(
                db, task,
                OperationalTaskStatus.COMPLETED if renewed else OperationalTaskStatus.CANCELLED,
                now,
                "lease_renewed" if renewed else "lease_inactive",
            )
        # Still active: complete rent tasks whose periods are now covered.
        if task.task_type in (OperationalTaskType.RENT_DUE, OperationalTaskType.RENT_OVERDUE):
            periods = lease_periods(lease)
            incomes = (
                db.query(Income)
                .filter(Income.lease_id == lease.id, Income.status == IncomeStatus.confirmed)
                .all()
            )
            covered = covered_periods(lease, periods, incomes)
            if _rent_covered(task, covered):
                return _transition(db, task, OperationalTaskStatus.COMPLETED, now, "rent_paid")
        return None

    repair_id: int | None = None
    if task.source_type == "repair":
        repair_id = task.source_id
    if repair_id is None:
        dedupe = task.dedupe_key or ""
        if dedupe.startswith("repair:"):
            try:
                repair_id = int(dedupe.split(":", 2)[1])
            except (ValueError, IndexError):
                repair_id = None
    if repair_id is None:
        details = task.details or {}
        meta_rid = (details.get("repair") or {}).get("id") if isinstance(details.get("repair"), dict) else None
        if meta_rid is not None:
            try:
                repair_id = int(meta_rid)
            except (ValueError, TypeError):
                repair_id = None
    repair_like = repair_id is not None
    if repair_like and repair_id is not None:
        repair = db.get(RepairOperation, repair_id)
        if repair is None:
            return None
        if repair.status == RepairOperationStatus.CLOSED:
            return _transition(db, task, OperationalTaskStatus.COMPLETED, now, "repair_closed")
        if repair.status == RepairOperationStatus.CANCELLED:
            return _transition(db, task, OperationalTaskStatus.CANCELLED, now, "repair_cancelled")
        return None

    if task.task_type == OperationalTaskType.MOVE_OUT_INSPECTION:
        # #14 orphan: provisional tasks source_type=lease source_id=lease.id are NEVER orphans;
        # only treat as orphan when BOTH source_id is None AND source_type not in BUSINESS_SOURCE_TYPES
        # AND cannot match by dedupe_key to active lease/inspection.
        BUSINESS_SOURCE_TYPES = frozenset({"lease", "expense", "commission_settlement"})
        is_business_orphan_safe = (
            task.source_type in BUSINESS_SOURCE_TYPES and task.source_id is not None
        )
        dedupe = task.dedupe_key or ""
        lease_id_from_dedupe: int | None = None
        if dedupe.startswith("lease:"):
            try:
                lease_id_from_dedupe = int(dedupe.split(":", 3)[1])
            except (ValueError, IndexError):
                lease_id_from_dedupe = None
        matched_by_dedupe = False
        lease_for_dedupe: Lease | None = None
        if lease_id_from_dedupe is not None:
            lk = db.get(Lease, lease_id_from_dedupe)
            # --- M8: dedupe match requires lease EXISTS + NOT deleted + status == active ONLY ---
            if lk is not None and lk.deleted_at is None and lk.status == LeaseStatus.active:
                matched_by_dedupe = True
                lease_for_dedupe = lk
            ik = (
                db.query(MoveOutInspection)
                .filter(
                    MoveOutInspection.lease_id == lease_id_from_dedupe,
                    MoveOutInspection.status.in_([
                        MoveOutInspectionStatus.SCHEDULED,
                        MoveOutInspectionStatus.INSPECTED,
                    ]),
                )
                .first()
            )
            if ik is not None:
                matched_by_dedupe = True
        # --- M8: inactive lease + no pending inspection → sourceless task CANCELLED ---
        # Case 1: matched_by_dedupe has a lease but it's INACTIVE (not active) + no SCHEDULED/INSPECTED inspection
        if lease_id_from_dedupe is not None and lease_for_dedupe is None:
            lk_raw = db.get(Lease, lease_id_from_dedupe)
            lease_inactive = False
            if lk_raw is None:
                lease_inactive = True
            elif lk_raw.deleted_at is not None:
                lease_inactive = True
            elif lk_raw.status != LeaseStatus.active:
                lease_inactive = True
            pending_insp_exists = (
                db.query(MoveOutInspection.id)
                .filter(
                    MoveOutInspection.lease_id == lease_id_from_dedupe,
                    MoveOutInspection.status.in_([
                        MoveOutInspectionStatus.SCHEDULED,
                        MoveOutInspectionStatus.INSPECTED,
                    ]),
                )
                .first()
                is not None
            )
            no_source_binding = task.source_id is None and task.source_type is None
            branch_a = no_source_binding
            branch_b = no_source_binding and (not is_business_orphan_safe and not matched_by_dedupe)
            is_sourceless_provisional = branch_a or branch_b
            if lease_inactive and not pending_insp_exists and is_sourceless_provisional:
                if task.status != OperationalTaskStatus.CANCELLED:
                    return _transition(
                        db, task, OperationalTaskStatus.CANCELLED, now,
                        "move_out_task_cancelled_inactive_lease_no_pending_inspection",
                    )
                return None
        if task.source_id is None and not is_business_orphan_safe and not matched_by_dedupe:
            return _transition(db, task, OperationalTaskStatus.CANCELLED, now, "move_out_inspection_task_orphan_source_id_none_fail_closed")
        if task.source_type == "move_out_inspection" and task.source_id is not None:
            inspection = db.get(MoveOutInspection, task.source_id)
            if inspection is None:
                return None
            if inspection.status == MoveOutInspectionStatus.CONFIRMED:
                return _transition(db, task, OperationalTaskStatus.COMPLETED, now, "move_out_inspection_confirmed")
            if inspection.status == MoveOutInspectionStatus.CANCELLED:
                return _transition(db, task, OperationalTaskStatus.CANCELLED, now, "move_out_inspection_cancelled")
        elif task.source_type == "lease" and task.source_id is not None:
            # Provisional task bound to lease; inspect lease truth + any inspection
            lease = db.get(Lease, task.source_id)
            if lease is None or lease.deleted_at is not None:
                return None
            if lease.status != LeaseStatus.active:
                return _transition(db, task, OperationalTaskStatus.CANCELLED, now, "move_out_inspection_provisional_lease_inactive")
            meta = lease.renewal_metadata or {}
            if not meta.get("not_renewed"):
                return _transition(db, task, OperationalTaskStatus.CANCELLED, now, "move_out_inspection_provisional_not_renewed_cleared")
            # If an active inspection now exists for this lease, it stays PENDING (forward sync handles it)
        return None

    if task.task_type == OperationalTaskType.DEPOSIT_SETTLEMENT:
        if task.source_id is None:
            return _transition(db, task, OperationalTaskStatus.CANCELLED, now, "deposit_settlement_task_orphan_source_id_none_fail_closed")
        if task.source_type == "deposit_settlement":
            settlement = db.get(DepositSettlement, task.source_id)
            if settlement is None:
                return None
            if settlement.status in (DepositSettlementStatus.CONFIRMED, DepositSettlementStatus.RECONCILED):
                return _transition(db, task, OperationalTaskStatus.COMPLETED, now, "deposit_settlement_confirmed")
        return None

    return None


def _rent_covered(task: OperationalTask, covered: set[str]) -> bool:
    details = task.details or {}
    if task.task_type == OperationalTaskType.RENT_OVERDUE:
        periods = details.get("periods") or []
        return bool(periods) and all(p in covered for p in periods)
    period = details.get("period")
    return bool(period) and period in covered


def _lease_renewed(db: Session, lease: Lease) -> bool:
    """True when another active lease exists for the same unit that started
    at/after this lease ended (a renewal), or an active lease now occupies
    the unit with a later end_date (overlapping early-renewal contract)."""
    renewed = (
        db.query(Lease)
        .filter(
            Lease.unit_id == lease.unit_id,
            Lease.id != lease.id,
            Lease.status == LeaseStatus.active,
            Lease.deleted_at.is_(None),
            Lease.end_date > lease.end_date,
            Lease.start_date <= lease.end_date,
        )
        .first()
    )
    if renewed is None:
        renewed = (
            db.query(Lease)
            .filter(
                Lease.unit_id == lease.unit_id,
                Lease.id != lease.id,
                Lease.status == LeaseStatus.active,
                Lease.deleted_at.is_(None),
                Lease.start_date >= lease.end_date,
            )
            .first()
        )
    return renewed is not None


def _transition(db, task, to, now, reason) -> str:
    if auto_transition(db, task, to=to, now=now, reason=reason):
        return "completed" if to == OperationalTaskStatus.COMPLETED else "cancelled"
    return None
