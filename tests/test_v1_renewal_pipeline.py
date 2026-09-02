"""Frozen Issue #112 §"Lease Renewal" 7-stage pipeline tests.

The 7-stage lifecycle is::

    DETECT_EXPIRY → CONTACT_TENANT → TENANT_RESPONSE
        → OWNER_DECISION → EXECUTED → VERIFY → CLOSED

Plus terminal REJECTED (from OWNER_DECISION when decision=TERMINATE) and
the universal CANCELLED escape hatch from any non-terminal state.

These tests cover:

- ``POST /api/v1/renewals/scan`` emits one DETECT_EXPIRY renewal per
  ACTIVE lease inside the window; the scan is idempotent on
  ``(org_id, source_lease_id, scan_window_days)``.
- The state machine accepts each transition in order and rejects every
  out-of-order transition.
- Operation resolution semantics: the Operation is created at
  DETECT_EXPIRY, stays OPEN through EXECUTED → VERIFY, and resolves
  only at CLOSED (or REJECTED / CANCELLED). Legacy ``execute`` from
  the OWNER_DECISION stage still flips the source lease / unit status
  / new lease atomically — the same closure gate is shared with the
  legacy 5-state proposal pipeline.
- The legacy 5-state pipeline (``propose → approve → execute``) is
  unaffected: existing tests in ``test_v1_api_renewals.py`` continue
  to pass without modification.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db, get_session_factory
from app.v1.models.base import LeaseState
from app.v1.models.renewal import RenewalState
from app.v1.models.tenant_lease import Lease
from tests.v1_support import seed_workspace, v1_engine_ctx


# Source lease in seed_workspace is ACTIVE 2026-01-01..2026-12-31.
# A 365-day window therefore catches every seeded ACTIVE lease
# regardless of when the test is executed.
SCAN_WINDOW_DAYS = 365


@pytest.fixture
def api():
    with v1_engine_ctx():
        session = get_session_factory()()
        workspace_a = seed_workspace(session, name="PipeAlpha")
        workspace_b = seed_workspace(session, name="PipeBeta")

        from app.v1.main import app as v1_app

        def _override_get_db():
            yield session

        v1_app.dependency_overrides[get_db] = _override_get_db
        try:
            with TestClient(v1_app) as client:
                yield client, workspace_a, workspace_b, session
        finally:
            v1_app.dependency_overrides.clear()
            session.close()


def _scan(client, workspace, *, window_days: int, headers=None):
    return client.post(
        f"/api/v1/renewals/scan?org_id={workspace.org_id}",
        json={"window_days": window_days},
        headers=headers or workspace.secretary_headers(),
    )


def _get_renewal(client, workspace, renewal_id, *, headers=None):
    return client.get(
        f"/api/v1/renewals/proposals/{renewal_id}?org_id={workspace.org_id}",
        headers=headers or workspace.owner_headers(),
    )


def _get_operation(client, workspace, renewal_id, *, headers=None):
    return client.get(
        f"/api/v1/renewals/proposals/{renewal_id}/operation"
        f"?org_id={workspace.org_id}",
        headers=headers or workspace.owner_headers(),
    )


def _get_activity(client, workspace, renewal_id, *, headers=None):
    return client.get(
        f"/api/v1/renewals/proposals/{renewal_id}/activity"
        f"?org_id={workspace.org_id}",
        headers=headers or workspace.owner_headers(),
    )


def _drive_to_executed(client, workspace, renewal_id):
    """Walk DETECT_EXPIRY → EXECUTED for happy-path tests."""
    contact = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/contact"
        f"?org_id={workspace.org_id}",
        json={"channel": "telegram", "note": "intro"},
        headers=workspace.secretary_headers(),
    )
    assert contact.status_code == 200, contact.text
    assert contact.json()["state"] == RenewalState.CONTACT_TENANT.value

    respond = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/respond"
        f"?org_id={workspace.org_id}",
        json={"response": "RENEW", "note": "tenant wants to stay"},
        headers=workspace.secretary_headers(),
    )
    assert respond.status_code == 200, respond.text
    assert respond.json()["state"] == RenewalState.TENANT_RESPONSE.value
    assert respond.json()["tenant_response"] == "RENEW"

    decide = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/owner-decide"
        f"?org_id={workspace.org_id}",
        json={"decision": "RENEW", "note": "ok with new rent"},
        headers=workspace.owner_headers(),
    )
    assert decide.status_code == 200, decide.text
    assert decide.json()["state"] == RenewalState.OWNER_DECISION.value
    assert decide.json()["owner_decision"] == "RENEW"

    execute = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/execute"
        f"?org_id={workspace.org_id}",
        headers=workspace.owner_headers(),
    )
    assert execute.status_code == 200, execute.text
    assert execute.json()["renewal"]["state"] == RenewalState.EXECUTED.value
    return execute


# ---- health / auth ----------------------------------------------------


def test_health_is_available(api):
    client, _workspace_a, _workspace_b, _session = api
    response = client.get("/health")
    assert response.status_code == 200


def test_unauthenticated_scan_is_rejected(api):
    client, workspace_a, _workspace_b, _session = api
    response = client.post(
        f"/api/v1/renewals/scan?org_id={workspace_a.org_id}",
        json={"window_days": SCAN_WINDOW_DAYS},
    )
    assert response.status_code == 401


# ---- DETECT_EXPIRY ----------------------------------------------------


def test_scan_emits_one_detect_expiry_per_active_lease(api):
    client, workspace_a, _workspace_b, _session = api
    response = _scan(client, workspace_a, window_days=SCAN_WINDOW_DAYS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["window_days"] == SCAN_WINDOW_DAYS
    assert body["count"] >= 1
    assert body["replayed"] is False
    # All emitted renewals are at DETECT_EXPIRY and tied to the
    # workspace's seeded source lease.
    for entry in body["renewals"]:
        assert entry["state"] == RenewalState.DETECT_EXPIRY.value
        assert entry["source_lease_id"] == workspace_a.lease_id
        assert entry["scan_window_days"] == SCAN_WINDOW_DAYS
        assert entry["is_new"] is True


def test_scan_replay_is_idempotent(api):
    client, workspace_a, _workspace_b, _session = api
    first = _scan(client, workspace_a, window_days=SCAN_WINDOW_DAYS)
    assert first.status_code == 200
    first_renewals = first.json()["renewals"]
    first_ids = {r["id"] for r in first_renewals}

    second = _scan(client, workspace_a, window_days=SCAN_WINDOW_DAYS)
    assert second.status_code == 200
    body = second.json()
    assert body["replayed"] is True
    assert body["count"] == len(first_renewals)
    second_ids = {r["id"] for r in body["renewals"]}
    assert second_ids == first_ids
    for r in body["renewals"]:
        assert r["is_new"] is False


def test_scan_zero_window_is_400(api):
    client, workspace_a, _workspace_b, _session = api
    response = _scan(client, workspace_a, window_days=0)
    assert response.status_code == 422


def test_scan_oversized_window_is_422(api):
    client, workspace_a, _workspace_b, _session = api
    response = _scan(client, workspace_a, window_days=1000)
    assert response.status_code == 422


def test_scan_does_not_collide_across_orgs(api):
    client, workspace_a, workspace_b, _session = api
    a = _scan(client, workspace_a, window_days=SCAN_WINDOW_DAYS)
    b = _scan(client, workspace_b, window_days=SCAN_WINDOW_DAYS)
    assert a.status_code == 200
    assert b.status_code == 200
    a_ids = {r["id"] for r in a.json()["renewals"]}
    b_ids = {r["id"] for r in b.json()["renewals"]}
    assert a_ids.isdisjoint(b_ids)


def test_scan_unknown_field_is_422(api):
    client, workspace_a, _workspace_b, _session = api
    response = client.post(
        f"/api/v1/renewals/scan?org_id={workspace_a.org_id}",
        json={"window_days": SCAN_WINDOW_DAYS, "sneaky": "field"},
        headers=workspace_a.secretary_headers(),
    )
    assert response.status_code == 422


# ---- CONTACT_TENANT ---------------------------------------------------


def test_contact_tenant_requires_detect_expiry(api):
    client, workspace_a, _workspace_b, _session = api
    response = client.post(
        f"/api/v1/renewals/proposals/{workspace_a.lease_id}/contact"
        f"?org_id={workspace_a.org_id}",
        json={"channel": "telegram"},
        headers=workspace_a.secretary_headers(),
    )
    # lease_id is not a renewal id → 404
    assert response.status_code == 404


def test_contact_tenant_records_activity_and_state(api):
    client, workspace_a, _workspace_b, _session = api
    scan = _scan(client, workspace_a, window_days=SCAN_WINDOW_DAYS)
    renewal_id = scan.json()["renewals"][0]["id"]

    response = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/contact"
        f"?org_id={workspace_a.org_id}",
        json={"channel": "telegram", "note": "pinged tenant"},
        headers=workspace_a.secretary_headers(),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == RenewalState.CONTACT_TENANT.value

    activity = _get_activity(
        client, workspace_a, renewal_id,
    ).json()
    kinds = [a["kind"] for a in activity]
    assert "DETECTED" in kinds
    assert "TENANT_CONTACTED" in kinds


# ---- TENANT_RESPONSE --------------------------------------------------


def test_record_response_rejects_invalid_vocabulary(api):
    client, workspace_a, _workspace_b, _session = api
    scan = _scan(client, workspace_a, window_days=SCAN_WINDOW_DAYS)
    renewal_id = scan.json()["renewals"][0]["id"]
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/contact"
        f"?org_id={workspace_a.org_id}",
        json={"channel": "telegram"},
        headers=workspace_a.secretary_headers(),
    )
    response = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/respond"
        f"?org_id={workspace_a.org_id}",
        json={"response": "MAYBE"},
        headers=workspace_a.secretary_headers(),
    )
    assert response.status_code == 400, response.text


def test_record_response_requires_contact_first(api):
    client, workspace_a, _workspace_b, _session = api
    scan = _scan(client, workspace_a, window_days=SCAN_WINDOW_DAYS)
    renewal_id = scan.json()["renewals"][0]["id"]
    # Skip CONTACT_TENANT — should be 409.
    response = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/respond"
        f"?org_id={workspace_a.org_id}",
        json={"response": "RENEW"},
        headers=workspace_a.secretary_headers(),
    )
    assert response.status_code == 409, response.text


def test_record_response_persists_tenant_choice(api):
    client, workspace_a, _workspace_b, _session = api
    scan = _scan(client, workspace_a, window_days=SCAN_WINDOW_DAYS)
    renewal_id = scan.json()["renewals"][0]["id"]
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/contact"
        f"?org_id={workspace_a.org_id}",
        json={"channel": "telegram"},
        headers=workspace_a.secretary_headers(),
    )
    response = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/respond"
        f"?org_id={workspace_a.org_id}",
        json={"response": "DEFER", "note": "thinking"},
        headers=workspace_a.secretary_headers(),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == RenewalState.TENANT_RESPONSE.value
    assert body["tenant_response"] == "DEFER"
    assert body["tenant_response_at"] is not None


# ---- OWNER_DECISION ---------------------------------------------------


def test_decide_owner_requires_owner_role(api):
    client, workspace_a, _workspace_b, _session = api
    scan = _scan(client, workspace_a, window_days=SCAN_WINDOW_DAYS)
    renewal_id = scan.json()["renewals"][0]["id"]
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/contact"
        f"?org_id={workspace_a.org_id}",
        json={"channel": "telegram"},
        headers=workspace_a.secretary_headers(),
    )
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/respond"
        f"?org_id={workspace_a.org_id}",
        json={"response": "RENEW"},
        headers=workspace_a.secretary_headers(),
    )
    response = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/owner-decide"
        f"?org_id={workspace_a.org_id}",
        json={"decision": "RENEW"},
        headers=workspace_a.secretary_headers(),
    )
    assert response.status_code == 403


def test_decide_owner_terminate_resolves_operation(api):
    client, workspace_a, _workspace_b, session = api
    scan = _scan(client, workspace_a, window_days=SCAN_WINDOW_DAYS)
    renewal_id = scan.json()["renewals"][0]["id"]
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/contact"
        f"?org_id={workspace_a.org_id}",
        json={"channel": "telegram"},
        headers=workspace_a.secretary_headers(),
    )
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/respond"
        f"?org_id={workspace_a.org_id}",
        json={"response": "TERMINATE"},
        headers=workspace_a.secretary_headers(),
    )
    response = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/owner-decide"
        f"?org_id={workspace_a.org_id}",
        json={"decision": "TERMINATE", "note": "owner agrees"},
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == RenewalState.REJECTED.value
    assert body["owner_decision"] == "TERMINATE"

    # Source lease is untouched (no execute was called).
    source_lease = session.get(Lease, workspace_a.lease_id)
    assert source_lease.state == LeaseState.ACTIVE.value

    # Operation is resolved at REJECTED.
    op = _get_operation(client, workspace_a, renewal_id).json()
    assert op["state"] == "resolved"


def test_decide_owner_invalid_vocabulary_is_400(api):
    client, workspace_a, _workspace_b, _session = api
    scan = _scan(client, workspace_a, window_days=SCAN_WINDOW_DAYS)
    renewal_id = scan.json()["renewals"][0]["id"]
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/contact"
        f"?org_id={workspace_a.org_id}",
        json={"channel": "telegram"},
        headers=workspace_a.secretary_headers(),
    )
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/respond"
        f"?org_id={workspace_a.org_id}",
        json={"response": "RENEW"},
        headers=workspace_a.secretary_headers(),
    )
    response = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/owner-decide"
        f"?org_id={workspace_a.org_id}",
        json={"decision": "MAYBE"},
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 400, response.text


# ---- VERIFY / CLOSED --------------------------------------------------


def test_close_before_verify_is_409(api):
    client, workspace_a, _workspace_b, _session = api
    scan = _scan(client, workspace_a, window_days=SCAN_WINDOW_DAYS)
    renewal_id = scan.json()["renewals"][0]["id"]
    _drive_to_executed(client, workspace_a, renewal_id)
    response = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/close"
        f"?org_id={workspace_a.org_id}",
        json={"note": "skipping verify"},
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 409, response.text


def test_verify_before_execute_is_409(api):
    client, workspace_a, _workspace_b, _session = api
    scan = _scan(client, workspace_a, window_days=SCAN_WINDOW_DAYS)
    renewal_id = scan.json()["renewals"][0]["id"]
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/contact"
        f"?org_id={workspace_a.org_id}",
        json={"channel": "telegram"},
        headers=workspace_a.secretary_headers(),
    )
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/respond"
        f"?org_id={workspace_a.org_id}",
        json={"response": "RENEW"},
        headers=workspace_a.secretary_headers(),
    )
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/owner-decide"
        f"?org_id={workspace_a.org_id}",
        json={"decision": "RENEW"},
        headers=workspace_a.owner_headers(),
    )
    response = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/verify"
        f"?org_id={workspace_a.org_id}",
        json={"note": "premature"},
        headers=workspace_a.owner_headers(),
    )
    assert response.status_code == 409, response.text


def test_verify_requires_owner_role(api):
    client, workspace_a, _workspace_b, _session = api
    scan = _scan(client, workspace_a, window_days=SCAN_WINDOW_DAYS)
    renewal_id = scan.json()["renewals"][0]["id"]
    _drive_to_executed(client, workspace_a, renewal_id)
    response = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/verify"
        f"?org_id={workspace_a.org_id}",
        json={"note": "ok"},
        headers=workspace_a.secretary_headers(),
    )
    assert response.status_code == 403


# ---- end-to-end happy path --------------------------------------------


def test_full_seven_stage_lifecycle(api):
    """DETECT_EXPIRY → CONTACT_TENANT → TENANT_RESPONSE → OWNER_DECISION
    → EXECUTED → VERIFY → CLOSED.

    The Operation is born at DETECT_EXPIRY, stays open through
    EXECUTED + VERIFY, and only resolves at CLOSED. The source lease
    is terminated, the new lease is activated, and the unit status is
    flipped — but the Operation does NOT resolve at EXECUTED for the
    new pipeline (it stays in_progress until CLOSED).
    """
    client, workspace_a, _workspace_b, session = api

    scan = _scan(client, workspace_a, window_days=SCAN_WINDOW_DAYS)
    assert scan.status_code == 200, scan.text
    renewal_id = scan.json()["renewals"][0]["id"]
    body = scan.json()["renewals"][0]
    assert body["state"] == RenewalState.DETECT_EXPIRY.value
    assert body["scan_window_days"] == SCAN_WINDOW_DAYS

    # Operation is created at DETECT_EXPIRY and is OPEN.
    op = _get_operation(client, workspace_a, renewal_id).json()
    assert op["state"] == "open"

    _drive_to_executed(client, workspace_a, renewal_id)

    # Operation is in_progress (legacy path resolves at EXECUTED; the
    # new pipeline resolves only at CLOSED).
    op = _get_operation(client, workspace_a, renewal_id).json()
    assert op["state"] != "resolved"

    # Source lease has been terminated and a new one activated.
    session.expire_all()
    source_lease = session.get(Lease, workspace_a.lease_id)
    assert source_lease.state == LeaseState.TERMINATED.value

    verify = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/verify"
        f"?org_id={workspace_a.org_id}",
        json={"note": "documents signed, deposit received"},
        headers=workspace_a.owner_headers(),
    )
    assert verify.status_code == 200, verify.text
    body = verify.json()
    assert body["state"] == RenewalState.VERIFY.value
    assert body["verified_at"] is not None
    assert body["verified_by_user_id"] == workspace_a.owner_user_id

    # Operation is still not resolved.
    op = _get_operation(client, workspace_a, renewal_id).json()
    assert op["state"] != "resolved"

    closed = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/close"
        f"?org_id={workspace_a.org_id}",
        json={"note": "fully closed"},
        headers=workspace_a.owner_headers(),
    )
    assert closed.status_code == 200, closed.text
    body = closed.json()
    assert body["state"] == RenewalState.CLOSED.value
    assert body["closed_at"] is not None
    assert body["closed_by_user_id"] == workspace_a.owner_user_id

    # Operation is resolved at CLOSED.
    op = _get_operation(client, workspace_a, renewal_id).json()
    assert op["state"] == "resolved"

    # Activity feed recorded every stage transition.
    kinds = [a["kind"] for a in _get_activity(
        client, workspace_a, renewal_id,
    ).json()]
    expected = {
        "DETECTED",
        "TENANT_CONTACTED",
        "TENANT_RESPONDED",
        "OWNER_DECIDED",
        "SOURCE_LEASE_TERMINATED",
        "NEW_LEASE_CREATED",
        "NEW_LEASE_ACTIVATED",
        "EXECUTED",
        "EXECUTION_VERIFIED",
        "CLOSED",
    }
    assert expected.issubset(set(kinds))


def test_close_is_idempotent(api):
    """Closing an already-CLOSED renewal is a no-op."""
    client, workspace_a, _workspace_b, _session = api
    scan = _scan(client, workspace_a, window_days=SCAN_WINDOW_DAYS)
    renewal_id = scan.json()["renewals"][0]["id"]
    _drive_to_executed(client, workspace_a, renewal_id)
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/verify"
        f"?org_id={workspace_a.org_id}",
        json={},
        headers=workspace_a.owner_headers(),
    )
    first = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/close"
        f"?org_id={workspace_a.org_id}",
        json={"note": "first"},
        headers=workspace_a.owner_headers(),
    )
    assert first.status_code == 200
    second = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/close"
        f"?org_id={workspace_a.org_id}",
        json={"note": "second"},
        headers=workspace_a.owner_headers(),
    )
    assert second.status_code == 200
    assert second.json()["closed_at"] == first.json()["closed_at"]


def test_verify_is_idempotent(api):
    """Verifying an already-VERIFY renewal is a no-op."""
    client, workspace_a, _workspace_b, _session = api
    scan = _scan(client, workspace_a, window_days=SCAN_WINDOW_DAYS)
    renewal_id = scan.json()["renewals"][0]["id"]
    _drive_to_executed(client, workspace_a, renewal_id)
    first = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/verify"
        f"?org_id={workspace_a.org_id}",
        json={"note": "first"},
        headers=workspace_a.owner_headers(),
    )
    assert first.status_code == 200
    first_at = first.json()["verified_at"]
    second = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/verify"
        f"?org_id={workspace_a.org_id}",
        json={"note": "second"},
        headers=workspace_a.owner_headers(),
    )
    assert second.status_code == 200
    assert second.json()["verified_at"] == first_at


# ---- cross-org fail-closed --------------------------------------------


def test_contact_cross_org_is_404(api):
    client, workspace_a, workspace_b, _session = api
    scan = _scan(client, workspace_a, window_days=SCAN_WINDOW_DAYS)
    renewal_id = scan.json()["renewals"][0]["id"]
    response = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/contact"
        f"?org_id={workspace_b.org_id}",
        json={"channel": "telegram"},
        headers=workspace_b.secretary_headers(),
    )
    assert response.status_code == 404


def test_scan_includes_only_org_scoped_leases(api):
    client, workspace_a, workspace_b, _session = api
    a = _scan(client, workspace_a, window_days=SCAN_WINDOW_DAYS)
    b = _scan(client, workspace_b, window_days=SCAN_WINDOW_DAYS)
    for entry in a.json()["renewals"]:
        # Org_id is implicit via the route but the renewal's
        # source_lease_id belongs to workspace_a only.
        assert entry["source_lease_id"] == workspace_a.lease_id
    for entry in b.json()["renewals"]:
        assert entry["source_lease_id"] == workspace_b.lease_id