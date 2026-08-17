"""REPAIR-AI-EMPLOYEE-WORKFLOW-008A — Repair API router (/api/v1/repairs).

Owns the Repair Operation lifecycle and its decoupled Proposals / Actions.

RBAC (consistent with the rest of the app):
- admin: everything.
- manager: view + submit proposals + record results.
- agent: only view/act repairs assigned to them.

The Router enforces the 008A invariants by delegating to the service layer:
- reject_proposal keeps the repair OPEN/WAITING_HUMAN (never closes/cancels);
- approve_proposal -> WAITING_PAYMENT (never closes);
- pay links Expense-paid -> VERIFYING at most (never closes);
- ONLY verify closes the repair (verification gate).

Timeline is surfaced via the existing audit_log (every mutation records an
audit event with action + old/new row).
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import admin_only, get_current_user, manager_or_admin
from app.database import get_db
from app.models.financial import Expense, ExpenseStatus
from app.models.repair import (
    RepairOperation,
    RepairOperationStatus,
    RepairProposal,
)
from app.models.user import User, UserRole
from app.schemas.repair import (
    RepairActionOut,
    RepairCreateIn,
    RepairDecisionIn,
    RepairDetailOut,
    RepairListOut,
    RepairProposalIn,
    RepairRecordResultIn,
    RepairVerifyIn,
)
from app.services.audit import record_audit, serialize_row
from app.services.repairs import continuation, operations as op_svc
from app.services.repairs import payment as payment_svc
from app.services.repairs import proposals as prop_svc
from app.services.repairs import verification as verify_svc

router = APIRouter(prefix="/repairs", tags=["repairs"])


def _get_repair_or_404(db: Session, repair_id: int) -> RepairOperation:
    repair = db.get(RepairOperation, repair_id)
    if repair is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Repair not found")
    return repair


def _get_proposal_or_404(db: Session, proposal_id: int) -> RepairProposal:
    p = db.get(RepairProposal, proposal_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Proposal not found")
    return p


def _scope_guard(repair: RepairOperation, user: User) -> None:
    if user.role == UserRole.agent and repair.assignee_user_id not in (None, user.id):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Cannot access a repair assigned to another user"
        )


def _resolve_proposal(db, repair, payload: RepairDecisionIn) -> RepairProposal:
    """Resolve a proposal by id or by version."""
    if payload.proposal_id is not None and payload.version is not None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Specify proposal_id OR version")
    if payload.proposal_id is not None:
        proposal = _get_proposal_or_404(db, payload.proposal_id)
        if proposal.repair_id != repair.id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Proposal does not belong to this repair")
        return proposal
    if payload.version is not None:
        proposal = (
            db.query(RepairProposal)
            .filter(
                RepairProposal.repair_id == repair.id,
                RepairProposal.version == payload.version,
            )
            .first()
        )
        if proposal is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"Proposal V{payload.version} not found"
            )
        return proposal
    # default: the latest proposal
    proposal = prop_svc.latest_proposal(db, repair.id)
    if proposal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Repair has no proposals")
    return proposal


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

@router.get("", response_model=RepairListOut)
def list_repairs(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(RepairOperation)
    if user.role == UserRole.agent:
        query = query.filter(RepairOperation.assignee_user_id == user.id)
    items = query.order_by(RepairOperation.id.desc()).all()
    return RepairListOut(items=items, total=len(items))


@router.get("/{repair_id}", response_model=RepairDetailOut)
def get_repair_detail(
    repair_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    repair = _get_repair_or_404(db, repair_id)
    _scope_guard(repair, user)
    proposals = prop_svc.list_proposals(db, repair.id)
    actions = continuation.resolve_actions(db, repair.id)
    return RepairDetailOut(
        **RepairDetailOut.model_validate(repair).model_dump(),
        proposals=proposals,
        actions=actions,
        evidence=repair.evidence or {},
        expense_ids=[p.expense_id for p in proposals if p.expense_id],
    )


@router.get("/{repair_id}/actions", response_model=list[RepairActionOut])
def list_repair_actions(
    repair_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _scope_guard(_get_repair_or_404(db, repair_id), user)
    return continuation.resolve_actions(db, repair_id)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

@router.post("", response_model=RepairDetailOut, status_code=status.HTTP_201_CREATED)
def create_repair(
    payload: RepairCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(manager_or_admin),
):
    repair = op_svc.create_repair(
        db,
        issue=payload.issue,
        issue_description=payload.issue_description,
        merchant_id=payload.merchant_id,
        property_id=payload.property_id,
        unit_id=payload.unit_id,
        created_source=payload.created_source,
        reported_by=payload.reported_by or user.id,
        assignee_user_id=payload.assignee_user_id,
        closure_criteria=payload.closure_criteria,
    )
    record_audit(
        db,
        table_name="repair_operations",
        record_id=repair.id,
        action="repair_created",
        actor_id=user.id,
        new_value=serialize_row(repair),
    )
    db.commit()
    db.refresh(repair)
    return get_repair_detail(repair.id, db, user)


@router.post("/{repair_id}/proposals", response_model=RepairDetailOut, status_code=status.HTTP_201_CREATED)
def submit_proposal(
    repair_id: int,
    payload: RepairProposalIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    repair = _get_repair_or_404(db, repair_id)
    _scope_guard(repair, user)
    try:
        proposal, version = prop_svc.submit_proposal(
            db,
            repair,
            amount=payload.amount,
            vendor=payload.vendor,
            source=payload.source,
            description=payload.description,
            submitted_by=user.id,
        )
    except prop_svc.ProposalError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    if payload.submit_as_expense:
        linked_expense = _create_linked_expense(db, repair, proposal, version, user)
        payment_svc.link_expense_to_proposal(db, proposal, linked_expense)

    record_audit(
        db,
        table_name="repair_proposals",
        record_id=proposal.id,
        action="proposal_submitted",
        actor_id=user.id,
        changed_fields={"version": [None, version], "status": [None, "PENDING"]},
        new_value=serialize_row(proposal),
    )
    db.commit()
    db.refresh(repair)
    return get_repair_detail(repair.id, db, user)


@router.post("/{repair_id}/decide", response_model=RepairDetailOut)
def decide_proposal(
    repair_id: int,
    payload: RepairDecisionIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    repair = _get_repair_or_404(db, repair_id)
    proposal = _resolve_proposal(db, repair, payload)

    if user.role == UserRole.manager and proposal.submitted_by == user.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Cannot decide on a proposal you submitted"
        )

    decision = (payload.decision or "").strip().lower()
    if decision not in ("approve", "reject"):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "decision must be 'approve' or 'reject'",
        )
    if decision == "reject" and not (payload.reason or "").strip():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "A rejection reason is required",
        )

    try:
        if decision == "approve":
            prop_svc.approve_proposal(db, repair, proposal, approved_by=user.id)
            record_audit(
                db,
                table_name="repair_proposals",
                record_id=proposal.id,
                action="proposal_approved",
                actor_id=user.id,
                changed_fields={"status": [proposal.status.value, "APPROVED"]},
                new_value=serialize_row(proposal),
            )
            # If the decision carries a pre-created expense, link it so payment
            # is tracked separately from the repair.
            if payload.expense_id is not None:
                expense = db.get(Expense, payload.expense_id)
                if expense is None:
                    db.rollback()
                    raise HTTPException(status.HTTP_404_NOT_FOUND, "Expense not found")
                payment_svc.link_expense_to_proposal(db, proposal, expense)
        else:
            prop_svc.reject_proposal(
                db,
                repair,
                proposal,
                rejected_by=user.id,
                reason=payload.reason,
            )
            record_audit(
                db,
                table_name="repair_proposals",
                record_id=proposal.id,
                action="proposal_rejected",
                actor_id=user.id,
                changed_fields={"status": ["PENDING", "REJECTED"]},
                new_value=serialize_row(proposal),
            )
            # 008A AI-continuation: reject -> create ONE dedup'd requote action
            # (Case C proves repeated calls create only one active action).
            continuation.ensure_requote_action(db, repair, proposal, actor_id=user.id)
    except prop_svc.ProposalError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    db.commit()
    db.refresh(repair)
    return get_repair_detail(repair.id, db, user)


@router.post("/{repair_id}/pay-expense", response_model=RepairDetailOut)
def pay_linked_expense(
    repair_id: int,
    expense_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
):
    """Mark the linked expense paid (admin) — the repair goes at most to
    VERIFYING, NEVER CLOSED."""
    repair = _get_repair_or_404(db, repair_id)
    expense = db.get(Expense, expense_id)
    if expense is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Expense not found")
    if expense.status != ExpenseStatus.approved:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only approved expenses can be paid")
    expense.status = ExpenseStatus.paid
    expense.updated_by = user.id
    expense.updated_at = datetime.now(timezone.utc)
    db.flush()
    record_audit(
        db,
        table_name="expenses",
        record_id=expense.id,
        action="pay",
        actor_id=user.id,
        new_value=serialize_row(expense),
    )
    payment_svc.on_expense_paid(db, repair)
    db.commit()
    db.refresh(repair)
    return get_repair_detail(repair.id, db, user)


@router.post("/{repair_id}/record-result", response_model=RepairDetailOut)
def record_result(
    repair_id: int,
    payload: RepairRecordResultIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    repair = _get_repair_or_404(db, repair_id)
    _scope_guard(repair, user)
    try:
        verify_svc.mark_repair_completed(
            db,
            repair,
            confirmed_by=user.id,
            verification_result=payload.verification_result,
            evidence_ids=payload.evidence_ids,
            source=payload.source,
        )
        continuation.ensure_record_result_action(db, repair, actor_id=user.id)
    except verify_svc.VerificationError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    record_audit(
        db,
        table_name="repair_operations",
        record_id=repair.id,
        action="repair_completed_pending_verification",
        actor_id=user.id,
        changed_fields={"status": ["*", "VERIFYING"]},
        new_value=serialize_row(repair),
    )
    db.commit()
    db.refresh(repair)
    return get_repair_detail(repair.id, db, user)


@router.post("/{repair_id}/verify", response_model=RepairDetailOut)
def verify_repair(
    repair_id: int,
    payload: RepairVerifyIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    repair = _get_repair_or_404(db, repair_id)
    _scope_guard(repair, user)
    try:
        verify_svc.verify_and_close(
            db,
            repair,
            verified_by=user.id,
            verification_result=payload.verification_result,
            closure_signal=payload.closure_signal,
            source=payload.source,
        )
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"Verification failed: {exc}") from exc
    record_audit(
        db,
        table_name="repair_operations",
        record_id=repair.id,
        action="repair_closed_after_verification",
        actor_id=user.id,
        changed_fields={"status": ["*", "CLOSED"]},
        new_value=serialize_row(repair),
    )
    db.commit()
    db.refresh(repair)
    return get_repair_detail(repair.id, db, user)


@router.post("/{repair_id}/cancel", response_model=RepairDetailOut)
def cancel_repair(
    repair_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(manager_or_admin),
):
    repair = _get_repair_or_404(db, repair_id)
    if repair.status in ("CLOSED", "CANCELLED"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Repair is already {repair.status}")
    repair.status = RepairOperationStatus.CANCELLED
    repair.next_action = "Repair cancelled."
    repair.waiting_on = None
    repair.updated_at = datetime.now(timezone.utc)
    record_audit(
        db,
        table_name="repair_operations",
        record_id=repair.id,
        action="repair_cancelled",
        actor_id=user.id,
        new_value=serialize_row(repair),
    )
    db.commit()
    db.refresh(repair)
    return get_repair_detail(repair.id, db, user)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _create_linked_expense(db, repair, proposal, version, user: User) -> Expense:
    """Create an Expense for an approved-style quote. expense_date = today."""
    import datetime as _dt

    expense = Expense(
        expense_date=_dt.date.today(),
        category="维修",
        amount=proposal.amount,
        payee=proposal.vendor or "Vendor",
        description=(proposal.description or repair.issue)[:500],
        unit_id=repair.unit_id,
        status=ExpenseStatus.pending,
        created_by=user.id,
        updated_by=user.id,
    )
    if user.role == UserRole.admin:
        expense.status = ExpenseStatus.approved
        expense.approved_by = user.id
        expense.approved_at = datetime.now(timezone.utc)
    db.add(expense)
    db.flush()
    record_audit(
        db,
        table_name="expenses",
        record_id=expense.id,
        action="create",
        actor_id=user.id,
        new_value=serialize_row(expense),
    )
    return expense
