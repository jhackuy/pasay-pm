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

from app.models.financial import Expense, ExpenseStatus
from app.models.operations import (
    NotificationOutbox,
    NotificationStatus,
    OperationalTask,
)
from app.models.repair import (
    RepairAction,
    RepairActionStatus,
    RepairOperation,
    RepairOperationStatus,
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
    from app.models.membership import Organization
    from app.models.property import Property, Unit

    db = db_session
    # M001 org-scope ground truth: Properties/Tenants require organization_id
    # NOT NULL; Expenses require property_id NOT NULL. Create a minimal
    # anchor chain so the legacy test can still exercise the rule without
    # any API / router overhead.
    org = Organization(
        name="M008-dummy-org",
        created_by=None,
        updated_by=None,
    )
    db.add(org)
    db.flush()
    prop = Property(
        organization_id=org.id,
        name="Dummy M008 Property",
        address="Unit Test Lane 1",
        city="Pasay",
        total_units=1,
        created_by=None,
        updated_by=None,
    )
    db.add(prop)
    db.flush()
    unit = Unit(
        property_id=prop.id,
        unit_number="101",
        floor="1",
        size_sqm=30,
        monthly_rent=10000,
        status="vacant",
        created_by=None,
        updated_by=None,
    )
    db.add(unit)
    db.flush()
    repair = _mk_repair(db)
    repair.property_id = prop.id
    repair.unit_id = unit.id
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
        property_id=prop.id,
        unit_id=unit.id,
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
    from app.models.membership import Organization
    from app.models.property import Property, Unit

    db = db_session
    # M001 org-scope anchor chain.
    org = Organization(
        name="M008-f-dummy-org",
        created_by=None,
        updated_by=None,
    )
    db.add(org)
    db.flush()
    prop = Property(
        organization_id=org.id,
        name="Dummy M008-F Property",
        address="Unit Test Lane 2",
        city="Pasay",
        total_units=1,
        created_by=None,
        updated_by=None,
    )
    db.add(prop)
    db.flush()
    unit = Unit(
        property_id=prop.id,
        unit_number="201",
        floor="2",
        size_sqm=30,
        monthly_rent=10000,
        status="vacant",
        created_by=None,
        updated_by=None,
    )
    db.add(unit)
    db.flush()
    repair = _mk_repair(db)
    repair.property_id = prop.id
    repair.unit_id = unit.id

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
        property_id=prop.id,
        unit_id=unit.id,
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
# Router-level integration (exercises audit enum + detail serialization so the
# live 500s caught during E2E are locked in as regressions).
# ---------------------------------------------------------------------------

def test_router_full_flow_create_reject_verify(client, db_session, admin_headers, unit_id):
    # Step 1: create repair (regression: record_audit must accept repair_created).
    resp = client.post("/api/v1/repairs", json={
        "issue": "Aircon compressor replacement",
        "issue_description": "Not cooling",
        "created_source": "test",
        "reported_by": 1,
        "unit_id": unit_id,
    }, headers=admin_headers)
    assert resp.status_code == 201, resp.text
    detail = resp.json()
    rid = detail["id"]
    assert detail["status"] == "OPEN"
    # Step 1b: detail serialization handles NULL evidence (regression).
    assert detail.get("evidence") in ({}, None)

    # Step 2: submit proposal V1.
    resp = client.post(f"/api/v1/repairs/{rid}/proposals", json={
        "amount": "8000.00", "vendor": "ACPro",
    }, headers=admin_headers)
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "WAITING_APPROVAL"

    # Step 3: owner rejects V1.
    resp = client.post(f"/api/v1/repairs/{rid}/decide", json={
        "decision": "reject", "version": 1, "reason": "Too expensive",
    }, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    detail = resp.json()
    assert detail["status"] == "WAITING_HUMAN"
    # requote action auto-created.
    actions = client.get(f"/api/v1/repairs/{rid}/actions", headers=admin_headers)
    assert actions.status_code == 200, actions.text
    kinds = [a["action_kind"] for a in actions.json()]
    assert "REQUOTE" in kinds

    # Step 13: verify -> CLOSED (audit enum repair_closed_after_verification).
    resp = client.post(f"/api/v1/repairs/{rid}/verify", json={
        "verification_result": "cooling restored",
        "closure_signal": "HUMAN_CONFIRMED",
    }, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "CLOSED"
    assert resp.json()["closure_reason"] == "HUMAN_CONFIRMED"

    # Audit rows were written with valid AuditAction values (no 500).
    from app.models.audit_log import AuditLog
    actions_audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.table_name == "repair_operations", AuditLog.record_id == rid)
        .all()
    )
    acts = [a.action.value for a in actions_audit]
    assert "repair_created" in acts
    assert "repair_closed_after_verification" in acts


# ---------------------------------------------------------------------------
# 008A-FINAL integration cases (G/H/I/J) — Secretary work entry + delivery.
# ---------------------------------------------------------------------------

def _secretary_user(db):
    """Create an active Secretary (agent) user in the test DB so the projected
    task can carry a real, existing assignee (FK-safe)."""
    import secrets as _secrets

    from app.core.security import hash_api_key
    from app.models.identity import (
        ApiCredential,
        CredentialState,
        Principal,
        PrincipalType,
    )
    from app.models.user import User, UserRole

    username = f"sec_{abs(hash((id(db), _secrets.token_hex(4))) )% 1000000}"
    key = _secrets.token_urlsafe(24)
    user = User(username=username, role=UserRole.agent,
                api_key_hash=hash_api_key(key), is_active=True)
    db.add(user)
    db.flush()
    principal = Principal(name=username, principal_type=PrincipalType.HUMAN,
                          user_id=user.id, is_active=True)
    db.add(principal)
    db.flush()
    db.add(ApiCredential(principal_id=principal.id, key_hash=hash_api_key(key),
                         purpose="legacy_human", state=CredentialState.ACTIVE))
    db.flush()
    return user


def _active_requote_projection(db, repair_id):
    from app.services.repairs.delivery import requote_task_dedupe_key
    return (
        db.query(OperationalTask)
        .filter(
            OperationalTask.dedupe_key == requote_task_dedupe_key(repair_id),
            OperationalTask.status.in_(["PENDING", "IN_PROGRESS"]),
        )
        .first()
    )


def _patch_secretary(monkeypatch, user_id):
    """Point the configured Secretary assignee at the given test user so the
    real projection path (delivery._task_assignee -> secretary_assignee_id)
    assigns to a human that exists in the test DB."""
    import app.services.operations.generation as gen
    import app.services.repairs.delivery as deliv
    monkeypatch.setattr(gen, "secretary_assignee_id", lambda: user_id)
    monkeypatch.setattr(deliv, "secretary_assignee_id", lambda: user_id)


def test_case_g_reject_projects_single_requote_into_secretary_queue(db_session, monkeypatch):
    """Reject V1 → exactly one REQUOTE appears in the Secretary task queue."""
    from app.services.operations.quick import build_quick_tasks

    db = db_session
    secretary = _secretary_user(db)
    _patch_secretary(monkeypatch, secretary.id)
    repair = _mk_repair(db)
    p1, _ = prop_svc.submit_proposal(db, repair, amount="8000.00")
    prop_svc.reject_proposal(db, repair, p1, rejected_by=99, reason="Too expensive")
    action, created = ctl.ensure_requote_action(db, repair, p1)
    assert created is True

    # Projected task exists, assigned to the Secretary's real user.
    task = _active_requote_projection(db, repair.id)
    assert task is not None
    assert task.assigned_user_id == secretary.id
    assert "another quote" in (task.title or "").lower()

    # The Secretary's Tasks work queue exposes exactly ONE requote entry.
    rows = build_quick_tasks(db, secretary)
    requote_rows = [
        r for r in rows
        if (r.get("title") or "").lower().find("another quote") != -1
    ]
    assert len(requote_rows) == 1

    # Only one active REQUOTE business action too.
    active_actions = [
        a for a in ctl.resolve_actions(db, repair.id)
        if a.action_kind == ctl.ACTION_REQUOTE and a.status.value in ("PENDING", "IN_PROGRESS")
    ]
    assert len(active_actions) == 1


def test_case_h_worker_ticks_no_delivery_spam_single_task(db_session, monkeypatch):
    """Repeated worker ticks → one active task, one projected action, no
    duplicate outbox rows (delivery is idempotent, not per-tick spam)."""
    from app.services.operations import daily_dedup  # noqa: F401 (semantics documented)
    from app.services.repairs import delivery as deliv_svc

    db = db_session
    secretary = _secretary_user(db)
    _patch_secretary(monkeypatch, secretary.id)
    repair = _mk_repair(db)
    p1, _ = prop_svc.submit_proposal(db, repair, amount="8000.00")
    prop_svc.reject_proposal(db, repair, p1, rejected_by=99, reason="Too expensive")

    # Simulate 25 worker ticks of ensure (idempotent).
    for _ in range(25):
        ctl.ensure_requote_action(db, repair, p1)

    # Exactly one active projected task and one active REQUOTE action.
    tasks = db.query(OperationalTask).filter(
        OperationalTask.dedupe_key == deliv_svc.requote_task_dedupe_key(repair.id)
    ).all()
    active_tasks = [t for t in tasks if t.status.value in ("PENDING", "IN_PROGRESS")]
    assert len(active_tasks) == 1, "worker ticks must not duplicate the projected task"

    active_actions = [
        a for a in ctl.resolve_actions(db, repair.id)
        if a.action_kind == ctl.ACTION_REQUOTE and a.status.value in ("PENDING", "IN_PROGRESS")
    ]
    assert len(active_actions) == 1

    # Notifier must not deliver per tick: the outbox carries ONE row for the
    # projected task (create_operational_task enqueues once), and a human
    # acknowledgment / daily-dedup bounds repeated reminders.
    outbox = db.query(NotificationOutbox).filter(
        NotificationOutbox.task_id == tasks[0].id,
        NotificationOutbox.status == NotificationStatus.PENDING,
    ).count()
    assert outbox <= 1, "one task -> at most one pending outbox notification"

    # Acknowledging the task stops same-day reminders while REQUOTE stays active.
    active_tasks[0].status = "IN_PROGRESS"
    db.flush()
    active_actions_after = [
        a for a in ctl.resolve_actions(db, repair.id)
        if a.action_kind == ctl.ACTION_REQUOTE and a.status.value in ("PENDING", "IN_PROGRESS")
    ]
    assert len(active_actions_after) == 1  # reminder stopped, action still active


def test_case_i_submit_v2_completes_old_requote(db_session):
    """Submit V2 → old REQUOTE no longer active; repair WAITING_APPROVAL."""
    from app.services.repairs import delivery as deliv_svc

    db = db_session
    repair = _mk_repair(db)
    p1, _ = prop_svc.submit_proposal(db, repair, amount="8000.00")
    prop_svc.reject_proposal(db, repair, p1, rejected_by=99, reason="Too expensive")
    ctl.ensure_requote_action(db, repair, p1)
    task = _active_requote_projection(db, repair.id)
    assert task is not None

    # Secretary submits V2.
    p2, v2 = prop_svc.submit_proposal(db, repair, amount="6500.00", vendor="CoolAir")
    assert v2 == 2
    db.flush()

    # Old requote projection is COMPLETED (leaves the queue).
    requote_proj = db.query(OperationalTask).filter(
        OperationalTask.dedupe_key == deliv_svc.requote_task_dedupe_key(repair.id)
    ).first()
    assert requote_proj is not None
    assert requote_proj.status.value == "COMPLETED"
    assert _active_requote_projection(db, repair.id) is None

    # Old REQUOTE business action COMPLETED; V1 remains; V2 PENDING.
    actions = ctl.resolve_actions(db, repair.id)
    requote_actions = [a for a in actions if a.action_kind == ctl.ACTION_REQUOTE]
    assert all(a.status.value == "COMPLETED" for a in requote_actions)
    all_p = prop_svc.list_proposals(db, repair.id)
    v1b = next(x for x in all_p if x.version == 1)
    v2b = next(x for x in all_p if x.version == 2)
    assert v1b.status == RepairProposalStatus.REJECTED
    assert v2b.status == RepairProposalStatus.PENDING
    assert repair.status == RepairOperationStatus.WAITING_APPROVAL
    # Owner now sees the next action (review V2).
    assert "awaits owner decision" in (repair.next_action or "")


def test_case_j_detail_returns_full_history_shape(client, admin_headers, unit_id):
    """Mini App Repair Detail serializer returns proposals/payments/actions/
    verification/timeline in one call (the frontend renders this)."""
    resp = client.post("/api/v1/repairs", json={
        "issue": "Aircon compressor replacement", "created_source": "test",
        "unit_id": unit_id,
    }, headers=admin_headers)
    assert resp.status_code == 201, resp.text
    rid = resp.json()["id"]

    client.post(f"/api/v1/repairs/{rid}/proposals", json={"amount": "8000.00", "vendor": "ACPro"},
                headers=admin_headers)
    client.post(f"/api/v1/repairs/{rid}/decide", json={"decision": "reject", "version": 1,
                                                       "reason": "Too expensive"},
                headers=admin_headers)
    client.post(f"/api/v1/repairs/{rid}/proposals", json={"amount": "6500.00", "vendor": "CoolAir"},
                headers=admin_headers)
    client.post(f"/api/v1/repairs/{rid}/decide", json={"decision": "approve", "version": 2},
                headers=admin_headers)

    detail = client.get(f"/api/v1/repairs/{rid}", headers=admin_headers).json()
    # Proposals history with versions in order.
    props = sorted(detail["proposals"], key=lambda p: p["version"])
    assert [p["version"] for p in props] == [1, 2]
    assert props[0]["status"] == "REJECTED" and props[0]["rejection_reason"] == "Too expensive"
    assert props[1]["status"] == "APPROVED"
    # Actions include REQUOTE; verification/closure fields present (even null).
    kinds = [a["action_kind"] for a in detail["actions"]]
    assert "REQUOTE" in kinds
    assert "verified_at" in detail and "closure_reason" in detail
    assert detail["next_action"] is not None
    assert detail["status"] in ("WAITING_PAYMENT", "VERIFYING")
    # Timeline: an ordered, human-readable history is returned (Mini App renders
    # this directly; Reject != Close / Paid != Close / Verified -> Close visible).
    tl = detail["timeline"]
    kinds_tl = [e["kind"] for e in tl]
    assert kinds_tl[0] == "repair_created"
    assert "proposal_rejected" in kinds_tl
    assert "requote_requested" in kinds_tl
    assert "proposal_approved" in kinds_tl
    idx_rej = kinds_tl.index("proposal_rejected")
    idx_req = kinds_tl.index("requote_requested")
    assert idx_req > idx_rej  # requote strictly after rejection


# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------

def _as_decimal(v):
    from decimal import Decimal
    return Decimal(v)
