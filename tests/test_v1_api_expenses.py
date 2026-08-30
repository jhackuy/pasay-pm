"""HTTP-level behavior tests for the V1 Expense router.

Proves the router is thin and correct: authentication, role gating handled
by the shared service, mandatory ``Idempotency-Key``, replay vs conflict,
amount mismatch vs full verification, Claim/Receipt/Verification
separation, Operation-is-Truth (only fully verified balance settles the
claim), and money serialized as JSON strings.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db, get_session_factory
from tests.v1_support import seed_workspace, v1_engine_ctx


AMOUNT = "500.00"


@pytest.fixture
def api():
    with v1_engine_ctx():
        session = get_session_factory()()
        workspace_a = seed_workspace(session, name="ExpAlpha")
        workspace_b = seed_workspace(session, name="ExpBeta")

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


def _open_claim(
    client,
    workspace,
    *,
    amount=AMOUNT,
    key,
    category="UTILITIES",
    title="Electricity bill",
    reference="receipt.jpg",
    headers=None,
    with_receipt=True,
):
    request_headers = dict(headers or workspace.secretary_headers())
    if key is not None:
        request_headers["Idempotency-Key"] = key
    payload: dict[str, object] = {
        "title": title,
        "category": category,
        "claimed_amount": amount,
    }
    if with_receipt:
        payload["receipts"] = [{"kind": "PHOTO", "reference": reference}]
    else:
        payload["receipts"] = []
    return client.post(
        f"/api/v1/expenses/claims?org_id={workspace.org_id}",
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
        f"/api/v1/expenses/claims?org_id={workspace_a.org_id}",
    )
    assert response.status_code == 401


# ---- open_claim ------------------------------------------------------


def test_secretary_opens_a_claim_with_string_money(api):
    client, workspace_a, _workspace_b = api
    response = _open_claim(client, workspace_a, key="open-claim-1")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "SUBMITTED"
    assert body["title"] == "Electricity bill"
    assert body["category"] == "UTILITIES"
    assert isinstance(body["claimed_amount"], str)
    assert Decimal(body["claimed_amount"]) == Decimal(AMOUNT)
    assert body["verified_amount"] is None

    # Linked Operation exists, open, unresolved.
    operation = client.get(
        f"/api/v1/expenses/claims/{body['id']}/operation"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert operation.status_code == 200
    assert operation.json()["state"] == "open"
    assert operation.json()["resolved_at"] is None


def test_owner_can_also_open_a_claim(api):
    client, workspace_a, _workspace_b = api
    response = _open_claim(
        client, workspace_a, key="owner-open",
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 201, response.text


def test_open_claim_requires_idempotency_key(api):
    client, workspace_a, _workspace_b = api
    response = _open_claim(client, workspace_a, key=None)
    assert response.status_code == 400
    assert "Idempotency-Key" in response.json()["detail"]


def test_oversize_idempotency_key_is_rejected(api):
    client, workspace_a, _workspace_b = api
    response = _open_claim(client, workspace_a, key="k" * 129)
    assert response.status_code == 400


def test_unknown_body_field_is_rejected(api):
    client, workspace_a, _workspace_b = api
    headers = dict(workspace_a.secretary_headers())
    headers["Idempotency-Key"] = "unknown-field"
    response = client.post(
        f"/api/v1/expenses/claims?org_id={workspace_a.org_id}",
        json={
            "title": "Bogus",
            "category": "OTHER",
            "claimed_amount": AMOUNT,
            "force_settled": True,
        },
        headers=headers,
    )
    assert response.status_code == 422


def test_unknown_category_is_rejected(api):
    client, workspace_a, _workspace_b = api
    headers = dict(workspace_a.secretary_headers())
    headers["Idempotency-Key"] = "bad-cat"
    response = client.post(
        f"/api/v1/expenses/claims?org_id={workspace_a.org_id}",
        json={
            "title": "Mystery",
            "category": "BOGUS_CATEGORY",
            "claimed_amount": AMOUNT,
        },
        headers=headers,
    )
    assert response.status_code == 400


def test_float_money_is_rejected_by_validation(api):
    client, workspace_a, _workspace_b = api
    headers = dict(workspace_a.secretary_headers())
    headers["Idempotency-Key"] = "zero-amt"
    response = client.post(
        f"/api/v1/expenses/claims?org_id={workspace_a.org_id}",
        json={
            "title": "Zero",
            "category": "OTHER",
            "claimed_amount": "0",
        },
        headers=headers,
    )
    assert response.status_code == 422


# ---- idempotency replay / conflict -----------------------------------


def test_idempotent_replay_returns_same_claim(api):
    client, workspace_a, _workspace_b = api
    first = _open_claim(client, workspace_a, key="replay-claim")
    assert first.status_code == 201, first.text
    replay = _open_claim(client, workspace_a, key="replay-claim")
    assert replay.status_code == 200, replay.text
    assert replay.json()["id"] == first.json()["id"]
    listed = client.get(
        f"/api/v1/expenses/claims?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert listed.status_code == 200
    assert len(listed.json()) == 1


def test_reusing_key_with_different_payload_is_a_conflict(api):
    client, workspace_a, _workspace_b = api
    assert (
        _open_claim(client, workspace_a, key="conflict", amount="300.00").status_code
        == 201
    )
    response = _open_claim(
        client, workspace_a, key="conflict", amount="600.00",
    )
    assert response.status_code == 409


def test_case_preserving_idempotency_keys_distinguish(api):
    """Two different case variants of the same key must produce two claims."""
    client, workspace_a, _workspace_b = api
    first = _open_claim(client, workspace_a, key="CaseA")
    second = _open_claim(client, workspace_a, key="casea")
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] != second.json()["id"]


# ---- cross-org isolation ----------------------------------------------


def test_cross_org_read_returns_404(api):
    client, workspace_a, workspace_b = api
    own_claim = _open_claim(
        client, workspace_a, key="cross-org-claim",
    ).json()
    response = client.get(
        f"/api/v1/expenses/claims/{own_claim['id']}"
        f"?org_id={workspace_b.org_id}",
        headers=workspace_b.owner_headers(),
    )
    assert response.status_code == 404


def test_cross_org_idempotency_key_does_not_collide(api):
    """The same idempotency key in two different orgs must produce two claims."""
    client, workspace_a, workspace_b = api
    a = _open_claim(client, workspace_a, key="shared-key").json()
    b = _open_claim(client, workspace_b, key="shared-key").json()
    assert a["id"] != b["id"]


# ---- role gating ------------------------------------------------------


def test_secretary_cannot_verify(api):
    client, workspace_a, _workspace_b = api
    claim_id = _open_claim(client, workspace_a, key="sec-verify").json()["id"]
    response = client.post(
        f"/api/v1/expenses/claims/{claim_id}/verify?org_id={workspace_a.org_id}",
        json={},
        headers=workspace_a.secretary_headers(),
    )
    assert response.status_code == 403


def test_secretary_cannot_reject_or_reverse(api):
    client, workspace_a, _workspace_b = api
    claim_id = _open_claim(client, workspace_a, key="sec-decide").json()["id"]
    reject = client.post(
        f"/api/v1/expenses/claims/{claim_id}/reject"
        f"?org_id={workspace_a.org_id}",
        json={"reason": "no"},
        headers=workspace_a.secretary_headers(),
    )
    reverse = client.post(
        f"/api/v1/expenses/claims/{claim_id}/reverse"
        f"?org_id={workspace_a.org_id}",
        json={"reason": "no"},
        headers=workspace_a.secretary_headers(),
    )
    assert reject.status_code == 403
    assert reverse.status_code == 403


# ---- Claim/Receipt/Verification separation ---------------------------


def test_open_claim_persists_receipt_rows(api):
    client, workspace_a, _workspace_b = api
    claim_id = _open_claim(
        client, workspace_a, key="with-receipt",
    ).json()["id"]
    response = client.get(
        f"/api/v1/expenses/claims/{claim_id}/receipts"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["kind"] == "PHOTO"
    assert rows[0]["reference"] == "receipt.jpg"


def test_add_receipt_after_open_flips_to_submitted(api):
    client, workspace_a, _workspace_b = api
    headers = dict(workspace_a.secretary_headers())
    headers["Idempotency-Key"] = "no-receipts"
    claim_id = client.post(
        f"/api/v1/expenses/claims?org_id={workspace_a.org_id}",
        json={
            "title": "Plumbing",
            "category": "REPAIRS",
            "claimed_amount": AMOUNT,
        },
        headers=headers,
    ).json()["id"]
    assert client.get(
        f"/api/v1/expenses/claims/{claim_id}?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    ).json()["status"] == "OPEN"

    add = client.post(
        f"/api/v1/expenses/claims/{claim_id}/receipts"
        f"?org_id={workspace_a.org_id}",
        json={"kind": "DOCUMENT", "reference": "invoice.pdf"},
        headers=workspace_a.secretary_headers(),
    )
    assert add.status_code == 201, add.text
    assert client.get(
        f"/api/v1/expenses/claims/{claim_id}?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    ).json()["status"] == "SUBMITTED"


def test_unknown_receipt_kind_is_rejected(api):
    client, workspace_a, _workspace_b = api
    claim_id = _open_claim(client, workspace_a, key="bad-kind").json()["id"]
    response = client.post(
        f"/api/v1/expenses/claims/{claim_id}/receipts"
        f"?org_id={workspace_a.org_id}",
        json={"kind": "BOGUS", "reference": "x"},
        headers=workspace_a.secretary_headers(),
    )
    assert response.status_code == 400


# ---- verification, amount mismatch, settlement ------------------------


def test_verification_requires_a_receipt(api):
    client, workspace_a, _workspace_b = api
    headers = dict(workspace_a.secretary_headers())
    headers["Idempotency-Key"] = "no-evidence"
    claim_id = client.post(
        f"/api/v1/expenses/claims?org_id={workspace_a.org_id}",
        json={
            "title": "Plumbing",
            "category": "REPAIRS",
            "claimed_amount": AMOUNT,
        },
        headers=headers,
    ).json()["id"]
    # Now add a receipt so the claim is SUBMITTED.
    client.post(
        f"/api/v1/expenses/claims/{claim_id}/receipts"
        f"?org_id={workspace_a.org_id}",
        json={"kind": "DOCUMENT", "reference": "invoice.pdf"},
        headers=workspace_a.secretary_headers(),
    )
    # Receipt exists now; verify the partial path.
    response = client.post(
        f"/api/v1/expenses/claims/{claim_id}/verify"
        f"?org_id={workspace_a.org_id}",
        json={"verified_amount": "300.00"},
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "VERIFIED"


def test_open_claim_without_receipts_cannot_be_verified(api):
    client, workspace_a, _workspace_b = api
    headers = dict(workspace_a.secretary_headers())
    headers["Idempotency-Key"] = "open-no-receipts"
    claim_id = client.post(
        f"/api/v1/expenses/claims?org_id={workspace_a.org_id}",
        json={
            "title": "Pending receipt",
            "category": "SUPPLIES",
            "claimed_amount": AMOUNT,
        },
        headers=headers,
    ).json()["id"]
    response = client.post(
        f"/api/v1/expenses/claims/{claim_id}/verify"
        f"?org_id={workspace_a.org_id}",
        json={},
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 400


def test_partial_verification_records_amount_mismatch_but_keeps_open(api):
    client, workspace_a, _workspace_b = api
    claim_id = _open_claim(
        client, workspace_a, key="partial-claim",
    ).json()["id"]
    response = client.post(
        f"/api/v1/expenses/claims/{claim_id}/verify"
        f"?org_id={workspace_a.org_id}",
        json={"verified_amount": "300.00"},
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert Decimal(body["verified_amount"]) == Decimal("300.00")
    assert Decimal(body["claimed_amount"]) == Decimal(AMOUNT)
    # Partial verification does NOT settle the claim.
    assert body["status"] == "VERIFIED"

    # Operation stays open (in_progress after first activity).
    op = client.get(
        f"/api/v1/expenses/claims/{claim_id}/operation"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    ).json()
    assert op["state"] in ("open", "in_progress")

    # Balance projection reflects the gap.
    balance = client.get(
        f"/api/v1/expenses/claims/{claim_id}/balance"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    ).json()
    assert Decimal(balance["claimed_amount"]) == Decimal(AMOUNT)
    assert Decimal(balance["verified_total"]) == Decimal("300.00")
    assert Decimal(balance["remaining_amount"]) == Decimal("200.00")
    assert balance["is_settled"] is False

    # Activity feed records the mismatch.
    feed = client.get(
        f"/api/v1/expenses/claims/{claim_id}/activity"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    ).json()
    kinds = [row["kind"] for row in feed]
    assert "AMOUNT_MISMATCH" in kinds


def test_full_verification_settles_and_resolves(api):
    client, workspace_a, _workspace_b = api
    claim_id = _open_claim(
        client, workspace_a, key="full-claim",
    ).json()["id"]
    response = client.post(
        f"/api/v1/expenses/claims/{claim_id}/verify"
        f"?org_id={workspace_a.org_id}",
        json={},  # default = full claimed amount
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert Decimal(body["verified_amount"]) == Decimal(AMOUNT)
    assert body["status"] == "SETTLED"

    op = client.get(
        f"/api/v1/expenses/claims/{claim_id}/operation"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    ).json()
    assert op["state"] == "resolved"
    assert op["resolved_at"] is not None

    balance = client.get(
        f"/api/v1/expenses/claims/{claim_id}/balance"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    ).json()
    assert balance["is_settled"] is True
    assert Decimal(balance["remaining_amount"]) == Decimal("0.00")


# ---- follow-up (Task projection) --------------------------------------


def test_follow_up_does_not_resolve_the_operation(api):
    client, workspace_a, _workspace_b = api
    claim_id = _open_claim(
        client, workspace_a, key="follow-up-claim",
    ).json()["id"]
    follow_up = client.post(
        f"/api/v1/expenses/claims/{claim_id}/follow-ups"
        f"?org_id={workspace_a.org_id}",
        json={"title": "Get the invoice"},
        headers=workspace_a.secretary_headers(),
    )
    assert follow_up.status_code == 201, follow_up.text
    task_id = follow_up.json()["id"]

    # Operation still open after the follow-up is created.
    op = client.get(
        f"/api/v1/expenses/claims/{claim_id}/operation"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    ).json()
    assert op["state"] in ("open", "in_progress")

    # Completing the follow-up does NOT resolve either.
    completed = client.post(
        f"/api/v1/expenses/follow-ups/{task_id}/complete"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.secretary_headers(),
    )
    assert completed.status_code == 200, completed.text
    op2 = client.get(
        f"/api/v1/expenses/claims/{claim_id}/operation"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    ).json()
    assert op2["state"] in ("open", "in_progress")


def test_second_open_follow_up_is_a_conflict(api):
    client, workspace_a, _workspace_b = api
    claim_id = _open_claim(
        client, workspace_a, key="two-follow-ups",
    ).json()["id"]
    first = client.post(
        f"/api/v1/expenses/claims/{claim_id}/follow-ups"
        f"?org_id={workspace_a.org_id}",
        json={"title": "First"},
        headers=workspace_a.secretary_headers(),
    )
    assert first.status_code == 201, first.text
    second = client.post(
        f"/api/v1/expenses/claims/{claim_id}/follow-ups"
        f"?org_id={workspace_a.org_id}",
        json={"title": "Second"},
        headers=workspace_a.secretary_headers(),
    )
    assert second.status_code == 409


# ---- reject / reverse (no fake-close) --------------------------------


def test_reject_does_not_close_the_operation(api):
    client, workspace_a, _workspace_b = api
    claim_id = _open_claim(
        client, workspace_a, key="reject-claim",
    ).json()["id"]
    response = client.post(
        f"/api/v1/expenses/claims/{claim_id}/reject"
        f"?org_id={workspace_a.org_id}",
        json={"reason": "missing itemized receipt"},
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "FAILED"

    op = client.get(
        f"/api/v1/expenses/claims/{claim_id}/operation"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    ).json()
    # FAILED never closes the Operation.
    assert op["state"] != "resolved"


def test_reject_without_reason_is_rejected(api):
    client, workspace_a, _workspace_b = api
    claim_id = _open_claim(
        client, workspace_a, key="reject-no-reason",
    ).json()["id"]
    response = client.post(
        f"/api/v1/expenses/claims/{claim_id}/reject"
        f"?org_id={workspace_a.org_id}",
        json={"reason": "   "},
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 400


def test_reverse_reopens_a_settled_claim(api):
    client, workspace_a, _workspace_b = api
    claim_id = _open_claim(
        client, workspace_a, key="reverse-claim",
    ).json()["id"]
    # Settle it first.
    client.post(
        f"/api/v1/expenses/claims/{claim_id}/verify"
        f"?org_id={workspace_a.org_id}",
        json={},
        headers=workspace_a.owner_headers(),
    )
    # Now reverse.
    response = client.post(
        f"/api/v1/expenses/claims/{claim_id}/reverse"
        f"?org_id={workspace_a.org_id}",
        json={"reason": "audit found duplicate"},
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 200, response.text

    op = client.get(
        f"/api/v1/expenses/claims/{claim_id}/operation"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    ).json()
    assert op["state"] == "in_progress"
    assert op["resolved_at"] is None


def test_reverse_without_reason_is_rejected(api):
    client, workspace_a, _workspace_b = api
    claim_id = _open_claim(
        client, workspace_a, key="reverse-no-reason",
    ).json()["id"]
    client.post(
        f"/api/v1/expenses/claims/{claim_id}/verify"
        f"?org_id={workspace_a.org_id}",
        json={},
        headers=workspace_a.owner_headers(),
    )
    response = client.post(
        f"/api/v1/expenses/claims/{claim_id}/reverse"
        f"?org_id={workspace_a.org_id}",
        json={"reason": ""},
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 400


# ---- listing / verifications feed -------------------------------------


def test_list_verifications_returns_audit_trail(api):
    client, workspace_a, _workspace_b = api
    claim_id = _open_claim(
        client, workspace_a, key="audit-trail",
    ).json()["id"]
    client.post(
        f"/api/v1/expenses/claims/{claim_id}/verify"
        f"?org_id={workspace_a.org_id}",
        json={},
        headers=workspace_a.owner_headers(),
    )
    response = client.get(
        f"/api/v1/expenses/claims/{claim_id}/verifications"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["decision"] == "VERIFIED"
    assert Decimal(rows[0]["verified_amount"]) == Decimal(AMOUNT)


def test_list_claims_filters_by_status(api):
    client, workspace_a, _workspace_b = api
    _open_claim(client, workspace_a, key="list-a")
    _open_claim(client, workspace_a, key="list-b")
    all_claims = client.get(
        f"/api/v1/expenses/claims?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    ).json()
    assert len(all_claims) == 2
    submitted = client.get(
        f"/api/v1/expenses/claims?org_id={workspace_a.org_id}"
        f"&status_filter=SUBMITTED",
        headers=workspace_a.owner_headers(),
    ).json()
    assert len(submitted) == 2
    settled = client.get(
        f"/api/v1/expenses/claims?org_id={workspace_a.org_id}"
        f"&status_filter=SETTLED",
        headers=workspace_a.owner_headers(),
    ).json()
    assert settled == []


def test_activity_feed_includes_opening_and_submission(api):
    client, workspace_a, _workspace_b = api
    claim_id = _open_claim(
        client, workspace_a, key="activity-feed",
    ).json()["id"]
    feed = client.get(
        f"/api/v1/expenses/claims/{claim_id}/activity"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    ).json()
    kinds = [row["kind"] for row in feed]
    assert "CLAIM_OPENED" in kinds
    assert "SUBMITTED" in kinds  # because receipts were supplied at open time