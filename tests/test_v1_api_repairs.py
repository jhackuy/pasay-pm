"""HTTP-level behavior tests for the V1 Repair router.

Proves the router is thin and correct: authentication, role gating handled
by the shared service, mandatory ``Idempotency-Key`` on report and quote,
the 9-state repair lifecycle, technician assignment (incl. external),
quote submit/approve/reject, work progress, completion claim, OWNER
verification as the closure gate, and the counterexample that a related
Expense/Payment verification MUST NOT close a Repair.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db, get_session_factory
from tests.v1_support import seed_workspace, v1_engine_ctx


QUOTE_AMOUNT = "3500.00"


@pytest.fixture
def api():
    with v1_engine_ctx():
        session = get_session_factory()()
        workspace_a = seed_workspace(session, name="RepAlpha")
        workspace_b = seed_workspace(session, name="RepBeta")

        from app.v1.main import app as v1_app

        def _override_get_db():
            yield session

        v1_app.dependency_overrides[get_db] = _override_get_db
        try:
            with TestClient(v1_app) as client:
                yield client, workspace_a, workspace_b
        finally:
            v1_app.dependency_overrides.clear()
            session.close()


def _open_report(
    client,
    workspace,
    *,
    key,
    unit_id=None,
    title="Leaking faucet",
    description="Bathroom faucet drips continuously",
    category="PLUMBING",
    severity="MEDIUM",
    headers=None,
    linked_expense_payment_id=None,
):
    request_headers = dict(headers or workspace.secretary_headers())
    if key is not None:
        request_headers["Idempotency-Key"] = key
    payload: dict[str, object] = {
        "unit_id": unit_id or workspace.unit_id,
        "title": title,
        "description": description,
        "category": category,
        "severity": severity,
    }
    if linked_expense_payment_id is not None:
        payload["linked_expense_payment_id"] = linked_expense_payment_id
    return client.post(
        f"/api/v1/repairs/reports?org_id={workspace.org_id}",
        json=payload,
        headers=request_headers,
    )


# ---- health / auth ---------------------------------------------------


def test_health_is_available(api):
    client, _workspace_a, _workspace_b = api
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_unauthenticated_request_is_rejected(api):
    client, workspace_a, _workspace_b = api
    response = client.get(
        f"/api/v1/repairs/reports?org_id={workspace_a.org_id}",
    )
    assert response.status_code == 401


# ---- open_report -----------------------------------------------------


def test_secretary_opens_a_report_with_idempotency_key(api):
    client, workspace_a, _workspace_b = api
    response = _open_report(client, workspace_a, key="open-1")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["state"] == "REPORTED"
    assert body["title"] == "Leaking faucet"
    assert body["category"] == "PLUMBING"
    assert body["idempotency_key"] == "open-1"


def test_owner_can_also_open_a_report(api):
    client, workspace_a, _workspace_b = api
    response = _open_report(
        client, workspace_a, key="open-owner", headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 201, response.text


def test_open_report_without_idempotency_key_is_400(api):
    client, workspace_a, _workspace_b = api
    request_headers = dict(workspace_a.secretary_headers())
    request_headers.pop("Idempotency-Key", None)
    response = client.post(
        f"/api/v1/repairs/reports?org_id={workspace_a.org_id}",
        json={
            "unit_id": workspace_a.unit_id,
            "title": "x",
            "description": "y",
            "category": "PLUMBING",
            "severity": "LOW",
        },
        headers=request_headers,
    )
    assert response.status_code == 400


def test_open_report_oversize_idempotency_key_is_400(api):
    client, workspace_a, _workspace_b = api
    response = _open_report(client, workspace_a, key="x" * 200)
    assert response.status_code == 400


def test_open_report_unknown_category_is_400(api):
    client, workspace_a, _workspace_b = api
    response = _open_report(
        client, workspace_a, key="open-bad-cat", category="UNKNOWN",
    )
    assert response.status_code == 400


def test_open_report_unknown_severity_is_400(api):
    client, workspace_a, _workspace_b = api
    response = _open_report(
        client, workspace_a, key="open-bad-sev", severity="CRITICAL",
    )
    assert response.status_code == 400


def test_open_report_unknown_field_is_422(api):
    client, workspace_a, _workspace_b = api
    request_headers = dict(workspace_a.secretary_headers())
    request_headers["Idempotency-Key"] = "open-unknown"
    response = client.post(
        f"/api/v1/repairs/reports?org_id={workspace_a.org_id}",
        json={
            "unit_id": workspace_a.unit_id,
            "title": "x",
            "description": "y",
            "category": "PLUMBING",
            "severity": "LOW",
            "evil_extra": True,
        },
        headers=request_headers,
    )
    assert response.status_code == 422


def test_idempotent_replay_returns_same_report_with_200(api):
    client, workspace_a, _workspace_b = api
    first = _open_report(client, workspace_a, key="replay-1")
    assert first.status_code == 201
    second = _open_report(client, workspace_a, key="replay-1")
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]


def test_idempotency_key_with_different_payload_is_409(api):
    client, workspace_a, _workspace_b = api
    first = _open_report(
        client, workspace_a, key="conflict-1", title="Faucet",
    )
    assert first.status_code == 201
    second = _open_report(
        client, workspace_a, key="conflict-1", title="Different",
    )
    assert second.status_code == 409


def test_case_preserving_idempotency_keys_distinguish(api):
    client, workspace_a, _workspace_b = api
    first = _open_report(client, workspace_a, key="CaseKey-1")
    assert first.status_code == 201
    second = _open_report(client, workspace_a, key="casekey-1")
    assert second.status_code == 201  # different case = different row


def test_cross_org_idempotency_key_does_not_collide(api):
    client, workspace_a, workspace_b = api
    first = _open_report(client, workspace_a, key="shared-key")
    second = _open_report(client, workspace_b, key="shared-key")
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] != second.json()["id"]


def test_cross_org_read_is_404(api):
    client, workspace_a, workspace_b = api
    response = _open_report(client, workspace_a, key="cross-1")
    report_id = response.json()["id"]
    # workspace_b tries to read workspace_a's report
    response = client.get(
        f"/api/v1/repairs/reports/{report_id}?org_id={workspace_b.org_id}",
        headers=workspace_b.owner_headers(),
    )
    assert response.status_code == 404


# ---- confirm / technician -------------------------------------------


def test_confirm_report_requires_owner_role(api):
    client, workspace_a, _workspace_b = api
    response = _open_report(client, workspace_a, key="confirm-1")
    report_id = response.json()["id"]
    # Secretary attempts to confirm
    response = client.post(
        f"/api/v1/repairs/reports/{report_id}/confirm?org_id={workspace_a.org_id}",
        headers=workspace_a.secretary_headers(),
    )
    assert response.status_code == 403


def test_owner_confirms_report(api):
    client, workspace_a, _workspace_b = api
    response = _open_report(client, workspace_a, key="confirm-ok")
    report_id = response.json()["id"]
    response = client.post(
        f"/api/v1/repairs/reports/{report_id}/confirm?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 200
    assert response.json()["state"] == "CONFIRMED"


def test_assign_technician_external(api):
    client, workspace_a, _workspace_b = api
    response = _open_report(client, workspace_a, key="tech-1")
    report_id = response.json()["id"]
    client.post(
        f"/api/v1/repairs/reports/{report_id}/confirm?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    response = client.post(
        f"/api/v1/repairs/reports/{report_id}/assign-technician"
        f"?org_id={workspace_a.org_id}",
        json={
            "technician_name": "Acme Plumbing",
            "technician_source": "EXTERNAL",
        },
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "AWAITING_TECHNICIAN"
    assert body["technician_source"] == "EXTERNAL"


def test_assign_technician_internal(api):
    client, workspace_a, _workspace_b = api
    response = _open_report(client, workspace_a, key="tech-internal")
    report_id = response.json()["id"]
    client.post(
        f"/api/v1/repairs/reports/{report_id}/confirm?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    response = client.post(
        f"/api/v1/repairs/reports/{report_id}/assign-technician"
        f"?org_id={workspace_a.org_id}",
        json={
            "technician_name": "House Staff",
            "technician_source": "INTERNAL",
        },
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 200
    assert response.json()["technician_source"] == "INTERNAL"


def test_assign_technician_unknown_source_is_400(api):
    client, workspace_a, _workspace_b = api
    response = _open_report(client, workspace_a, key="tech-bad")
    report_id = response.json()["id"]
    client.post(
        f"/api/v1/repairs/reports/{report_id}/confirm?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    response = client.post(
        f"/api/v1/repairs/reports/{report_id}/assign-technician"
        f"?org_id={workspace_a.org_id}",
        json={
            "technician_name": "X",
            "technician_source": "FROM_MARS",
        },
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 400


# ---- quote submit / approve / reject ---------------------------------


def _drive_to_quote_received(client, workspace, *, key_prefix):
    """Helper: open → confirm → assign → request-quote."""
    open_resp = _open_report(
        client, workspace, key=f"{key_prefix}-open",
    )
    report_id = open_resp.json()["id"]
    client.post(
        f"/api/v1/repairs/reports/{report_id}/confirm?org_id={workspace.org_id}",
        headers=workspace.owner_headers(),
    )
    client.post(
        f"/api/v1/repairs/reports/{report_id}/assign-technician"
        f"?org_id={workspace.org_id}",
        json={"technician_name": "X", "technician_source": "EXTERNAL"},
        headers=workspace.owner_headers(),
    )
    client.post(
        f"/api/v1/repairs/reports/{report_id}/request-quote"
        f"?org_id={workspace.org_id}",
        headers=workspace.secretary_headers(),
    )
    return report_id


def test_submit_quote_advances_to_quote_received(api):
    client, workspace_a, _workspace_b = api
    report_id = _drive_to_quote_received(
        client, workspace_a, key_prefix="quote-1",
    )
    response = client.post(
        f"/api/v1/repairs/reports/{report_id}/quotes"
        f"?org_id={workspace_a.org_id}",
        json={
            "amount": QUOTE_AMOUNT,
            "description": "Replace washer + cartridge",
            "technician_name": "Acme Plumbing",
        },
        headers={**workspace_a.secretary_headers(), "Idempotency-Key": "q-1"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["decision"] == "SUBMITTED"
    report = client.get(
        f"/api/v1/repairs/reports/{report_id}?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert report.json()["state"] == "QUOTE_RECEIVED"
    assert report.json()["quoted_amount"] == "3500.00"


def test_submit_quote_oversize_key_is_400(api):
    client, workspace_a, _workspace_b = api
    report_id = _drive_to_quote_received(
        client, workspace_a, key_prefix="quote-over",
    )
    response = client.post(
        f"/api/v1/repairs/reports/{report_id}/quotes"
        f"?org_id={workspace_a.org_id}",
        json={
            "amount": QUOTE_AMOUNT,
            "description": "x",
            "technician_name": "X",
        },
        headers={**workspace_a.secretary_headers(), "Idempotency-Key": "x" * 200},
    )
    assert response.status_code == 400


def test_submit_quote_float_money_is_rejected(api):
    client, workspace_a, _workspace_b = api
    report_id = _drive_to_quote_received(
        client, workspace_a, key_prefix="quote-float",
    )
    # JSON float — rejected at the schema boundary (AGENTS.md §4).
    response = client.post(
        f"/api/v1/repairs/reports/{report_id}/quotes"
        f"?org_id={workspace_a.org_id}",
        json={
            "amount": 3500.5,  # JSON float
            "description": "x",
            "technician_name": "X",
        },
        headers={**workspace_a.secretary_headers(), "Idempotency-Key": "q-float"},
    )
    assert response.status_code == 422


def test_quote_idempotent_replay_returns_same_quote(api):
    client, workspace_a, _workspace_b = api
    report_id = _drive_to_quote_received(
        client, workspace_a, key_prefix="quote-replay",
    )
    payload = {
        "amount": QUOTE_AMOUNT,
        "description": "x",
        "technician_name": "X",
    }
    first = client.post(
        f"/api/v1/repairs/reports/{report_id}/quotes"
        f"?org_id={workspace_a.org_id}",
        json=payload,
        headers={**workspace_a.secretary_headers(), "Idempotency-Key": "q-rep"},
    )
    second = client.post(
        f"/api/v1/repairs/reports/{report_id}/quotes"
        f"?org_id={workspace_a.org_id}",
        json=payload,
        headers={**workspace_a.secretary_headers(), "Idempotency-Key": "q-rep"},
    )
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]


def test_approve_quote_requires_owner(api):
    client, workspace_a, _workspace_b = api
    report_id = _drive_to_quote_received(
        client, workspace_a, key_prefix="approve-secretary",
    )
    quote_resp = client.post(
        f"/api/v1/repairs/reports/{report_id}/quotes"
        f"?org_id={workspace_a.org_id}",
        json={
            "amount": QUOTE_AMOUNT,
            "description": "x",
            "technician_name": "X",
        },
        headers={**workspace_a.secretary_headers(), "Idempotency-Key": "q-sec"},
    )
    quote_id = quote_resp.json()["id"]
    response = client.post(
        f"/api/v1/repairs/reports/{report_id}/quotes/{quote_id}/approve"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.secretary_headers(),
    )
    assert response.status_code == 403


def test_owner_approves_quote(api):
    client, workspace_a, _workspace_b = api
    report_id = _drive_to_quote_received(
        client, workspace_a, key_prefix="approve-1",
    )
    quote_resp = client.post(
        f"/api/v1/repairs/reports/{report_id}/quotes"
        f"?org_id={workspace_a.org_id}",
        json={
            "amount": QUOTE_AMOUNT,
            "description": "x",
            "technician_name": "X",
        },
        headers={**workspace_a.secretary_headers(), "Idempotency-Key": "q-app"},
    )
    quote_id = quote_resp.json()["id"]
    response = client.post(
        f"/api/v1/repairs/reports/{report_id}/quotes/{quote_id}/approve"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 200
    assert response.json()["state"] == "QUOTE_APPROVED"


def test_owner_rejects_quote_returns_to_requested(api):
    client, workspace_a, _workspace_b = api
    report_id = _drive_to_quote_received(
        client, workspace_a, key_prefix="reject-1",
    )
    quote_resp = client.post(
        f"/api/v1/repairs/reports/{report_id}/quotes"
        f"?org_id={workspace_a.org_id}",
        json={
            "amount": QUOTE_AMOUNT,
            "description": "x",
            "technician_name": "X",
        },
        headers={**workspace_a.secretary_headers(), "Idempotency-Key": "q-rej"},
    )
    quote_id = quote_resp.json()["id"]
    response = client.post(
        f"/api/v1/repairs/reports/{report_id}/quotes/{quote_id}/reject"
        f"?org_id={workspace_a.org_id}",
        json={"reason": "too expensive"},
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 200
    assert response.json()["state"] == "QUOTE_REQUESTED"
    # The reject reason must be preserved on the audit row.
    quotes = client.get(
        f"/api/v1/repairs/reports/{report_id}/quotes"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert quotes.json()[0]["decision"] == "REJECTED"
    assert quotes.json()[0]["reason"] == "too expensive"


def test_reject_quote_without_reason_is_400(api):
    client, workspace_a, _workspace_b = api
    report_id = _drive_to_quote_received(
        client, workspace_a, key_prefix="reject-noreason",
    )
    quote_resp = client.post(
        f"/api/v1/repairs/reports/{report_id}/quotes"
        f"?org_id={workspace_a.org_id}",
        json={
            "amount": QUOTE_AMOUNT,
            "description": "x",
            "technician_name": "X",
        },
        headers={**workspace_a.secretary_headers(), "Idempotency-Key": "q-nr"},
    )
    quote_id = quote_resp.json()["id"]
    response = client.post(
        f"/api/v1/repairs/reports/{report_id}/quotes/{quote_id}/reject"
        f"?org_id={workspace_a.org_id}",
        json={"reason": "   "},
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 400


# ---- work progress ---------------------------------------------------


def _drive_to_in_progress(client, workspace, *, key_prefix):
    open_resp = _open_report(
        client, workspace, key=f"{key_prefix}-open",
    )
    report_id = open_resp.json()["id"]
    client.post(
        f"/api/v1/repairs/reports/{report_id}/confirm?org_id={workspace.org_id}",
        headers=workspace.owner_headers(),
    )
    client.post(
        f"/api/v1/repairs/reports/{report_id}/assign-technician"
        f"?org_id={workspace.org_id}",
        json={"technician_name": "X", "technician_source": "EXTERNAL"},
        headers=workspace.owner_headers(),
    )
    client.post(
        f"/api/v1/repairs/reports/{report_id}/request-quote"
        f"?org_id={workspace.org_id}",
        headers=workspace.secretary_headers(),
    )
    quote_resp = client.post(
        f"/api/v1/repairs/reports/{report_id}/quotes"
        f"?org_id={workspace.org_id}",
        json={
            "amount": QUOTE_AMOUNT,
            "description": "x",
            "technician_name": "X",
        },
        headers={**workspace.secretary_headers(), "Idempotency-Key": f"{key_prefix}-q"},
    )
    quote_id = quote_resp.json()["id"]
    client.post(
        f"/api/v1/repairs/reports/{report_id}/quotes/{quote_id}/approve"
        f"?org_id={workspace.org_id}",
        headers=workspace.owner_headers(),
    )
    response = client.post(
        f"/api/v1/repairs/reports/{report_id}/work"
        f"?org_id={workspace.org_id}",
        json={"state": "STARTED", "note": "Arrived on site"},
        headers=workspace.secretary_headers(),
    )
    assert response.status_code == 201, response.text
    return report_id


def test_work_started_advances_to_in_progress(api):
    client, workspace_a, _workspace_b = api
    report_id = _drive_to_in_progress(
        client, workspace_a, key_prefix="work-1",
    )
    response = client.get(
        f"/api/v1/repairs/reports/{report_id}?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert response.json()["state"] == "IN_PROGRESS"


def test_work_progress_log_persists(api):
    client, workspace_a, _workspace_b = api
    report_id = _drive_to_in_progress(
        client, workspace_a, key_prefix="work-log",
    )
    for state, note in (
        ("PROGRESS", "Removed old cartridge"),
        ("BLOCKED", "Waiting for replacement part"),
        ("PROGRESS", "Installed new cartridge"),
    ):
        response = client.post(
            f"/api/v1/repairs/reports/{report_id}/work"
            f"?org_id={workspace_a.org_id}",
            json={"state": state, "note": note},
            headers=workspace_a.secretary_headers(),
        )
        assert response.status_code == 201
    works = client.get(
        f"/api/v1/repairs/reports/{report_id}/work"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert len(works.json()) == 4  # STARTED + 3 progress events


def test_work_before_quote_approved_is_409(api):
    client, workspace_a, _workspace_b = api
    response = _open_report(client, workspace_a, key="work-early")
    report_id = response.json()["id"]
    response = client.post(
        f"/api/v1/repairs/reports/{report_id}/work"
        f"?org_id={workspace_a.org_id}",
        json={"state": "STARTED", "note": "x"},
        headers=workspace_a.secretary_headers(),
    )
    assert response.status_code == 409


# ---- completion claim / verify / counterexample ---------------------


def test_completion_claim_advances_to_completion_claimed(api):
    client, workspace_a, _workspace_b = api
    report_id = _drive_to_in_progress(
        client, workspace_a, key_prefix="complete-1",
    )
    response = client.post(
        f"/api/v1/repairs/reports/{report_id}/completion-claim"
        f"?org_id={workspace_a.org_id}",
        json={"summary": "Replaced washer; tested, no more leak"},
        headers=workspace_a.secretary_headers(),
    )
    assert response.status_code == 201
    report = client.get(
        f"/api/v1/repairs/reports/{report_id}?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert report.json()["state"] == "COMPLETION_CLAIMED"


def test_verify_completion_closes_report_and_operation(api):
    client, workspace_a, _workspace_b = api
    report_id = _drive_to_in_progress(
        client, workspace_a, key_prefix="verify-1",
    )
    client.post(
        f"/api/v1/repairs/reports/{report_id}/completion-claim"
        f"?org_id={workspace_a.org_id}",
        json={"summary": "done"},
        headers=workspace_a.secretary_headers(),
    )
    response = client.post(
        f"/api/v1/repairs/reports/{report_id}/verify-completion"
        f"?org_id={workspace_a.org_id}",
        json={"reason": "owner confirmed on-site"},
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "COMPLETED"
    assert body["completed_at"] is not None
    # Operation must also be resolved.
    op = client.get(
        f"/api/v1/repairs/reports/{report_id}/operation"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert op.json()["state"] == "resolved"
    assert op.json()["resolved_at"] is not None


def test_verify_without_completion_claim_does_not_close(api):
    """Counterexample: verify path is the closure gate, but a claim
    must exist for verification to even apply. Direct verify on a
    non-COMPLETION_CLAIMED report is rejected.
    """
    client, workspace_a, _workspace_b = api
    report_id = _drive_to_in_progress(
        client, workspace_a, key_prefix="verify-no-claim",
    )
    # No completion claim yet.
    response = client.post(
        f"/api/v1/repairs/reports/{report_id}/verify-completion"
        f"?org_id={workspace_a.org_id}",
        json={"reason": "premature"},
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 409
    report = client.get(
        f"/api/v1/repairs/reports/{report_id}?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert report.json()["state"] == "IN_PROGRESS"


def test_reject_completion_keeps_report_open(api):
    client, workspace_a, _workspace_b = api
    report_id = _drive_to_in_progress(
        client, workspace_a, key_prefix="reject-complete",
    )
    client.post(
        f"/api/v1/repairs/reports/{report_id}/completion-claim"
        f"?org_id={workspace_a.org_id}",
        json={"summary": "done"},
        headers=workspace_a.secretary_headers(),
    )
    response = client.post(
        f"/api/v1/repairs/reports/{report_id}/reject-completion"
        f"?org_id={workspace_a.org_id}",
        json={"reason": "leak still present"},
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    # Rejection returns to IN_PROGRESS (not COMPLETED).
    assert body["state"] == "IN_PROGRESS"
    # The report may be claimed again and re-verified.
    client.post(
        f"/api/v1/repairs/reports/{report_id}/completion-claim"
        f"?org_id={workspace_a.org_id}",
        json={"summary": "actually fixed"},
        headers=workspace_a.secretary_headers(),
    )
    response = client.post(
        f"/api/v1/repairs/reports/{report_id}/verify-completion"
        f"?org_id={workspace_a.org_id}",
        json={"reason": "owner confirmed fixed"},
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 200
    assert response.json()["state"] == "COMPLETED"


def test_reverse_verification_reopens_report(api):
    client, workspace_a, _workspace_b = api
    report_id = _drive_to_in_progress(
        client, workspace_a, key_prefix="reverse-1",
    )
    client.post(
        f"/api/v1/repairs/reports/{report_id}/completion-claim"
        f"?org_id={workspace_a.org_id}",
        json={"summary": "done"},
        headers=workspace_a.secretary_headers(),
    )
    client.post(
        f"/api/v1/repairs/reports/{report_id}/verify-completion"
        f"?org_id={workspace_a.org_id}",
        json={"reason": "ok"},
        headers=workspace_a.owner_headers(),
    )
    response = client.post(
        f"/api/v1/repairs/reports/{report_id}/reverse-verification"
        f"?org_id={workspace_a.org_id}",
        json={"reason": "discovered later issue"},
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    # Reversal returns to COMPLETION_CLAIMED (not COMPLETED).
    assert body["state"] == "COMPLETION_CLAIMED"
    assert body["completed_at"] is None
    # The reversal is recorded in the verification audit log.
    verifications = client.get(
        f"/api/v1/repairs/reports/{report_id}/verifications"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    decisions = [v["decision"] for v in verifications.json()]
    assert "VERIFIED" in decisions
    assert "REVERSED" in decisions


def test_reverse_on_unverified_report_is_409(api):
    client, workspace_a, _workspace_b = api
    report_id = _drive_to_in_progress(
        client, workspace_a, key_prefix="reverse-premature",
    )
    response = client.post(
        f"/api/v1/repairs/reports/{report_id}/reverse-verification"
        f"?org_id={workspace_a.org_id}",
        json={"reason": "too early"},
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 409


# ---- COUNTEREXAMPLE: related Expense verification does NOT close repair --


def test_related_expense_verification_does_not_close_repair(api):
    """Business Truth First: Expense/Payment truth != Repair truth.

    A completed expense/payment verification on a different domain
    MUST NOT close a Repair. Only the OWNER's verify_completion on
    the Repair itself can resolve the linked Operation.

    The counterexample: open a repair, drive it to COMPLETION_CLAIMED,
    but DO NOT verify the completion. Independently, open + verify
    an expense claim (separate domain). The repair must still be in
    COMPLETION_CLAIMED, not COMPLETED, and the Operation must still
    be in_progress, not resolved.
    """
    client, workspace_a, _workspace_b = api
    # Drive a repair to COMPLETION_CLAIMED (but do NOT verify).
    report_id = _drive_to_in_progress(
        client, workspace_a, key_prefix="counter-1",
    )
    client.post(
        f"/api/v1/repairs/reports/{report_id}/completion-claim"
        f"?org_id={workspace_a.org_id}",
        json={"summary": "done"},
        headers=workspace_a.secretary_headers(),
    )
    # Independently: a fully-verified expense claim in the same org.
    open_resp = client.post(
        f"/api/v1/expenses/claims?org_id={workspace_a.org_id}",
        json={
            "title": "Plumbing parts",
            "category": "REPAIRS",
            "claimed_amount": "3500.00",
            "receipts": [{"kind": "PHOTO", "reference": "invoice.jpg"}],
        },
        headers={
            **workspace_a.secretary_headers(),
            "Idempotency-Key": "counter-expense-1",
        },
    )
    assert open_resp.status_code == 201
    claim_id = open_resp.json()["id"]
    # OWNER verifies the expense.
    verify_resp = client.post(
        f"/api/v1/expenses/claims/{claim_id}/verify"
        f"?org_id={workspace_a.org_id}",
        json={"verified_amount": "3500.00"},
        headers=workspace_a.owner_headers(),
    )
    assert verify_resp.status_code == 200
    assert verify_resp.json()["status"] == "SETTLED"
    # The repair must still be in COMPLETION_CLAIMED, NOT COMPLETED.
    report = client.get(
        f"/api/v1/repairs/reports/{report_id}?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert report.json()["state"] == "COMPLETION_CLAIMED"
    # The linked Operation must still be in_progress, NOT resolved.
    op = client.get(
        f"/api/v1/repairs/reports/{report_id}/operation"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert op.json()["state"] == "in_progress"


# ---- linked_expense_payment_id is advisory only ---------------------


def test_linked_expense_payment_id_is_accepted_but_does_not_close(api):
    client, workspace_a, _workspace_b = api
    response = _open_report(
        client, workspace_a, key="linked-1", linked_expense_payment_id=999,
    )
    assert response.status_code == 201
    body = response.json()
    assert body["linked_expense_payment_id"] == 999
    # Even with the advisory linked_expense_payment_id set, the report
    # is just REPORTED — no closure.
    assert body["state"] == "REPORTED"


def test_linked_expense_payment_id_zero_is_422(api):
    client, workspace_a, _workspace_b = api
    request_headers = dict(workspace_a.secretary_headers())
    request_headers["Idempotency-Key"] = "linked-zero"
    response = client.post(
        f"/api/v1/repairs/reports?org_id={workspace_a.org_id}",
        json={
            "unit_id": workspace_a.unit_id,
            "title": "x",
            "description": "y",
            "category": "PLUMBING",
            "severity": "LOW",
            "linked_expense_payment_id": 0,  # gt=0
        },
        headers=request_headers,
    )
    assert response.status_code == 422


# ---- follow-up (Task projection) ------------------------------------


def test_follow_up_does_not_close_repair(api):
    client, workspace_a, _workspace_b = api
    report_id = _drive_to_in_progress(
        client, workspace_a, key_prefix="follow-1",
    )
    response = client.post(
        f"/api/v1/repairs/reports/{report_id}/follow-ups"
        f"?org_id={workspace_a.org_id}",
        json={"title": "Call vendor tomorrow"},
        headers=workspace_a.secretary_headers(),
    )
    assert response.status_code == 201
    task_id = response.json()["id"]
    # Complete the follow-up.
    response = client.post(
        f"/api/v1/repairs/follow-ups/{task_id}/complete"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.secretary_headers(),
    )
    assert response.status_code == 200
    # The repair must still be in IN_PROGRESS, NOT COMPLETED.
    report = client.get(
        f"/api/v1/repairs/reports/{report_id}?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert report.json()["state"] == "IN_PROGRESS"


def test_second_open_follow_up_is_409(api):
    client, workspace_a, _workspace_b = api
    report_id = _drive_to_in_progress(
        client, workspace_a, key_prefix="follow-2",
    )
    response = client.post(
        f"/api/v1/repairs/reports/{report_id}/follow-ups"
        f"?org_id={workspace_a.org_id}",
        json={"title": "First"},
        headers=workspace_a.secretary_headers(),
    )
    assert response.status_code == 201
    response = client.post(
        f"/api/v1/repairs/reports/{report_id}/follow-ups"
        f"?org_id={workspace_a.org_id}",
        json={"title": "Second"},
        headers=workspace_a.secretary_headers(),
    )
    assert response.status_code == 409


# ---- cancel ----------------------------------------------------------


def test_cancel_terminal_is_409(api):
    client, workspace_a, _workspace_b = api
    report_id = _drive_to_in_progress(
        client, workspace_a, key_prefix="cancel-terminal",
    )
    client.post(
        f"/api/v1/repairs/reports/{report_id}/completion-claim"
        f"?org_id={workspace_a.org_id}",
        json={"summary": "done"},
        headers=workspace_a.secretary_headers(),
    )
    client.post(
        f"/api/v1/repairs/reports/{report_id}/verify-completion"
        f"?org_id={workspace_a.org_id}",
        json={"reason": "ok"},
        headers=workspace_a.owner_headers(),
    )
    # Already COMPLETED — cannot cancel.
    response = client.post(
        f"/api/v1/repairs/reports/{report_id}/cancel"
        f"?org_id={workspace_a.org_id}",
        json={"reason": "too late"},
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 409


def test_cancel_open_report_is_allowed(api):
    client, workspace_a, _workspace_b = api
    response = _open_report(client, workspace_a, key="cancel-open")
    report_id = response.json()["id"]
    response = client.post(
        f"/api/v1/repairs/reports/{report_id}/cancel"
        f"?org_id={workspace_a.org_id}",
        json={"reason": "duplicate"},
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 200
    assert response.json()["state"] == "CANCELLED"


# ---- activity feed ---------------------------------------------------


def test_activity_feed_records_all_transitions(api):
    client, workspace_a, _workspace_b = api
    report_id = _drive_to_in_progress(
        client, workspace_a, key_prefix="activity-1",
    )
    client.post(
        f"/api/v1/repairs/reports/{report_id}/completion-claim"
        f"?org_id={workspace_a.org_id}",
        json={"summary": "done"},
        headers=workspace_a.secretary_headers(),
    )
    client.post(
        f"/api/v1/repairs/reports/{report_id}/verify-completion"
        f"?org_id={workspace_a.org_id}",
        json={"reason": "ok"},
        headers=workspace_a.owner_headers(),
    )
    response = client.get(
        f"/api/v1/repairs/reports/{report_id}/activity"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    kinds = [a["kind"] for a in response.json()]
    # Full audit trail
    assert "REPORTED" in kinds
    assert "CONFIRMED" in kinds
    assert "TECHNICIAN_ASSIGNED" in kinds
    assert "QUOTE_REQUESTED" in kinds
    assert "QUOTE_SUBMITTED" in kinds
    assert "QUOTE_APPROVED" in kinds
    assert "WORK_STARTED" in kinds
    assert "COMPLETION_CLAIMED" in kinds
    assert "VERIFIED" in kinds
    assert "COMPLETED" in kinds


# ---- list / get projections ----------------------------------------


def test_list_reports_filters_by_state(api):
    client, workspace_a, _workspace_b = api
    _open_report(client, workspace_a, key="list-1")
    _open_report(client, workspace_a, key="list-2")
    response = client.get(
        f"/api/v1/repairs/reports?org_id={workspace_a.org_id}"
        f"&state=REPORTED",
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 200
    assert len(response.json()) == 2
    response = client.get(
        f"/api/v1/repairs/reports?org_id={workspace_a.org_id}"
        f"&state=COMPLETED",
        headers=workspace_a.owner_headers(),
    )
    assert response.json() == []
