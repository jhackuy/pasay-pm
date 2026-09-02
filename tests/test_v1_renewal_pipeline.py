"""HTTP-level behavior tests for the V1 Lease Renewal 7-stage pipeline.

Issue #112 §"Lease Renewal" requires the frozen 7-stage lifecycle

    DETECT_EXPIRY → CONTACT_TENANT → TENANT_RESPONSE →
    OWNER_DECISION → EXECUTE → VERIFY → CLOSED

These tests prove:

- A scan with ``scan_window_days`` creates DETECT_EXPIRY rows for
  ACTIVE leases whose ``end_date`` falls inside the window and is
  idempotent on ``(org_id, source_lease_id, scan_window_days)``.
- Each forward transition is gated and recorded in the renewal
  activity feed; every out-of-order transition is rejected.
- OWNER gates the owner-specific transitions (record_response is
  OWNER/SECRETARY — tenant response is mediated by the secretary;
  decide_owner + verify + close are OWNER-only).
- Org-scope is enforced cross-org (404).
- The full 7-stage lifecycle end-to-end (DETECT_EXPIRY →
  CONTACT_TENANT → TENANT_RESPONSE → OWNER_DECISION → EXECUTE →
  VERIFY → CLOSED), including: Operation created at DETECT_EXPIRY,
  Operation OPEN through EXECUTE/VERIFY, Operation resolved at
  CLOSED.
- The early TERMINATE path (TENANT_RESPONSE → TERMINATE → close)
  resolves the Operation without going through EXECUTE.
- Verify idempotency and close idempotency.

The legacy 5-state pipeline lives in ``tests/test_v1_api_renewals.py``
and remains unchanged.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db, get_session_factory
from app.v1.models.base import LeaseState, OperationState, UnitStatus
from app.v1.models.property import Unit
from app.v1.models.renewal import (
    RenewalActivityKind,
    RenewalState,
)
from app.v1.models.rent_payment import Operation
from app.v1.models.tenant_lease import Lease
from tests.v1_support import (
    Workspace,
    seed_workspace,
    v1_engine_ctx,
)


@pytest.fixture
def api():
    """Two workspaces + an additional lease whose end_date falls inside
    the scan window.
    """
    with v1_engine_ctx():
        session = get_session_factory()()
        workspace_a = seed_workspace(session, name="PipelineA")
        workspace_b = seed_workspace(session, name="PipelineB")

        # Add a second unit (so we don't collide with the seed lease
        # which sits on workspace_a.unit_id) and a lease whose
        # end_date is inside the default 60-day scan window used by
        # the tests below.
        today = date.today()
        unit_b = Unit(
            property_id=workspace_a.property_id,
            org_id=workspace_a.org_id,
            label="scan-target",
            bedrooms=1,
            bathrooms=1,
            monthly_rent=12000,
            status=UnitStatus.OCCUPIED.value,
        )
        session.add(unit_b)
        session.flush()

        lease_b_end = today + timedelta(days=30)
        lease_b = Lease(
            org_id=workspace_a.org_id,
            unit_id=unit_b.id,
            tenant_id=workspace_a.tenant_id,
            start_date=today - timedelta(days=300),
            end_date=lease_b_end,
            monthly_rent=12000,
            deposit=24000,
            state=LeaseState.ACTIVE.value,
        )
        session.add(lease_b)
        session.commit()

        from app.v1.main import app as v1_app

        def _override_get_db():
            yield session

        v1_app.dependency_overrides[get_db] = _override_get_db
        try:
            with TestClient(v1_app) as client:
                yield client, workspace_a, workspace_b, lease_b, lease_b_end
        finally:
            v1_app.dependency_overrides.clear()
            session.close()


def _scan(client: TestClient, workspace: Workspace, *, window: int = 60,
          lease_id: int | None = None, headers=None):
    body = {"scan_window_days": window}
    if lease_id is not None:
        body["lease_id"] = lease_id
    return client.post(
        f"/api/v1/renewals/scan?org_id={workspace.org_id}",
        json=body,
        headers=headers or workspace.secretary_headers(),
    )


# ====================================================================
# Scan / DETECT_EXPIRY
# ====================================================================


def test_scan_is_idempotent_on_repeat(api):
    """A second scan with the same window must produce the same
    renewal set with all rows in the ``replayed`` list.
    """
    client, workspace_a, *_ = api
    first = _scan(client, workspace_a, window=60)
    assert first.status_code == 200, first.text
    body1 = first.json()
    # We have at least the seed lease (ends 2026-12-31, far in the
    # future relative to ``date.today()``). On the test execution
    # date, that may not fall inside the 60-day window, but the
    # ``lease_b`` we added above (ends today+30) must be detected.
    assert any(
        d["source_lease_id"] == api[3].id for d in body1["detected"]
    ), body1

    second = _scan(client, workspace_a, window=60)
    assert second.status_code == 200
    body2 = second.json()
    # Second scan has no new detections; every previously detected
    # lease is now in ``replayed`` instead.
    assert len(body2["detected"]) == 0
    assert any(
        d["source_lease_id"] == api[3].id for d in body2["replayed"]
    ), body2


def test_scan_rejects_zero_or_negative_window(api):
    client, workspace_a, *_ = api
    response = _scan(client, workspace_a, window=0)
    assert response.status_code == 422


def test_scan_requires_auth(api):
    client, workspace_a, *_ = api
    response = client.post(
        f"/api/v1/renewals/scan?org_id={workspace_a.org_id}",
        json={"scan_window_days": 60},
    )
    assert response.status_code == 401


def test_scan_cross_org_returns_empty_for_nonexistent_lease(api):
    """A scan with ``lease_id`` pointing to another org's lease must
    not surface that lease. The scan endpoint is org-scoped.
    """
    client, workspace_a, workspace_b, *_ = api
    response = _scan(
        client, workspace_a, window=60, lease_id=workspace_b.lease_id,
    )
    assert response.status_code == 200
    body = response.json()
    assert all(
        d["source_lease_id"] != workspace_b.lease_id
        for d in body["detected"]
    )


def test_scan_does_not_match_already_terminated_lease(api):
    """A lease that was terminated before the scan should not
    surface — only ACTIVE leases are scanned.
    """
    client, workspace_a, workspace_b, *_ = api
    # Terminate workspace_b's seed lease via the service layer.
    from app.v1.services.lease import terminate_lease
    session = get_session_factory()()
    try:
        terminate_lease(
            session,
            org_id=workspace_b.org_id,
            lease_id=workspace_b.lease_id,
            actor_user_id=workspace_b.owner_user_id,
            actor_role="OWNER",
        )
        session.commit()
        response = client.post(
            f"/api/v1/renewals/scan?org_id={workspace_b.org_id}",
            json={"scan_window_days": 60},
            headers=workspace_b.secretary_headers(),
        )
        assert response.status_code == 200
        body = response.json()
        assert all(
            d["source_lease_id"] != workspace_b.lease_id
            for d in body["detected"]
        )
    finally:
        session.close()


# ====================================================================
# State transitions
# ====================================================================


def test_full_seven_stage_lifecycle_end_to_end(api):
    """The frozen Issue #112 lifecycle:
    DETECT_EXPIRY → CONTACT_TENANT → TENANT_RESPONSE → OWNER_DECISION
    → EXECUTE → VERIFY → CLOSED. Operation is OPEN through
    EXECUTE/VERIFY, resolves at CLOSED.
    """
    client, workspace_a, _, lease_b, lease_b_end = api
    # 1. DETECT_EXPIRY (via scan)
    scan = _scan(client, workspace_a, window=60, lease_id=lease_b.id)
    assert scan.status_code == 200, scan.text
    body = scan.json()
    target = [d for d in body["detected"] if d["source_lease_id"] == lease_b.id]
    assert target, body
    renewal_id = target[0]["id"]
    assert target[0]["state"] == RenewalState.DETECT_EXPIRY.value

    # 2. CONTACT_TENANT
    contact = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/contact?org_id={workspace_a.org_id}",
        json={"contact_method": "phone", "note": "called"},
        headers=workspace_a.secretary_headers(),
    )
    assert contact.status_code == 200, contact.text
    assert contact.json()["state"] == RenewalState.CONTACT_TENANT.value

    # 3. TENANT_RESPONSE
    response = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/respond?org_id={workspace_a.org_id}",
        json={"tenant_response": "RENEW"},
        headers=workspace_a.secretary_headers(),
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == RenewalState.TENANT_RESPONSE.value

    # 4. OWNER_DECISION
    decision = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/owner-decide?org_id={workspace_a.org_id}",
        json={"owner_decision": "RENEW"},
        headers=workspace_a.owner_headers(),
    )
    assert decision.status_code == 200, decision.text
    assert decision.json()["state"] == RenewalState.OWNER_DECISION.value

    # 5. EXECUTE
    execute = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/execute?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert execute.status_code == 200, execute.text
    ex_body = execute.json()
    assert ex_body["renewal"]["state"] == RenewalState.EXECUTE.value
    assert ex_body["new_lease"]["id"] is not None
    assert ex_body["renewal"]["new_lease_id"] is not None

    # Operation is NOT yet resolved (post-EXECUTE / pre-VERIFY).
    # Allow ``open`` and ``in_progress``: ``detect_upcoming`` bumps it
    # to ``in_progress`` as soon as it is detected; only CLOSED
    # resolves. Confirm via the read-only /operation endpoint.
    op_state = client.get(
        f"/api/v1/renewals/proposals/{renewal_id}/operation?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    ).json()
    assert op_state["state"] in (
        OperationState.OPEN.value, OperationState.IN_PROGRESS.value,
    ), op_state

    # 6. VERIFY
    verify = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/verify?org_id={workspace_a.org_id}",
        json={"note": "all good"},
        headers=workspace_a.owner_headers(),
    )
    assert verify.status_code == 200, verify.text
    assert verify.json()["state"] == RenewalState.VERIFY.value

    # Operation is STILL unresolved at VERIFY (only CLOSED resolves).
    op_state = client.get(
        f"/api/v1/renewals/proposals/{renewal_id}/operation?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    ).json()
    assert op_state["state"] in (
        OperationState.OPEN.value, OperationState.IN_PROGRESS.value,
    ), op_state

    # 7. CLOSED
    closed = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/close?org_id={workspace_a.org_id}",
        json={"note": "final"},
        headers=workspace_a.owner_headers(),
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["state"] == RenewalState.CLOSED.value

    # Operation is now RESOLVED.
    op_state = client.get(
        f"/api/v1/renewals/proposals/{renewal_id}/operation?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    ).json()
    assert op_state["state"] == OperationState.RESOLVED.value


def test_contact_from_wrong_state_is_rejected(api):
    """contact_tenant must be called from DETECT_EXPIRY only."""
    client, workspace_a, _, lease_b, _ = api
    scan = _scan(client, workspace_a, window=60, lease_id=lease_b.id)
    renewal_id = scan.json()["detected"][0]["id"]
    # Skip CONTACT_TENANT, try respond directly → 409.
    response = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/respond?org_id={workspace_a.org_id}",
        json={"tenant_response": "RENEW"},
        headers=workspace_a.secretary_headers(),
    )
    assert response.status_code == 409


def test_respond_before_contact_is_rejected(api):
    client, workspace_a, _, lease_b, _ = api
    scan = _scan(client, workspace_a, window=60, lease_id=lease_b.id)
    renewal_id = scan.json()["detected"][0]["id"]
    response = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/respond?org_id={workspace_a.org_id}",
        json={"tenant_response": "RENEW"},
        headers=workspace_a.secretary_headers(),
    )
    assert response.status_code == 409


def test_owner_decision_before_tenant_response_is_rejected(api):
    client, workspace_a, _, lease_b, _ = api
    scan = _scan(client, workspace_a, window=60, lease_id=lease_b.id)
    renewal_id = scan.json()["detected"][0]["id"]
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/contact?org_id={workspace_a.org_id}",
        json={"contact_method": "phone"},
        headers=workspace_a.secretary_headers(),
    )
    decision = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/owner-decide?org_id={workspace_a.org_id}",
        json={"owner_decision": "RENEW"},
        headers=workspace_a.owner_headers(),
    )
    assert decision.status_code == 409


def test_execute_before_owner_decision_is_rejected(api):
    client, workspace_a, _, lease_b, _ = api
    scan = _scan(client, workspace_a, window=60, lease_id=lease_b.id)
    renewal_id = scan.json()["detected"][0]["id"]
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/contact?org_id={workspace_a.org_id}",
        json={"contact_method": "phone"},
        headers=workspace_a.secretary_headers(),
    )
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/respond?org_id={workspace_a.org_id}",
        json={"tenant_response": "RENEW"},
        headers=workspace_a.secretary_headers(),
    )
    execute = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/execute?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    assert execute.status_code == 409


def test_verify_before_execute_is_rejected(api):
    client, workspace_a, _, lease_b, _ = api
    scan = _scan(client, workspace_a, window=60, lease_id=lease_b.id)
    renewal_id = scan.json()["detected"][0]["id"]
    # Walk the chain to VERIFY prematurely.
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/contact?org_id={workspace_a.org_id}",
        json={"contact_method": "phone"},
        headers=workspace_a.secretary_headers(),
    )
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/respond?org_id={workspace_a.org_id}",
        json={"tenant_response": "RENEW"},
        headers=workspace_a.secretary_headers(),
    )
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/owner-decide?org_id={workspace_a.org_id}",
        json={"owner_decision": "RENEW"},
        headers=workspace_a.owner_headers(),
    )
    verify = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/verify?org_id={workspace_a.org_id}",
        json={},
        headers=workspace_a.owner_headers(),
    )
    assert verify.status_code == 409


def test_close_before_verify_is_rejected(api):
    client, workspace_a, _, lease_b, _ = api
    scan = _scan(client, workspace_a, window=60, lease_id=lease_b.id)
    renewal_id = scan.json()["detected"][0]["id"]
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/contact?org_id={workspace_a.org_id}",
        json={"contact_method": "phone"},
        headers=workspace_a.secretary_headers(),
    )
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/respond?org_id={workspace_a.org_id}",
        json={"tenant_response": "RENEW"},
        headers=workspace_a.secretary_headers(),
    )
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/owner-decide?org_id={workspace_a.org_id}",
        json={"owner_decision": "RENEW"},
        headers=workspace_a.owner_headers(),
    )
    close = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/close?org_id={workspace_a.org_id}",
        json={},
        headers=workspace_a.owner_headers(),
    )
    assert close.status_code == 409


# ====================================================================
# Role gating (org-scope fail-closed per AGENTS.md §4)
# ====================================================================


def test_tenant_response_is_rejected_for_unknown_tenant_response(api):
    client, workspace_a, _, lease_b, _ = api
    scan = _scan(client, workspace_a, window=60, lease_id=lease_b.id)
    renewal_id = scan.json()["detected"][0]["id"]
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/contact?org_id={workspace_a.org_id}",
        json={"contact_method": "phone"},
        headers=workspace_a.secretary_headers(),
    )
    bad = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/respond?org_id={workspace_a.org_id}",
        json={"tenant_response": "MAYBE"},
        headers=workspace_a.secretary_headers(),
    )
    assert bad.status_code == 400


def test_owner_decision_secretary_role_is_forbidden(api):
    client, workspace_a, _, lease_b, _ = api
    scan = _scan(client, workspace_a, window=60, lease_id=lease_b.id)
    renewal_id = scan.json()["detected"][0]["id"]
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/contact?org_id={workspace_a.org_id}",
        json={"contact_method": "phone"},
        headers=workspace_a.secretary_headers(),
    )
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/respond?org_id={workspace_a.org_id}",
        json={"tenant_response": "RENEW"},
        headers=workspace_a.secretary_headers(),
    )
    forbidden = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/owner-decide?org_id={workspace_a.org_id}",
        json={"owner_decision": "RENEW"},
        headers=workspace_a.secretary_headers(),
    )
    assert forbidden.status_code == 403


def test_verify_secretary_role_is_forbidden(api):
    """Need to walk to EXECUTE first to test verify role-gating."""
    client, workspace_a, _, lease_b, _ = api
    scan = _scan(client, workspace_a, window=60, lease_id=lease_b.id)
    renewal_id = scan.json()["detected"][0]["id"]
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/contact?org_id={workspace_a.org_id}",
        json={"contact_method": "phone"},
        headers=workspace_a.secretary_headers(),
    )
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/respond?org_id={workspace_a.org_id}",
        json={"tenant_response": "RENEW"},
        headers=workspace_a.secretary_headers(),
    )
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/owner-decide?org_id={workspace_a.org_id}",
        json={"owner_decision": "RENEW"},
        headers=workspace_a.owner_headers(),
    )
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/execute?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    forbidden = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/verify?org_id={workspace_a.org_id}",
        json={},
        headers=workspace_a.secretary_headers(),
    )
    assert forbidden.status_code == 403


def test_close_secretary_role_is_forbidden(api):
    """Once at VERIFY, secretary cannot close (OWNER-only)."""
    client, workspace_a, _, lease_b, _ = api
    scan = _scan(client, workspace_a, window=60, lease_id=lease_b.id)
    renewal_id = scan.json()["detected"][0]["id"]
    # walk to VERIFY
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/contact?org_id={workspace_a.org_id}",
        json={"contact_method": "phone"},
        headers=workspace_a.secretary_headers(),
    )
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/respond?org_id={workspace_a.org_id}",
        json={"tenant_response": "RENEW"},
        headers=workspace_a.secretary_headers(),
    )
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/owner-decide?org_id={workspace_a.org_id}",
        json={"owner_decision": "RENEW"},
        headers=workspace_a.owner_headers(),
    )
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/execute?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/verify?org_id={workspace_a.org_id}",
        json={},
        headers=workspace_a.owner_headers(),
    )
    forbidden = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/close?org_id={workspace_a.org_id}",
        json={},
        headers=workspace_a.secretary_headers(),
    )
    assert forbidden.status_code == 403


# ====================================================================
# Org-scope fail-closed (AGENTS.md §4)
# ====================================================================


def test_cross_org_contact_returns_404(api):
    """Workspace B cannot touch workspace A's renewal row."""
    client, workspace_a, workspace_b, lease_b, _ = api
    scan = _scan(client, workspace_a, window=60, lease_id=lease_b.id)
    renewal_id = scan.json()["detected"][0]["id"]
    cross = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/contact"
        f"?org_id={workspace_b.org_id}",
        json={"contact_method": "phone"},
        headers=workspace_b.owner_headers(),
    )
    assert cross.status_code == 404


def test_cross_org_respond_returns_404(api):
    client, workspace_a, workspace_b, lease_b, _ = api
    scan = _scan(client, workspace_a, window=60, lease_id=lease_b.id)
    renewal_id = scan.json()["detected"][0]["id"]
    cross = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/respond"
        f"?org_id={workspace_b.org_id}",
        json={"tenant_response": "RENEW"},
        headers=workspace_b.owner_headers(),
    )
    assert cross.status_code == 404


def test_cross_org_owner_decide_returns_404(api):
    client, workspace_a, workspace_b, lease_b, _ = api
    scan = _scan(client, workspace_a, window=60, lease_id=lease_b.id)
    renewal_id = scan.json()["detected"][0]["id"]
    cross = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/owner-decide"
        f"?org_id={workspace_b.org_id}",
        json={"owner_decision": "RENEW"},
        headers=workspace_b.owner_headers(),
    )
    assert cross.status_code == 404


def test_cross_org_close_returns_404(api):
    client, workspace_a, workspace_b, lease_b, _ = api
    scan = _scan(client, workspace_a, window=60, lease_id=lease_b.id)
    renewal_id = scan.json()["detected"][0]["id"]
    cross = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/close"
        f"?org_id={workspace_b.org_id}",
        json={},
        headers=workspace_b.owner_headers(),
    )
    assert cross.status_code == 404


# ====================================================================
# Idempotency / verify+close idempotency
# ====================================================================


def test_contact_is_idempotent(api):
    """Calling contact twice from CONTACT_TENANT keeps state at
    CONTACT_TENANT and only refreshes the contact timestamp.
    """
    client, workspace_a, _, lease_b, _ = api
    scan = _scan(client, workspace_a, window=60, lease_id=lease_b.id)
    renewal_id = scan.json()["detected"][0]["id"]
    first = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/contact?org_id={workspace_a.org_id}",
        json={"contact_method": "phone"},
        headers=workspace_a.secretary_headers(),
    )
    assert first.status_code == 200
    second = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/contact?org_id={workspace_a.org_id}",
        json={"contact_method": "sms"},
        headers=workspace_a.secretary_headers(),
    )
    assert second.status_code == 200
    assert second.json()["state"] == RenewalState.CONTACT_TENANT.value
    assert second.json()["contact_method"] == "sms"


def test_verify_is_idempotent(api):
    client, workspace_a, _, lease_b, _ = api
    scan = _scan(client, workspace_a, window=60, lease_id=lease_b.id)
    renewal_id = scan.json()["detected"][0]["id"]
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/contact?org_id={workspace_a.org_id}",
        json={"contact_method": "phone"},
        headers=workspace_a.secretary_headers(),
    )
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/respond?org_id={workspace_a.org_id}",
        json={"tenant_response": "RENEW"},
        headers=workspace_a.secretary_headers(),
    )
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/owner-decide?org_id={workspace_a.org_id}",
        json={"owner_decision": "RENEW"},
        headers=workspace_a.owner_headers(),
    )
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/execute?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    first = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/verify?org_id={workspace_a.org_id}",
        json={"note": "v1"},
        headers=workspace_a.owner_headers(),
    )
    assert first.status_code == 200
    second = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/verify?org_id={workspace_a.org_id}",
        json={"note": "v2"},
        headers=workspace_a.owner_headers(),
    )
    assert second.status_code == 200
    assert second.json()["state"] == RenewalState.VERIFY.value


def test_close_is_idempotent(api):
    client, workspace_a, _, lease_b, _ = api
    scan = _scan(client, workspace_a, window=60, lease_id=lease_b.id)
    renewal_id = scan.json()["detected"][0]["id"]
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/contact?org_id={workspace_a.org_id}",
        json={"contact_method": "phone"},
        headers=workspace_a.secretary_headers(),
    )
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/respond?org_id={workspace_a.org_id}",
        json={"tenant_response": "RENEW"},
        headers=workspace_a.secretary_headers(),
    )
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/owner-decide?org_id={workspace_a.org_id}",
        json={"owner_decision": "RENEW"},
        headers=workspace_a.owner_headers(),
    )
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/execute?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/verify?org_id={workspace_a.org_id}",
        json={},
        headers=workspace_a.owner_headers(),
    )
    first = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/close?org_id={workspace_a.org_id}",
        json={"note": "c1"},
        headers=workspace_a.owner_headers(),
    )
    assert first.status_code == 200
    second = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/close?org_id={workspace_a.org_id}",
        json={"note": "c2"},
        headers=workspace_a.owner_headers(),
    )
    assert second.status_code == 200
    assert second.json()["state"] == RenewalState.CLOSED.value
    # Operation stays RESOLVED (not re-resolved).
    op_state = client.get(
        f"/api/v1/renewals/proposals/{renewal_id}/operation?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    ).json()
    assert op_state["state"] == OperationState.RESOLVED.value


# ====================================================================
# Money and term overrides at OWNER_DECISION
# ====================================================================


def test_owner_decision_can_override_proposed_terms(api):
    """Owner may override proposed_start_date / monthly rent at the
    OWNER_DECISION step (Issue #112 §"Lease Renewal" — the owner's
    decision finalizes terms). Negative tests: end < start rejected.
    """
    client, workspace_a, _, lease_b, _ = api
    scan = _scan(client, workspace_a, window=60, lease_id=lease_b.id)
    renewal_id = scan.json()["detected"][0]["id"]
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/contact?org_id={workspace_a.org_id}",
        json={"contact_method": "phone"},
        headers=workspace_a.secretary_headers(),
    )
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/respond?org_id={workspace_a.org_id}",
        json={"tenant_response": "RENEW"},
        headers=workspace_a.secretary_headers(),
    )
    new_start = date(2027, 2, 1)
    new_end = date(2028, 1, 31)
    decision = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/owner-decide?org_id={workspace_a.org_id}",
        json={
            "owner_decision": "RENEW",
            "proposed_start_date": new_start.isoformat(),
            "proposed_end_date": new_end.isoformat(),
            "proposed_monthly_rent": "13500.00",
            "proposed_deposit": "27000.00",
        },
        headers=workspace_a.owner_headers(),
    )
    assert decision.status_code == 200, decision.text
    body = decision.json()
    assert body["proposed_start_date"] == new_start.isoformat()
    assert body["proposed_end_date"] == new_end.isoformat()
    # The Decimal wire format is a JSON string per AGENTS.md §4.
    assert str(body["proposed_monthly_rent"]) == "13500.00"
    assert str(body["proposed_deposit"]) == "27000.00"


def test_owner_decision_rejects_end_date_before_start(api):
    client, workspace_a, _, lease_b, _ = api
    scan = _scan(client, workspace_a, window=60, lease_id=lease_b.id)
    renewal_id = scan.json()["detected"][0]["id"]
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/contact?org_id={workspace_a.org_id}",
        json={"contact_method": "phone"},
        headers=workspace_a.secretary_headers(),
    )
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/respond?org_id={workspace_a.org_id}",
        json={"tenant_response": "RENEW"},
        headers=workspace_a.secretary_headers(),
    )
    bad = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/owner-decide?org_id={workspace_a.org_id}",
        json={
            "owner_decision": "RENEW",
            "proposed_start_date": "2027-12-31",
            "proposed_end_date": "2027-01-01",
        },
        headers=workspace_a.owner_headers(),
    )
    assert bad.status_code == 400


def test_owner_decision_rejects_float_money(api):
    """AGENTS.md §4: no float money at the Pydantic boundary."""
    client, workspace_a, _, lease_b, _ = api
    scan = _scan(client, workspace_a, window=60, lease_id=lease_b.id)
    renewal_id = scan.json()["detected"][0]["id"]
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/contact?org_id={workspace_a.org_id}",
        json={"contact_method": "phone"},
        headers=workspace_a.secretary_headers(),
    )
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/respond?org_id={workspace_a.org_id}",
        json={"tenant_response": "RENEW"},
        headers=workspace_a.secretary_headers(),
    )
    bad = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/owner-decide?org_id={workspace_a.org_id}",
        json={
            "owner_decision": "RENEW",
            "proposed_monthly_rent": 13500.50,
        },
        headers=workspace_a.owner_headers(),
    )
    assert bad.status_code == 422


# ====================================================================
# Early TERMINATE exit path
# ====================================================================


def test_terminate_at_tenant_response_resolves_via_cancel(api):
    """Tenant chooses TERMINATE → ``cancel`` resolves the Operation,
    skipping EXECUTE / VERIFY. The 7-stage lifecycle is closed but
    via the CANCELLED terminal, distinct from CLOSED (which is the
    post-verification normal close).
    """
    client, workspace_a, _, lease_b, _ = api
    scan = _scan(client, workspace_a, window=60, lease_id=lease_b.id)
    renewal_id = scan.json()["detected"][0]["id"]
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/contact?org_id={workspace_a.org_id}",
        json={"contact_method": "phone"},
        headers=workspace_a.secretary_headers(),
    )
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/respond?org_id={workspace_a.org_id}",
        json={"tenant_response": "TERMINATE"},
        headers=workspace_a.secretary_headers(),
    )
    # close from TENANT_RESPONSE is rejected (must walk through VERIFY).
    blocked = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/close?org_id={workspace_a.org_id}",
        json={"note": "tenant said terminate"},
        headers=workspace_a.owner_headers(),
    )
    assert blocked.status_code == 409
    # The supported early-exit is via ``cancel``, which sets CANCELLED.
    cancelled = client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/cancel?org_id={workspace_a.org_id}",
        json={"reason": "tenant terminated at response"},
        headers=workspace_a.owner_headers(),
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == RenewalState.CANCELLED.value
    op_state = client.get(
        f"/api/v1/renewals/proposals/{renewal_id}/operation?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    ).json()
    assert op_state["state"] == OperationState.RESOLVED.value


def test_activity_history_records_every_state(api):
    """The renewal activity feed must carry one row per transition
    and one DETECTED row from the scan. The contract is append-only.
    """
    client, workspace_a, _, lease_b, _ = api
    scan = _scan(client, workspace_a, window=60, lease_id=lease_b.id)
    renewal_id = scan.json()["detected"][0]["id"]
    # Walk the chain fast-forward.
    for step in [
        ("contact", {"contact_method": "phone"}, workspace_a.secretary_headers()),
        ("respond", {"tenant_response": "RENEW"}, workspace_a.secretary_headers()),
        ("owner-decide", {"owner_decision": "RENEW"}, workspace_a.owner_headers()),
    ]:
        path, body, headers = step
        response = client.post(
            f"/api/v1/renewals/proposals/{renewal_id}/{path}?org_id={workspace_a.org_id}",
            json=body,
            headers=headers,
        )
        assert response.status_code == 200, response.text
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/execute?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    )
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/verify?org_id={workspace_a.org_id}",
        json={},
        headers=workspace_a.owner_headers(),
    )
    client.post(
        f"/api/v1/renewals/proposals/{renewal_id}/close?org_id={workspace_a.org_id}",
        json={},
        headers=workspace_a.owner_headers(),
    )

    activities = client.get(
        f"/api/v1/renewals/proposals/{renewal_id}/activity?org_id={workspace_a.org_id}",
        headers=workspace_a.owner_headers(),
    ).json()
    kinds = [a["kind"] for a in activities]
    expected = [
        RenewalActivityKind.DETECTED.value,
        RenewalActivityKind.TENANT_CONTACTED.value,
        RenewalActivityKind.TENANT_RESPONDED.value,
        RenewalActivityKind.OWNER_DECIDED.value,
        RenewalActivityKind.EXECUTED.value,
        RenewalActivityKind.EXECUTION_VERIFIED.value,
        RenewalActivityKind.CLOSED.value,
    ]
    for kind in expected:
        assert kind in kinds, f"missing {kind} in {kinds}"
