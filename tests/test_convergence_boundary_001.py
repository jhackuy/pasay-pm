"""Convergence boundary regression band: canonical-to-legacy OperationalTask."""
from __future__ import annotations
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from app.models.audit_log import AuditAction
from app.models.financial import Expense, ExpenseStatus, Income, IncomeStatus
from app.models.operations import (
    OperationalTask, OperationalTaskStatus, OperationalTaskType,
)
from app.models.repair import (
    RepairAction, RepairActionStatus, RepairOperationStatus,
)
from app.services.expense_claims import create_claim, verify_claim
from app.services.expense_payment_truth import payment_truth
from app.services.operations import projection as task_projection
from app.services.repairs import (
    continuation as ctl, delivery, operations as op_svc, proposals as prop_svc,
)
from app.services.repairs import verification as ver_svc
from app.services.repairs.payment import on_expense_paid


def _utc_now():
    return datetime.now(timezone.utc)


def _mk_pending_repair_with_proposal(db, *, issue, amount="3000.00"):
    """Fresh repair + PENDING V1 (use before approve)."""
    r = op_svc.create_repair(db, issue=issue)
    p, _ = prop_svc.submit_proposal(db, r, amount=amount)
    db.flush()
    return r, p


def _mk_repair_with_rejected_requote_proposal(db, *, issue, amount="3000.00"):
    """Repair + REJECTED V1 (pass to ensure_requote_action; never
    try to approve the returned proposal)."""
    r = op_svc.create_repair(db, issue=issue)
    p, _ = prop_svc.submit_proposal(db, r, amount=amount)
    prop_svc.reject_proposal(db, r, p, rejected_by=99, reason="x")
    db.flush()
    return r, p


def test_real_task_complete_api_leaves_rent_expense_repair_unchanged(
    client, db_session, admin_headers,
):
    """Real /api/v1/operations/tasks/{id}/complete must NOT mutate
    Income / Expense / Repair / RepairAction truth (reverse-dep).
    """
    db = db_session
    income = Income(amount=Decimal("5000.00"), received_date=_utc_now().date(),
        payment_method="bank", status=IncomeStatus.confirmed)
    expense = Expense(expense_date=_utc_now().date(), category="x",
        amount=Decimal("3000.00"), payee="V", status=ExpenseStatus.approved)
    db.add_all([income, expense]); db.flush()
    repair, rejected = _mk_repair_with_rejected_requote_proposal(
        db, issue="Plumbing", amount="3000.00",
    )
    action, _ = ctl.ensure_requote_action(db, repair, rejected)
    db.flush()
    proj_task = db.query(OperationalTask).filter(
        OperationalTask.dedupe_key == delivery.requote_task_dedupe_key(repair.id),
        OperationalTask.status.in_([
            OperationalTaskStatus.PENDING, OperationalTaskStatus.IN_PROGRESS,
        ]),
    ).one()
    t_pay = OperationalTask(task_type=OperationalTaskType.PAYMENT_PENDING,
        title="t-pay", source_type="expense", source_id=expense.id,
        priority="high", status=OperationalTaskStatus.PENDING, due_at=_utc_now())
    db.add(t_pay); db.commit()
    snap_i = (income.status, income.confirmed_by, income.amount)
    snap_e = (expense.status, expense.amount)
    snap_r = (repair.status, repair.next_action,
              repair.waiting_on, repair.blocked_reason)
    snap_a = (action.status, action.dedupe_key)
    resp = client.post(
        f"/api/v1/operations/tasks/{proj_task.id}/complete",
        headers=admin_headers)
    assert resp.status_code == 200, resp.text
    db.expire_all()
    db.refresh(income); db.refresh(expense); db.refresh(repair)
    db.refresh(action); db.refresh(proj_task); db.refresh(t_pay)
    assert (income.status, income.confirmed_by, income.amount) == snap_i
    assert (expense.status, expense.amount) == snap_e
    assert t_pay.status == OperationalTaskStatus.PENDING
    assert (repair.status, repair.next_action,
            repair.waiting_on, repair.blocked_reason) == snap_r
    # Active RepairAction status / dedupe_key must be unchanged.
    assert (action.status, action.dedupe_key) == snap_a
    # The legacy task itself flipped COMPLETED; the canonical side did not.
    assert proj_task.status == OperationalTaskStatus.COMPLETED


def test_approve_paid_close_invariants_003b_008a(db_session):
    """Approve != paid; Expense PAID advances Repair at most to VERIFYING."""
    db = db_session
    expense = Expense(expense_date=_utc_now().date(), category="x",
        amount=Decimal("12000.00"), payee="V", status=ExpenseStatus.approved)
    db.add(expense); db.flush()
    create_claim(db, expense, claimed_amount=Decimal("12000.00"), claimed_by=1)
    assert payment_truth(db, expense).verified_paid == Decimal("0.00")
    repair, prop = _mk_pending_repair_with_proposal(db, issue="X")
    prop_svc.approve_proposal(db, repair, prop, approved_by=1)
    prop.expense_id = expense.id
    cl, _ = create_claim(db, expense, claimed_amount=Decimal("12000.00"), claimed_by=1)
    verify_claim(db, expense, cl.id, verified_by=1)
    db.flush(); on_expense_paid(db, repair); db.flush()
    assert repair.status != RepairOperationStatus.CLOSED
    assert repair.status in (
        RepairOperationStatus.WAITING_PAYMENT, RepairOperationStatus.VERIFYING)


def test_projection_rejects_inactive_and_wrong_repair_and_closed_repair(db_session):
    """Inactive Action / wrong-repair Action / CLOSED RepairOperation must
    never spawn a legacy task. Three sub-cases in one focused test."""
    db = db_session
    # 1. Inactive Action.
    r1, p1 = _mk_repair_with_rejected_requote_proposal(db, issue="R1")
    a1, _ = ctl.ensure_requote_action(db, r1, p1)
    a1.status = RepairActionStatus.COMPLETED
    a1.resolved_at = _utc_now(); db.commit()
    assert delivery.project_requote_to_task(db, r1, a1) == (None, False)
    db.rollback()
    # 2. Wrong-repair Action (built directly to avoid the ensure projection).
    ra, pa = _mk_repair_with_rejected_requote_proposal(db, issue="A")
    rb = op_svc.create_repair(db, issue="B")
    bad_action = RepairAction(
        repair_id=ra.id, action_kind=ctl.ACTION_REQUOTE,
        title="stale", status=RepairActionStatus.PENDING,
        dedupe_key=f"repair:{ra.id}:requote:v999",
        # Link to the rejected proposal goes in JSONB detail;
        # RepairAction has no proposal_id mapped column.
        detail={"proposal_id": pa.id})
    db.add(bad_action); db.commit()
    assert delivery.project_requote_to_task(db, rb, bad_action) == (None, False)
    db.rollback()
    # 3. CLOSED Repair with a stale PENDING Action (data drift after
    # canonical close). The guard must defend on RepairOperation terminal
    # status, not on Action state - so we force act_c back to ACTIVE.
    rc, _ = _mk_repair_with_rejected_requote_proposal(db, issue="C", amount="200.00")
    act_c = RepairAction(repair_id=rc.id, action_kind=ctl.ACTION_REQUOTE,
        title="stale", status=RepairActionStatus.PENDING,
        dedupe_key=f"repair:{rc.id}:requote:v1")
    db.add(act_c); db.flush()
    ver_svc.mark_repair_completed(db, rc, confirmed_by=1, source="t")
    ver_svc.verify_and_close(db, rc, verified_by=1, verification_result="ok")
    db.commit(); db.refresh(rc); db.refresh(act_c)
    assert rc.status == RepairOperationStatus.CLOSED
    assert act_c.status == RepairActionStatus.COMPLETED
    act_c.status = RepairActionStatus.PENDING  # simulate stale ACTIVE
    db.flush()
    assert delivery.project_requote_to_task(db, rc, act_c) == (None, False), \
        "CLOSED Repair must reject even with stale ACTIVE Action"


def test_in_progress_sibling_under_dedupe_is_reused_not_duplicated(db_session):
    """IN_PROGRESS widens the dedupe read; refresh reuses, never duplicates."""
    db = db_session
    repair, proposal = _mk_repair_with_rejected_requote_proposal(db, issue="IPS")
    action, _ = ctl.ensure_requote_action(db, repair, proposal)
    db.commit()
    task = db.query(OperationalTask).filter(
        OperationalTask.dedupe_key == delivery.requote_task_dedupe_key(repair.id)
    ).one()
    task.status = OperationalTaskStatus.IN_PROGRESS; db.flush()
    action2, created = ctl.ensure_requote_action(db, repair, proposal)
    assert action2.id == action.id and created is False
    result, created2 = delivery.project_requote_to_task(db, repair, action2)
    assert result is not None and result.id == task.id and created2 is False


def test_close_result_projection_filters_wrong_repair_tasks(db_session):
    """Wrong-repair tasks must be FILTERED, not completed."""
    db = db_session
    repair = op_svc.create_repair(db, issue="A")
    wrong = OperationalTask(task_type=OperationalTaskType.FOLLOWUP,
        title="x", source_type="repair", source_id=repair.id, priority="medium",
        status=OperationalTaskStatus.PENDING, due_at=_utc_now(),
        dedupe_key=delivery.requote_task_dedupe_key(repair.id),
        details={"repair_id": repair.id + 1})
    db.add(wrong); db.commit()
    assert delivery.close_result_projection(db, repair) == 0
    assert wrong.status == OperationalTaskStatus.PENDING


def test_provenance_marker_persists_on_task_created_directly_by_adapter(db_session):
    """Adapter writes the canonical_projection marker into task.details."""
    db = db_session
    repair, _ = _mk_repair_with_rejected_requote_proposal(db, issue="P")
    action = RepairAction(repair_id=repair.id, action_kind=ctl.ACTION_REQUOTE,
        title="x", status=RepairActionStatus.PENDING,
        dedupe_key=f"repair:{repair.id}:requote:v1")
    db.add(action); db.commit()
    task, _ = delivery.project_requote_to_task(db, repair, action)
    db.commit()
    assert task is not None
    assert (task.details or {}).get("canonical_projection") == {
        "direction": "canonical_to_legacy",
        "canonical_entity": "repair_action",
        "canonical_id": action.id,
    }
    rows = db.query(OperationalTask).filter(
        OperationalTask.source_type == "repair",
        OperationalTask.source_id == repair.id,
        OperationalTask.status == OperationalTaskStatus.PENDING,
    ).all()
    assert any(r.id == task.id for r in rows)


def test_close_active_projections_preserves_existing_label_and_updated_by(db_session):
    """Adapter honors caller-supplied audit label and sets task.updated_by."""
    db = db_session
    task = OperationalTask(task_type=OperationalTaskType.PAYMENT_PENDING,
        title="pay", source_type="expense", source_id=999, priority="high",
        status=OperationalTaskStatus.PENDING, due_at=_utc_now())
    db.add(task); db.flush()
    closed = task_projection.close_active_projections(
        db, tasks=[task], actor_id=42, reason="expense_paid",
        source_domain="expense.payment",
        audit_action="task_completed_via_payment")
    db.flush()
    assert closed == 1
    db.refresh(task)
    assert task.completed_by == 42 and task.updated_by == 42
    assert task.status == OperationalTaskStatus.COMPLETED


def test_state_machine_supports_verify_to_cancelled(db_session):
    """state.transition_to allows VERIFYING -> CANCELLED so the service
    cancel preserves the pre-refactor route contract (any nonterminal).
    """
    from app.services.repairs.state import _ALLOWED_TRANSITIONS, ACTIVE_STATUSES
    assert ("VERIFYING", "CANCELLED") in _ALLOWED_TRANSITIONS
    for s in ACTIVE_STATUSES:
        assert (s, "CANCELLED") in _ALLOWED_TRANSITIONS
    db = db_session
    r, prop = _mk_pending_repair_with_proposal(db, issue="X", amount="100.00")
    prop_svc.approve_proposal(db, r, prop, approved_by=1)
    r.status = RepairOperationStatus.VERIFYING
    op_svc.cancel_repair(r, actor_id=1)
    assert r.status == RepairOperationStatus.CANCELLED


def test_static_write_freeze_guard():
    """Future-developer guard: convergence boundary must not be silently
    re-bypassed by direct OperationalTask writes, direct RepairOperation
    CANCELLED writes, or new ``task_projected_*`` AuditAction slots.
    """
    repo = Path(__file__).resolve().parent.parent
    read = lambda rel: (repo / rel).read_text(encoding="utf-8")
    delivery_src = read("app/services/repairs/delivery.py")
    expense_src = read("app/api/routers/expense.py")
    repairs_src = read("app/api/routers/repairs.py")
    # delivery.py: no direct create, no direct status write.
    assert "create_operational_task" not in delivery_src
    assert "OperationalTask.status =" not in delivery_src
    # expense.py: close via seam only, no direct mutation.
    assert "task_projection.close_active_projections" in expense_src
    assert "task.status = " not in expense_src
    assert "task.completed_at = " not in expense_src
    assert "task.completed_by = " not in expense_src
    assert "task.reminder_generation = " not in expense_src
    # repairs.py: cancel via service boundary, no direct status write.
    assert "op_svc.cancel_repair(" in repairs_src
    assert "repair.status = RepairOperationStatus.CANCELLED" not in repairs_src
    # No new task_projected_* AuditAction slot was introduced.
    for slot in ("task_projected_created", "task_projected_completed",
                 "task_projected_rejected"):
        assert not hasattr(AuditAction, slot), \
            f"AuditAction must NOT have slot: {slot}"
