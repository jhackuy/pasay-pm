"""HTTP-level tests for V1 dashboard + audit endpoints."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db, get_session_factory
from tests.v1_support import seed_workspace, v1_engine_ctx


@pytest.fixture
def api():
    with v1_engine_ctx():
        session = get_session_factory()()
        ws = seed_workspace(session, name="DashOrg")

        from app.v1.main import app as v1_app

        def _override_get_db():
            yield session

        v1_app.dependency_overrides[get_db] = _override_get_db
        try:
            with TestClient(v1_app) as client:
                yield client, ws
        finally:
            v1_app.dependency_overrides.clear()
            session.close()


def test_dashboard_home_returns_aggregates(api):
    client, ws = api
    resp = client.get(
        f"/api/v1/dashboard/home?org_id={ws.org_id}",
        headers=ws.owner_headers(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # All keys present and lists present.
    for key in (
        "open_operations", "overdue_rent_count", "overdue_rent",
        "open_repairs_count", "open_repairs",
        "pending_renewals_count", "pending_renewals",
        "open_move_outs_count", "open_move_outs",
        "pending_expense_claims_count", "pending_expense_claims",
        "open_tasks", "generated_at",
    ):
        assert key in body, f"missing key {key}"


def test_dashboard_home_cross_org_is_403(api):
    client, ws = api
    resp = client.get(
        f"/api/v1/dashboard/home?org_id={ws.org_id + 999}",
        headers=ws.owner_headers(),
    )
    assert resp.status_code in (403, 404)


def test_audit_timeline(api):
    client, ws = api
    resp = client.get(
        f"/api/v1/audit?org_id={ws.org_id}",
        headers=ws.owner_headers(),
    )
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), list)


def test_audit_timeline_limit(api):
    client, ws = api
    resp = client.get(
        f"/api/v1/audit?org_id={ws.org_id}&limit=5",
        headers=ws.owner_headers(),
    )
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
    assert len(resp.json()) <= 5


def test_audit_requires_auth(api):
    client, ws = api
    resp = client.get(f"/api/v1/audit?org_id={ws.org_id}")
    assert resp.status_code == 401
