"""REPAIR-AI-EMPLOYEE-WORKFLOW-008A — Expense/Payment → Repair coordination.

008A §6 / Case D: an Expense linked to a Repair Proposal is a FINANCIAL
record tracked separately from the Repair Operation. When that expense is
PAID:

- the repair advances toward verification (at most VERIFYING);
- it is NEVER auto-CLOSED, never auto-REJECTED, never auto-CANCELLED.

Payment is only one of the prerequisites for finishing a repair. Closure
requires a real verification (see ``app.services.repairs.verification``), and
that closure happens only through ``verify_and_close`` with a valid closure
signal.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.financial import Expense, ExpenseStatus
from app.models.repair import (
    RepairOperation,
    RepairOperationStatus,
    RepairProposal,
    RepairProposalStatus,
)


class PaymentCoordinationError(Exception):
    """Expense payment could not be reflected on the repair."""


def link_expense_to_proposal(
    db: Session,
    proposal: RepairProposal,
    expense: Expense,
    *,
    now: datetime | None = None,
) -> None:
    """Link a created Expense to the proposal that funded it.

    This establishes the read path Expense -> Repair WITHOUT ever merging the
    two objects: expense rows still live in ``expenses``; the repair is still
    a ``repair_operations`` row. The link is just an FK from the proposal."""
    now = now or datetime.now(timezone.utc)
    proposal.expense_id = expense.id
    proposal.updated_at = now
    db.flush()


def on_expense_paid(
    db: Session,
    repair: RepairOperation,
    *,
    paid_at: datetime | None = None,
) -> RepairOperation:
    """React to an Expense being PAID (idempotent).

    Advances WAITING_* repair to VERIFYING (the highest allowed state on
    payment). NEVER closes. A WAITING_PAYMENT repair that is already paid is a
    no-op if already beyond that point. Returns the repair.
    """
    paid_at = paid_at or datetime.now(timezone.utc)
    if repair.status == RepairOperationStatus.CLOSED:
        # Already closed earlier through verification — nothing to do.
        return repair
    if repair.status == RepairOperationStatus.CANCELLED:
        return repair
    if repair.status in (
        RepairOperationStatus.WAITING_PAYMENT,
        RepairOperationStatus.WAITING_APPROVAL,
    ):
        repair.status = RepairOperationStatus.VERIFYING
        repair.next_action = (
            "Expense paid. The repair now needs real-world verification before it "
            "can be closed."
        )
        repair.waiting_on = "secretary"
        repair.details = dict(repair.details or {})
        repair.details["expense_paid_at"] = paid_at.isoformat()
        repair.updated_at = paid_at
        db.flush()
    # All other states are left untouched — payment alone never closes.
    return repair
