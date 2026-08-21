"""PASAY-MILESTONE-002 — Repair Closure Truth + Quote≠Expense + Evidence tests.

Mirror pattern of test_repair_ai_employee_008.py (Case A..F).  New:

  Case G  — Proposal APPROVED auto-creates linked Expense (quote ≠ expense:
            RepairProposal.status.APPROVED with its own amount/state AND
            Expense with its own status=approved + separate financial lifecycle)
  Case H  — COMPLETION_EVENT signal WITHOUT evidence_ids/blob → close fails
            (contractor says done ≠ repair closed)
  Case I  — After paid + HUMAN_CONFIRMED close → linked OperationalTasks
            APPROVAL_PENDING / PAYMENT_PENDING / FOLLOWUP → COMPLETED
            (repair truth → task projection; tasks never close repair)
  Case J  — cross-org: owner_b can't close org_a repair (404 fail-closed)
  Case K  — evidence + HUMAN_CONFIRMED → verify_and_close passes (closes repair)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models.operations import (
    OperationalTask,
    OperationalTaskPriority,
    OperationalTaskStatus,
    OperationalTaskType,
)

API = "/api/v1"


def _create_repair(client, headers, property_id, unit_id, issue="leaking faucet"):
    r = client.post(
        f"{API}/repairs",
        json={
            "issue": issue,
            "unit_id": unit_id,
            "property_id": property_id,
            "closure_criteria": "Faucet no longer leaks + receipt photo",
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _submit_proposal(client, headers, repair_id, amount, vendor="Fix-It Co"):
    r = client.post(
        f"{API}/repairs/{repair_id}/proposals",
        json={
            "amount": amount,
            "vendor": vendor,
            "description": "parts + 1hr labor",
            "submit_as_expense": False,
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _decide(client, headers, repair_id, decision, version=None, reason=None, expense_id=None):
    payload = {"decision": decision}
    if version is not None:
        payload["version"] = version
    if reason:
        payload["reason"] = reason
    if expense_id is not None:
        payload["expense_id"] = expense_id
    r = client.post(
        f"{API}/repairs/{repair_id}/decide", json=payload, headers=headers
    )
    assert r.status_code == 200, r.text
    return r.json()


def _detail(client, headers, repair_id):
    r = client.get(f"{API}/repairs/{repair_id}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _record_result(client, headers, repair_id, evidence=None, result="Vendor finished"):
    payload = {"verification_result": result}
    if evidence:
        payload["evidence_ids"] = evidence
    r = client.post(
        f"{API}/repairs/{repair_id}/record-result",
        json=payload,
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _verify_close(
    client,
    headers,
    repair_id,
    signal="HUMAN_CONFIRMED",
    result="Owner checked: faucet no longer leaks",
):
    r = client.post(
        f"{API}/repairs/{repair_id}/verify",
        json={
            "closure_signal": signal,
            "verification_result": result,
        },
        headers=headers,
    )
    return r


def _create_repair_task_db(db, repair_id, task_type="PAYMENT_PENDING"):
    """Plant a linked OperationalTask using the standard details.repair_id
    projection pattern. Inserted directly into the DB so we can set the
    metadata JSONB column (TaskCreateIn has no `details` field)."""
    _TYPE_MAP = {
        "APPROVAL_PENDING": OperationalTaskType.APPROVAL_PENDING,
        "PAYMENT_PENDING": OperationalTaskType.PAYMENT_PENDING,
        "FOLLOWUP": OperationalTaskType.FOLLOWUP,
    }
    now = datetime.now(timezone.utc)
    t = OperationalTask(
        task_type=_TYPE_MAP[task_type],
        title=f"{task_type} for repair",
        status=OperationalTaskStatus.PENDING,
        dedupe_key=f"repair:{repair_id}:{task_type}",
        priority=OperationalTaskPriority.high,
        due_at=now + timedelta(days=3),
        source_type="repair_closure_test",
        metadata={"repair_id": repair_id},
        created_by=None,
        updated_by=None,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _task_status_db(db, task_id):
    t = db.get(OperationalTask, task_id)
    assert t is not None
    return t.status


# Case G: Proposal APPROVED → auto-creates linked Expense (quote ≠ expense)
def test_g_approve_creates_expense(
    client, owner_a, org_a, property_id, unit_id
):
    headers = {"Authorization": f"Bearer {owner_a[1]}"}
    rep = _create_repair(client, headers, property_id, unit_id)
    _submit_proposal(client, headers, rep["id"], amount="3500.00")
    detail_before = _detail(client, headers, rep["id"])
    # Before approve, no linked expense
    assert detail_before["expense_ids"] == [] or all(
        e is None for e in detail_before["expense_ids"]
    )
    # Approve → expense_ids should now have one linked expense
    after = _decide(client, headers, rep["id"], decision="approve", version=1)
    expense_ids = [e for e in after["expense_ids"] if e is not None]
    assert len(expense_ids) == 1, (
        "Proposal APPROVE should auto-create linked Expense; got expense_ids="
        f"{after['expense_ids']}"
    )
    # Proposal (quote) status = APPROVED; Expense status = approved (NOT paid)
    proposal = [p for p in after["proposals"] if p["version"] == 1][0]
    assert proposal["status"] == "APPROVED"
    assert proposal["amount"] == "3500.00"
    eid = expense_ids[0]
    r = client.get(f"{API}/expenses/{eid}", headers=headers)
    assert r.status_code == 200, r.text
    exp = r.json()
    assert exp["amount"] == "3500.00"
    assert exp["status"] == "approved"
    assert exp["status"] != "paid"
    assert after["status"] == "WAITING_PAYMENT"
    assert after["status"] != "CLOSED"


# Case H: COMPLETION_EVENT signal WITHOUT evidence → VERIFY fails (409)
def test_h_completion_event_without_evidence_fails(
    client, owner_a, org_a, property_id, unit_id
):
    headers = {"Authorization": f"Bearer {owner_a[1]}"}
    rep = _create_repair(client, headers, property_id, unit_id)
    _submit_proposal(client, headers, rep["id"], amount="3500.00")
    _decide(client, headers, rep["id"], decision="approve", version=1)
    d = _detail(client, headers, rep["id"])
    eid = [e for e in d["expense_ids"] if e is not None][0]
    # Pay the linked expense (move repair → VERIFYING)
    r = client.post(
        f"{API}/repairs/{rep['id']}/pay-expense",
        params={"expense_id": eid},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    # Now try to close with COMPLETION_EVENT without evidence → BLOCKED
    r = _verify_close(
        client,
        headers,
        rep["id"],
        signal="COMPLETION_EVENT",
        result="Vendor SMS: job done",
    )
    assert r.status_code == 409, (
        "COMPLETION_EVENT without evidence should fail, got "
        f"{r.status_code}: {r.text}"
    )
    # Repair should remain NOT closed
    d = _detail(client, headers, rep["id"])
    assert d["status"] != "CLOSED"


# Case I: evidence + HUMAN_CONFIRMED → closes repair, tasks COMPLETED
def test_i_human_confirmed_with_evidence_closes_and_syncs_tasks(
    client, owner_a, org_a, property_id, unit_id, db_session
):
    headers = {"Authorization": f"Bearer {owner_a[1]}"}
    rep = _create_repair(client, headers, property_id, unit_id)
    t1 = _create_repair_task_db(db_session, rep["id"], "APPROVAL_PENDING")
    t2 = _create_repair_task_db(db_session, rep["id"], "PAYMENT_PENDING")
    t3 = _create_repair_task_db(db_session, rep["id"], "FOLLOWUP")
    _submit_proposal(client, headers, rep["id"], amount="3500.00")
    _decide(client, headers, rep["id"], decision="approve", version=1)
    d = _detail(client, headers, rep["id"])
    eid = [e for e in d["expense_ids"] if e is not None][0]
    # Pay the linked expense (repair → VERIFYING)
    client.post(
        f"{API}/repairs/{rep['id']}/pay-expense",
        params={"expense_id": eid},
        headers=headers,
    )
    # Evidence: vendor photo + receipt (fake ids 42, 77)
    _record_result(client, headers, rep["id"], evidence=[42, 77])
    # Verify close with HUMAN_CONFIRMED
    r = _verify_close(
        client,
        headers,
        rep["id"],
        signal="HUMAN_CONFIRMED",
        result="Tenant confirmed: faucet no longer leaks",
    )
    assert r.status_code == 200, r.text
    d = _detail(client, headers, rep["id"])
    assert d["status"] == "CLOSED"
    # Linked tasks → COMPLETED (truth → projection; tasks never close repair)
    db_session.commit()
    assert _task_status_db(db_session, t1.id) == OperationalTaskStatus.COMPLETED
    assert _task_status_db(db_session, t2.id) == OperationalTaskStatus.COMPLETED
    assert _task_status_db(db_session, t3.id) == OperationalTaskStatus.COMPLETED


# Case J: cross-org fail-closed owner_b → org_a repair
def test_j_cross_org_fail_closed(
    client, owner_a, owner_b, org_a, org_b, property_id, unit_id
):
    headers_a = {"Authorization": f"Bearer {owner_a[1]}"}
    headers_b = {"Authorization": f"Bearer {owner_b[1]}"}
    rep = _create_repair(client, headers_a, property_id, unit_id)
    # owner_b GET repair → 404 fail-closed
    r = client.get(f"{API}/repairs/{rep['id']}", headers=headers_b)
    assert r.status_code == 404, r.text
    # owner_b record-result → 404
    r = client.post(
        f"{API}/repairs/{rep['id']}/record-result",
        json={"verification_result": "stolen attempt"},
        headers=headers_b,
    )
    assert r.status_code == 404, r.text
    # owner_b verify close → 404
    r = client.post(
        f"{API}/repairs/{rep['id']}/verify",
        json={
            "closure_signal": "HUMAN_CONFIRMED",
            "verification_result": "stolen attempt",
        },
        headers=headers_b,
    )
    assert r.status_code == 404, r.text


# Case L: COMPLETION_EVENT with evidence_ids → allow verify_and_close pass
def test_l_completion_event_with_evidence_passes(
    client, owner_a, org_a, property_id, unit_id
):
    headers = {"Authorization": f"Bearer {owner_a[1]}"}
    rep = _create_repair(client, headers, property_id, unit_id)
    _submit_proposal(client, headers, rep["id"], amount="2200.00")
    _decide(client, headers, rep["id"], decision="approve", version=1)
    d = _detail(client, headers, rep["id"])
    eid = [e for e in d["expense_ids"] if e is not None][0]
    client.post(
        f"{API}/repairs/{rep['id']}/pay-expense",
        params={"expense_id": eid},
        headers=headers,
    )
    # Evidence [1, 2] recorded via record-result (sets completion_evidence_ids)
    _record_result(client, headers, rep["id"], evidence=[1, 2])
    r = _verify_close(
        client,
        headers,
        rep["id"],
        signal="COMPLETION_EVENT",
        result="Structured completion event from vendor portal",
    )
    assert r.status_code == 200, r.text
    assert _detail(client, headers, rep["id"])["status"] == "CLOSED"
