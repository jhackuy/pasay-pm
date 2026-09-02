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
   ``v1_units`` that:

   a. locks the parent ``v1_properties`` row identified by
      ``NEW.property_id`` via ``SELECT ... FOR UPDATE``, so
      concurrent writers serialize per target property under
      PostgreSQL's default ``READ COMMITTED``;
   b. re-counts units per property (excluding the row being
      updated, which is still at ``OLD.property_id``);
   c. raises ``check_violation units_per_property_cap_exceeded``
      if the resulting count would exceed the cap.

   Without the parent-row ``FOR UPDATE`` the trigger is **not**
   race-safe — verified empirically against the deployed
   PostgreSQL isolation level (5/5 concurrent races ended at 16
   units; see PR #116 review). With the lock, two concurrent
   INSERTs/UPDATEs targeting the same property serialize and the
   loser sees the new committed count and is rejected.

This file proves both surfaces:
- The HTTP service guard: 15 inserts succeed (201), the 16th is
  rejected (409), and the existing 15 rows are unchanged.
- The service guard: ``ConflictError`` is raised directly from
  ``PropertyService.create_unit``.
- The DB trigger: against the **real** Alembic migration
  (``alembic upgrade head``, not a copy of the trigger SQL):
  - direct ORM insert at cap is rejected with
    ``IntegrityError`` + ``units_per_property_cap_exceeded`` text;
  - ``alembic downgrade -1`` cleanly removes the trigger +
    helper function and a 16th insert succeeds again;
  - two concurrent INSERTs targeting the same property
    deterministically serialize: exactly one commits, exactly
    one is rejected, final count = 15;
  - UPDATE moving a unit into a property that already has 15
    units is rejected (lock acquired on the target parent).
- Org-scope: the cap is per property **and** per org — a second
  property in the same org has its own 15-unit budget.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from decimal import Decimal

import psycopg2
import psycopg2.errors as pgerrors
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.db.session import bind_engine, get_db, get_session_factory, reset_engine_cache
from app.v1.main import app as v1_app
from app.v1.models.base import UnitStatus, V1Base
from app.v1.models.property import Property, Unit

from tests import v1_support


UNITS_CAP = 15  # mirrors app.v1.services.property.UNITS_PER_PROPERTY_CAP

# Root of the repository (so ``alembic.ini`` is on disk).
_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir),
)


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
# These tests install the full Alembic migration chain
# (``alembic upgrade head``) so the trigger exists on the table as
# production would see it, then exercise the trigger directly. We
# deliberately do NOT keep a byte-for-byte copy of the migration SQL
# in this file — PR #116 review showed that pattern drifts from the
# real migration. The single source of truth is
# ``alembic/versions/0003_units_cap.py``; these tests load it via
# ``alembic``.


def _postgres_url_or_skip() -> str:
    """Return the CI PostgreSQL URL or skip if not configured."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set; trigger test requires CI PostgreSQL")
    return url


def _alembic(args: list[str], *, url: str) -> None:
    """Run ``python -m alembic <args>`` against ``url``.

    ``alembic.ini`` lives at the repo root; ``env.py`` reads
    ``DATABASE_URL`` (and the ``-x db_url=`` override) to pick the
    target. We use ``-x db_url=...`` so the same URL controls both
    ``sqlalchemy.url`` (via ``env.py``) and the runtime engine.
    """
    cmd = [
        sys.executable, "-m", "alembic",
        "-c", os.path.join(_REPO_ROOT, "alembic.ini"),
        "-x", f"db_url={url}",
        *args,
    ]
    env = os.environ.copy()
    env["DATABASE_URL"] = url
    proc = subprocess.run(
        cmd, cwd=_REPO_ROOT, env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, (
        f"alembic {' '.join(args)} failed (rc={proc.returncode}):\n"
        f"STDOUT:\n{proc.stdout[-2000:]}\n"
        f"STDERR:\n{proc.stderr[-2000:]}"
    )


@pytest.fixture
def migrated_db_engine():
    """Return an Engine bound to a fresh schema where the **real**
    Alembic chain (``upgrade head``) has been applied. ``downgrade -1``
    followed by ``upgrade head`` is part of the fixture contract so
    any future trigger-edit cycle is exercised end-to-end.
    """
    url = _postgres_url_or_skip()
    reset_engine_cache()
    engine = bind_engine(url)

    # Drop + recreate the public schema for isolation.
    with engine.begin() as conn:
        conn.execute(sa.text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(sa.text("CREATE SCHEMA public"))
        conn.execute(sa.text("GRANT ALL ON SCHEMA public TO public"))

    _alembic(["upgrade", "head"], url=url)
    try:
        yield engine
    finally:
        reset_engine_cache()
        # Re-drop the schema on teardown so other tests are unaffected.
        cleanup_engine = sa.create_engine(url, future=True)
        try:
            with cleanup_engine.begin() as conn:
                conn.execute(
                    sa.text(
                        "SELECT pg_terminate_backend(pid) "
                        "FROM pg_stat_activity "
                        "WHERE datname = current_database() "
                        "AND pid <> pg_backend_pid()"
                    ),
                )
                conn.execute(sa.text("DROP SCHEMA IF EXISTS public CASCADE"))
                conn.execute(sa.text("CREATE SCHEMA public"))
        finally:
            cleanup_engine.dispose()


def _seed_org_property_units(
    engine,
    *,
    property_units: dict[int, int],
) -> dict[str, int]:
    """Insert one org + one property per property_id + N units each.

    Returns a dict ``{property_id: int}`` plus ``org_id`` and
    ``unit_ids_by_property``. The pre-existing seed unit count plus
    the inserted units must add up to ``UNITS_CAP`` when the test
    intends to be at the cap.
    """
    org_id: int = -1
    prop_ids: list[int] = []
    unit_ids_by_property: dict[int, list[int]] = {}

    with engine.begin() as conn:
        org_id = conn.execute(
            sa.text(
                "INSERT INTO v1_organizations(name) "
                "VALUES ('cap-org') RETURNING id"
            )
        ).scalar_one()

    for prop_index, n_units in property_units.items():
        with engine.begin() as conn:
            prop_id = conn.execute(
                sa.text(
                    "INSERT INTO v1_properties(org_id, name, address_line1) "
                    "VALUES (:o, :n, 'addr') RETURNING id"
                ),
                {"o": org_id, "n": f"prop-{prop_index}"},
            ).scalar_one()
            prop_ids.append(prop_id)
            ids: list[int] = []
            for i in range(n_units):
                uid = conn.execute(
                    sa.text(
                        "INSERT INTO v1_units("
                        "property_id, org_id, label, monthly_rent, status"
                        ") VALUES (:p, :o, :l, 10000.00, 'AVAILABLE') "
                        "RETURNING id"
                    ),
                    {
                        "p": prop_id, "o": org_id,
                        "l": f"u-{prop_index}-{i:02d}",
                    },
                ).scalar_one()
                ids.append(uid)
            unit_ids_by_property[prop_id] = ids

    return {
        "org_id": org_id,
        "property_ids": prop_ids,
        "unit_ids_by_property": unit_ids_by_property,
    }


@pytest.mark.skipif(
    not bool(os.environ.get("DATABASE_URL")),
    reason="DATABASE_URL not set; trigger test requires CI PostgreSQL",
)
class TestDBTrigger:
    """Defense-in-depth: the installed migration trigger rejects any
    INSERT/UPDATE that would push a property past ``UNITS_CAP``,
    regardless of caller. These tests exercise the **real** Alembic
    migration (not a copied SQL fragment) so they cannot pass while
    the migration itself regresses.
    """

    def test_trigger_rejects_direct_orm_insert_at_cap(self, migrated_db_engine):
        """At cap=15 the next raw-SQL insert (bypassing the service
        guard AND the ORM) must be rejected with
        ``units_per_property_cap_exceeded``. After ``downgrade -1``
        the trigger is gone and the same insert succeeds; after
        ``upgrade head`` the trigger is back and the cap is enforced
        again. This is the round-trip contract from the migration
        review.

        The seed/insert use raw SQL against the
        ``alembic upgrade head`` schema on purpose: the V1 ORM
        ``v1_users.telegram_user_id`` does not match migration 0001's
        ``v1_users.telegram_id`` — a pre-existing mismatch that this
        PR does not touch. The trigger's behavior is identical
        regardless of which client path inserts the row.
        """
        url = _postgres_url_or_skip()
        engine = migrated_db_engine

        # 1) Verify the trigger + helper function are installed.
        with engine.begin() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT 1 FROM pg_trigger "
                    "WHERE tgname = 'trg_v1_units_units_per_property_cap'"
                )
            ).first()
            assert row is not None, (
                "trigger trg_v1_units_units_per_property_cap not installed "
                "by alembic upgrade head"
            )
            row = conn.execute(
                sa.text(
                    "SELECT 1 FROM pg_proc "
                    "WHERE proname = 'v1_enforce_units_per_property_cap'"
                )
            ).first()
            assert row is not None, (
                "function v1_enforce_units_per_property_cap not installed "
                "by alembic upgrade head"
            )

        # 2) Seed property A to exactly UNITS_CAP units.
        seed = _seed_org_property_units(
            engine, property_units={1: UNITS_CAP},
        )
        org_id = seed["org_id"]
        prop_a = seed["property_ids"][0]

        # 3) Direct raw-SQL INSERT at cap must be rejected by the
        #    trigger. We use a separate psycopg2 connection so the
        #    INSERT runs in its own transaction (no shared state with
        #    the engine above), and so the trigger sees the cap
        #    count without any SQLAlchemy autoflush interference.
        dsn_args = _psycopg2_connect(url)
        dsn_args.close()
        conn = _psycopg2_connect(url)
        try:
            with conn:
                with conn.cursor() as cur:
                    with pytest.raises(
                        (pgerrors.IntegrityError, sa.exc.DBAPIError),
                    ) as exc_info:
                        cur.execute(
                            "INSERT INTO v1_units("
                            "property_id, org_id, label, "
                            "monthly_rent, status"
                            ") VALUES (%s, %s, %s, 10000.00, "
                            "'AVAILABLE')",
                            (prop_a, org_id, "trigger-overflow"),
                        )
            msg = str(exc_info.value)
            assert "units_per_property_cap_exceeded" in msg, (
                f"trigger error missing cap marker: {msg!r}"
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass

        # Sanity: count is still exactly UNITS_CAP.
        with engine.begin() as conn:
            cnt = conn.execute(
                sa.text(
                    "SELECT COUNT(*) FROM v1_units WHERE property_id = :p"
                ),
                {"p": prop_a},
            ).scalar_one()
        assert cnt == UNITS_CAP

        # 4) Round-trip: downgrade -1 drops the trigger + function.
        _alembic(["downgrade", "-1"], url=url)
        with engine.begin() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT 1 FROM pg_trigger "
                    "WHERE tgname = 'trg_v1_units_units_per_property_cap'"
                )
            ).first()
            assert row is None, (
                "trigger still present after alembic downgrade -1"
            )
            row = conn.execute(
                sa.text(
                    "SELECT 1 FROM pg_proc "
                    "WHERE proname = 'v1_enforce_units_per_property_cap'"
                )
            ).first()
            assert row is None, (
                "function still present after alembic downgrade -1"
            )

        # 5) Without the trigger, the same INSERT now succeeds.
        conn = _psycopg2_connect(url)
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO v1_units("
                        "property_id, org_id, label, "
                        "monthly_rent, status"
                        ") VALUES (%s, %s, %s, 10000.00, "
                        "'AVAILABLE')",
                        (prop_a, org_id, "no-trigger-overflow"),
                    )
        finally:
            try:
                conn.close()
            except Exception:
                pass

        with engine.begin() as conn:
            cnt = conn.execute(
                sa.text(
                    "SELECT COUNT(*) FROM v1_units WHERE property_id = :p"
                ),
                {"p": prop_a},
            ).scalar_one()
        assert cnt == UNITS_CAP + 1, (
            f"insert without trigger should succeed; count={cnt}"
        )

        # The migration's pre-flight refuses to install the trigger
        # if any property already has > UNITS_CAP units. Drop the
        # over-cap row we just inserted so the round-trip upgrade
        # head below can install the trigger.
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "DELETE FROM v1_units "
                    "WHERE property_id = :p AND label = :l"
                ),
                {"p": prop_a, "l": "no-trigger-overflow"},
            )
        with engine.begin() as conn:
            cnt = conn.execute(
                sa.text(
                    "SELECT COUNT(*) FROM v1_units WHERE property_id = :p"
                ),
                {"p": prop_a},
            ).scalar_one()
        assert cnt == UNITS_CAP

        # 6) Round-trip back: upgrade head reinstalls the trigger.
        _alembic(["upgrade", "head"], url=url)
        with engine.begin() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT 1 FROM pg_trigger "
                    "WHERE tgname = 'trg_v1_units_units_per_property_cap'"
                )
            ).first()
            assert row is not None, (
                "trigger not reinstalled by second alembic upgrade head"
            )

    def test_trigger_rejects_update_moving_unit_into_full_property(
        self, migrated_db_engine,
    ):
        """UPDATE moving a unit from a non-full property into a
        property that is already at ``UNITS_CAP`` must be rejected by
        the trigger. This proves the ``BEFORE UPDATE OF property_id``
        arm uses the same parent-row lock discipline as the INSERT
        arm — the lock is acquired on the **target** parent, so
        moving into a full property is detected atomically.
        """
        url = _postgres_url_or_skip()
        engine = migrated_db_engine

        # Property A: exactly UNITS_CAP units (at cap).
        # Property B: one unit (will be moved to A).
        # Migration only ships a BEFORE INSERT OR UPDATE OF
        # property_id trigger — the seed uses raw SQL so we don't
        # rely on V1Service.create_unit semantics here.
        seed = _seed_org_property_units(
            engine,
            property_units={1: UNITS_CAP, 2: 1},
        )
        org_id = seed["org_id"]
        prop_a = seed["property_ids"][0]
        prop_b = seed["property_ids"][1]
        movable_unit_id = seed["unit_ids_by_property"][prop_b][0]

        # UPDATE ... SET property_id = A must be rejected.
        with engine.begin() as conn:
            try:
                conn.execute(
                    sa.text(
                        "UPDATE v1_units SET property_id = :a "
                        "WHERE id = :u"
                    ),
                    {"a": prop_a, "u": movable_unit_id},
                )
                conn.commit()
            except sa.exc.DBAPIError as exc:
                # SQLAlchemy wraps the underlying psycopg2 error.
                msg = str(exc).lower()
                assert (
                    "units_per_property_cap_exceeded" in msg
                    or "check_violation" in msg
                ), f"unexpected DB error: {exc!r}"
            else:
                pytest.fail(
                    "UPDATE into full property must be rejected by trigger",
                )

        # Verify the unit is still on property B (the UPDATE did NOT apply).
        with engine.begin() as conn:
            row = conn.execute(
                sa.text("SELECT property_id FROM v1_units WHERE id = :u"),
                {"u": movable_unit_id},
            ).first()
            assert row[0] == prop_b, (
                f"unit moved into full property anyway: row={row!r}"
            )
            count_a = conn.execute(
                sa.text(
                    "SELECT COUNT(*) FROM v1_units WHERE property_id = :p"
                ),
                {"p": prop_a},
            ).scalar_one()
            assert count_a == UNITS_CAP, (
                f"property A count changed: {count_a} != {UNITS_CAP}"
            )


def _psycopg2_connect(url: str):
    """Convert SQLAlchemy URL → psycopg2 DSN."""
    from sqlalchemy.engine.url import make_url
    u = make_url(url)
    return psycopg2.connect(
        host=u.host, port=u.port or 5432,
        user=u.username, password=u.password,
        dbname=u.database,
    )


def _concurrent_insert_at_cap(
    engine,
    *,
    property_id: int,
    org_id: int,
) -> tuple[list[bool], list[Exception | None], int]:
    """Run two real concurrent INSERTs (separate psycopg2
    connections / transactions) against ``property_id`` and return
    ``(commits, errors, final_count)``.

    ``commits[i]`` is True iff thread ``i``'s INSERT committed
    without error; ``errors[i]`` is the exception raised by
    thread ``i``'s INSERT (None on success). ``final_count`` is
    the row count for ``property_id`` after both transactions
    have settled.

    The test contract is:
      - exactly one ``commits[i]`` is True;
      - exactly one ``errors[i]`` is a ``psycopg2.errors.
        IntegrityError`` whose message contains
        ``units_per_property_cap_exceeded``;
      - ``final_count`` == ``UNITS_CAP``.

    Implementation: a ``threading.Barrier(2)`` releases both threads
    simultaneously so the first ``INSERT`` SQL is issued by both
    threads while they each hold only their own transaction (no
    row-level lock from a prior COUNT, no application-level
    serializer). The trigger's parent-row ``FOR UPDATE`` then
    serializes them inside the database.
    """
    url = engine.url.render_as_string(hide_password=False)
    dsn_args = _psycopg2_connect(url)
    # We use the URL directly via libpq psycopg2.connect() with kwargs
    # for safety; close the placeholder.
    dsn_args.close()

    barrier = threading.Barrier(2, timeout=30)
    results: dict[str, tuple[bool, Exception | None]] = {}
    results_lock = threading.Lock()

    def _worker(tag: str, label: str) -> None:
        conn = _psycopg2_connect(url)
        try:
            # Make sure both threads start at READ COMMITTED (the
            # default; we verify in the test) and each holds its own
            # transaction. autocommit=False (default) → INSERT runs
            # in an implicit transaction.
            with conn:  # context manager commits on success, rolls back on exc
                with conn.cursor() as cur:
                    barrier.wait(timeout=30)
                    try:
                        cur.execute(
                            "INSERT INTO v1_units("
                            "property_id, org_id, label, monthly_rent, status"
                            ") VALUES (%s, %s, %s, 10000.00, 'AVAILABLE')",
                            (property_id, org_id, label),
                        )
                    except Exception as e:  # noqa: BLE001
                        # Capture; raise so the ``with conn`` block
                        # rolls back the transaction.
                        with results_lock:
                            results[tag] = (False, e)
                        raise
                # If cur.execute() did not raise, ``with conn``
                # commits the transaction.
                with results_lock:
                    results[tag] = (True, None)
        except Exception:
            # Already recorded inside; swallow so the thread exits.
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    t1 = threading.Thread(
        target=_worker, args=("T1", f"concurrent-T1-{time.monotonic_ns()}"),
    )
    t2 = threading.Thread(
        target=_worker, args=("T2", f"concurrent-T2-{time.monotonic_ns()}"),
    )
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)

    commits = [results["T1"][0], results["T2"][0]]
    errors = [results["T1"][1], results["T2"][1]]

    with engine.begin() as conn:
        final_count = conn.execute(
            sa.text(
                "SELECT COUNT(*) FROM v1_units WHERE property_id = :p"
            ),
            {"p": property_id},
        ).scalar_one()
    return commits, errors, int(final_count)


@pytest.mark.skipif(
    not bool(os.environ.get("DATABASE_URL")),
    reason="DATABASE_URL not set; concurrent trigger test requires CI PostgreSQL",
)
class TestDBTriggerConcurrent:
    """The headline race-safety guarantee from PR #116 review:

    Seed 14 units on a property, then start two independent DB
    connections/transactions inserting into the same property
    together. Exactly one commits, one gets
    ``units_per_property_cap_exceeded``, final count = 15.
    """

    def test_two_concurrent_inserts_serialize_to_one_winner(
        self, migrated_db_engine,
    ):
        engine = migrated_db_engine

        # Verify the deployed isolation level — the race condition
        # was first observed at READ COMMITTED.
        with engine.begin() as conn:
            iso = conn.execute(sa.text("SHOW transaction_isolation")).scalar()
        assert iso is not None and "read committed" in iso.lower(), (
            f"deployed isolation is not READ COMMITTED: {iso!r}"
        )

        # Pre-flight: seed 14 units on property A.
        seed = _seed_org_property_units(engine, property_units={1: UNITS_CAP - 1})
        prop_a = seed["property_ids"][0]
        org_id = seed["org_id"]

        # Sanity: 14 units before the race.
        with engine.begin() as conn:
            cnt = conn.execute(
                sa.text(
                    "SELECT COUNT(*) FROM v1_units WHERE property_id = :p"
                ),
                {"p": prop_a},
            ).scalar_one()
        assert cnt == UNITS_CAP - 1

        # Run the race.
        commits, errors, final_count = _concurrent_insert_at_cap(
            engine, property_id=prop_a, org_id=org_id,
        )

        # Exactly one commit, exactly one failure with the cap marker.
        assert sorted(commits) == [False, True], (
            f"expected exactly one winner; got commits={commits} "
            f"errors={errors}"
        )
        winner_idx = commits.index(True)
        loser_idx = 1 - winner_idx
        loser_err = errors[loser_idx]
        assert loser_err is not None, "loser raised nothing"
        # The underlying driver is psycopg2 (via raw SQL); the
        # trigger raises ``check_violation`` which surfaces as
        # ``psycopg2.errors.IntegrityError`` or wrapped under
        # ``sa.exc.DBAPIError`` depending on path. We accept both
        # as long as the marker is in the message.
        assert isinstance(
            loser_err, (pgerrors.IntegrityError, sa.exc.DBAPIError),
        ), f"unexpected loser error type: {type(loser_err).__name__}"
        msg = str(loser_err)
        assert "units_per_property_cap_exceeded" in msg, (
            f"loser error missing cap marker: {msg!r}"
        )

        # Final count is exactly the cap.
        assert final_count == UNITS_CAP, (
            f"final count drifted past cap: {final_count} != {UNITS_CAP}"
        )


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