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
from app.models.financial import Expense, ExpenseStatus, Income, IncomeStatus
from app.models.lease import Lease, LeaseStatus
from app.models.operations import (
    OperationalTask,
    OperationalTaskStatus,
    OperationalTaskType,
)
from app.services.audit import record_audit, serialize_row
from app.services.operations.rent_math import covered_periods, lease_periods


def auto_transition(db: Session, task: OperationalTask, *, to: OperationalTaskStatus,
                     now: datetime, reason: str) -> bool:
    """Atomic PENDING->target transition; returns False if someone else
    already transitioned the task (no duplicate audit)."""
    values = {"status": to, "updated_at": now}
    if to == OperationalTaskStatus.COMPLETED:
        values["completed_at"] = now
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
        changed_fields={"status": ["PENDING", to.value], "reason": reason},
        old_value=serialize_row(task),
        new_value=serialize_row(task),
    )
    return True


def reconcile_tasks(db: Session, *, now: datetime) -> tuple[int, int]:
    """Settle stale PENDING tasks. Returns (auto_completed, auto_cancelled)."""
    completed = 0
    cancelled = 0
    tasks = (
        db.query(OperationalTask)
        .filter(OperationalTask.status == OperationalTaskStatus.PENDING)
        .all()
    )
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
    the unit."""
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
