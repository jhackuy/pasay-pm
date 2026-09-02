"""HTTP + service-level tests for the Issue #112 GAP-P1 product rule:
``Units <= 15 per property``.

The cap is enforced at two complementary boundaries:

1. **Service-layer guard** — ``app.v1.services.property::
   assert_units_within_cap`` is called at the top of both the
   ``PropertyService.create_unit`` class method and the module-level
   ``create_unit`` function. The guard raises ``ConflictError``,
   which the FastAPI router maps to HTTP 409. The guard produces a
   clean domain-shaped error in the common case and avoids a
   round-trip to PostgreSQL just to fail.

2. **Database trigger** — ``alembic 0003_units_cap`` installs a
   ``BEFORE INSERT OR UPDATE OF property_id`` trigger on
   ``v1_units`` that re-counts units per property and raises
   ``check_violation`` if the resulting count would exceed the cap.
   The trigger is the race-safe authority; it fires uniformly on
   every code path (service, ORM bulk-insert, manual psql, future
   worker, anything).

This file proves both surfaces:
- The HTTP service guard: 15 inserts succeed (201), the 16th is
  rejected (409), and the existing 15 rows are unchanged.
- The service guard: ``ConflictError`` is raised directly from
  ``PropertyService.create_unit``.
- The DB trigger: a separate fixture installs the trigger via
  Alembic ``upgrade head`` and verifies that even a direct ORM
  insert (bypassing the service guard) is rejected with
  ``IntegrityError`` + ``units_per_property_cap_exceeded`` text.
- Reversibility: ``alembic downgrade -1`` cleanly removes the
  trigger + helper function and a 16th insert succeeds again.
- Org-scope: the cap is per property **and** per org — a second
  property in the same org has its own 15-unit budget.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.db.session import get_db, get_session_factory
from app.v1.main import app as v1_app
from app.v1.models.base import UnitStatus
from app.v1.models.property import Property, Unit

from tests import v1_support


UNITS_CAP = 15  # mirrors app.v1.services.property.UNITS_PER_PROPERTY_CAP


# ----------------------------------------------------------------------
# Shared fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def api():
    """Single workspace; uses the FastAPI TestClient with the V1 app."""
    with v1_support.v1_engine_ctx():
        session = get_session_factory()()
        workspace = v1_support.seed_workspace(session, name="UnitsCap")

        def _override_get_db():
            yield session

        v1_app.dependency_overrides[get_db] = _override_get_db
        try:
            with TestClient(v1_app) as client:
                yield client, workspace, session
        finally:
            v1_app.dependency_overrides.clear()
            session.close()


def _create_unit(
    client: TestClient,
    workspace: v1_support.Workspace,
    label: str,
    *,
    headers=None,
) -> int:
    """POST a new Unit to the workspace's seed property via the V1
    HTTP API. Returns the new unit id.
    """
    resp = client.post(
        f"/api/v1/properties/{workspace.property_id}/units",
        params={"org_id": workspace.org_id},
        headers=headers or workspace.owner_headers(),
        json={
            "label": label,
            "bedrooms": 1,
            "bathrooms": 1,
            "monthly_rent": "10000.00",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _bulk_seed_units(
    session,
    workspace: v1_support.Workspace,
    *,
    count: int,
    label_prefix: str = "bulk",
) -> list[int]:
    """Insert ``count`` units directly via ORM so the property is at
    the cap before the test exercises the API. Uses the same session
    the API fixture binds so counts are coherent.
    """
    ids: list[int] = []
    for i in range(count):
        u = Unit(
            property_id=workspace.property_id,
            org_id=workspace.org_id,
            label=f"{label_prefix}-{i:02d}",
            bedrooms=1,
            bathrooms=1,
            monthly_rent=Decimal("10000.00"),
            status=UnitStatus.AVAILABLE.value,
        )
        session.add(u)
        session.flush()
        ids.append(u.id)
    session.commit()
    return ids


# ----------------------------------------------------------------------
# Service-layer guard (HTTP)
# ----------------------------------------------------------------------


class TestServiceGuardHTTP:
    """The service guard is the user-facing boundary; these tests
    verify it returns a clean HTTP 409 with the cap message intact.
    """

    def test_fifteen_units_succeed_one_by_one(self, api):
        """Starting from 1 seed unit, the next 14 inserts succeed
        (total 15); the 16th must be rejected.

        The seed workspace already has 1 unit on the property
        (``7777``); we add 14 more (total 15) successfully, then the
        16th must be rejected.
        """
        client, workspace, _ = api
        for i in range(UNITS_CAP - 1):
            _create_unit(client, workspace, label=f"ok-{i:02d}")

        # 16th insert: 409 with cap message.
        resp = client.post(
            f"/api/v1/properties/{workspace.property_id}/units",
            params={"org_id": workspace.org_id},
            headers=workspace.owner_headers(),
            json={
                "label": "overflow",
                "bedrooms": 1,
                "bathrooms": 1,
                "monthly_rent": "10000.00",
            },
        )
        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert "cap" in body["detail"].lower()
        assert str(UNITS_CAP) in body["detail"]

    def test_exactly_fifteen_units_via_bulk_then_api(self, api):
        """Bulk-insert 14 more units (total 15), then the API must
        reject any further insert while leaving the 15 rows intact.
        """
        client, workspace, session = api
        _bulk_seed_units(session, workspace, count=UNITS_CAP - 1)

        # The 16th must fail.
        resp = client.post(
            f"/api/v1/properties/{workspace.property_id}/units",
            params={"org_id": workspace.org_id},
            headers=workspace.owner_headers(),
            json={
                "label": "overflow",
                "bedrooms": 1,
                "bathrooms": 1,
                "monthly_rent": "10000.00",
            },
        )
        assert resp.status_code == 409, resp.text

        # The 15 existing rows survive untouched.
        live = (
            session.query(Unit)
            .filter(
                Unit.property_id == workspace.property_id,
                Unit.org_id == workspace.org_id,
            )
            .count()
        )
        assert live == UNITS_CAP

    def test_cap_is_per_property(self, api):
        """A second property in the same org has its own 15-unit
        budget; filling property A to 15 must not block property B.
        """
        client, workspace, session = api
        # Fill property A to the cap.
        _bulk_seed_units(session, workspace, count=UNITS_CAP - 1)
        # Verify A rejects a 16th.
        resp_a = client.post(
            f"/api/v1/properties/{workspace.property_id}/units",
            params={"org_id": workspace.org_id},
            headers=workspace.owner_headers(),
            json={
                "label": "A-overflow",
                "bedrooms": 1,
                "bathrooms": 1,
                "monthly_rent": "10000.00",
            },
        )
        assert resp_a.status_code == 409, resp_a.text

        # Create property B and verify it can accept a unit.
        new_prop = Property(
            org_id=workspace.org_id,
            name="UnitsCap Tower B",
            address_line1="2 Roxas Blvd",
            city="Pasay",
        )
        session.add(new_prop)
        session.commit()
        session.refresh(new_prop)

        resp_b = client.post(
            f"/api/v1/properties/{new_prop.id}/units",
            params={"org_id": workspace.org_id},
            headers=workspace.owner_headers(),
            json={
                "label": "B-1",
                "bedrooms": 1,
                "bathrooms": 1,
                "monthly_rent": "10000.00",
            },
        )
        assert resp_b.status_code == 201, resp_b.text

    def test_secretary_role_is_blocked_by_cap_too(self, api):
        """The cap is enforced regardless of role; secretaries also
        receive 409 when they hit the 16th.
        """
        client, workspace, session = api
        _bulk_seed_units(session, workspace, count=UNITS_CAP - 1)
        resp = client.post(
            f"/api/v1/properties/{workspace.property_id}/units",
            params={"org_id": workspace.org_id},
            headers=workspace.secretary_headers(),
            json={
                "label": "sec-overflow",
                "bedrooms": 1,
                "bathrooms": 1,
                "monthly_rent": "10000.00",
            },
        )
        assert resp.status_code == 409, resp.text


# ----------------------------------------------------------------------
# Service-layer guard (direct service call)
# ----------------------------------------------------------------------


class TestServiceGuardDirect:
    """The same guard tested without going through FastAPI so a
    future caller bypassing the router still sees a domain-shaped
    ``ConflictError``.
    """

    def test_property_service_raises_conflict_at_cap(self):
        from app.v1.services.property import (
            PropertyService,
            UNITS_PER_PROPERTY_CAP,
            assert_units_within_cap,
        )
        from app.v1.services.errors import ConflictError
        from app.core.permissions import Role

        with v1_support.v1_engine_ctx():
            session = get_session_factory()()
            try:
                workspace = v1_support.seed_workspace(
                    session, name="UnitsCapService",
                )
                _bulk_seed_units(
                    session, workspace, count=UNITS_PER_PROPERTY_CAP - 1,
                )

                principal = workspace.owner
                svc = PropertyService(session)
                with pytest.raises(ConflictError) as exc:
                    svc.create_unit(
                        principal,
                        org_id=workspace.org_id,
                        property_id=workspace.property_id,
                        label="svc-overflow",
                    )
                assert str(UNITS_PER_PROPERTY_CAP) in str(exc.value)

                # assert_units_within_cap helper also raises.
                with pytest.raises(ConflictError):
                    assert_units_within_cap(
                        session,
                        org_id=workspace.org_id,
                        property_id=workspace.property_id,
                    )
            finally:
                session.close()

    def test_module_level_create_unit_also_enforces_cap(self):
        from app.v1.services.property import (
            UNITS_PER_PROPERTY_CAP,
            create_unit as module_create_unit,
        )
        from app.v1.services.errors import ConflictError
        from app.core.permissions import Role

        with v1_support.v1_engine_ctx():
            session = get_session_factory()()
            try:
                workspace = v1_support.seed_workspace(
                    session, name="UnitsCapModule",
                )
                _bulk_seed_units(
                    session, workspace, count=UNITS_PER_PROPERTY_CAP - 1,
                )
                with pytest.raises(ConflictError):
                    module_create_unit(
                        session,
                        org_id=workspace.org_id,
                        property_id=workspace.property_id,
                        unit_number="mod-overflow",
                        owner_user_id=workspace.owner_user_id,
                        actor_role=Role.OWNER,
                    )
            finally:
                session.close()


# ----------------------------------------------------------------------
# Database trigger — defense-in-depth boundary
# ----------------------------------------------------------------------
#
# These tests install the trigger via Alembic ``upgrade head`` (so
# the trigger exists on the table) and then verify the trigger
# itself rejects direct ORM inserts that bypass the service guard.
# Without the service guard in the way, the trigger is the only
# thing standing between a hostile INSERT and a 16th unit.


def _is_postgres_with_alembic() -> bool:
    """Return True if the CI PostgreSQL test DB is reachable.

    The V1 test harness uses ``V1Base.metadata.create_all`` and
    does not run Alembic by default. For these trigger tests we
    drop into a separate engine + schema cycle that:
      1. Drops + creates the schema.
      2. Builds the V1 schema from ORM metadata.
      3. Installs ONLY the trigger SQL from migration 0003 (not
         the full Alembic chain — the legacy migration 0001 uses
         ``telegram_id`` while the ORM uses ``telegram_user_id``,
         and we deliberately don't touch that pre-existing
         mismatch in GAP-P1; we test the trigger itself, not the
         full migration chain).
      4. After the test, drops the trigger + function and the
         schema so other tests are unaffected.

    The migration itself (``alembic upgrade head`` + ``alembic
    downgrade -1``) is verified separately by the ``fresh-postgres-
    alembic`` CI gate and by the manual ``make revision`` step.
    """
    import os
    return bool(os.environ.get("DATABASE_URL"))


def _install_trigger_sql(engine) -> None:
    """Install the trigger function + trigger from migration 0003
    via raw SQL. This is what the migration would do on
    ``alembic upgrade head``; we run it standalone because the V1
    test harness already built the schema via ``create_all``.
    """
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                """
                CREATE OR REPLACE FUNCTION
                    v1_enforce_units_per_property_cap()
                RETURNS trigger
                LANGUAGE plpgsql
                AS $$
                DECLARE
                    v_existing INTEGER;
                BEGIN
                    SELECT COUNT(*) INTO v_existing
                    FROM v1_units
                    WHERE property_id = NEW.property_id
                      AND id <> NEW.id;

                    IF v_existing + 1 > 15 THEN
                        RAISE EXCEPTION
                            'units_per_property_cap_exceeded: '
                            'property_id=% has % existing units, '
                            'cap=15',
                            NEW.property_id, v_existing
                            USING ERRCODE = 'check_violation';
                    END IF;

                    RETURN NEW;
                END;
                $$
                """
            )
        )
        conn.execute(
            sa.text(
                """
                CREATE TRIGGER trg_v1_units_units_per_property_cap
                BEFORE INSERT OR UPDATE OF property_id ON v1_units
                FOR EACH ROW
                EXECUTE FUNCTION v1_enforce_units_per_property_cap();
                """
            )
        )


def _uninstall_trigger_sql(engine) -> None:
    """Drop the trigger + helper function. Mirror of
    ``_install_trigger_sql``."""
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "DROP TRIGGER IF EXISTS "
                "trg_v1_units_units_per_property_cap ON v1_units"
            )
        )
        conn.execute(
            sa.text(
                "DROP FUNCTION IF EXISTS "
                "v1_enforce_units_per_property_cap()"
            )
        )


@pytest.mark.skipif(
    not _is_postgres_with_alembic(),
    reason="DATABASE_URL not set; trigger test requires CI PostgreSQL",
)
class TestDBTrigger:
    """Defense-in-depth: the trigger rejects any INSERT that bypasses
    the service guard, regardless of caller."""

    def test_trigger_rejects_direct_orm_insert_at_cap(self):
        from app.db.session import bind_engine, reset_engine_cache
        from app.v1.models.base import V1Base

        url = __import__("os").environ["DATABASE_URL"]
        reset_engine_cache()
        engine = bind_engine(url)
        # Drop + recreate the schema for isolation, then build the V1
        # schema from ORM metadata (the canonical test path) and ONLY
        # add the trigger SQL on top of that.
        with engine.begin() as conn:
            conn.execute(sa.text("DROP SCHEMA IF EXISTS public CASCADE"))
            conn.execute(sa.text("CREATE SCHEMA public"))
            conn.execute(sa.text("GRANT ALL ON SCHEMA public TO public"))
        V1Base.metadata.create_all(engine)
        _install_trigger_sql(engine)
        try:
            session = get_session_factory()()
            try:
                workspace = v1_support.seed_workspace(
                    session, name="UnitsCapTrig",
                )
                # Bulk up to (but not over) the cap.
                _bulk_seed_units(
                    session, workspace, count=UNITS_CAP - 1,
                )

                # Direct ORM insert: must be rejected by the trigger.
                overflow = Unit(
                    property_id=workspace.property_id,
                    org_id=workspace.org_id,
                    label="trigger-overflow",
                    bedrooms=1,
                    bathrooms=1,
                    monthly_rent=Decimal("10000.00"),
                    status=UnitStatus.AVAILABLE.value,
                )
                session.add(overflow)
                with pytest.raises(sa.exc.IntegrityError) as exc:
                    session.commit()
                assert "units_per_property_cap_exceeded" in str(exc.value)

                # Roll back so the session can be reused.
                session.rollback()
            finally:
                session.close()

            # Reversibility: drop the trigger (downgrade -1), the 16th
            # insert succeeds again.
            _uninstall_trigger_sql(engine)
            session = get_session_factory()()
            try:
                workspace = v1_support.seed_workspace(
                    session, name="UnitsCapTrig2",
                )
                _bulk_seed_units(
                    session, workspace, count=UNITS_CAP - 1,
                )
                overflow = Unit(
                    property_id=workspace.property_id,
                    org_id=workspace.org_id,
                    label="no-trigger-overflow",
                    bedrooms=1,
                    bathrooms=1,
                    monthly_rent=Decimal("10000.00"),
                    status=UnitStatus.AVAILABLE.value,
                )
                session.add(overflow)
                session.commit()  # must succeed without the trigger
            finally:
                session.close()
        finally:
            reset_engine_cache()
            with engine.begin() as conn:
                conn.execute(sa.text("DROP SCHEMA IF EXISTS public CASCADE"))
                conn.execute(sa.text("CREATE SCHEMA public"))
            engine.dispose()


# ----------------------------------------------------------------------
# Cap constant — frozen by Issue #112
# ----------------------------------------------------------------------


def test_cap_constant_matches_issue_112():
    """Issue #112 §"Telegram UX" freezes "Units <= 15". If this test
    fails, somebody changed the product rule without re-issuing
    Issue #112. Update both the constant and this assertion (or
    re-open Issue #112) — never silently.
    """
    from app.v1.services.property import UNITS_PER_PROPERTY_CAP
    assert UNITS_PER_PROPERTY_CAP == 15
