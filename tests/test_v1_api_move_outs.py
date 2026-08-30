"""HTTP-level behavior tests for the V1 Move-out / Settlement router.

Proves the router is thin and correct: authentication, role gating, the
mandatory ``Idempotency-Key`` on request, replay vs conflict, OWNER-only
settlement, the closure gate (DepositSettlement recorded → move-out
transitions to SETTLED → Operation resolved), ``FULL_REFUND`` /
``NO_REFUND`` amount invariants, the ``Reminder != Completion``
invariant (completing a follow-up never resolves the Operation), and
the counterexample that recording a damage does not close the move-out.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db, get_session_factory
from tests.v1_support import seed_workspace, v1_engine_ctx


DEPOSIT = "24000.00"


@pytest.fixture
def api():
    with v1_engine_ctx():
        session = get_session_factory()()
        workspace_a = seed_workspace(session, name="MoAlpha")
        workspace_b = seed_workspace(session, name="MoBeta")

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


def _request(
    client,
    workspace,
    *,
    key,
    headers=None,
    lease_id=None,
    planned_move_out_date="2026-12-31",
    notes="End of term",
):
    request_headers = dict(headers or workspace.secretary_headers())
    if key is not None:
        request_headers["Idempotency-Key"] = key
    return client.post(
        f"/api/v1/move-outs?org_id={workspace.org_id}",
        json={
            "lease_id": lease_id or workspace.lease_id,
            "planned_move_out_date": planned_move_out_date,
            "notes": notes,
        },
        headers=request_headers,
    )


def _inspection(client, workspace, move_out_id, *, summary="Walk-through done"):
    return client.post(
        f"/api/v1/move-outs/{move_out_id}/inspections?org_id={workspace.org_id}",
        json={"summary": summary},
        headers=workspace.owner_headers(),
    )


def _settle(
    client,
    workspace,
    move_out_id,
    *,
    disposition,
    deposit_held=DEPOSIT,
    refund_amount="0",
    additional_owed="0",
    headers=None,
):
    return client.post(
        f"/api/v1/move-outs/{move_out_id}/settlement?org_id={workspace.org_id}",
        json={
            "disposition": disposition,
            "deposit_held": deposit_held,
            "refund_amount": refund_amount,
            "additional_owed": additional_owed,
        },
        headers=headers or workspace.owner_headers(),
    )


# ---- health / auth ---------------------------------------------------


def test_health_is_available(api):
    client, _workspace_a, _workspace_b = api
    response = client.get("/health")
    assert response.status_code == 200


def test_unauthenticated_request_is_rejected(api):
    client, workspace_a, _workspace_b = api
    response = client.get(f"/api/v1/move-outs?org_id={workspace_a.org_id}")
    assert response.status_code == 401


# ---- request: happy paths --------------------------------------------


def test_secretary_requests_move_out_with_idempotency_key(api):
    client, workspace_a, _workspace_b = api
    response = _request(client, workspace_a, key="mo-1")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["state"] == "REQUESTED"
    assert body["lease_id"] == workspace_a.lease_id


def test_owner_can_also_request(api):
    client, workspace_a, _workspace_b = api
    response = _request(
        client, workspace_a, key="mo-owner",
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 201


# ---- request: negative paths -----------------------------------------


def test_request_without_idempotency_key_is_400(api):
    client, workspace_a, _workspace_b = api
    request_headers = dict(workspace_a.secretary_headers())
    request_headers.pop("Idempotency-Key", None)
    response = client.post(
        f"/api/v1/move-outs?org_id={workspace_a.org_id}",
        json={
            "lease_id": workspace_a.lease_id,
            "planned_move_out_date": "2026-12-31",
            "notes": "x",
        },
        headers=request_headers,
    )
    assert response.status_code == 400


def test_request_oversize_idempotency_key_is_400(api):
    client, workspace_a, _workspace_b = api
    response = _request(client, workspace_a, key="x" * 129)
    assert response.status_code == 400


def test_request_unknown_lease_is_404(api):
    client, workspace_a, _workspace_b = api
    response = _request(client, workspace_a, key="bad-lease", lease_id=99999999)
    assert response.status_code == 404


def test_request_lease_in_other_org_is_404(api):
    client, workspace_a, workspace_b = api
    response = _request(
        client, workspace_a, key="cross-org",
        lease_id=workspace_b.lease_id,
    )
    assert response.status_code == 404


# ---- request: idempotency --------------------------------------------


def test_idempotent_replay_returns_same_move_out_with_200(api):
    client, workspace_a, _workspace_b = api
    first = _request(client, workspace_a, key="replay-1")
    assert first.status_code == 201
    second = _request(client, workspace_a, key="replay-1")
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]


def test_idempotency_key_with_different_payload_is_409(api):
    client, workspace_a, _workspace_b = api
    first = _request(client, workspace_a, key="conflict")
    assert first.status_code == 201
    second = _request(
        client, workspace_a, key="conflict",
        planned_move_out_date="2026-11-30",
    )
    assert second.status_code == 409


def test_case_preserving_idempotency_keys_distinguish(api):
    client, workspace_a, _workspace_b = api
    upper = _request(client, workspace_a, key="MixedCase-1")
    assert upper.status_code == 201
    lower = _request(client, workspace_a, key="mixedcase-1")
    assert lower.status_code == 201
    assert upper.json()["id"] != lower.json()["id"]


def test_cross_org_idempotency_key_does_not_collide(api):
    client, workspace_a, workspace_b = api
    a = _request(client, workspace_a, key="shared-key")
    b = _request(client, workspace_b, key="shared-key")
    assert a.status_code == 201
    assert b.status_code == 201
    assert a.json()["id"] != b.json()["id"]


def test_cross_org_read_is_404(api):
    client, workspace_a, workspace_b = api
    a = _request(client, workspace_a, key="cross-read")
    assert a.status_code == 201
    other_headers = {"Authorization": f"Bearer {workspace_b.owner_api_key}"}
    response = client.get(
        f"/api/v1/move-outs/{a.json()['id']}?org_id={workspace_b.org_id}",
        headers=other_headers,
    )
    assert response.status_code == 404


# ---- inspection ------------------------------------------------------


def test_record_inspection_advances_to_inspected(api):
    client, workspace_a, _workspace_b = api
    mo = _request(client, workspace_a, key="mo-1")
    move_out_id = mo.json()["id"]
    response = _inspection(client, workspace_a, move_out_id)
    assert response.status_code == 201
    after = client.get(
        f"/api/v1/move-outs/{move_out_id}?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert after.status_code == 200
    assert after.json()["state"] == "INSPECTED"


def test_settle_before_inspection_is_409(api):
    client, workspace_a, _workspace_b = api
    mo = _request(client, workspace_a, key="no-inspection")
    move_out_id = mo.json()["id"]
    response = _settle(client, workspace_a, move_out_id, disposition="FULL_REFUND",
                       refund_amount=DEPOSIT)
    assert response.status_code == 409


# ---- settlement (closure gate) ---------------------------------------


def test_full_refund_settles_the_move_out(api):
    client, workspace_a, _workspace_b = api
    mo = _request(client, workspace_a, key="full-refund")
    move_out_id = mo.json()["id"]
    _inspection(client, workspace_a, move_out_id)
    response = _settle(client, workspace_a, move_out_id, disposition="FULL_REFUND",
                       refund_amount=DEPOSIT)
    assert response.status_code == 200, response.text
    after = client.get(
        f"/api/v1/move-outs/{move_out_id}?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert after.json()["state"] == "SETTLED"
    op = client.get(
        f"/api/v1/move-outs/{move_out_id}/operation?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert op.json()["state"] == "resolved"


def test_no_refund_settles_the_move_out(api):
    client, workspace_a, _workspace_b = api
    mo = _request(client, workspace_a, key="no-refund")
    move_out_id = mo.json()["id"]
    _inspection(client, workspace_a, move_out_id)
    response = _settle(client, workspace_a, move_out_id, disposition="NO_REFUND")
    assert response.status_code == 200, response.text


def test_full_refund_with_wrong_amounts_is_422(api):
    client, workspace_a, _workspace_b = api
    mo = _request(client, workspace_a, key="bad-full")
    move_out_id = mo.json()["id"]
    _inspection(client, workspace_a, move_out_id)
    response = _settle(
        client, workspace_a, move_out_id,
        disposition="FULL_REFUND", refund_amount="100.00",
    )
    assert response.status_code == 422


def test_no_refund_with_refund_amount_is_422(api):
    client, workspace_a, _workspace_b = api
    mo = _request(client, workspace_a, key="bad-no-refund")
    move_out_id = mo.json()["id"]
    _inspection(client, workspace_a, move_out_id)
    response = _settle(
        client, workspace_a, move_out_id,
        disposition="NO_REFUND", refund_amount="100.00",
    )
    assert response.status_code == 422


def test_secretary_cannot_settle(api):
    client, workspace_a, _workspace_b = api
    mo = _request(client, workspace_a, key="sec-settle")
    move_out_id = mo.json()["id"]
    _inspection(client, workspace_a, move_out_id)
    response = _settle(
        client, workspace_a, move_out_id, disposition="FULL_REFUND",
        refund_amount=DEPOSIT, headers=workspace_a.secretary_headers(),
    )
    assert response.status_code == 403


def test_double_settle_is_409(api):
    client, workspace_a, _workspace_b = api
    mo = _request(client, workspace_a, key="double-settle")
    move_out_id = mo.json()["id"]
    _inspection(client, workspace_a, move_out_id)
    first = _settle(
        client, workspace_a, move_out_id, disposition="FULL_REFUND",
        refund_amount=DEPOSIT,
    )
    assert first.status_code == 200
    second = _settle(
        client, workspace_a, move_out_id, disposition="FULL_REFUND",
        refund_amount=DEPOSIT,
    )
    assert second.status_code == 409


# ---- damages ---------------------------------------------------------


def test_record_damage_before_inspection_is_409(api):
    client, workspace_a, _workspace_b = api
    mo = _request(client, workspace_a, key="early-damage")
    move_out_id = mo.json()["id"]
    response = client.post(
        f"/api/v1/move-outs/{move_out_id}/damages?org_id={workspace_a.org_id}",
        json={
            "kind": "CLEANING",
            "description": "Deep clean",
            "amount": "1500.00",
        },
        headers=workspace_a.secretary_headers(),
    )
    assert response.status_code == 409


def test_record_damage_after_inspection_persists(api):
    client, workspace_a, _workspace_b = api
    mo = _request(client, workspace_a, key="post-inspect-damage")
    move_out_id = mo.json()["id"]
    _inspection(client, workspace_a, move_out_id)
    response = client.post(
        f"/api/v1/move-outs/{move_out_id}/damages?org_id={workspace_a.org_id}",
        json={
            "kind": "REPAIR",
            "description": "Hole in wall",
            "amount": "3000.00",
        },
        headers=workspace_a.secretary_headers(),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["kind"] == "REPAIR"
    assert body["amount"] == "3000.00"
    # Move-out state is still INSPECTED (damage alone does not close).
    after = client.get(
        f"/api/v1/move-outs/{move_out_id}?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert after.json()["state"] == "INSPECTED"


def test_partial_refund_with_deduction_settles(api):
    client, workspace_a, _workspace_b = api
    mo = _request(client, workspace_a, key="partial-refund")
    move_out_id = mo.json()["id"]
    _inspection(client, workspace_a, move_out_id)
    client.post(
        f"/api/v1/move-outs/{move_out_id}/damages?org_id={workspace_a.org_id}",
        json={
            "kind": "REPAIR",
            "description": "Repaint",
            "amount": "4000.00",
            "accepted_amount": "3000.00",
        },
        headers=workspace_a.secretary_headers(),
    )
    response = _settle(
        client, workspace_a, move_out_id,
        disposition="PARTIAL_REFUND",
        refund_amount="21000.00",
    )
    assert response.status_code == 200, response.text


# ---- balance / activity ----------------------------------------------


def test_balance_pre_settlement_reflects_deductions(api):
    client, workspace_a, _workspace_b = api
    mo = _request(client, workspace_a, key="balance-check")
    move_out_id = mo.json()["id"]
    _inspection(client, workspace_a, move_out_id)
    client.post(
        f"/api/v1/move-outs/{move_out_id}/damages?org_id={workspace_a.org_id}",
        json={
            "kind": "REPAIR",
            "description": "x",
            "amount": "1000.00",
            "accepted_amount": "700.00",
        },
        headers=workspace_a.secretary_headers(),
    )
    response = client.get(
        f"/api/v1/move-outs/{move_out_id}/balance?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["deductions_total"] == "700.00"
    assert body["is_settled"] is False


def test_activity_feed_records_transitions(api):
    client, workspace_a, _workspace_b = api
    mo = _request(client, workspace_a, key="activity-feed")
    move_out_id = mo.json()["id"]
    _inspection(client, workspace_a, move_out_id)
    _settle(
        client, workspace_a, move_out_id,
        disposition="FULL_REFUND", refund_amount=DEPOSIT,
    )
    response = client.get(
        f"/api/v1/move-outs/{move_out_id}/activity?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 200
    kinds = [a["kind"] for a in response.json()]
    assert "REQUESTED" in kinds
    assert "INSPECTED" in kinds
    assert "SETTLED" in kinds


# ---- cancel ----------------------------------------------------------


def test_cancel_with_reason_resolves_operation(api):
    client, workspace_a, _workspace_b = api
    mo = _request(client, workspace_a, key="cancel-1")
    move_out_id = mo.json()["id"]
    response = client.post(
        f"/api/v1/move-outs/{move_out_id}/cancel?org_id={workspace_a.org_id}",
        json={"reason": "Tenant withdrew"},
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "CANCELLED"
    op = client.get(
        f"/api/v1/move-outs/{move_out_id}/operation?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert op.json()["state"] == "resolved"


def test_cancel_settled_is_409(api):
    client, workspace_a, _workspace_b = api
    mo = _request(client, workspace_a, key="cancel-settled")
    move_out_id = mo.json()["id"]
    _inspection(client, workspace_a, move_out_id)
    _settle(
        client, workspace_a, move_out_id,
        disposition="FULL_REFUND", refund_amount=DEPOSIT,
    )
    response = client.post(
        f"/api/v1/move-outs/{move_out_id}/cancel?org_id={workspace_a.org_id}",
        json={"reason": "too late"},
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 409


# ---- follow-up -------------------------------------------------------


def test_follow_up_creates_a_task(api):
    client, workspace_a, _workspace_b = api
    mo = _request(client, workspace_a, key="follow-up-1")
    move_out_id = mo.json()["id"]
    response = client.post(
        f"/api/v1/move-outs/follow-ups?org_id={workspace_a.org_id}",
        json={"move_out_id": move_out_id, "title": "Schedule walk-through"},
        headers=workspace_a.secretary_headers(),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["state"] == "open"
    assert body["title"] == "Schedule walk-through"


def test_follow_up_complete_does_not_resolve_operation(api):
    client, workspace_a, _workspace_b = api
    mo = _request(client, workspace_a, key="follow-up-complete")
    move_out_id = mo.json()["id"]
    create = client.post(
        f"/api/v1/move-outs/follow-ups?org_id={workspace_a.org_id}",
        json={"move_out_id": move_out_id, "title": "Reminder"},
        headers=workspace_a.secretary_headers(),
    )
    task_id = create.json()["id"]
    done = client.post(
        f"/api/v1/move-outs/follow-ups/{task_id}/complete?org_id={workspace_a.org_id}",
        headers=workspace_a.secretary_headers(),
    )
    assert done.status_code == 200
    op = client.get(
        f"/api/v1/move-outs/{move_out_id}/operation?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert op.json()["state"] != "resolved"


def test_second_open_follow_up_is_409(api):
    client, workspace_a, _workspace_b = api
    mo = _request(client, workspace_a, key="follow-up-second")
    move_out_id = mo.json()["id"]
    first = client.post(
        f"/api/v1/move-outs/follow-ups?org_id={workspace_a.org_id}",
        json={"move_out_id": move_out_id, "title": "First"},
        headers=workspace_a.secretary_headers(),
    )
    assert first.status_code == 201
    second = client.post(
        f"/api/v1/move-outs/follow-ups?org_id={workspace_a.org_id}",
        json={"move_out_id": move_out_id, "title": "Second"},
        headers=workspace_a.secretary_headers(),
    )
    assert second.status_code == 409


# ---- list ------------------------------------------------------------


def test_list_filter_by_state(api):
    client, workspace_a, _workspace_b = api
    _request(client, workspace_a, key="l1")
    mo2 = _request(client, workspace_a, key="l2")
    move_out_id = mo2.json()["id"]
    _inspection(client, workspace_a, move_out_id)
    response = client.get(
        f"/api/v1/move-outs?org_id={workspace_a.org_id}&state=REQUESTED",
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 200
    items = response.json()
    assert len(items) == 1
    assert items[0]["state"] == "REQUESTED"
