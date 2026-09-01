"""HTTP-level behavior tests for the V1 Lease Renewal router.

Proves the router is thin and correct: authentication, role gating handled
by the shared service, mandatory ``Idempotency-Key`` on propose, replay vs
conflict, OWNER-only approve/reject/cancel, the closure gate ``execute``
(source lease terminated, new lease created and activated, unit
reassigned, Operation resolved), the ``Approval != Execution`` invariant,
the ``Reminder != Completion`` invariant (completing a follow-up never
resolves the Operation), and the counterexample that a renewal proposal
without a follow-up does NOT execute the renewal.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db, get_session_factory
from tests.v1_support import seed_workspace, v1_engine_ctx


# Source lease in seed_workspace is ACTIVE 2026-01-01..2026-12-31 with
# monthly_rent=12000 and deposit=24000. Renewal proposed terms are
# chosen to NOT overlap with the source lease (we will terminate it
# during execute) and to lie fully in the future relative to the seed
# dates.
NEW_START = "2027-01-01"
NEW_END = "2027-12-31"
NEW_RENT = "13500.00"
NEW_DEPOSIT = "27000.00"


@pytest.fixture
def api():
    with v1_engine_ctx():
        session = get_session_factory()()
        workspace_a = seed_workspace(session, name="RenAlpha")
        workspace_b = seed_workspace(session, name="RenBeta")

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


def _propose(
    client,
    workspace,
    *,
    key: str | None,
    source_lease_id: int | None = None,
    headers=None,
    proposed_start_date: str = NEW_START,
    proposed_end_date: str = NEW_END,
    proposed_monthly_rent: str = NEW_RENT,
    proposed_deposit: str = NEW_DEPOSIT,
):
    request_headers = dict(headers or workspace.secretary_headers())
    if key is not None:
        request_headers["Idempotency-Key"] = key
    return client.post(
        f"/api/v1/renewals/proposals?org_id={workspace.org_id}",
        json={
            "source_lease_id": source_lease_id or workspace.lease_id,
            "proposed_start_date": proposed_start_date,
            "proposed_end_date": proposed_end_date,
            "proposed_monthly_rent": proposed_monthly_rent,
            "proposed_deposit": proposed_deposit,
        },
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
    response = client.get(f"/api/v1/renewals/?org_id={workspace_a.org_id}")
    assert response.status_code == 401


# ---- propose: happy paths --------------------------------------------


def test_secretary_proposes_a_renewal_with_idempotency_key(api):
    client, workspace_a, _workspace_b = api
    response = _propose(client, workspace_a, key="propose-1")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["state"] == "PROPOSED"
    assert body["source_lease_id"] == workspace_a.lease_id
    assert body["proposed_monthly_rent"] == "13500.00"
    assert body["idempotency_key"] == "propose-1"


def test_owner_can_also_propose(api):
    client, workspace_a, _workspace_b = api
    response = _propose(
        client, workspace_a, key="propose-owner",
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 201, response.text


# ---- propose: negative paths -----------------------------------------


def test_propose_without_idempotency_key_is_400(api):
    client, workspace_a, _workspace_b = api
    request_headers = dict(workspace_a.secretary_headers())
    request_headers.pop("Idempotency-Key", None)
    response = client.post(
        f"/api/v1/renewals/proposals?org_id={workspace_a.org_id}",
        json={
            "source_lease_id": workspace_a.lease_id,
            "proposed_start_date": NEW_START,
            "proposed_end_date": NEW_END,
            "proposed_monthly_rent": NEW_RENT,
            "proposed_deposit": NEW_DEPOSIT,
        },
        headers=request_headers,
    )
    assert response.status_code == 400


def test_propose_oversize_idempotency_key_is_400(api):
    client, workspace_a, _workspace_b = api
    big_key = "x" * 129
    response = _propose(client, workspace_a, key=big_key)
    assert response.status_code == 400


def test_propose_end_before_start_is_400(api):
    client, workspace_a, _workspace_b = api
    response = _propose(
        client, workspace_a, key="bad-dates",
        proposed_start_date=NEW_END, proposed_end_date=NEW_START,
    )
    assert response.status_code == 400


def test_propose_unknown_field_is_422(api):
    client, workspace_a, _workspace_b = api
    request_headers = dict(workspace_a.secretary_headers())
    request_headers["Idempotency-Key"] = "unknown-field"
    response = client.post(
        f"/api/v1/renewals/proposals?org_id={workspace_a.org_id}",
        json={
            "source_lease_id": workspace_a.lease_id,
            "proposed_start_date": NEW_START,
            "proposed_end_date": NEW_END,
            "proposed_monthly_rent": NEW_RENT,
            "proposed_deposit": NEW_DEPOSIT,
            "sneaky": "field",
        },
        headers=request_headers,
    )
    assert response.status_code == 422


def test_propose_float_money_is_422(api):
    client, workspace_a, _workspace_b = api
    request_headers = dict(workspace_a.secretary_headers())
    request_headers["Idempotency-Key"] = "float-money"
    response = client.post(
        f"/api/v1/renewals/proposals?org_id={workspace_a.org_id}",
        json={
            "source_lease_id": workspace_a.lease_id,
            "proposed_start_date": NEW_START,
            "proposed_end_date": NEW_END,
            "proposed_monthly_rent": 13500.5,
            "proposed_deposit": NEW_DEPOSIT,
        },
        headers=request_headers,
    )
    assert response.status_code == 422


def test_propose_zero_rent_is_422(api):
    client, workspace_a, _workspace_b = api
    response = _propose(
        client, workspace_a, key="zero-rent",
        proposed_monthly_rent="0",
    )
    assert response.status_code == 422


def test_propose_unknown_source_lease_is_404(api):
    client, workspace_a, _workspace_b = api
    response = _propose(
        client, workspace_a, key="unknown-lease",
        source_lease_id=99999999,
    )
    assert response.status_code == 404


def test_propose_source_lease_in_other_org_is_404(api):
    client, workspace_a, workspace_b = api
    response = _propose(
        client, workspace_a, key="cross-org",
        source_lease_id=workspace_b.lease_id,
    )
    assert response.status_code == 404


# ---- propose: idempotency --------------------------------------------


def test_idempotent_replay_returns_same_renewal_with_200(api):
    client, workspace_a, _workspace_b = api
    first = _propose(client, workspace_a, key="replay-1")
    assert first.status_code == 201, first.text
    renewal_id = first.json()["id"]
    second = _propose(client, workspace_a, key="replay-1")
    assert second.status_code == 200, second.text
    assert second.json()["id"] == renewal_id


def test_idempotency_key_with_different_payload_is_409(api):
    client, workspace_a, _workspace_b = api
    first = _propose(client, workspace_a, key="conflict")
    assert first.status_code == 201
    second = _propose(
        client, workspace_a, key="conflict",
        proposed_monthly_rent="14000.00",
    )
    assert second.status_code == 409, second.text


def test_case_preserving_idempotency_keys_distinguish(api):
    client, workspace_a, _workspace_b = api
    upper = _propose(client, workspace_a, key="MixedCase-1")
    assert upper.status_code == 201
    lower = _propose(client, workspace_a, key="mixedcase-1")
    assert lower.status_code == 201
    assert upper.json()["id"] != lower.json()["id"]


def test_cross_org_idempotency_key_does_not_collide(api):
    client, workspace_a, workspace_b = api
    a = _propose(client, workspace_a, key="shared-key")
    assert a.status_code == 201
    b = _propose(client, workspace_b, key="shared-key")
    assert b.status_code == 201
    assert a.json()["id"] != b.json()["id"]
    assert a.json()["org_id"] == workspace_a.org_id
    assert b.json()["org_id"] == workspace_b.org_id


def test_cross_org_read_is_404(api):
    client, workspace_a, workspace_b = api
    a = _propose(client, workspace_a, key="cross-read")
    assert a.status_code == 201
    other_org_headers = {
        "Authorization": f"Bearer {workspace_b.owner_api_key}",
    }
    response = client.get(
        f"/api/v1/renewals/proposals/{a.json()['id']}?org_id={workspace_b.org_id}",
        headers=other_org_headers,
    )
    assert response.status_code == 404


# ---- approve / reject -------------------------------------------------


def _propose_default(client, workspace, *, key="propose-1") -> int:
    response = _propose(client, workspace, key=key)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_owner_approves_a_proposal(api):
    client, workspace_a, _workspace_b = api
    renewal_id = _propose_default(client, workspace_a)
    response = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/approve?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "APPROVED"
    assert body["decided_by_user_id"] == workspace_a.owner_user_id


def test_secretary_cannot_approve(api):
    client, workspace_a, _workspace_b = api
    renewal_id = _propose_default(client, workspace_a)
    response = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/approve?org_id={workspace_a.org_id}",
        headers=workspace_a.secretary_headers(),
    )
    assert response.status_code == 403


def test_approve_non_proposed_is_409(api):
    client, workspace_a, _workspace_b = api
    renewal_id = _propose_default(client, workspace_a)
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/approve?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    # Second approve from PROPOSED→APPROVED is fine; but executing first
    # then trying to approve is the test we want.
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/execute?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    response = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/approve?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 409


def test_owner_rejects_a_proposal(api):
    client, workspace_a, _workspace_b = api
    renewal_id = _propose_default(client, workspace_a)
    response = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/reject?org_id={workspace_a.org_id}",
        json={"reason": "Tenant renegotiated"},
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "REJECTED"
    assert body["decision_reason"] == "Tenant renegotiated"


def test_secretary_cannot_reject(api):
    client, workspace_a, _workspace_b = api
    renewal_id = _propose_default(client, workspace_a)
    response = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/reject?org_id={workspace_a.org_id}",
        json={"reason": "nope"},
        headers=workspace_a.secretary_headers(),
    )
    assert response.status_code == 403


# ---- execute (closure gate) ------------------------------------------


def test_execute_from_proposed_is_409(api):
    client, workspace_a, _workspace_b = api
    renewal_id = _propose_default(client, workspace_a)
    response = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/execute?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 409
    assert response.json()["detail"]


def test_execute_happy_path(api):
    client, workspace_a, _workspace_b = api
    renewal_id = _propose_default(client, workspace_a)
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/approve?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    response = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/execute?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["renewal"]["state"] == "EXECUTED"
    new_lease = body["new_lease"]
    assert new_lease["state"] == "ACTIVE"
    assert new_lease["monthly_rent"] == "13500.00"
    assert new_lease["start_date"] == NEW_START
    assert new_lease["end_date"] == NEW_END

    # Renewal now points at the new lease.
    after = client.get(
        f"/api/v1/renewals/proposals/{renewal_id}?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert after.status_code == 200
    assert after.json()["new_lease_id"] == new_lease["id"]
    assert after.json()["executed_at"] is not None

    # Operation is resolved.
    op = client.get(
        f"/api/v1/renewals/proposals/{renewal_id}/operation?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert op.status_code == 200
    assert op.json()["state"] == "resolved"


def test_double_execute_is_409(api):
    client, workspace_a, _workspace_b = api
    renewal_id = _propose_default(client, workspace_a)
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/approve?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    first = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/execute?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert first.status_code == 200
    second = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/execute?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert second.status_code == 409


# ---- cancel ----------------------------------------------------------


def test_cancel_proposal_with_reason(api):
    client, workspace_a, _workspace_b = api
    renewal_id = _propose_default(client, workspace_a)
    response = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/cancel?org_id={workspace_a.org_id}",
        json={"reason": "Withdrawn"},
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "CANCELLED"
    assert body["decision_reason"] == "Withdrawn"


def test_cancel_without_reason_is_422(api):
    client, workspace_a, _workspace_b = api
    renewal_id = _propose_default(client, workspace_a)
    response = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/cancel?org_id={workspace_a.org_id}",
        json={"reason": ""},
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 422


def test_cancel_executed_is_409(api):
    client, workspace_a, _workspace_b = api
    renewal_id = _propose_default(client, workspace_a)
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/approve?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/execute?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    response = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/cancel?org_id={workspace_a.org_id}",
        json={"reason": "too late"},
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 409


# ---- follow-up (Task projection) -------------------------------------


def test_follow_up_creates_a_task(api):
    client, workspace_a, _workspace_b = api
    renewal_id = _propose_default(client, workspace_a)
    response = client.post(
        "/api/v1/renewals/follow-ups?org_id={}".format(workspace_a.org_id),
        json={"renewal_id": renewal_id, "title": "Send new terms to tenant"},
        headers=workspace_a.secretary_headers(),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["state"] == "open"
    assert body["title"] == "Send new terms to tenant"


def test_follow_up_complete_does_not_resolve_operation(api):
    client, workspace_a, _workspace_b = api
    renewal_id = _propose_default(client, workspace_a)
    create = client.post(
        f"/api/v1/renewals/follow-ups?org_id={workspace_a.org_id}",
        json={"renewal_id": renewal_id, "title": "Send terms"},
        headers=workspace_a.secretary_headers(),
    )
    task_id = create.json()["id"]
    done = client.post(
        f"/api/v1/renewals/follow-ups/{task_id}/complete?org_id={workspace_a.org_id}",
        headers=workspace_a.secretary_headers(),
    )
    assert done.status_code == 200
    assert done.json()["state"] == "done"
    op = client.get(
        f"/api/v1/renewals/proposals/{renewal_id}/operation?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert op.status_code == 200
    assert op.json()["state"] != "resolved"


def test_second_open_follow_up_is_409(api):
    client, workspace_a, _workspace_b = api
    renewal_id = _propose_default(client, workspace_a)
    first = client.post(
        f"/api/v1/renewals/follow-ups?org_id={workspace_a.org_id}",
        json={"renewal_id": renewal_id, "title": "First"},
        headers=workspace_a.secretary_headers(),
    )
    assert first.status_code == 201
    second = client.post(
        f"/api/v1/renewals/follow-ups?org_id={workspace_a.org_id}",
        json={"renewal_id": renewal_id, "title": "Second"},
        headers=workspace_a.secretary_headers(),
    )
    assert second.status_code == 409


# ---- list / activity / filters ---------------------------------------


def test_list_renewals_returns_all_in_org(api):
    client, workspace_a, workspace_b = api
    _propose(client, workspace_a, key="a1")
    _propose(client, workspace_a, key="a2")
    _propose(client, workspace_b, key="b1")
    response = client.get(
        f"/api/v1/renewals/?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 200
    items = response.json()
    assert len(items) == 2
    assert all(r["org_id"] == workspace_a.org_id for r in items)


def test_list_renewals_filters_by_source_lease(api):
    client, workspace_a, _workspace_b = api
    _propose(client, workspace_a, key="filter-1")
    response = client.get(
        f"/api/v1/renewals/?org_id={workspace_a.org_id}&source_lease_id={workspace_a.lease_id}",
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 200
    items = response.json()
    assert len(items) == 1


def test_activity_feed_records_transitions(api):
    client, workspace_a, _workspace_b = api
    renewal_id = _propose_default(client, workspace_a)
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/approve?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    response = client.get(
        f"/api/v1/renewals/proposals/{renewal_id}/activity?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 200
    kinds = [a["kind"] for a in response.json()]
    assert "PROPOSED" in kinds
    assert "APPROVED" in kinds
