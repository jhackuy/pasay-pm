"""REPAIR-AI-EMPLOYEE-WORKFLOW-008A — business-rule tests (Cases A–F).

These prove the Repair Operation model is truly decoupled from Proposal /
Expense and that Closure only happens through verification.

Cases:
  Case A  — Reject does not close Repair (Repair stays OPEN/alive)
  Case B  — Requote: V1 & V2 coexist; new proposal after reject
  Case C  — Dedup: repeated worker runs create ONE active requote action
  Case D  — Payment does not close Repair (expense paid != closed)
  Case E  — Verification closes Repair
  Case F  — History preserved after CLOSED (V1 rejected, V2 approved,
            expense paid, verification) all queryable
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func

from app.models.financial import Expense, ExpenseStatus
from app.models.repair import (
    RepairAction,
    RepairActionStatus,
    RepairOperation,
    RepairOperationStatus,
    RepairProposal,
    RepairProposalStatus,
)
from app.services.repairs import continuation as ctl
from app.services.repairs import operations as op_svc
from app.services.repairs import payment as pay_svc
from app.services.repairs import proposals as prop_svc
from app.services.repairs import verification as ver_svc


def _now():
    return datetime.now(timezone.utc)


def _mk_repair(db, issue="Aircon compressor replacement") -> RepairOperation:
    return op_svc.create_repair(
        db,
        issue=issue,
        issue_description="Not cooling; compressor dead",
        reported_by=None,
        assignee_user_id=None,
    )


# ---------------------------------------------------------------------------
# Case A — Reject does not close Repair
# ---------------------------------------------------------------------------

def test_case_a_reject_does_not_close_repair(db_session):
    db = db_session
    repair = _mk_repair(db)
    assert repair.status == RepairOperationStatus.OPEN

    proposal, v = prop_svc.submit_proposal(
        db, repair, amount="8000.00", vendor="ACPro", description="Compressor replacement"
    )
    assert v == 1
    assert proposal.status == RepairProposalStatus.PENDING
    # A pending proposal waits on owner approval.
    assert repair.status == RepairOperationStatus.WAITING_APPROVAL

    prop_svc.reject_proposal(db, repair, proposal, rejected_by=99, reason="Too expensive")

    db.flush()
    # P0 guard: Owner rejected must NEVER close the repair.
    assert repair.status != RepairOperationStatus.CLOSED
    assert repair.status != RepairOperationStatus.CANCELLED
    # Only the proposal is rejected; repair stays alive waiting on a human.
    assert proposal.status == RepairProposalStatus.REJECTED
    assert proposal.rejection_reason == "Too expensive"
    assert proposal.decision_by == 99
    assert repair.status == RepairOperationStatus.WAITING_HUMAN


# ---------------------------------------------------------------------------
# Case B — Requote: V1 & V2 coexist, new proposal after reject
# ---------------------------------------------------------------------------

def test_case_b_requote_keeps_v1_and_creates_v2(db_session):
    db = db_session
    repair = _mk_repair(db)
    p1, _ = prop_svc.submit_proposal(db, repair, amount="8000.00", vendor="V1AC")
    assert p1.version == 1

    prop_svc.reject_proposal(db, repair, p1, rejected_by=99, reason="Too expensive")

    # AI continuation creates the requote action.
    action, created = ctl.ensure_requote_action(db, repair, p1)
    assert created is True
    assert action is not None
    assert action.action_kind == ctl.ACTION_REQUOTE

    # Secretary submits V2.
    p2, v2 = prop_svc.submit_proposal(
        db, repair, amount="6500.00", vendor="V2AC", description="Cheaper compressor"
    )
    assert v2 == 2
    assert p2.version == 2

    db.flush()
    # V1 and V2 BOTH exist; V1 is not overwritten/deleted.
    all_p = prop_svc.list_proposals(db, repair.id)
    versions = sorted(x.version for x in all_p)
    assert versions == [1, 2]
    v1 = next(x for x in all_p if x.version == 1)
    assert v1.status == RepairProposalStatus.REJECTED
    assert v1.amount == _as_decimal("8000.00")


# ---------------------------------------------------------------------------
# Case C — Dedup: repeated worker runs create ONE active requote action
# ---------------------------------------------------------------------------

def test_case_c_repeated_worker_ticks_dedup_single_requote(db_session):
    db = db_session
    repair = _mk_repair(db)
    p1, _ = prop_svc.submit_proposal(db, repair, amount="8000.00")
    prop_svc.reject_proposal(db, repair, p1, rejected_by=99, reason="Too expensive")

    # Simulate the worker ticking many times, plus bot callback deliveries and
    # API retries — all must converge on ONE active requote action.
    created_flags = []
    for _ in range(50):
        action, created = ctl.ensure_requote_action(db, repair, p1)
        created_flags.append(created)
    db.flush()

    active = (
        db.query(RepairAction)
        .filter(
            RepairAction.repair_id == repair.id,
            RepairAction.action_kind == ctl.ACTION_REQUOTE,
            RepairAction.status.in_(
                [RepairActionStatus.PENDING, RepairActionStatus.IN_PROGRESS]
            ),
        )
        .all()
    )
    assert len(active) == 1
    # Exactly one creation; the rest were no-ops.
    assert sum(1 for c in created_flags if c) == 1


def test_case_c_new_requote_allowed_after_previous_resolved(db_session):
    db = db_session
    repair = _mk_repair(db)
    p1, _ = prop_svc.submit_proposal(db, repair, amount="8000.00")
    prop_svc.reject_proposal(db, repair, p1, rejected_by=99, reason="Too expensive")
    action, _ = ctl.ensure_requote_action(db, repair, p1)

    # Complete the requote action explicitly.
    action.status = RepairActionStatus.COMPLETED
    action.resolved_at = _now()
    db.flush()

    # Now the SAME step may be re-created after a further rejection.
    action2, created = ctl.ensure_requote_action(db, repair, p1)
    assert created is True
    assert action2 is not None


# ---------------------------------------------------------------------------
# Case D — Payment does not close Repair
# ---------------------------------------------------------------------------

def test_case_d_payment_does_not_close_repair(db_session):
    db = db_session
    repair = _mk_repair(db)
    p1, _ = prop_svc.submit_proposal(db, repair, amount="8000.00")
    # Approve the proposal -> WAITING_PAYMENT (not CLOSED).
    prop_svc.approve_proposal(db, repair, p1, approved_by=99)
    assert repair.status == RepairOperationStatus.WAITING_PAYMENT
    assert repair.status != RepairOperationStatus.CLOSED

    # Create + link an expense, then mark it PAID.
    expense = Expense(
        expense_date=datetime.now(timezone.utc).date(),
        category="维修",
        amount=p1.amount,
        payee="ACPro",
        status=ExpenseStatus.approved,
    )
    db.add(expense)
    db.flush()
    pay_svc.link_expense_to_proposal(db, p1, expense)

    pay_svc.on_expense_paid(db, repair)

    # P0 guard: Expense PAID must NOT close the repair.
    assert repair.status != RepairOperationStatus.CLOSED
    # Payment advances to at most VERIFYING.
    assert repair.status in (
        RepairOperationStatus.WAITING_PAYMENT,
        RepairOperationStatus.VERIFYING,
    )


# ---------------------------------------------------------------------------
# Case E — Verification closes Repair
# ---------------------------------------------------------------------------

def test_case_e_verification_closes_repair(db_session):
    db = db_session
    repair = _mk_repair(db)
    # Drive to VERIFYING (human confirms the work is done).
    ver_svc.mark_repair_completed(db, repair, confirmed_by=200, source="Secretary confirmed")
    assert repair.status == RepairOperationStatus.VERIFYING
    assert repair.status != RepairOperationStatus.CLOSED

    # Record the verification -> CLOSED with full closure record.
    ver_svc.verify_and_close(
        db,
        repair,
        verified_by=200,
        verification_result="cooling restored",
        closure_signal=ver_svc.ClosureSignal.HUMAN_CONFIRMED.value,
    )
    db.flush()
    assert repair.status == RepairOperationStatus.CLOSED
    assert repair.verified_by == 200
    assert repair.verified_at is not None
    assert repair.closed_at is not None
    assert repair.verification_result == "cooling restored"
    assert repair.closure_reason == "HUMAN_CONFIRMED"


def test_case_e_cannot_close_without_verification_signal(db_session):
    from app.services.repairs.state import TransitionError

    db = db_session
    repair = _mk_repair(db)
    try:
        ver_svc.verify_and_close(
            db, repair, verified_by=200, closure_signal="payment_paid"
        )
        assert False, "closing via a non-verification signal must be refused"
    except TransitionError:
        pass
    assert repair.status != RepairOperationStatus.CLOSED


# ---------------------------------------------------------------------------
# Case F — History preserved after CLOSED
# ---------------------------------------------------------------------------

def test_case_f_history_preserved_after_closed(db_session):
    db = db_session
    repair = _mk_repair(db)

    # V1 rejected
    p1, _ = prop_svc.submit_proposal(db, repair, amount="8000.00", vendor="V1AC")
    prop_svc.reject_proposal(db, repair, p1, rejected_by=99, reason="Too expensive")
    ctl.ensure_requote_action(db, repair, p1)

    # V2 approved
    p2, _ = prop_svc.submit_proposal(db, repair, amount="6500.00", vendor="V2AC")
    prop_svc.approve_proposal(db, repair, p2, approved_by=99)

    # Expense paid
    expense = Expense(
        expense_date=datetime.now(timezone.utc).date(),
        category="维修",
        amount=p2.amount,
        payee="V2AC",
        status=ExpenseStatus.approved,
    )
    db.add(expense)
    db.flush()
    pay_svc.link_expense_to_proposal(db, p2, expense)
    pay_svc.on_expense_paid(db, repair)

    # Verification completed
    ver_svc.mark_repair_completed(db, repair, confirmed_by=200, source="done")
    ver_svc.verify_and_close(
        db,
        repair,
        verified_by=200,
        verification_result="ok",
        closure_signal=ver_svc.ClosureSignal.HUMAN_CONFIRMED.value,
    )
    db.flush()
    assert repair.status == RepairOperationStatus.CLOSED

    # AFTER CLOSED: everything is still fully queryable.
    all_p = prop_svc.list_proposals(db, repair.id)
    versions = sorted(x.version for x in all_p)
    assert versions == [1, 2]
    v1 = next(x for x in all_p if x.version == 1)
    v2 = next(x for x in all_p if x.version == 2)
    assert v1.status == RepairProposalStatus.REJECTED
    assert v1.rejection_reason == "Too expensive"
    assert v2.status == RepairProposalStatus.APPROVED
    assert expense.status == ExpenseStatus.paid or expense.status == ExpenseStatus.approved
    assert v2.expense_id == expense.id

    actions = ctl.resolve_actions(db, repair.id)
    kinds = sorted(a.action_kind for a in actions)
    # Requote action history exists alongside the closure.
    assert ctl.ACTION_REQUOTE in kinds
    assert repair.closure_reason == "HUMAN_CONFIRMED"
    assert repair.verified_at is not None


# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------

def _as_decimal(v):
    from decimal import Decimal
    return Decimal(v)
