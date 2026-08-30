"""HTTP-level behavior tests for the V1 Rent/Payment router.

Proves the router is thin and correct: authentication, role gating handled
by the shared service, mandatory ``Idempotency-Key``, replay vs conflict,
partial vs full verification, and money serialized as JSON strings.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db, get_session_factory
from tests.v1_support import seed_workspace, v1_engine_ctx


PERIOD_START = "2026-03-01"
DUE_DATE = "2026-03-05"
AMOUNT = "12000.00"


@pytest.fixture
def api():
    with v1_engine_ctx():
        session = get_session_factory()()
        workspace_a = seed_workspace(session, name="ApiAlpha")
        workspace_b = seed_workspace(session, name="ApiBeta")

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


def _create_schedule(client, workspace, *, amount=AMOUNT):
    return client.post(
        f"/api/v1/rent/due-schedules?org_id={workspace.org_id}",
        json={
            "lease_id": workspace.lease_id,
            "period_start": PERIOD_START,
            "due_date": DUE_DATE,
            "amount_due": amount,
        },
        headers=workspace.owner_headers(),
    )


def _claim(
    client,
    workspace,
    schedule_id,
    *,
    amount,
    key,
    headers=None,
    reference="receipt.jpg",
):
    request_headers = dict(headers or workspace.secretary_headers())
    if key is not None:
        request_headers["Idempotency-Key"] = key
    return client.post(
        f"/api/v1/rent/due-schedules/{schedule_id}/claims"
        f"?org_id={workspace.org_id}",
        json={
            "claimed_amount": amount,
            "evidence": [{"kind": "PHOTO", "reference": reference}],
        },
        headers=request_headers,
    )


def test_health_is_available(api):
    client, _workspace_a, _workspace_b = api
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_unauthenticated_request_is_rejected(api):
    client, workspace_a, _workspace_b = api
    response = client.post(
        f"/api/v1/rent/due-schedules?org_id={workspace_a.org_id}",
        json={
            "lease_id": workspace_a.lease_id,
            "period_start": PERIOD_START,
            "due_date": DUE_DATE,
            "amount_due": AMOUNT,
        },
    )
    assert response.status_code == 401


def test_owner_creates_a_due_schedule_with_string_money(api):
    client, workspace_a, _workspace_b = api
    response = _create_schedule(client, workspace_a)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["state"] == "DUE"
    assert body["lease_id"] == workspace_a.lease_id
    # Money crosses the wire as a string, never as a float.
    assert isinstance(body["amount_due"], str)
    assert Decimal(body["amount_due"]) == Decimal(AMOUNT)

    operation = client.get(
        f"/api/v1/rent/due-schedules/{body['id']}/operation"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert operation.status_code == 200
    assert operation.json()["state"] == "open"
    assert operation.json()["resolved_at"] is None


def test_float_money_is_rejected_by_validation(api):
    client, workspace_a, _workspace_b = api
    response = client.post(
        f"/api/v1/rent/due-schedules?org_id={workspace_a.org_id}",
        json={
            "lease_id": workspace_a.lease_id,
            "period_start": PERIOD_START,
            "due_date": DUE_DATE,
            "amount_due": "0",
        },
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 422


def test_unknown_body_field_is_rejected(api):
    client, workspace_a, _workspace_b = api
    response = client.post(
        f"/api/v1/rent/due-schedules?org_id={workspace_a.org_id}",
        json={
            "lease_id": workspace_a.lease_id,
            "period_start": PERIOD_START,
            "due_date": DUE_DATE,
            "amount_due": AMOUNT,
            "mark_as_paid": True,
        },
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 422


def test_claim_requires_an_idempotency_key(api):
    client, workspace_a, _workspace_b = api
    schedule_id = _create_schedule(client, workspace_a).json()["id"]
    response = _claim(
        client, workspace_a, schedule_id, amount="5000.00", key=None,
    )
    assert response.status_code == 400
    assert "Idempotency-Key" in response.json()["detail"]


def test_oversize_idempotency_key_is_rejected(api):
    client, workspace_a, _workspace_b = api
    schedule_id = _create_schedule(client, workspace_a).json()["id"]
    response = _claim(
        client, workspace_a, schedule_id, amount="5000.00", key="k" * 129,
    )
    assert response.status_code == 400


def test_claim_then_identical_replay_returns_the_same_claim(api):
    client, workspace_a, _workspace_b = api
    schedule_id = _create_schedule(client, workspace_a).json()["id"]
    first = _claim(
        client, workspace_a, schedule_id, amount="5000.00", key="api-claim-1",
    )
    assert first.status_code == 201, first.text
    assert first.json()["status"] == "PENDING"
    assert first.json()["verified_amount"] is None

    replay = _claim(
        client, workspace_a, schedule_id, amount="5000.00", key="api-claim-1",
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["id"] == first.json()["id"]

    listed = client.get(
        f"/api/v1/rent/due-schedules/{schedule_id}/claims"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert listed.status_code == 200
    assert len(listed.json()) == 1


def test_reusing_a_key_with_a_different_payload_is_a_conflict(api):
    client, workspace_a, _workspace_b = api
    schedule_id = _create_schedule(client, workspace_a).json()["id"]
    assert (
        _claim(
            client,
            workspace_a,
            schedule_id,
            amount="5000.00",
            key="api-claim-1",
        ).status_code
        == 201
    )
    conflict = _claim(
        client, workspace_a, schedule_id, amount="6000.00", key="api-claim-1",
    )
    assert conflict.status_code == 409


def test_a_claim_alone_does_not_move_the_balance(api):
    client, workspace_a, _workspace_b = api
    schedule_id = _create_schedule(client, workspace_a).json()["id"]
    _claim(
        client, workspace_a, schedule_id, amount="5000.00", key="claim-only",
    )
    balance = client.get(
        f"/api/v1/rent/due-schedules/{schedule_id}/balance"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    ).json()
    assert Decimal(balance["verified_total"]) == Decimal("0.00")
    assert Decimal(balance["remaining_balance"]) == Decimal(AMOUNT)
    assert balance["is_paid"] is False


def test_secretary_cannot_verify(api):
    client, workspace_a, _workspace_b = api
    schedule_id = _create_schedule(client, workspace_a).json()["id"]
    claim_id = _claim(
        client, workspace_a, schedule_id, amount=AMOUNT, key="sec-verify",
    ).json()["id"]
    response = client.post(
        f"/api/v1/rent/claims/{claim_id}/verify?org_id={workspace_a.org_id}",
        json={},
        headers=workspace_a.secretary_headers(),
    )
    assert response.status_code == 403


def test_verification_requires_evidence(api):
    client, workspace_a, _workspace_b = api
    schedule_id = _create_schedule(client, workspace_a).json()["id"]
    created = client.post(
        f"/api/v1/rent/due-schedules/{schedule_id}/claims"
        f"?org_id={workspace_a.org_id}",
        json={"claimed_amount": AMOUNT, "evidence": []},
        headers={
            **workspace_a.secretary_headers(),
            "Idempotency-Key": "no-evidence",
        },
    )
    assert created.status_code == 201, created.text
    response = client.post(
        f"/api/v1/rent/claims/{created.json()['id']}/verify"
        f"?org_id={workspace_a.org_id}",
        json={},
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 400


def test_partial_then_full_verification_pays_and_resolves(api):
    client, workspace_a, _workspace_b = api
    schedule_id = _create_schedule(client, workspace_a).json()["id"]

    follow_up = client.post(
        f"/api/v1/rent/due-schedules/{schedule_id}/follow-ups"
        f"?org_id={workspace_a.org_id}",
        json={"title": "Collect the rent"},
        headers=workspace_a.secretary_headers(),
    )
    assert follow_up.status_code == 201, follow_up.text

    first_claim = _claim(
        client,
        workspace_a,
        schedule_id,
        amount="5000.00",
        key="api-split-1",
        reference="first.jpg",
    ).json()
    verified = client.post(
        f"/api/v1/rent/claims/{first_claim['id']}/verify"
        f"?org_id={workspace_a.org_id}",
        json={},
        headers=workspace_a.owner_headers(),
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["status"] == "VERIFIED"

    balance = client.get(
        f"/api/v1/rent/due-schedules/{schedule_id}/balance"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    ).json()
    assert Decimal(balance["remaining_balance"]) == Decimal("7000.00")
    assert balance["is_paid"] is False
    operation = client.get(
        f"/api/v1/rent/due-schedules/{schedule_id}/operation"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    ).json()
    assert operation["state"] == "in_progress"
    assert operation["resolved_at"] is None

    second_claim = _claim(
        client,
        workspace_a,
        schedule_id,
        amount="7000.00",
        key="api-split-2",
        reference="second.jpg",
    ).json()
    assert (
        client.post(
            f"/api/v1/rent/claims/{second_claim['id']}/verify"
            f"?org_id={workspace_a.org_id}",
            json={},
            headers=workspace_a.owner_headers(),
        ).status_code
        == 200
    )

    balance = client.get(
        f"/api/v1/rent/due-schedules/{schedule_id}/balance"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    ).json()
    assert Decimal(balance["remaining_balance"]) == Decimal("0.00")
    assert balance["is_paid"] is True

    schedule = client.get(
        f"/api/v1/rent/due-schedules/{schedule_id}"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    ).json()
    assert schedule["state"] == "PAID"

    operation = client.get(
        f"/api/v1/rent/due-schedules/{schedule_id}/operation"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    ).json()
    assert operation["state"] == "resolved"
    assert operation["resolved_at"] is not None

    tasks = client.get(
        f"/api/v1/rent/due-schedules/{schedule_id}/follow-ups"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    ).json()
    assert [task["state"] for task in tasks] == ["cancelled"]

    activity = client.get(
        f"/api/v1/rent/due-schedules/{schedule_id}/activity"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    ).json()
    kinds = [entry["kind"] for entry in activity]
    assert "PARTIAL_VERIFIED" in kinds
    assert "PAID" in kinds


def test_completing_a_follow_up_does_not_resolve_the_operation(api):
    client, workspace_a, _workspace_b = api
    schedule_id = _create_schedule(client, workspace_a).json()["id"]
    task = client.post(
        f"/api/v1/rent/due-schedules/{schedule_id}/follow-ups"
        f"?org_id={workspace_a.org_id}",
        json={"title": "Call the tenant"},
        headers=workspace_a.secretary_headers(),
    ).json()
    done = client.post(
        f"/api/v1/rent/follow-ups/{task['id']}/complete"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.secretary_headers(),
    )
    assert done.status_code == 200, done.text
    assert done.json()["state"] == "done"
    operation = client.get(
        f"/api/v1/rent/due-schedules/{schedule_id}/operation"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    ).json()
    assert operation["state"] == "in_progress"
    assert operation["resolved_at"] is None


def test_a_second_open_follow_up_is_a_conflict(api):
    client, workspace_a, _workspace_b = api
    schedule_id = _create_schedule(client, workspace_a).json()["id"]
    url = (
        f"/api/v1/rent/due-schedules/{schedule_id}/follow-ups"
        f"?org_id={workspace_a.org_id}"
    )
    assert (
        client.post(
            url,
            json={"title": "First"},
            headers=workspace_a.secretary_headers(),
        ).status_code
        == 201
    )
    second = client.post(
        url, json={"title": "Second"}, headers=workspace_a.secretary_headers(),
    )
    assert second.status_code == 409


def test_rejection_keeps_the_operation_open(api):
    client, workspace_a, _workspace_b = api
    schedule_id = _create_schedule(client, workspace_a).json()["id"]
    claim_id = _claim(
        client, workspace_a, schedule_id, amount=AMOUNT, key="api-reject",
    ).json()["id"]
    rejected = client.post(
        f"/api/v1/rent/claims/{claim_id}/reject?org_id={workspace_a.org_id}",
        json={"reason": "no proof of transfer"},
        headers=workspace_a.owner_headers(),
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "FAILED"
    balance = client.get(
        f"/api/v1/rent/due-schedules/{schedule_id}/balance"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    ).json()
    assert Decimal(balance["remaining_balance"]) == Decimal(AMOUNT)
    assert balance["is_paid"] is False
    operation = client.get(
        f"/api/v1/rent/due-schedules/{schedule_id}/operation"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    ).json()
    assert operation["state"] == "in_progress"
    assert operation["resolved_at"] is None


def test_rejection_requires_a_reason(api):
    client, workspace_a, _workspace_b = api
    schedule_id = _create_schedule(client, workspace_a).json()["id"]
    claim_id = _claim(
        client, workspace_a, schedule_id, amount=AMOUNT, key="api-no-reason",
    ).json()["id"]
    response = client.post(
        f"/api/v1/rent/claims/{claim_id}/reject?org_id={workspace_a.org_id}",
        json={"reason": ""},
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 422


def test_reversal_reopens_a_paid_period(api):
    client, workspace_a, _workspace_b = api
    schedule_id = _create_schedule(client, workspace_a).json()["id"]
    claim_id = _claim(
        client, workspace_a, schedule_id, amount=AMOUNT, key="api-reverse",
    ).json()["id"]
    assert (
        client.post(
            f"/api/v1/rent/claims/{claim_id}/verify"
            f"?org_id={workspace_a.org_id}",
            json={},
            headers=workspace_a.owner_headers(),
        ).status_code
        == 200
    )
    reversed_response = client.post(
        f"/api/v1/rent/claims/{claim_id}/reverse?org_id={workspace_a.org_id}",
        json={"reason": "bank reversed the transfer"},
        headers=workspace_a.owner_headers(),
    )
    assert reversed_response.status_code == 200, reversed_response.text
    assert reversed_response.json()["status"] == "REVERSED"
    balance = client.get(
        f"/api/v1/rent/due-schedules/{schedule_id}/balance"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    ).json()
    assert Decimal(balance["remaining_balance"]) == Decimal(AMOUNT)
    assert balance["is_paid"] is False
    operation = client.get(
        f"/api/v1/rent/due-schedules/{schedule_id}/operation"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    ).json()
    assert operation["state"] == "in_progress"
    assert operation["resolved_at"] is None


def test_overdue_listing_and_marking(api):
    client, workspace_a, _workspace_b = api
    schedule_id = _create_schedule(client, workspace_a).json()["id"]
    overdue = client.get(
        f"/api/v1/rent/overdue?org_id={workspace_a.org_id}&as_of=2026-03-06",
        headers=workspace_a.owner_headers(),
    )
    assert overdue.status_code == 200
    assert [item["id"] for item in overdue.json()] == [schedule_id]

    marked = client.post(
        f"/api/v1/rent/mark-overdue?org_id={workspace_a.org_id}"
        f"&as_of=2026-03-06",
        headers=workspace_a.owner_headers(),
    )
    assert marked.status_code == 200, marked.text
    assert [item["state"] for item in marked.json()] == ["OVERDUE"]


def test_cross_org_read_is_not_found(api):
    client, workspace_a, workspace_b = api
    schedule_id = _create_schedule(client, workspace_a).json()["id"]
    response = client.get(
        f"/api/v1/rent/due-schedules/{schedule_id}"
        f"?org_id={workspace_b.org_id}",
        headers=workspace_b.owner_headers(),
    )
    assert response.status_code == 404


def test_cross_org_scope_mismatch_is_forbidden(api):
    client, workspace_a, workspace_b = api
    schedule_id = _create_schedule(client, workspace_a).json()["id"]
    response = client.get(
        f"/api/v1/rent/due-schedules/{schedule_id}"
        f"?org_id={workspace_a.org_id}",
        headers=workspace_b.owner_headers(),
    )
    assert response.status_code == 403
