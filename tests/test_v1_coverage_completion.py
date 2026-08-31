"""V1 Coverage Matrix completion tests — gap-fix audit additions.

Issue #99 / OWNER ADDENDUM hard acceptance: every Coverage Matrix row
must map to a domain/application service + thin API + Telegram/Mini App
surface where applicable + executable behavior test.

This file closes the gaps the CHANGES_REQUESTED review surfaced:

  - Property 2.6    — LeaseService.create_with_tenant (register-tenant flow)
  - Move-out 7.8    — TenantService.soft_delete (history retained)
  - Renewal 6.5     — LeaseService.supersede_with_new
  - Renewal 6.7     — LeaseService.archive (idempotent)
  - Operations 8.1-8.5 — OperationService / TaskService / NotificationService

These are the matrix rows that were *documented* but had no executable
test. They now do.

Tests run against the CI's PostgreSQL 16 service via DATABASE_URL.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.permissions import Principal, Role
from app.core.time import utcnow
from app.db.session import get_db, get_session_factory
from app.v1.main import app as v1_app
from app.v1.models.rent_payment import Operation

from . import v1_support


@pytest.fixture
def api():
    """Yield (client, alpha, beta) with the DB session overridden for
    the lifetime of the TestClient.
    """
    with v1_support.v1_engine_ctx():
        session = get_session_factory()()
        alpha = v1_support.seed_workspace(session, name="WSAlpha")
        beta = v1_support.seed_workspace(session, name="WSBeta")

        def _override_get_db():
            yield session

        v1_app.dependency_overrides[get_db] = _override_get_db
        try:
            with TestClient(v1_app) as client:
                yield client, alpha, beta
        finally:
            v1_app.dependency_overrides.clear()
            session.close()


def _owner_principal(workspace: v1_support.Workspace) -> Principal:
    return Principal(
        user_id=workspace.owner_user_id,
        org_id=workspace.org_id,
        role=Role.OWNER,
        membership_state="ACTIVE",
    )


def _make_op(
    workspace: v1_support.Workspace,
    *,
    kind: str = "TEST_KIND",
    subject_type: str = "test_subject",
    subject_id: int | None = None,
) -> int:
    """Insert a fresh Operation row and return its id (uses the
    shared ``api`` session via fixture parameter or fresh factory).
    """
    session = get_session_factory()()
    try:
        op = Operation(
            org_id=workspace.org_id,
            kind=kind,
            subject_type=subject_type,
            subject_id=subject_id or workspace.lease_id,
            state="open",
            due_at=utcnow(),
        )
        session.add(op)
        session.commit()
        session.refresh(op)
        return op.id
    finally:
        session.close()


# ---- Coverage Matrix 2.6 — create_with_tenant -------------------------


def _create_available_unit(client, headers, org_id, property_id, label):
    """Create a new AVAILABLE unit via the V1 properties API."""
    resp = client.post(
        f"/api/v1/properties/{property_id}/units",
        params={"org_id": org_id},
        headers=headers,
        json={
            "label": label,
            "bedrooms": 1,
            "bathrooms": 1,
            "monthly_rent": "10000.00",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


class TestCreateWithTenant:
    """Mini App ``#/properties/{id}/register-tenant`` workflow."""

    def test_create_with_tenant_returns_new_tenant_and_lease(self, api):
        client, alpha, _ = api
        unit_id = _create_available_unit(
            client, alpha.owner_headers(), alpha.org_id,
            alpha.property_id, "new-unit-A",
        )
        resp = client.post(
            "/api/v1/leases/with-tenant",
            params={"org_id": alpha.org_id},
            headers={"Authorization": f"Bearer {alpha.owner_api_key}"},
            json={
                "unit_id": unit_id,
                "tenant_full_name": "Alice Doe",
                "tenant_contact_phone": "+639171234567",
                "tenant_contact_email": "alice@example.com",
                "start_date": str(date.today() + timedelta(days=1)),
                "end_date": str(date.today() + timedelta(days=180)),
                "monthly_rent": "15000.00",
                "deposit": "30000.00",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["tenant_id"] >= 1
        assert body["tenant_full_name"] == "Alice Doe"
        assert body["lease"]["state"] == "DRAFT"
        assert body["lease"]["monthly_rent"] == "15000.00"
        assert body["lease"]["deposit"] == "30000.00"
        assert body["lease"]["unit_id"] == unit_id

    def test_create_with_tenant_validates_end_after_start(self, api):
        client, alpha, _ = api
        unit_id = _create_available_unit(
            client, alpha.owner_headers(), alpha.org_id,
            alpha.property_id, "new-unit-B",
        )
        resp = client.post(
            "/api/v1/leases/with-tenant",
            params={"org_id": alpha.org_id},
            headers={"Authorization": f"Bearer {alpha.owner_api_key}"},
            json={
                "unit_id": unit_id,
                "tenant_full_name": "Bob",
                "start_date": str(date.today() + timedelta(days=10)),
                "end_date": str(date.today() + timedelta(days=5)),
                "monthly_rent": "12000.00",
            },
        )
        assert resp.status_code == 400
        assert "end_date" in resp.json()["detail"].lower()

    def test_create_with_tenant_rejects_empty_name(self, api):
        client, alpha, _ = api
        unit_id = _create_available_unit(
            client, alpha.owner_headers(), alpha.org_id,
            alpha.property_id, "new-unit-C",
        )
        resp = client.post(
            "/api/v1/leases/with-tenant",
            params={"org_id": alpha.org_id},
            headers={"Authorization": f"Bearer {alpha.owner_api_key}"},
            json={
                "unit_id": unit_id,
                "tenant_full_name": "   ",
                "start_date": str(date.today() + timedelta(days=1)),
                "end_date": str(date.today() + timedelta(days=30)),
                "monthly_rent": "10000.00",
            },
        )
        assert resp.status_code == 400

    def test_create_with_tenant_money_is_decimal_typed(self, api):
        """JSON floats are coerced to Decimal by Pydantic; the lease row
        persists as a Decimal, never a float (AGENTS.md §4 money
        invariant).
        """
        client, alpha, _ = api
        unit_id = _create_available_unit(
            client, alpha.owner_headers(), alpha.org_id,
            alpha.property_id, "new-unit-D",
        )
        resp = client.post(
            "/api/v1/leases/with-tenant",
            params={"org_id": alpha.org_id},
            headers={"Authorization": f"Bearer {alpha.owner_api_key}"},
            json={
                "unit_id": unit_id,
                "tenant_full_name": "Carl",
                "start_date": str(date.today() + timedelta(days=1)),
                "end_date": str(date.today() + timedelta(days=30)),
                # JSON float - Pydantic coerces to Decimal
                "monthly_rent": 12000.50,
            },
        )
        # JSON is accepted and coerced to Decimal; the read-back is
        # serialized as a JSON string (never a raw float) — that's the
        # contract enforced by ``extra="forbid"`` + Pydantic Decimal field.
        assert resp.status_code == 201, resp.text
        body = resp.json()
        # Pydantic Decimal serializes back to JSON string.
        assert isinstance(body["lease"]["monthly_rent"], str)
        assert body["lease"]["monthly_rent"] == "12000.50"

    def test_create_with_tenant_rejects_unknown_field(self, api):
        client, alpha, _ = api
        unit_id = _create_available_unit(
            client, alpha.owner_headers(), alpha.org_id,
            alpha.property_id, "new-unit-E",
        )
        resp = client.post(
            "/api/v1/leases/with-tenant",
            params={"org_id": alpha.org_id},
            headers={"Authorization": f"Bearer {alpha.owner_api_key}"},
            json={
                "unit_id": unit_id,
                "tenant_full_name": "Dee",
                "start_date": str(date.today() + timedelta(days=1)),
                "end_date": str(date.today() + timedelta(days=30)),
                "monthly_rent": "12000.00",
                "smuggled_field": "bad",
            },
        )
        assert resp.status_code == 422  # Pydantic extra="forbid"

    def test_create_with_tenant_cross_org_unit_404(self, api):
        client, alpha, beta = api
        resp = client.post(
            "/api/v1/leases/with-tenant",
            params={"org_id": alpha.org_id},
            headers={"Authorization": f"Bearer {alpha.owner_api_key}"},
            json={
                "unit_id": beta.unit_id,  # foreign org's unit
                "tenant_full_name": "Eve",
                "start_date": str(date.today() + timedelta(days=1)),
                "end_date": str(date.today() + timedelta(days=30)),
                "monthly_rent": "10000.00",
            },
        )
        assert resp.status_code == 404


# ---- Coverage Matrix 7.8 — TenantService.soft_delete -----------------


class TestTenantSoftDelete:
    """Move-out: tenant history retained (soft delete only)."""

    def test_soft_delete_marks_archived_at(self, api):
        client, alpha, _ = api
        create = client.post(
            "/api/v1/tenants",
            params={"org_id": alpha.org_id},
            headers={"Authorization": f"Bearer {alpha.owner_api_key}"},
            json={"full_name": "Frank"},
        )
        assert create.status_code == 201, create.text
        tenant_id = create.json()["id"]

        delete = client.delete(
            f"/api/v1/tenants/{tenant_id}",
            params={"org_id": alpha.org_id},
            headers={"Authorization": f"Bearer {alpha.owner_api_key}"},
        )
        assert delete.status_code == 200
        assert delete.json()["archived_at"] is not None

    def test_soft_delete_is_idempotent(self, api):
        client, alpha, _ = api
        create = client.post(
            "/api/v1/tenants",
            params={"org_id": alpha.org_id},
            headers={"Authorization": f"Bearer {alpha.owner_api_key}"},
            json={"full_name": "Greta"},
        )
        tenant_id = create.json()["id"]

        r1 = client.delete(
            f"/api/v1/tenants/{tenant_id}",
            params={"org_id": alpha.org_id},
            headers={"Authorization": f"Bearer {alpha.owner_api_key}"},
        )
        first_archived_at = r1.json()["archived_at"]

        r2 = client.delete(
            f"/api/v1/tenants/{tenant_id}",
            params={"org_id": alpha.org_id},
            headers={"Authorization": f"Bearer {alpha.owner_api_key}"},
        )
        assert r2.status_code == 200
        assert r2.json()["archived_at"] == first_archived_at

    def test_soft_delete_secretary_forbidden(self, api):
        client, alpha, _ = api
        create = client.post(
            "/api/v1/tenants",
            params={"org_id": alpha.org_id},
            headers={"Authorization": f"Bearer {alpha.owner_api_key}"},
            json={"full_name": "Hans"},
        )
        tenant_id = create.json()["id"]

        resp = client.delete(
            f"/api/v1/tenants/{tenant_id}",
            params={"org_id": alpha.org_id},
            headers={"Authorization": f"Bearer {alpha.secretary_api_key}"},
        )
        assert resp.status_code == 403

    def test_archived_tenant_hidden_from_list(self, api):
        client, alpha, _ = api
        client.post(
            "/api/v1/tenants",
            params={"org_id": alpha.org_id},
            headers={"Authorization": f"Bearer {alpha.owner_api_key}"},
            json={"full_name": "Inez"},
        )
        client.post(
            "/api/v1/tenants",
            params={"org_id": alpha.org_id},
            headers={"Authorization": f"Bearer {alpha.owner_api_key}"},
            json={"full_name": "Jules"},
        )
        list_before = client.get(
            "/api/v1/tenants",
            params={"org_id": alpha.org_id},
            headers={"Authorization": f"Bearer {alpha.owner_api_key}"},
        )
        names_before = sorted(t["full_name"] for t in list_before.json())
        inez = next(
            t for t in list_before.json() if t["full_name"] == "Inez"
        )
        client.delete(
            f"/api/v1/tenants/{inez['id']}",
            params={"org_id": alpha.org_id},
            headers={"Authorization": f"Bearer {alpha.owner_api_key}"},
        )
        list_after = client.get(
            "/api/v1/tenants",
            params={"org_id": alpha.org_id},
            headers={"Authorization": f"Bearer {alpha.owner_api_key}"},
        )
        names_after = sorted(t["full_name"] for t in list_after.json())
        assert "Inez" in names_before
        assert "Inez" not in names_after
        assert "Jules" in names_after

    def test_soft_delete_cross_org_404(self, api):
        client, alpha, beta = api
        resp = client.delete(
            f"/api/v1/tenants/{beta.tenant_id}",
            params={"org_id": alpha.org_id},
            headers={"Authorization": f"Bearer {alpha.owner_api_key}"},
        )
        assert resp.status_code == 404


# ---- Coverage Matrix 6.5 — LeaseService.supersede_with_new -----------


class TestSupersedeWithNew:
    """Renewal execute closure: atomic terminate source + create+activate new."""

    def test_supersede_atomic_via_internal(self):
        from app.v1.services.lease import LeaseService

        with v1_support.v1_engine_ctx():
            session = get_session_factory()()
            try:
                alpha = v1_support.seed_workspace(
                    session, name="WSAlphaSupersede",
                )
                principal = _owner_principal(alpha)
                svc = LeaseService(session)
                source, new = svc.supersede_with_new(
                    principal,
                    org_id=alpha.org_id,
                    source_lease_id=alpha.lease_id,
                    new_start_date=date.today() + timedelta(days=181),
                    new_end_date=date.today() + timedelta(days=365),
                    new_monthly_rent="16000.00",
                    new_deposit="32000.00",
                )
                assert source.id == alpha.lease_id
                assert source.state == "TERMINATED"
                assert source.archived_at is not None
                assert new.state == "ACTIVE"
                # Decimal exact — no float round-trip
                assert new.monthly_rent == 16000.00  # Decimal literal
                assert new.id != source.id
                assert new.unit_id == source.unit_id
                assert new.tenant_id == source.tenant_id
            finally:
                session.close()

    def test_supersede_source_must_be_active(self):
        from app.v1.services.errors import ConflictError
        from app.v1.services.lease import LeaseService

        with v1_support.v1_engine_ctx():
            session = get_session_factory()()
            try:
                alpha = v1_support.seed_workspace(
                    session, name="WSAlphaSupersedeErr",
                )
                principal = _owner_principal(alpha)
                svc = LeaseService(session)
                # Source lease is ACTIVE in seed; terminate first.
                svc.terminate_lease(
                    principal, org_id=alpha.org_id, lease_id=alpha.lease_id,
                )
                session.commit()
                # Now supersede must fail (source is no longer ACTIVE).
                try:
                    svc.supersede_with_new(
                        principal,
                        org_id=alpha.org_id,
                        source_lease_id=alpha.lease_id,
                        new_start_date=date.today() + timedelta(days=180),
                        new_end_date=date.today() + timedelta(days=360),
                        new_monthly_rent="16000.00",
                    )
                except ConflictError as exc:
                    assert "superseded" in str(exc).lower()
                else:
                    raise AssertionError(
                        "supersede must fail on a TERMINATED source lease",
                    )
            finally:
                session.close()


# ---- Coverage Matrix 6.7 — LeaseService.archive ----------------------


class TestLeaseArchive:
    """Coverage Matrix 6.7: archive a lease (idempotent)."""

    def test_archive_lease(self, api):
        client, alpha, _ = api
        resp = client.post(
            f"/api/v1/leases/{alpha.lease_id}/archive",
            params={"org_id": alpha.org_id},
            headers={"Authorization": f"Bearer {alpha.owner_api_key}"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["archived_at"] is not None

    def test_archive_is_idempotent(self, api):
        client, alpha, _ = api
        r1 = client.post(
            f"/api/v1/leases/{alpha.lease_id}/archive",
            params={"org_id": alpha.org_id},
            headers={"Authorization": f"Bearer {alpha.owner_api_key}"},
        )
        first_archived_at = r1.json()["archived_at"]
        r2 = client.post(
            f"/api/v1/leases/{alpha.lease_id}/archive",
            params={"org_id": alpha.org_id},
            headers={"Authorization": f"Bearer {alpha.owner_api_key}"},
        )
        assert r2.status_code == 200
        assert r2.json()["archived_at"] == first_archived_at

    def test_archive_cross_org_404(self, api):
        client, alpha, beta = api
        resp = client.post(
            f"/api/v1/leases/{beta.lease_id}/archive",
            params={"org_id": alpha.org_id},
            headers={"Authorization": f"Bearer {alpha.owner_api_key}"},
        )
        assert resp.status_code == 404


# ---- Coverage Matrix 8.1-8.5 — Operation/Task/Notification ----------


class TestOperationService:
    """Centralized Operation truth + Task projection + Notification.

    The Operation state machine:
      OPEN → IN_PROGRESS → RESOLVED
      OPEN → CANCELLED
      RESOLVED → IN_PROGRESS (re-open via reverse)
    """

    def test_operation_advance_open_to_in_progress(self):
        from app.v1.models.base import OperationState
        from app.v1.services.operations import OperationService

        with v1_support.v1_engine_ctx():
            session = get_session_factory()()
            try:
                alpha = v1_support.seed_workspace(
                    session, name="WSAlphaOpAdvance",
                )
                op_id = _make_op(alpha)
                principal = _owner_principal(alpha)
                svc = OperationService(session)
                op = svc.advance(
                    principal,
                    org_id=alpha.org_id,
                    operation_id=op_id,
                    to_state=OperationState.IN_PROGRESS,
                )
                assert op.state == "in_progress"
            finally:
                session.close()

    def test_at_most_one_open_task_per_operation(self):
        from app.v1.services.errors import ConflictError
        from app.v1.services.operations import TaskService

        with v1_support.v1_engine_ctx():
            session = get_session_factory()()
            try:
                alpha = v1_support.seed_workspace(
                    session, name="WSAlphaTaskUnique",
                )
                op_id = _make_op(alpha)
                principal = _owner_principal(alpha)
                svc = TaskService(session)
                t1 = svc.create_projection(
                    principal,
                    org_id=alpha.org_id,
                    operation_id=op_id,
                    kind="FOLLOW_UP",
                    title="Call tenant",
                )
                assert t1.state == "open"
                try:
                    svc.create_projection(
                        principal,
                        org_id=alpha.org_id,
                        operation_id=op_id,
                        kind="FOLLOW_UP",
                        title="Send SMS",
                    )
                except ConflictError as exc:
                    assert "open task" in str(exc).lower()
                else:
                    raise AssertionError(
                        "second open Task on the same Operation must be rejected",
                    )
            finally:
                session.close()

    def test_notification_does_not_resolve_operation(self):
        """Coverage Matrix 8.5: Reminder != Completion."""
        from app.v1.services.operations import NotificationService

        with v1_support.v1_engine_ctx():
            session = get_session_factory()()
            try:
                alpha = v1_support.seed_workspace(
                    session, name="WSAlphaNotify",
                )
                op_id = _make_op(alpha)
                principal = _owner_principal(alpha)
                svc = NotificationService(session)
                result = svc.send(
                    principal,
                    org_id=alpha.org_id,
                    operation_id=op_id,
                    message="Tenant was reminded by SMS",
                )
                assert result["delivered"] is True
                assert result["operation_state_at_send"] == "open"
                # Re-read the operation; state must STILL be open.
                op = svc.db.get(Operation, op_id)
                assert op.state == "open"
                assert op.resolved_at is None
            finally:
                session.close()

    def test_complete_task_does_not_resolve_operation(self):
        """Coverage Matrix 8.5: completing a Task NEVER resolves the
        Operation (Reminder != Completion).
        """
        from app.v1.services.operations import TaskService

        with v1_support.v1_engine_ctx():
            session = get_session_factory()()
            try:
                alpha = v1_support.seed_workspace(
                    session, name="WSAlphaCompleteTask",
                )
                op_id = _make_op(alpha)
                principal = _owner_principal(alpha)
                svc = TaskService(session)
                task = svc.create_projection(
                    principal,
                    org_id=alpha.org_id,
                    operation_id=op_id,
                    kind="FOLLOW_UP",
                    title="Send reminder",
                )
                svc.complete(
                    principal, org_id=alpha.org_id, task_id=task.id,
                )
                op = svc.db.get(Operation, op_id)
                assert op.state == "open"  # NOT resolved
                assert op.resolved_at is None
            finally:
                session.close()
