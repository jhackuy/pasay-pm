"""Dashboard API — Owner command center with urgent items + next actions.

Issue #99 OWNER ADDENDUM: Mini App #/dashboard.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.permissions import PermissionDenied, Principal, require_org_scope
from app.v1.deps import get_current_principal, get_db_dep
from app.v1.models.base import OperationState, TaskState
from app.v1.models.expense import ExpenseClaim, ExpenseClaimStatus
from app.v1.models.move_out import MoveOut, MoveOutState
from app.v1.models.renewal import LeaseRenewal, RenewalState
from app.v1.models.rent_payment import Operation, RentDueSchedule, RentDueState, Task
from app.v1.models.repair import RepairReport, RepairState

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/home", response_model=dict[str, Any])
def home(
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> dict[str, Any]:
    """Dashboard home — urgent Operations + counts + next actions."""
    try:
        require_org_scope(principal, org_id)
    except PermissionDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    now = datetime.now(timezone.utc)

    open_operations = (
        db.query(Operation)
        .filter(
            Operation.org_id == org_id,
            Operation.state.in_((
                OperationState.OPEN.value,
                OperationState.IN_PROGRESS.value,
            )),
        )
        .order_by(Operation.id.desc())
        .limit(20)
        .all()
    )

    overdue_due_schedules = (
        db.query(RentDueSchedule)
        .filter(
            RentDueSchedule.org_id == org_id,
            RentDueSchedule.state.in_((
                RentDueState.DUE.value, RentDueState.OVERDUE.value,
            )),
            RentDueSchedule.due_date < now,
        )
        .order_by(RentDueSchedule.due_date.asc())
        .limit(20)
        .all()
    )

    open_repairs = (
        db.query(RepairReport)
        .filter(
            RepairReport.org_id == org_id,
            RepairReport.state.in_((
                RepairState.REPORTED.value,
                RepairState.CONFIRMED.value,
                RepairState.AWAITING_TECHNICIAN.value,
                RepairState.QUOTE_REQUESTED.value,
                RepairState.QUOTE_RECEIVED.value,
                RepairState.QUOTE_APPROVED.value,
                RepairState.IN_PROGRESS.value,
                RepairState.COMPLETION_CLAIMED.value,
            )),
        )
        .order_by(RepairReport.id.desc())
        .limit(20)
        .all()
    )

    pending_renewals = (
        db.query(LeaseRenewal)
        .filter(
            LeaseRenewal.org_id == org_id,
            LeaseRenewal.state == RenewalState.PROPOSED.value,
        )
        .order_by(LeaseRenewal.id.desc())
        .limit(20)
        .all()
    )

    open_move_outs = (
        db.query(MoveOut)
        .filter(
            MoveOut.org_id == org_id,
            MoveOut.state.in_((
                MoveOutState.REQUESTED.value,
                MoveOutState.INSPECTED.value,
            )),
        )
        .order_by(MoveOut.id.desc())
        .limit(20)
        .all()
    )

    pending_expense_claims = (
        db.query(ExpenseClaim)
        .filter(
            ExpenseClaim.org_id == org_id,
            ExpenseClaim.status.in_((
                ExpenseClaimStatus.SUBMITTED.value,
            )),
        )
        .order_by(ExpenseClaim.id.desc())
        .limit(20)
        .all()
    )

    open_tasks = (
        db.query(Task)
        .filter(
            Task.org_id == org_id,
            Task.state == TaskState.OPEN.value,
        )
        .order_by(Task.id.desc())
        .limit(20)
        .all()
    )

    return {
        "open_operations": [_op_view(o) for o in open_operations],
        "overdue_rent_count": len(overdue_due_schedules),
        "overdue_rent": [_due_view(d) for d in overdue_due_schedules],
        "open_repairs_count": len(open_repairs),
        "open_repairs": [_repair_view(r) for r in open_repairs],
        "pending_renewals_count": len(pending_renewals),
        "pending_renewals": [_renewal_view(r) for r in pending_renewals],
        "open_move_outs_count": len(open_move_outs),
        "open_move_outs": [_move_view(m) for m in open_move_outs],
        "pending_expense_claims_count": len(pending_expense_claims),
        "pending_expense_claims": [_expense_view(e) for e in pending_expense_claims],
        "open_tasks": [_task_view(t) for t in open_tasks],
        "generated_at": now.isoformat(),
    }


def _op_view(o: Operation) -> dict[str, Any]:
    return {
        "id": o.id,
        "kind": o.kind,
        "subject_type": o.subject_type,
        "subject_id": o.subject_id,
        "state": o.state,
        "due_at": o.due_at.isoformat() if o.due_at else None,
    }


def _due_view(d: RentDueSchedule) -> dict[str, Any]:
    return {
        "id": d.id,
        "lease_id": d.lease_id,
        "due_date": d.due_date.isoformat(),
        "monthly_rent": str(d.monthly_rent),
        "state": d.state,
    }


def _repair_view(r: RepairReport) -> dict[str, Any]:
    return {
        "id": r.id,
        "title": r.title,
        "state": r.state,
        "severity": r.severity,
        "category": r.category,
    }


def _renewal_view(r: LeaseRenewal) -> dict[str, Any]:
    return {
        "id": r.id,
        "lease_id": r.lease_id,
        "state": r.state,
        "proposed_start_date": (
            r.proposed_start_date.isoformat() if r.proposed_start_date else None
        ),
    }


def _move_view(m: MoveOut) -> dict[str, Any]:
    return {
        "id": m.id,
        "lease_id": m.lease_id,
        "state": m.state,
    }


def _expense_view(e: ExpenseClaim) -> dict[str, Any]:
    return {
        "id": e.id,
        "title": e.title,
        "category": e.category,
        "status": e.status,
        "claimed_amount": str(e.claimed_amount),
    }


def _task_view(t: Task) -> dict[str, Any]:
    return {
        "id": t.id,
        "operation_id": t.operation_id,
        "kind": t.kind,
        "state": t.state,
    }


__all__ = ["router", "home"]
