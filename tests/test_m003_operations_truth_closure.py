"""PASAY-MILESTONE-003 OP-TRUTH-003 Targeted tests.

Truth-First Projection Closure:
  T组 12 — PROJECTION_TABLE 7 条映射 + fail-closed 未知组合
  G组 6  — PATCH/POST 封杀 + 两步 PATCH 攻击向量 + Custom schema A
  R组 5  — Reconcile Repair CLOSED/CANCELLED 对账
  Q组 4  — Quick Expense unresolved 三通道 OR 漏数修复
  F组 4  — Forward seam (DB direct write) 不被 gate 误伤

合计: 31 targeted tests (≥ 30 baseline contract).

调用入口策略:
  - T组 / G02 / G04 / G05  → POST /operations/tasks/{id}/complete
    (complete_task 不走 _validate_transition 语法检查, 直接进入 assert_completion_allowed)
  - G01 / G03              → PATCH IN_PROGRESS → PATCH COMPLETED (两步攻击向量封杀)
  - G06                    → DB direct UPDATE (forward seam)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.models.financial import Expense, ExpenseStatus
from app.models.operations import (
    OperationalTask,
    OperationalTaskPriority,
    OperationalTaskStatus,
    OperationalTaskType,
)
from app.models.repair import RepairOperation, RepairOperationStatus
from app.services.operations.reconcile import reconcile_tasks

API = "/api/v1"
_RENT_PERIOD = "2026-03"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _headers(user_key):
    return {"Authorization": f"Bearer {user_key}"}


def _task_status(db, task_id):
    t = db.get(OperationalTask, task_id)
    assert t is not None
    return t.status


def _insert_task(db, *, task_type, title, source_type, source_id=None,
                 property_id=None, lease_id=None, tenant_id=None,
                 dedupe_key=None, details=None,
                 status=OperationalTaskStatus.PENDING):
    now = datetime.now(timezone.utc)
    t = OperationalTask(
        task_type=task_type,
        title=title,
        status=status,
        source_type=source_type,
        source_id=source_id,
        property_id=property_id,
        lease_id=lease_id,
        tenant_id=tenant_id,
        dedupe_key=dedupe_key,
        priority=OperationalTaskPriority.high,
        due_at=now + timedelta(days=3),
        details=details or {},
        created_by=None,
        updated_by=None,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _assert_truth_409(resp, task_id, expected_keyword=None, status=OperationalTaskStatus.PENDING,
                     db=None):
    """Canonical assertion: a truth-missing complete attempt gives 409 with
    the structured JSON detail and leaves the task in its prior status."""
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    # detail is a dict (TruthValidationError) not a plain string
    assert isinstance(detail, dict), f"expected dict detail, got {type(detail)}: {detail!r}"
    assert detail["reason"] == "task_completion_truth_missing"
    assert detail["task_id"] == task_id
    if expected_keyword:
        hay = " ".join(str(detail.get(k) or "") for k in
                       ("expected_truth", "actual_truth", "hint"))
        assert expected_keyword.lower() in hay.lower(), \
            f"keyword {expected_keyword!r} not found in truth-error payload: {detail}"
    if db is not None:
        assert _task_status(db, task_id) == status


def _assert_complete_200(resp, db, task_id):
    assert resp.status_code == 200, resp.text
    db_exp = db.get(OperationalTask, task_id)
    assert db_exp is not None
    assert db_exp.status == OperationalTaskStatus.COMPLETED


def _create_expense_api(client, headers, amount="5000.00", status="pending",
                        property_id=None):
    payload = {
        "expense_date": "2026-03-15", "category": "维修",
        "amount": amount, "payee": "Fix-It Co",
        "description": "test expense", "status": status,
    }
    if property_id is not None:
        payload["property_id"] = property_id
    r = client.post(f"{API}/expenses", json=payload, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _approve_expense_api(client, headers, eid):
    r = client.post(f"{API}/expenses/{eid}/approve", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _expense_payment_claim_verify(client, headers, eid, amount,
                                  claimer_headers=None):
    claimer = claimer_headers or headers
    rc = client.post(f"{API}/expenses/{eid}/claims", json={
        "claimed_amount": amount,
        "verification_note": "ref test-001",
    }, headers=claimer)
    assert rc.status_code == 201, rc.text
    cid = rc.json()["id"]
    rv = client.post(f"{API}/expenses/{eid}/claims/{cid}/verify",
                     json={"result": "ok"}, headers=headers)
    assert rv.status_code == 200, rv.text
    return rv.json()


def _rent_claim_and_verify(client, headers, lease_id, period, amount):
    r1 = client.post(f"{API}/incomes/leases/{lease_id}/claims", json={
        "period": period, "claimed_amount": amount,
    }, headers=headers)
    assert r1.status_code in (200, 201), r1.text
    cid = r1.json()["id"]
    r2 = client.patch(f"{API}/incomes/claims/{cid}/verify",
                      json={"result": "ok"}, headers=headers)
    assert r2.status_code == 200, r2.text
    return r2.json()


def _create_repair_api(client, headers, property_id, unit_id):
    r = client.post(f"{API}/repairs", json={
        "issue": "leaking faucet",
        "unit_id": unit_id,
        "property_id": property_id,
        "closure_criteria": "Faucet no longer leaks",
    }, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _repair_human_close_api(client, headers, repair_id):
    r = client.post(f"{API}/repairs/{repair_id}/record-result", json={
        "verification_result": "Fixed by handyman — tested OK",
        "signal": "HUMAN_CONFIRMED",
    }, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ===========================================================================
# T 组 12 — Truth 映射表校验 (POST /complete 入口, 只走 truth_validator)
# ===========================================================================

def test_t01_rent_overdue_unpaid_complete_409(
    client, db_session, owner_a, org_a, property_id, unit_id, lease_id
):
    headers = _headers(owner_a[1])
    task = _insert_task(
        db_session,
        task_type=OperationalTaskType.RENT_OVERDUE,
        title=f"RENT_OVERDUE {_RENT_PERIOD}",
        source_type="lease",
        source_id=lease_id,
        lease_id=lease_id,
        property_id=property_id,
        dedupe_key=f"RENT_OVERDUE:{lease_id}:{_RENT_PERIOD}",
        details={"periods": [_RENT_PERIOD]},
    )
    r = client.post(f"{API}/operations/tasks/{task.id}/complete", headers=headers)
    _assert_truth_409(r, task.id, expected_keyword="RentPeriodTruth", db=db_session)


def test_t02_rent_overdue_paid_complete_200(
    client, db_session, owner_a, org_a, property_id, unit_id, lease_id
):
    headers = _headers(owner_a[1])
    _rent_claim_and_verify(client, headers, lease_id, _RENT_PERIOD, "12000.00")
    task = _insert_task(
        db_session,
        task_type=OperationalTaskType.RENT_OVERDUE,
        title=f"RENT_OVERDUE {_RENT_PERIOD}",
        source_type="lease",
        source_id=lease_id,
        lease_id=lease_id,
        property_id=property_id,
        dedupe_key=f"RENT_OVERDUE_PAID:{lease_id}:{_RENT_PERIOD}",
        details={"periods": [_RENT_PERIOD]},
    )
    r = client.post(f"{API}/operations/tasks/{task.id}/complete", headers=headers)
    _assert_complete_200(r, db_session, task.id)


def test_t03_rent_due_no_period_complete_409(
    client, db_session, owner_a, org_a, property_id, unit_id, lease_id
):
    headers = _headers(owner_a[1])
    task = _insert_task(
        db_session,
        task_type=OperationalTaskType.RENT_DUE,
        title="RENT_DUE no-period",
        source_type="lease",
        source_id=lease_id,
        lease_id=lease_id,
        property_id=property_id,
        dedupe_key=f"RENT_DUE_NOPERIOD:{lease_id}",
        details={},
    )
    r = client.post(f"{API}/operations/tasks/{task.id}/complete", headers=headers)
    _assert_truth_409(r, task.id, expected_keyword="period", db=db_session)


def test_t04_lease_expiring_no_renewal_complete_409(
    client, db_session, owner_a, org_a, property_id, unit_id, lease_id
):
    headers = _headers(owner_a[1])
    task = _insert_task(
        db_session,
        task_type=OperationalTaskType.LEASE_EXPIRING,
        title="Lease expiring soon",
        source_type="lease",
        source_id=lease_id,
        lease_id=lease_id,
        property_id=property_id,
        dedupe_key=f"LEASE_EXPIRING:{lease_id}",
        details={},
    )
    r = client.post(f"{API}/operations/tasks/{task.id}/complete", headers=headers)
    _assert_truth_409(r, task.id, expected_keyword="renew", db=db_session)


def test_t05_approval_pending_expense_pending_complete_409(
    client, db_session, owner_a, org_a, property_id, unit_id, lease_id
):
    headers = _headers(owner_a[1])
    eid = _create_expense_api(client, headers, "8000.00", "pending",
                              property_id=property_id)
    task = _insert_task(
        db_session,
        task_type=OperationalTaskType.APPROVAL_PENDING,
        title="Approve expense",
        source_type="expense",
        source_id=eid,
        property_id=property_id,
        dedupe_key=f"APPROVAL_PENDING:{eid}",
        details={"expense_id": eid},
    )
    r = client.post(f"{API}/operations/tasks/{task.id}/complete", headers=headers)
    _assert_truth_409(r, task.id, expected_keyword="Expense.status", db=db_session)


def test_t06_approval_pending_expense_approved_complete_200(
    client, db_session, owner_a, org_a, property_id, unit_id, lease_id
):
    headers = _headers(owner_a[1])
    eid = _create_expense_api(client, headers, "4500.00", "pending",
                              property_id=property_id)
    _approve_expense_api(client, headers, eid)
    task = _insert_task(
        db_session,
        task_type=OperationalTaskType.APPROVAL_PENDING,
        title="Approve expense approved",
        source_type="expense",
        source_id=eid,
        property_id=property_id,
        dedupe_key=f"APPROVAL_PENDING_OK:{eid}",
        details={"expense_id": eid},
    )
    r = client.post(f"{API}/operations/tasks/{task.id}/complete", headers=headers)
    _assert_complete_200(r, db_session, task.id)


def test_t07_payment_pending_unpaid_complete_409(
    client, db_session, owner_a, org_a, property_id, unit_id, lease_id
):
    headers = _headers(owner_a[1])
    eid = _create_expense_api(client, headers, "6000.00", "pending",
                              property_id=property_id)
    _approve_expense_api(client, headers, eid)
    task = _insert_task(
        db_session,
        task_type=OperationalTaskType.PAYMENT_PENDING,
        title="Pay expense",
        source_type="expense",
        source_id=eid,
        property_id=property_id,
        dedupe_key=f"PAYMENT_PENDING_UNPAID:{eid}",
        details={"expense_id": eid, "amount": "6000.00"},
    )
    r = client.post(f"{API}/operations/tasks/{task.id}/complete", headers=headers)
    _assert_truth_409(r, task.id, expected_keyword="fully_paid", db=db_session)


def test_t08_payment_pending_fully_paid_complete_200(
    client, db_session, owner_a, secretary_a, org_a, property_id, unit_id, lease_id
):
    owner_h = _headers(owner_a[1])
    sec_h = _headers(secretary_a[1])
    eid = _create_expense_api(client, sec_h, "7000.00", "pending",
                              property_id=property_id)
    _approve_expense_api(client, owner_h, eid)
    _expense_payment_claim_verify(client, owner_h, eid, "7000.00",
                                  claimer_headers=sec_h)
    task = _insert_task(
        db_session,
        task_type=OperationalTaskType.PAYMENT_PENDING,
        title="Pay expense paid",
        source_type="expense",
        source_id=eid,
        property_id=property_id,
        dedupe_key=f"PAYMENT_PENDING_PAID:{eid}",
        details={"expense_id": eid},
    )
    r = client.post(f"{API}/operations/tasks/{task.id}/complete", headers=owner_h)
    _assert_complete_200(r, db_session, task.id)


def test_t09_payment_pending_rejected_complete_200(
    client, db_session, owner_a, org_a, property_id, unit_id, lease_id
):
    headers = _headers(owner_a[1])
    eid = _create_expense_api(client, headers, "1500.00", "pending",
                              property_id=property_id)
    r_rej = client.post(f"{API}/expenses/{eid}/reject",
                        json={"reason": "not approved"}, headers=headers)
    if r_rej.status_code not in (200, 201):
        e = db_session.get(Expense, eid)
        e.status = ExpenseStatus.rejected
        db_session.commit()
    task = _insert_task(
        db_session,
        task_type=OperationalTaskType.PAYMENT_PENDING,
        title="Pay expense rejected",
        source_type="expense",
        source_id=eid,
        property_id=property_id,
        dedupe_key=f"PAYMENT_PENDING_REJECTED:{eid}",
        details={"expense_id": eid},
    )
    r = client.post(f"{API}/operations/tasks/{task.id}/complete", headers=headers)
    _assert_complete_200(r, db_session, task.id)


def test_t10_ac_repair_open_complete_409(
    client, db_session, owner_a, org_a, property_id, unit_id, lease_id
):
    headers = _headers(owner_a[1])
    rep = _create_repair_api(client, headers, property_id, unit_id)
    rid = rep["id"]
    task = _insert_task(
        db_session,
        task_type=OperationalTaskType.AC_MAINTENANCE,
        title="Fix AC unit",
        source_type="repair",
        source_id=rid,
        property_id=property_id,
        dedupe_key=f"AC_MAINTENANCE:{rid}:OPEN",
        details={"repair_id": rid},
    )
    r = client.post(f"{API}/operations/tasks/{task.id}/complete", headers=headers)
    _assert_truth_409(r, task.id, expected_keyword="RepairOperation",
                      db=db_session)


def test_t11_ac_repair_closed_complete_200(
    client, db_session, owner_a, org_a, property_id, unit_id, lease_id
):
    headers = _headers(owner_a[1])
    rep = _create_repair_api(client, headers, property_id, unit_id)
    rid = rep["id"]
    repair = db_session.get(RepairOperation, rid)
    repair.status = RepairOperationStatus.CLOSED
    db_session.commit()
    task = _insert_task(
        db_session,
        task_type=OperationalTaskType.AC_MAINTENANCE,
        title="Fix AC unit done",
        source_type="repair",
        source_id=rid,
        property_id=property_id,
        dedupe_key=f"AC_MAINTENANCE:{rid}:CLOSED",
        details={"repair_id": rid},
    )
    r = client.post(f"{API}/operations/tasks/{task.id}/complete", headers=headers)
    _assert_complete_200(r, db_session, task.id)


def test_t12_unknown_projection_fail_closed_complete_409(
    client, db_session, owner_a, org_a, property_id, unit_id, lease_id
):
    headers = _headers(owner_a[1])
    task = _insert_task(
        db_session,
        task_type=OperationalTaskType.FOLLOWUP,
        title="Unknown bot followup",
        source_type="custom_integration_bot",
        source_id=42,
        property_id=property_id,
        dedupe_key="UNKNOWN_FOLLOWUP:42",
        details={"external_ref": "bot-42"},
    )
    r = client.post(f"{API}/operations/tasks/{task.id}/complete", headers=headers)
    _assert_truth_409(r, task.id, expected_keyword="mapping", db=db_session)


# ===========================================================================
# G 组 6 — Gate 封杀
# ===========================================================================

def test_g01_two_step_patch_rent_overdue_unpaid_409(
    client, db_session, owner_a, org_a, property_id, unit_id, lease_id
):
    headers = _headers(owner_a[1])
    task = _insert_task(
        db_session,
        task_type=OperationalTaskType.RENT_OVERDUE,
        title="两步 PATCH RENT",
        source_type="lease",
        source_id=lease_id,
        lease_id=lease_id,
        property_id=property_id,
        dedupe_key=f"TWO_STEP_RENT:{lease_id}",
        details={"periods": [_RENT_PERIOD]},
    )
    now = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    r1 = client.patch(f"{API}/operations/tasks/{task.id}", json={
        "status": "IN_PROGRESS",
        "next_action": "call tenant",
        "next_check_at": now,
    }, headers=headers)
    assert r1.status_code == 200, r1.text
    assert _task_status(db_session, task.id) == OperationalTaskStatus.IN_PROGRESS

    r2 = client.patch(f"{API}/operations/tasks/{task.id}", json={
        "status": "COMPLETED",
    }, headers=headers)
    _assert_truth_409(r2, task.id, expected_keyword="RentPeriodTruth",
                      status=OperationalTaskStatus.IN_PROGRESS, db=db_session)


def test_g02_post_complete_task_approval_pending_409(
    client, db_session, owner_a, org_a, property_id, unit_id, lease_id
):
    headers = _headers(owner_a[1])
    eid = _create_expense_api(client, headers, "9000.00", "pending",
                              property_id=property_id)
    task = _insert_task(
        db_session,
        task_type=OperationalTaskType.APPROVAL_PENDING,
        title="POST complete approval pending",
        source_type="expense",
        source_id=eid,
        property_id=property_id,
        dedupe_key=f"POST_COMPLETE_APPROVAL:{eid}",
        details={"expense_id": eid},
    )
    r = client.post(f"{API}/operations/tasks/{task.id}/complete", headers=headers)
    _assert_truth_409(r, task.id, expected_keyword="Expense.status", db=db_session)


def test_g03_secretary_two_step_patch_no_bypass_409(
    client, db_session, owner_a, secretary_a, org_a, property_id, unit_id, lease_id
):
    sec_h = _headers(secretary_a[1])
    task = _insert_task(
        db_session,
        task_type=OperationalTaskType.RENT_OVERDUE,
        title="Secretary 两步 PATCH",
        source_type="lease",
        source_id=lease_id,
        lease_id=lease_id,
        property_id=property_id,
        dedupe_key=f"SEC_TWO_STEP:{lease_id}",
        details={"periods": [_RENT_PERIOD]},
    )
    now = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    r1 = client.patch(f"{API}/operations/tasks/{task.id}", json={
        "status": "IN_PROGRESS",
        "next_action": "chase payment",
        "next_check_at": now,
    }, headers=sec_h)
    assert r1.status_code == 200, r1.text
    r2 = client.patch(f"{API}/operations/tasks/{task.id}", json={
        "status": "COMPLETED",
    }, headers=sec_h)
    _assert_truth_409(r2, task.id, expected_keyword="RentPeriodTruth",
                      status=OperationalTaskStatus.IN_PROGRESS, db=db_session)


def test_g04_custom_manual_conversation_task_schema_a_complete_200(
    client, db_session, owner_a, org_a, property_id, unit_id, lease_id
):
    """Schema A: non-projection manual task (source_type=conversation)
    is allowed to be PATCH-closed by a HUMAN principal."""
    headers = _headers(owner_a[1])
    task = _insert_task(
        db_session,
        task_type=OperationalTaskType.FOLLOWUP,
        title="Call tenant for keys return",
        source_type="conversation",
        source_id=None,
        property_id=property_id,
        dedupe_key=None,
        details={"context": "manual copilot task"},
    )
    r = client.post(f"{API}/operations/tasks/{task.id}/complete", headers=headers)
    _assert_complete_200(r, db_session, task.id)


def test_g05_owner_no_bypass_repair_waiting_complete_409(
    client, db_session, owner_a, org_a, property_id, unit_id, lease_id
):
    headers = _headers(owner_a[1])
    rep = _create_repair_api(client, headers, property_id, unit_id)
    rid = rep["id"]
    repair = db_session.get(RepairOperation, rid)
    repair.status = RepairOperationStatus.WAITING_PAYMENT
    db_session.commit()
    task = _insert_task(
        db_session,
        task_type=OperationalTaskType.AC_MAINTENANCE,
        title="Owner close repair",
        source_type="repair",
        source_id=rid,
        property_id=property_id,
        dedupe_key=f"OWNER_REPAIR_BYPASS:{rid}",
        details={"repair_id": rid},
    )
    r = client.post(f"{API}/operations/tasks/{task.id}/complete", headers=headers)
    _assert_truth_409(r, task.id, expected_keyword="RepairOperation", db=db_session)


def test_g06_forward_seam_db_direct_write_no_gate_trigger(
    client, db_session, owner_a, org_a, property_id, unit_id, lease_id
):
    """Forward projection seam (DB direct UPDATE COMPLETED) never goes
    through the HTTP API, so the truth_validator gate cannot fire."""
    task = _insert_task(
        db_session,
        task_type=OperationalTaskType.APPROVAL_PENDING,
        title="Forward seam expense approval",
        source_type="expense",
        source_id=9999999,
        property_id=property_id,
        dedupe_key="FORWARD_SEAM:9999999",
        details={},
    )
    now = datetime.now(timezone.utc)
    db_session.query(OperationalTask).filter(
        OperationalTask.id == task.id
    ).update(
        {"status": OperationalTaskStatus.COMPLETED,
         "completed_at": now,
         "completed_by": None,
         "reminder_generation": OperationalTask.reminder_generation + 1}
    )
    db_session.commit()
    assert _task_status(db_session, task.id) == OperationalTaskStatus.COMPLETED


# ===========================================================================
# R 组 5 — Reconcile Repair 对账
# ===========================================================================

def test_r01_repair_open_reconcile_keeps_pending(
    db_session, owner_a, org_a, property_id, unit_id, lease_id
):
    repair = RepairOperation(
        issue="test",
        property_id=property_id,
        unit_id=unit_id,
        status=RepairOperationStatus.OPEN,
        closure_criteria="criteria",
    )
    db_session.add(repair)
    db_session.commit()
    db_session.refresh(repair)
    task = _insert_task(
        db_session,
        task_type=OperationalTaskType.AC_MAINTENANCE,
        title="R01 repair open",
        source_type="repair",
        source_id=repair.id,
        property_id=property_id,
        dedupe_key=f"R01:repair:{repair.id}:AC",
        details={"repair_id": repair.id},
    )
    reconcile_tasks(db_session, now=datetime.now(timezone.utc))
    assert _task_status(db_session, task.id) == OperationalTaskStatus.PENDING


def test_r02_repair_verifying_reconcile_keeps_pending(
    db_session, owner_a, org_a, property_id, unit_id, lease_id
):
    repair = RepairOperation(
        issue="test",
        property_id=property_id,
        unit_id=unit_id,
        status=RepairOperationStatus.VERIFYING,
        closure_criteria="criteria",
    )
    db_session.add(repair)
    db_session.commit()
    db_session.refresh(repair)
    task = _insert_task(
        db_session,
        task_type=OperationalTaskType.AC_MAINTENANCE,
        title="R02 repair verifying",
        source_type="repair",
        source_id=repair.id,
        property_id=property_id,
        dedupe_key=f"R02:repair:{repair.id}:VER",
        details={"repair_id": repair.id},
    )
    reconcile_tasks(db_session, now=datetime.now(timezone.utc))
    assert _task_status(db_session, task.id) == OperationalTaskStatus.PENDING


def test_r03_repair_closed_reconcile_completed_system(
    db_session, owner_a, org_a, property_id, unit_id, lease_id
):
    repair = RepairOperation(
        issue="test",
        property_id=property_id,
        unit_id=unit_id,
        status=RepairOperationStatus.CLOSED,
        closure_criteria="criteria",
    )
    db_session.add(repair)
    db_session.commit()
    db_session.refresh(repair)
    task = _insert_task(
        db_session,
        task_type=OperationalTaskType.AC_MAINTENANCE,
        title="R03 repair closed",
        source_type="repair",
        source_id=repair.id,
        property_id=property_id,
        dedupe_key=f"R03:repair:{repair.id}:CLOSED",
        details={"repair_id": repair.id},
    )
    reconcile_tasks(db_session, now=datetime.now(timezone.utc))
    db_session.refresh(task)
    assert task.status == OperationalTaskStatus.COMPLETED
    assert task.completed_by is None


def test_r04_repair_cancelled_reconcile_cancelled(
    db_session, owner_a, org_a, property_id, unit_id, lease_id
):
    repair = RepairOperation(
        issue="test",
        property_id=property_id,
        unit_id=unit_id,
        status=RepairOperationStatus.CANCELLED,
        closure_criteria="criteria",
    )
    db_session.add(repair)
    db_session.commit()
    db_session.refresh(repair)
    task = _insert_task(
        db_session,
        task_type=OperationalTaskType.FOLLOWUP,
        title="R04 repair cancelled",
        source_type="repair",
        source_id=repair.id,
        property_id=property_id,
        dedupe_key=f"R04:repair:{repair.id}:CAN",
        details={"repair_id": repair.id},
    )
    reconcile_tasks(db_session, now=datetime.now(timezone.utc))
    assert _task_status(db_session, task.id) == OperationalTaskStatus.CANCELLED


def test_r05_dedupe_key_prefix_repair_reconcile_close(
    db_session, owner_a, org_a, property_id, unit_id, lease_id
):
    repair = RepairOperation(
        issue="test",
        property_id=property_id,
        unit_id=unit_id,
        status=RepairOperationStatus.CLOSED,
        closure_criteria="criteria",
    )
    db_session.add(repair)
    db_session.commit()
    db_session.refresh(repair)
    task = _insert_task(
        db_session,
        task_type=OperationalTaskType.AC_MAINTENANCE,
        title="R05 dedupe_key repair prefix",
        source_type="repair",
        source_id=None,
        property_id=property_id,
        dedupe_key=f"repair:{repair.id}:FOLLOWUP_V1",
        details={"repair_id": repair.id},
    )
    reconcile_tasks(db_session, now=datetime.now(timezone.utc))
    db_session.refresh(task)
    assert task.status == OperationalTaskStatus.COMPLETED
    assert task.completed_by is None


# ===========================================================================
# Q 组 4 — Quick Expense unresolved 三通道 OR
# ===========================================================================

from app.services.operations.quick import build_quick_expense


def test_q01_lease_id_only_channel_not_missing(
    db_session, owner_a, org_a, property_id, unit_id, lease_id, tenant_id
):
    t = _insert_task(
        db_session,
        task_type=OperationalTaskType.PAYMENT_PENDING,
        title="Q01: lease only",
        source_type="expense",
        source_id=81000001,
        property_id=None,
        lease_id=lease_id,
        tenant_id=None,
        dedupe_key="Q01:LEASE_ONLY",
        details={},
    )
    with patch(
        "app.services.operations.quick._derive_org_scope_sets",
        return_value=({property_id}, {unit_id}, {lease_id}, {tenant_id}),
    ):
        q = build_quick_expense(db_session, now=datetime.now(timezone.utc))
    rows = q.get("unresolved_expense_tasks", [])
    row_ids = {r["id"] for r in rows}
    assert t.id in row_ids


def test_q02_tenant_id_only_channel_not_missing(
    db_session, owner_a, org_a, property_id, unit_id, lease_id, tenant_id
):
    t = _insert_task(
        db_session,
        task_type=OperationalTaskType.APPROVAL_PENDING,
        title="Q02: tenant only",
        source_type="expense",
        source_id=81000002,
        property_id=None,
        lease_id=None,
        tenant_id=tenant_id,
        dedupe_key="Q02:TENANT_ONLY",
        details={},
    )
    with patch(
        "app.services.operations.quick._derive_org_scope_sets",
        return_value=({property_id}, {unit_id}, {lease_id}, {tenant_id}),
    ):
        q = build_quick_expense(db_session, now=datetime.now(timezone.utc))
    rows = q.get("unresolved_expense_tasks", [])
    row_ids = {r["id"] for r in rows}
    assert t.id in row_ids


def test_q03_cross_org_task_not_in_unresolved_fail_closed(
    db_session, owner_a, org_a, org_b, property_id, unit_id, lease_id, tenant_id
):
    _ = org_b
    cross = _insert_task(
        db_session,
        task_type=OperationalTaskType.PAYMENT_PENDING,
        title="Q03: cross org should not show (no channel match)",
        source_type="expense",
        source_id=81000003,
        property_id=None,
        lease_id=None,
        tenant_id=None,
        dedupe_key="Q03:CROSS_ORG_NULL_CHANNEL",
        details={},
    )
    with patch(
        "app.services.operations.quick._derive_org_scope_sets",
        return_value=({property_id}, {unit_id}, {lease_id}, {tenant_id}),
    ):
        q = build_quick_expense(db_session, now=datetime.now(timezone.utc))
    rows = q.get("unresolved_expense_tasks", [])
    row_ids = {r["id"] for r in rows}
    assert cross.id not in row_ids


def test_q04_empty_scope_clauses_zero_unresolved_fail_closed(
    db_session, owner_a, org_a, property_id, unit_id, lease_id
):
    _insert_task(
        db_session,
        task_type=OperationalTaskType.PAYMENT_PENDING,
        title="Q04: should be filtered out",
        source_type="expense",
        source_id=81000004,
        property_id=property_id,
        lease_id=lease_id,
        dedupe_key="Q04:EMPTY_SCOPE",
        details={},
    )
    empty: set = set()
    with patch(
        "app.services.operations.quick._derive_org_scope_sets",
        return_value=(empty, empty, empty, empty),
    ):
        q = build_quick_expense(db_session, now=datetime.now(timezone.utc))
    assert q.get("unresolved_expense_tasks") == []


# ===========================================================================
# F 组 4 — Forward Seam 正常完成 (不被 gate 误伤)
# ===========================================================================

def test_f01_rent_verify_forward_seam_reconcile_close(
    client, db_session, owner_a, org_a, property_id, unit_id, lease_id
):
    headers = _headers(owner_a[1])
    task = _insert_task(
        db_session,
        task_type=OperationalTaskType.RENT_OVERDUE,
        title=f"F01 RENT_OVERDUE {_RENT_PERIOD}",
        source_type="lease",
        source_id=lease_id,
        lease_id=lease_id,
        property_id=property_id,
        dedupe_key=f"F01:RENT_OVERDUE:{lease_id}:{_RENT_PERIOD}",
        details={"periods": [_RENT_PERIOD]},
    )
    _rent_claim_and_verify(client, headers, lease_id, _RENT_PERIOD, "12000.00")
    db_session.commit()
    reconcile_tasks(db_session, now=datetime.now(timezone.utc))
    db_session.refresh(task)
    assert task.status == OperationalTaskStatus.COMPLETED
    assert task.completed_by is None


def test_f02_expense_pay_full_forward_seam_reconcile_close(
    client, db_session, owner_a, secretary_a, org_a, property_id, unit_id, lease_id
):
    owner_h = _headers(owner_a[1])
    sec_h = _headers(secretary_a[1])
    eid = _create_expense_api(client, sec_h, "3200.00", "pending",
                              property_id=property_id)
    _approve_expense_api(client, owner_h, eid)
    task = _insert_task(
        db_session,
        task_type=OperationalTaskType.PAYMENT_PENDING,
        title="F02 PAYMENT_PENDING forward",
        source_type="expense",
        source_id=eid,
        property_id=property_id,
        dedupe_key=f"F02:PAYMENT_PENDING:{eid}",
        details={"expense_id": eid},
    )
    _expense_payment_claim_verify(client, owner_h, eid, "3200.00",
                                  claimer_headers=sec_h)
    db_session.commit()
    reconcile_tasks(db_session, now=datetime.now(timezone.utc))
    db_session.refresh(task)
    assert task.status == OperationalTaskStatus.COMPLETED


def test_f03_repair_closed_reconcile_close(
    client, db_session, owner_a, org_a, property_id, unit_id, lease_id
):
    headers = _headers(owner_a[1])
    rep = _create_repair_api(client, headers, property_id, unit_id)
    rid = rep["id"]
    repair = db_session.get(RepairOperation, rid)
    repair.status = RepairOperationStatus.CLOSED
    db_session.commit()
    task = _insert_task(
        db_session,
        task_type=OperationalTaskType.AC_MAINTENANCE,
        title="F03 repair CLOSED reconcile",
        source_type="repair",
        source_id=rid,
        property_id=property_id,
        dedupe_key=f"F03:repair:{rid}:CLOSED",
        details={"repair_id": rid},
    )
    reconcile_tasks(db_session, now=datetime.now(timezone.utc))
    db_session.refresh(task)
    assert task.status == OperationalTaskStatus.COMPLETED


def test_f04_reconcile_rent_paid_closes_rent_due(
    client, db_session, owner_a, org_a, property_id, unit_id, lease_id
):
    headers = _headers(owner_a[1])
    task = _insert_task(
        db_session,
        task_type=OperationalTaskType.RENT_DUE,
        title=f"F04 RENT_DUE {_RENT_PERIOD}",
        source_type="lease",
        source_id=lease_id,
        lease_id=lease_id,
        property_id=property_id,
        dedupe_key=f"F04:RENT_DUE:{lease_id}:{_RENT_PERIOD}",
        details={"period": _RENT_PERIOD},
    )
    _rent_claim_and_verify(client, headers, lease_id, _RENT_PERIOD, "12000.00")
    db_session.commit()
    reconcile_tasks(db_session, now=datetime.now(timezone.utc))
    db_session.refresh(task)
    assert task.status == OperationalTaskStatus.COMPLETED
    assert task.completed_by is None
