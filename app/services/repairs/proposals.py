"""REPAIR-AI-EMPLOYEE-WORKFLOW-008A — Repair Proposal service.

A Proposal is ONE versioned solution candidate for a Repair Operation. It is
fully decoupled from the Repair: rejecting a proposal NEVER rejects the repair
(008A core Gate §3). History is preserved — V1 is never overwritten or deleted;
each version is a distinct ``repair_proposals`` row under ``(repair_id,
version)``.

Key invariants (tested by Cases A / B / D):
- submit_proposal -> the proposal is PENDING and the repair moves to
  WAITING_APPROVAL (or stays OPEN when owner approval is not yet required).
- reject_proposal -> ONLY the proposal becomes REJECTED (stored reason+actor);
  the repair stays alive (WAITING_HUMAN for requote). NEVER CLOSED/CANCELLED.
- approve_proposal -> the proposal becomes APPROVED and the repair moves to
  WAITING_PAYMENT (NEVER CLOSED). An Expense may be linked so payment is
  tracked separately from the repair.
- A next proposal (V2) must NOT be created while a previous version is still
  APPROVED — you do not requote an already-approved plan. Only when the latest
  is REJECTED/SUPERSEDED may a new version be submitted.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.repair import (
    RepairOperation,
    RepairOperationStatus,
    RepairProposal,
    RepairProposalStatus,
)


class ProposalError(Exception):
    """Invalid proposal operation (e.g. requoting an approved plan)."""


def _money_decimal(amount) -> Decimal:
    return Decimal(str(amount)).quantize(Decimal("0.01"))


def latest_proposal(db: Session, repair_id: int) -> RepairProposal | None:
    """The highest-version proposal for a repair (or None)."""
    return (
        db.query(RepairProposal)
        .filter(RepairProposal.repair_id == repair_id)
        .order_by(RepairProposal.version.desc())
        .first()
    )


def list_proposals(db: Session, repair_id: int) -> list[RepairProposal]:
    return (
        db.query(RepairProposal)
        .filter(RepairProposal.repair_id == repair_id)
        .order_by(RepairProposal.version.asc())
        .all()
    )


def submit_proposal(
    db: Session,
    repair: RepairOperation,
    *,
    amount,
    vendor: str | None = None,
    source: str | None = None,
    description: str | None = None,
    submitted_by: int | None = None,
    now: datetime | None = None,
) -> tuple[RepairProposal, int]:
    """Submit the NEXT versioned proposal (V1, V2, ...) for a repair.

    Returns ``(proposal, version)``. Version auto-increments from the latest.
    Refuses to submit a new version while the latest is APPROVED (an approved
    plan is never silently re-quoted unless it is first superseded/rejected).
    """
    now = now or datetime.now(timezone.utc)
    latest = latest_proposal(db, repair.id)
    if latest is not None and latest.status == RepairProposalStatus.APPROVED:
        raise ProposalError(
            f"Proposal V{latest.version} is already APPROVED; "
            "re-quote is not allowed until it is superseded or the repair moves on"
        )
    next_version = (latest.version + 1) if latest else 1
    proposal = RepairProposal(
        repair_id=repair.id,
        version=next_version,
        vendor=vendor,
        source=source,
        description=description,
        amount=_money_decimal(amount),
        submitted_by=submitted_by,
        submitted_at=now,
        status=RepairProposalStatus.PENDING,
        created_by=submitted_by,
    )
    db.add(proposal)
    db.flush()
    # Submitting the next proposal resolves the earlier requote work entry: the
    # old "get another quote" task completes (008A-F gate B §3.3) so it leaves
    # the Secretary queue and stops reminding, while this new proposal waits on
    # owner approval. The requote goes COMPLETED; history stays in repair_actions.
    if next_version > 1:
        from app.services.repairs.delivery import close_requote_projection
        close_requote_projection(db, repair, now=now)
        _complete_prior_requote_actions(db, repair.id, now=now)
    # Repair waits on owner approval for this candidate (never closes).
    repair.status = RepairOperationStatus.WAITING_APPROVAL
    repair.next_action = f"Proposal V{next_version} awaits owner decision."
    repair.waiting_on = "owner"
    repair.updated_at = now
    return proposal, next_version


def _complete_prior_requote_actions(db: Session, repair_id: int, *, now: datetime) -> None:
    """Complete active REQUOTE repair_actions once a newer proposal is submitted
    (they are superseded by the new candidate). History is preserved (Q row left
    as COMPLETED, never deleted)."""
    from app.models.repair import RepairAction, RepairActionStatus

    actions = (
        db.query(RepairAction)
        .filter(
            RepairAction.repair_id == repair_id,
            RepairAction.action_kind == "REQUOTE",
            RepairAction.status.in_(
                [RepairActionStatus.PENDING, RepairActionStatus.IN_PROGRESS]
            ),
        )
        .all()
    )
    for action in actions:
        action.status = RepairActionStatus.COMPLETED
        action.resolved_at = now
        action.updated_at = now


def reject_proposal(
    db: Session,
    repair: RepairOperation,
    proposal: RepairProposal,
    *,
    rejected_by: int | None = None,
    reason: str | None = None,
    now: datetime | None = None,
) -> None:
    """REJECT one proposal version. The Repair Operation MUST stay alive.

    Core 008A rule: ``Owner rejected -> Repair CLOSED`` is a P0 error. Here we
    mark ONLY this proposal REJECTED, record the reason/actor, and move the
    repair to WAITING_HUMAN so the continuation engine can create a requote /
    next-step action. The repair is never closed or cancelled by this call.
    """
    now = now or datetime.now(timezone.utc)
    if proposal.status in (RepairProposalStatus.APPROVED, RepairProposalStatus.REJECTED):
        raise ProposalError(
            f"Proposal V{proposal.version} is already "
            f"{proposal.status.value}; cannot reject it again"
        )
    proposal.status = RepairProposalStatus.REJECTED
    proposal.decision_by = rejected_by
    proposal.decision_at = now
    proposal.rejection_reason = reason
    proposal.updated_by = rejected_by
    proposal.updated_at = now
    db.flush()

    # Repair remains OPEN/alive and now waits on a human to continue.
    repair.status = RepairOperationStatus.WAITING_HUMAN
    repair.next_action = (
        f"Owner rejected quote V{proposal.version}"
        + (f" ({reason})" if reason else "")
        + ". Get another quote / propose an alternative — repair stays open."
    )
    repair.waiting_on = "secretary"
    repair.blocked_reason = reason or "Owner rejected the quote"
    repair.updated_at = now
    repair.updated_by = rejected_by


def approve_proposal(
    db: Session,
    repair: RepairOperation,
    proposal: RepairProposal,
    *,
    approved_by: int | None = None,
    now: datetime | None = None,
) -> Expense | None:
    """APPROVE the proposal VERSION. The repair moves to WAITING_PAYMENT —
    it is NOT closed (payment has not happened, and even payment alone would
    not close it; only verification does).

    PASAY-MILESTONE-002 (quote ≠ expense): if no Expense is already linked to
    the proposal, this path creates one at approval time. The RepairProposal
    remains the authoritative QUOTE record (status APPROVED with amount =
    proposal.amount); the Expense is the separate financial spend record
    (status approved, amount = proposal.amount) with its own independent
    claim→verify→paid lifecycle. The two objects are never conflated.

    Returns the newly-linked Expense (created or already-linked) for the
    caller to surface in timeline/audit; None if the repair lacks a
    property-chain anchor needed to construct a valid Expense.
    """
    from datetime import date as _dt_date

    from app.models.financial import Expense, ExpenseStatus
    from app.models.property import Unit
    from app.services.audit import record_audit, serialize_row
    from app.services.repairs.payment import link_expense_to_proposal

    now = now or datetime.now(timezone.utc)
    if proposal.status != RepairProposalStatus.PENDING:
        raise ProposalError(
            f"Proposal V{proposal.version} is {proposal.status.value}, not PENDING"
        )
    proposal.status = RepairProposalStatus.APPROVED
    proposal.decision_by = approved_by
    proposal.decision_at = now
    proposal.updated_by = approved_by
    proposal.updated_at = now
    db.flush()

    linked_expense: Expense | None = None
    # --- Create linked Expense if not already present (quote ≠ expense, but
    # both exist and are linked for history; never overwrite an existing link)
    if proposal.expense_id is None:
        repair_property_id: int | None = None
        if repair.unit_id is not None:
            row = (
                db.query(Unit.property_id)
                .filter(Unit.id == repair.unit_id, Unit.deleted_at.is_(None))
                .one_or_none()
            )
            if row is not None:
                repair_property_id = row[0]
        if repair_property_id is None:
            repair_property_id = repair.property_id
        if repair_property_id is not None:
            today = now.date() if hasattr(now, "date") else _dt_date.today()
            linked_expense = Expense(
                expense_date=today,
                category="维修",
                amount=proposal.amount,
                payee=proposal.vendor or "-",
                description=(
                    f"[Repair #{repair.id} V{proposal.version}] "
                    + (proposal.description or repair.issue or "")
                )[:500],
                unit_id=repair.unit_id,
                property_id=repair_property_id,
                status=ExpenseStatus.approved,
                approved_by=approved_by,
                approved_at=now,
                created_by=approved_by,
                updated_by=approved_by,
            )
            db.add(linked_expense)
            db.flush()
            link_expense_to_proposal(db, proposal, linked_expense, now=now)
            record_audit(
                db,
                table_name="expenses",
                record_id=linked_expense.id,
                action="expense_created_from_approved_repair_proposal",
                actor_id=approved_by,
                changed_fields={
                    "status": [None, ExpenseStatus.approved.value],
                    "proposal_version": [None, proposal.version],
                },
                new_value=serialize_row(linked_expense),
            )
    else:
        linked_expense = db.get(Expense, proposal.expense_id)

    repair.status = RepairOperationStatus.WAITING_PAYMENT
    repair.next_action = (
        f"Quote V{proposal.version} (₱{_money_decimal(proposal.amount)}) approved. "
        "The linked expense now awaits payment."
    )
    repair.waiting_on = "payer"
    repair.updated_at = now
    return linked_expense


def supersede_pending_previous(
    db: Session, repair_id: int, new_version: int, *, now: datetime | None = None
) -> int:
    """Mark older PENDING proposals as SUPERSEDED when a new version lands.

    Returns the count superseded. Approved versions are never superseded here
    (they are the accepted plan). This keeps history visible (V1 stays) while
    making only one candidate PENDING at a time.
    """
    now = now or datetime.now(timezone.utc)
    rows = (
        db.query(RepairProposal)
        .filter(
            RepairProposal.repair_id == repair_id,
            RepairProposal.version < new_version,
            RepairProposal.status == RepairProposalStatus.PENDING,
        )
        .all()
    )
    for row in rows:
        row.status = RepairProposalStatus.SUPERSEDED
        row.updated_at = now
    db.flush()
    return len(rows)


def next_version_for(db: Session, repair_id: int) -> int:
    latest = latest_proposal(db, repair_id)
    return (latest.version + 1) if latest else 1


def count_proposals(db: Session, repair_id: int) -> int:
    return int(
        db.query(func.count(RepairProposal.id))
        .filter(RepairProposal.repair_id == repair_id)
        .scalar()
        or 0
    )
