"""V1 rewrite tests — Lease contact/follow-up state (Coverage Matrix Rent #2).

The legacy PASAY lease had a ``Lease.contact_status`` field updated by the
Telegram NL bridge ("Tenant replied" / "Wrong number") and by the Owner /
Secretary contact flow. The V1 rewrite preserves the same surface:

- ``Lease.contact_status`` column (PENDING / REPLIED / WRONG_NUMBER /
  DISCONNECTED / NO_ANSWER) with a DB CHECK constraint
- ``LeaseService.set_contact_status`` service method
- ``PATCH /api/v1/leases/{lease_id}/contact`` thin router endpoint
- Cross-org 404 (read) / 403 (write)
- Default value PENDING on lease creation
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_session_factory, reset_engine_cache
from app.v1.deps import get_db
from tests.v1_support import seed_workspace, v1_engine_ctx


@pytest.fixture
def api():
    """Yield (client, workspace_a, workspace_b) backed by the rewrite engine."""
    with v1_engine_ctx():
        reset_engine_cache()
        session = get_session_factory()()
        workspace_a = seed_workspace(session, name="ContactAlpha")
        workspace_b = seed_workspace(session, name="ContactBeta")

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


def test_lease_contact_status_defaults_to_pending(api):
    """A freshly seeded lease must have contact_status=PENDING."""
    client, ws, _ = api
    r = client.get(
        f"/api/v1/leases?org_id={ws.org_id}",
        headers={"Authorization": f"Bearer {ws.owner_api_key}"},
    )
    assert r.status_code == 200, r.text
    leases = r.json()
    assert leases, "expected at least one lease"
    assert all(l["contact_status"] == "PENDING" for l in leases)


def test_owner_can_update_lease_contact_status(api):
    """PATCH /api/v1/leases/{id}/contact updates the contact status."""
    client, ws, _ = api
    r = client.patch(
        f"/api/v1/leases/{ws.lease_id}/contact?org_id={ws.org_id}",
        headers={"Authorization": f"Bearer {ws.owner_api_key}"},
        json={"contact_status": "REPLIED"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["contact_status"] == "REPLIED"


def test_secretary_can_update_lease_contact_status(api):
    """SECRETARY may also update lease contact status."""
    client, ws, _ = api
    r = client.patch(
        f"/api/v1/leases/{ws.lease_id}/contact?org_id={ws.org_id}",
        headers={"Authorization": f"Bearer {ws.secretary_api_key}"},
        json={"contact_status": "WRONG_NUMBER"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["contact_status"] == "WRONG_NUMBER"


def test_unknown_contact_status_is_rejected(api):
    """Unknown contact_status value yields 400."""
    client, ws, _ = api
    r = client.patch(
        f"/api/v1/leases/{ws.lease_id}/contact?org_id={ws.org_id}",
        headers={"Authorization": f"Bearer {ws.owner_api_key}"},
        json={"contact_status": "NOT_A_REAL_STATE"},
    )
    assert r.status_code == 400, r.text


def test_unauthenticated_contact_update_is_rejected(api):
    """PATCH /contact without auth yields 401."""
    client, ws, _ = api
    r = client.patch(
        f"/api/v1/leases/{ws.lease_id}/contact?org_id={ws.org_id}",
        json={"contact_status": "REPLIED"},
    )
    assert r.status_code == 401, r.text


def test_cross_org_contact_update_is_404(api):
    """Cross-org contact update returns 404 (foreign lease not found)."""
    client, ws_alpha, ws_beta = api
    r = client.patch(
        f"/api/v1/leases/{ws_alpha.lease_id}/contact?org_id={ws_beta.org_id}",
        headers={"Authorization": f"Bearer {ws_beta.owner_api_key}"},
        json={"contact_status": "REPLIED"},
    )
    assert r.status_code == 404, r.text


def test_all_supported_contact_states_round_trip(api):
    """Every documented LeaseContactStatus value round-trips through the API."""
    client, ws, _ = api
    headers = {"Authorization": f"Bearer {ws.owner_api_key}"}
    for status in ("PENDING", "REPLIED", "WRONG_NUMBER", "DISCONNECTED", "NO_ANSWER"):
        r = client.patch(
            f"/api/v1/leases/{ws.lease_id}/contact?org_id={ws.org_id}",
            headers=headers,
            json={"contact_status": status},
        )
        assert r.status_code == 200, f"{status}: {r.text}"
        assert r.json()["contact_status"] == status