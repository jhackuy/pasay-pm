"""Behavior tests for the org-scoped top-level list routes that the Mini App
dashboard consumes.

The Mini App Home view calls four list endpoints in parallel
(/api/v1/rent/overdue, /api/v1/rent/claims, /api/v1/repairs, /api/v1/leases).
Three of those have always existed as canonical endpoints; this file locks
in the two aliases that were added so the dashboard renders without the
SPA-fallback index.html shim shadowing the response.

Regression contract:
- GET /api/v1/rent/claims?org_id=N returns a JSON array (not HTML SPA fallback).
- GET /api/v1/repairs?org_id=N returns a JSON array (not HTML SPA fallback).
- Both endpoints honor Bearer auth + org scope + role guard.
- Both endpoints isolate org data (cross-org ids are not visible).
- Both endpoints reflect newly-created rows.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db, get_session_factory
from app.v1.models.property import Property
from app.v1.models.tenant_lease import Tenant
from app.v1.services.rent_payment import RentPaymentService
from app.v1.services.repair import RepairService
from tests.v1_support import seed_workspace, v1_engine_ctx


@pytest.fixture
def api():
    with v1_engine_ctx():
        session = get_session_factory()()
        workspace_a = seed_workspace(session, name="TopListAlpha")
        workspace_b = seed_workspace(session, name="TopListBeta")

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


class TestTopLevelRentClaimsRoute:
    """GET /api/v1/rent/claims is the org-scoped list the Mini App dashboard calls."""

    def test_returns_json_array_not_spa_fallback(self, api):
        client, workspace_a, _workspace_b = api
        response = client.get(
            f"/api/v1/rent/claims?org_id={workspace_a.org_id}",
            headers=workspace_a.owner_headers(),
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        body = response.json()
        assert isinstance(body, list)

    def test_requires_bearer_token(self, api):
        client, workspace_a, _ = api
        response = client.get(f"/api/v1/rent/claims?org_id={workspace_a.org_id}")
        assert response.status_code == 401

    def test_refuses_cross_org_scope(self, api):
        client, _workspace_a, workspace_b = api
        response = client.get(
            f"/api/v1/rent/claims?org_id={workspace_b.org_id}",
            headers=_workspace_a.owner_headers(),
        )
        # Either 403 (org-scope mismatch) or 404 (cross-org principal lookup)
        # is acceptable per AGENTS.md §4 fail-closed contract; 200 is NOT.
        assert response.status_code in (403, 404)

    def test_reflects_newly_created_claim(self, api):
        client, workspace_a, _workspace_b = api
        # Create a due schedule and a claim through the service layer.
        svc = RentPaymentService(get_session_factory()())
        from datetime import date
        schedule = svc.create_due_schedule(
            workspace_a.owner,
            org_id=workspace_a.org_id,
            lease_id=workspace_a.lease_id,
            period_start=date(2026, 4, 1),
            due_date=date(2026, 4, 5),
            amount_due=Decimal(AMOUNT := "9000.00"),
        )
        svc.claim_payment(
            workspace_a.owner,
            org_id=workspace_a.org_id,
            due_schedule_id=schedule.id,
            claimed_amount=Decimal(AMOUNT),
            evidence=[],
            idempotency_key="toplist-claim-1",
        )
        response = client.get(
            f"/api/v1/rent/claims?org_id={workspace_a.org_id}",
            headers=workspace_a.owner_headers(),
        )
        assert response.status_code == 200
        body = response.json()
        assert len(body) >= 1
        claim = next(item for item in body if item["idempotency_key"] == "toplist-claim-1")
        assert claim["claimed_amount"] == AMOUNT
        assert claim["status"] == "PENDING"

    def test_status_filter_narrows_results(self, api):
        client, workspace_a, _workspace_b = api
        svc = RentPaymentService(get_session_factory()())
        from datetime import date
        schedule = svc.create_due_schedule(
            workspace_a.owner,
            org_id=workspace_a.org_id,
            lease_id=workspace_a.lease_id,
            period_start=date(2026, 5, 1),
            due_date=date(2026, 5, 5),
            amount_due=Decimal("6000.00"),
        )
        svc.claim_payment(
            workspace_a.owner,
            org_id=workspace_a.org_id,
            due_schedule_id=schedule.id,
            claimed_amount=Decimal("6000.00"),
            evidence=[],
            idempotency_key="toplist-claim-filter",
        )
        response = client.get(
            f"/api/v1/rent/claims?org_id={workspace_a.org_id}&status=PENDING",
            headers=workspace_a.owner_headers(),
        )
        assert response.status_code == 200
        for item in response.json():
            assert item["status"] == "PENDING"


class TestTopLevelRepairsRoute:
    """GET /api/v1/repairs is the org-scoped list the Mini App dashboard calls."""

    def test_returns_json_array_not_spa_fallback(self, api):
        client, workspace_a, _workspace_b = api
        response = client.get(
            f"/api/v1/repairs?org_id={workspace_a.org_id}",
            headers=workspace_a.owner_headers(),
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        body = response.json()
        assert isinstance(body, list)

    def test_requires_bearer_token(self, api):
        client, workspace_a, _ = api
        response = client.get(f"/api/v1/repairs?org_id={workspace_a.org_id}")
        assert response.status_code == 401

    def test_refuses_cross_org_scope(self, api):
        client, _workspace_a, workspace_b = api
        response = client.get(
            f"/api/v1/repairs?org_id={workspace_b.org_id}",
            headers=_workspace_a.owner_headers(),
        )
        assert response.status_code in (403, 404)

    def test_reflects_newly_opened_report(self, api):
        client, workspace_a, _workspace_b = api
        svc = RepairService(get_session_factory()())
        result = svc.open_report(
            workspace_a.owner,
            org_id=workspace_a.org_id,
            unit_id=workspace_a.unit_id,
            title="Leaking faucet",
            description="Kitchen sink drips continuously.",
            category="PLUMBING",
            severity="MEDIUM",
            linked_expense_payment_id=None,
            idempotency_key="toplist-repair-1",
        )
        response = client.get(
            f"/api/v1/repairs?org_id={workspace_a.org_id}",
            headers=workspace_a.owner_headers(),
        )
        assert response.status_code == 200
        body = response.json()
        assert any(item["id"] == result.report.id for item in body)
        mine = next(item for item in body if item["id"] == result.report.id)
        assert mine["title"] == "Leaking faucet"
        assert mine["state"] == "REPORTED"

    def test_state_filter_narrows_results(self, api):
        client, workspace_a, _workspace_b = api
        svc = RepairService(get_session_factory()())
        result = svc.open_report(
            workspace_a.owner,
            org_id=workspace_a.org_id,
            unit_id=workspace_a.unit_id,
            title="Broken window",
            description="Bedroom window shattered.",
            category="STRUCTURAL",
            severity="HIGH",
            linked_expense_payment_id=None,
            idempotency_key="toplist-repair-filter",
        )
        # Confirm the report is in REPORTED state.
        assert result.report.state == "REPORTED"
        response = client.get(
            f"/api/v1/repairs?org_id={workspace_a.org_id}&state=REPORTED",
            headers=workspace_a.owner_headers(),
        )
        assert response.status_code == 200
        for item in response.json():
            assert item["state"] == "REPORTED"
        assert any(item["id"] == result.report.id for item in response.json())
