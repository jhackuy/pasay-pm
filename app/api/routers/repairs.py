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
from app.services.organization_scope import (
    CrossOrgReference,
    OrganizationRole,
    OwnerRequired,
    ScopeBlocked,
    property_org_id,
    resolve_org_membership,
    scoped_get_repair,
    scoped_list_repairs,
    unit_org_id,
)
from app.services.repairs import continuation
from app.services.repairs import operations as op_svc
from app.services.repairs import payment as payment_svc
from app.services.repairs import proposals as prop_svc
from app.services.repairs import verification as verify_svc
from app.services.repairs.state import TransitionError

router = APIRouter(prefix="/repairs", tags=["repairs"])


def _scope_exception_to_http(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, (ScopeBlocked, OwnerRequired)):
        return HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    if isinstance(exc, CrossOrgReference):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, type(exc).__name__)


def _scope_guard(repair: RepairOperation, user: User) -> None:
    if user.role == UserRole.agent and repair.assignee_user_id not in (None, user.id):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Cannot access a repair assigned to another user"
        )


def _assert_unit_co_org(db: Session, user: User, unit_id: int | None) -> None:
    if unit_id is None:
        return
    object_org_id = unit_org_id(db, unit_id)
    if object_org_id is None:
        raise CrossOrgReference(
            f"Unit id={unit_id} not found or has no organization"
        )
    try:
        resolve_org_membership(db, user.id, object_org_id)
    except ScopeBlocked:
        raise CrossOrgReference(
            f"Unit id={unit_id} does not belong to the caller's organization"
        ) from None


def _assert_property_co_org(db: Session, user: User, property_id: int | None) -> None:
    if property_id is None:
        return
    object_org_id = property_org_id(db, property_id)
    if object_org_id is None:
        raise CrossOrgReference(
            f"Property id={property_id} not found or has no organization"
        )
    try:
        resolve_org_membership(db, user.id, object_org_id)
    except ScopeBlocked:
        raise CrossOrgReference(
            f"Property id={property_id} does not belong to the caller's organization"
        ) from None


def _resolve_target_org_id_for_create(
    db: Session, payload: RepairCreateIn
) -> int | None:
    if payload.unit_id is not None:
        oid = unit_org_id(db, payload.unit_id)
        if oid is not None:
            return oid
    if payload.property_id is not None:
        return property_org_id(db, payload.property_id)
    return None


def _get_proposal_or_404(db: Session, proposal_id: int) -> RepairProposal:
    p = db.get(RepairProposal, proposal_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Proposal not found")
    return p


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
    try:
        items = scoped_list_repairs(db, for_user_id=user.id)
    except Exception as exc:
        raise _scope_exception_to_http(exc) from exc
    if user.role == UserRole.agent:
        items = [r for r in items if r.assignee_user_id == user.id]
    return RepairListOut(items=items, total=len(items))


@router.get("/{repair_id}", response_model=RepairDetailOut)
def get_repair_detail(
    repair_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        repair, _membership = scoped_get_repair(db, repair_id, for_user_id=user.id)
    except Exception as exc:
        raise _scope_exception_to_http(exc) from exc
    _scope_guard(repair, user)
    proposals = prop_svc.list_proposals(db, repair.id)
    actions = continuation.resolve_actions(db, repair.id)
    from app.services.repairs.timeline import build_timeline

    base = RepairDetailOut.model_validate(repair).model_dump(
        exclude={"proposals", "actions", "evidence", "expense_ids", "timeline"}
    )
    return RepairDetailOut(
        **base,
        proposals=proposals,
        actions=actions,
        evidence=repair.evidence or {},
        expense_ids=[p.expense_id for p in proposals if p.expense_id],
        timeline=build_timeline(db, repair, proposals, actions),
    )


@router.get("/{repair_id}/actions", response_model=list[RepairActionOut])
def list_repair_actions(
    repair_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        repair, _membership = scoped_get_repair(db, repair_id, for_user_id=user.id)
    except Exception as exc:
        raise _scope_exception_to_http(exc) from exc
    _scope_guard(repair, user)
    return continuation.resolve_actions(db, repair_id)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

@router.post("", response_model=RepairDetailOut, status_code=status.HTTP_201_CREATED)
def create_repair(
    payload: RepairCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        _assert_unit_co_org(db, user, payload.unit_id)
        _assert_property_co_org(db, user, payload.property_id)
    except Exception as exc:
        raise _scope_exception_to_http(exc) from exc

    target_org_id = _resolve_target_org_id_for_create(db, payload)
    if target_org_id is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Cannot determine organization for repair (unit_id/property_id missing or invalid)",
        )
    try:
        resolve_org_membership(
            db, user.id, target_org_id,
            role=[OrganizationRole.SECRETARY, OrganizationRole.OWNER],
        )
    except ScopeBlocked as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc

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
    try:
        repair, _membership = scoped_get_repair(db, repair_id, for_user_id=user.id)
    except Exception as exc:
        raise _scope_exception_to_http(exc) from exc
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
    try:
        repair, membership = scoped_get_repair(db, repair_id, for_user_id=user.id)
    except Exception as exc:
        raise _scope_exception_to_http(exc) from exc
    proposal = _resolve_proposal(db, repair, payload)

    if membership.role == OrganizationRole.SECRETARY.value and proposal.submitted_by == user.id:
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
    """Mark the linked expense paid via a VERIFIED payment claim (003B) — the
    repair goes at most to VERIFYING, NEVER CLOSED."""
    try:
        repair, membership = scoped_get_repair(db, repair_id, for_user_id=user.id)
    except Exception as exc:
        raise _scope_exception_to_http(exc) from exc
    if membership.role != OrganizationRole.OWNER.value:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only ACTIVE OWNER may pay a repair-linked expense",
        )
    expense = db.get(Expense, expense_id)
    if expense is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Expense not found")
    if expense.status != ExpenseStatus.approved:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only approved expenses can be paid")
    try:
        from app.services.expense_claims import create_claim, verify_claim

        claim, _ = create_claim(
            db, expense, claimed_amount=expense.amount, claimed_by=user.id,
            idempotency_key=f"repair:{repair_id}:expense:{expense.id}:owner-pay",
        )
        verify_claim(db, expense, claim.id, verified_by=user.id)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"Payment verification failed: {exc}") from exc
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
    try:
        repair, _membership = scoped_get_repair(db, repair_id, for_user_id=user.id)
    except Exception as exc:
        raise _scope_exception_to_http(exc) from exc
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
    try:
        repair, _membership = scoped_get_repair(db, repair_id, for_user_id=user.id)
    except Exception as exc:
        raise _scope_exception_to_http(exc) from exc
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
    except Exception as exc:
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
    """Cancel a Repair Operation.

    Convergence boundary (PASAY-VNEXT-FOUNDATION-LEGACY-001): the canonical
    state-machine transition runs through ``op_svc.cancel_repair`` so the
    explicit transition table (``_ALLOWED_TRANSITIONS``) guards it — a
    direct ``op.status = CANCELLED`` write inside the router would defeat
    the guard. Terminal states are rejected here so the message is
    consistent for the bot / tests.
    """
    try:
        repair, _membership = scoped_get_repair(db, repair_id, for_user_id=user.id)
    except Exception as exc:
        raise _scope_exception_to_http(exc) from exc
    if repair.status in ("CLOSED", "CANCELLED"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Repair is already {repair.status}")
    try:
        op_svc.cancel_repair(repair, actor_id=user.id)
    except TransitionError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
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
