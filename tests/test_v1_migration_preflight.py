"""Focused regression tests for ``scripts/migration_preflight.py``.

The deploy workflow (``.github/workflows/deploy.yml``) calls this
script before ``alembic upgrade head`` to decide whether the rewrite
migration chain can run, whether the retired legacy schema must be
reset first, or whether the deploy must refuse to proceed.

The legacy preflight hard-coded ``EXPECTED_REVISION = "0001_baseline"``
and rejected the legitimate rewrite chain once a second migration
(``0002_renewal_pipeline``) was added (run #354). This file pins the
graph-aware behaviour the recovery script preserves:

* PROCEED for the recorded rewrite head (``0003_units_cap``).
* PROCEED for any recorded rewrite ancestor (``0001_baseline`` or
  ``0002_renewal_pipeline``).
* PROCEED for an empty ``alembic_version`` table (first-time bootstrap).
* LEGACY_RESET for the exact retired revision
  ``r3_grant_all_public_20260828`` — and a real PostgreSQL
  legacy-drop validation proving ``alembic upgrade head`` succeeds on
  the cleaned schema.
* FAIL_CLOSED for any other recorded revision.
* FAIL_CLOSED for multi-row ``alembic_version``.

Each behaviour is exercised against a **real PostgreSQL** database
(skipped when ``DATABASE_URL`` is not configured) so a future
refactor cannot silently accept an off-graph revision by mocking the
DB.

No workflow, no ``.github/workflows/*`` change, no dependency, no
legacy gate. The script's deployment contract is the same as the
previous inline preflight: drop legacy relation objects, preserve
public schema owner / ACLs / default privileges, never mutate
``alembic_version`` for non-legacy revisions.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa

from scripts.migration_preflight import (
    EXPECTED_REVISION,
    FAIL_CLOSED,
    LEGACY_RESET,
    LEGACY_REVISION,
    PROCEED,
    _load_rewrite_chain,
    run_preflight,
)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


_REPO_ROOT = Path(__file__).resolve().parent.parent

# Use a dedicated DB so the rewrite CI test fixture (which drops the
# ``public`` schema every test) does not collide with our preflight
# scenarios. We never drop this DB's schema in tests; we re-shape the
# ``public`` schema only.
def _preflight_db_url() -> str | None:
    """Return the test DB URL used by these tests, or ``None`` if
    PostgreSQL is not configured.

    The preflight DB is independent of the rewrite test DB
    (``pasay_pm``) because preflight scenarios must plant and remove
    their own state — including legacy relations and off-graph
    ``alembic_version`` rows — without touching the schema the regular
    V1 test suite depends on.
    """
    base = os.environ.get("DATABASE_URL")
    if not base:
        return None
    # Replace database name with ``pasay_test_preflight``. This DB is
    # created lazily by ``_ensure_preflight_db`` below.
    return base.rsplit("/", 1)[0] + "/pasay_test_preflight"


def _ensure_preflight_db(url: str) -> None:
    """Create the preflight test DB if it does not exist.

    Idempotent; safe to call from every test that needs the DB.
    """
    from sqlalchemy.engine.url import make_url
    u = make_url(url)
    admin_engine = sa.create_engine(
        u.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
    )
    try:
        with admin_engine.connect() as conn:
            exists = conn.execute(
                sa.text("SELECT 1 FROM pg_database WHERE datname = :n"),
                {"n": u.database},
            ).scalar()
            if not exists:
                conn.execute(sa.text(f'CREATE DATABASE "{u.database}"'))
    finally:
        admin_engine.dispose()


def _reset_schema(engine: sa.Engine) -> None:
    """Drop and recreate the ``public`` schema; re-grant default
    privileges so the ``pasay`` role owns relations Alembic creates.
    """
    with engine.begin() as conn:
        conn.execute(sa.text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(sa.text("CREATE SCHEMA public"))
        conn.execute(sa.text("GRANT ALL ON SCHEMA public TO public"))


def _plant_alembic_version(engine: sa.Engine, revisions: list[str]) -> None:
    """Plant the ``alembic_version`` table with the given rows."""
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE IF NOT EXISTS alembic_version ("
                "    version_num VARCHAR(32) NOT NULL"
                ")"
            ),
        )
        # Always start from a known-clean table so multi-row tests
        # don't accumulate rows from earlier cases.
        conn.execute(sa.text("DELETE FROM alembic_version"))
        for rev in revisions:
            conn.execute(
                sa.text(
                    "INSERT INTO alembic_version (version_num) "
                    "VALUES (:r)"
                ),
                {"r": rev},
            )


def _create_legacy_fixture(engine: sa.Engine) -> int:
    """Create a few legacy relation objects so we can prove the
    LEGACY_RESET branch actually drops them. Returns the number of
    objects created.
    """
    objects_sql = [
        "CREATE TABLE legacy_table (id INT)",
        "CREATE VIEW legacy_view AS SELECT 1 AS one",
        "CREATE SEQUENCE legacy_seq",
    ]
    with engine.begin() as conn:
        for sql in objects_sql:
            conn.execute(sa.text(sql))
    return len(objects_sql)


def _count_relations(engine: sa.Engine) -> dict[str, int]:
    """Count relations in ``public`` grouped by kind, used to assert
    the LEGACY_RESET drop is exhaustive.
    """
    with engine.begin() as conn:
        rows = conn.execute(
            sa.text(
                """
                SELECT c.relkind, COUNT(*)::int AS n
                FROM pg_class AS c
                JOIN pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relkind IN ('v', 'm', 'r', 'p', 'f', 'S')
                GROUP BY c.relkind
                """
            ),
        ).all()
    return {r[0]: int(r[1]) for r in rows}


def _alembic(*args: str, url: str) -> subprocess.CompletedProcess:
    """Run ``python -m alembic <args>`` against ``url``.

    ``alembic.ini`` lives at the repo root; the script location is
    read from ``alembic.ini`` (``alembic/versions``).
    """
    cmd = [
        sys.executable, "-m", "alembic",
        "-c", str(_REPO_ROOT / "alembic.ini"),
        "-x", f"db_url={url}",
        *args,
    ]
    env = os.environ.copy()
    env["DATABASE_URL"] = url
    proc = subprocess.run(
        cmd, cwd=str(_REPO_ROOT), env=env,
        capture_output=True, text=True, timeout=180,
    )
    return proc


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def preflight_db():
    """Yield a SQLAlchemy engine bound to the preflight test DB.

    The DB's ``public`` schema is reset to empty before each test so
    tests can plant their own ``alembic_version`` rows + legacy
    relation objects without colliding with the rewrite test suite's
    DB.

    Skipped when PostgreSQL is not configured (no ``DATABASE_URL``).
    """
    url = _preflight_db_url()
    if url is None:
        pytest.skip("DATABASE_URL not set; preflight tests need PostgreSQL")
    _ensure_preflight_db(url)
    engine = sa.create_engine(url)
    try:
        _reset_schema(engine)
        yield engine
    finally:
        # Re-clean the schema after the test so the next test starts
        # from a deterministic empty state regardless of what the
        # current test did.
        _reset_schema(engine)
        engine.dispose()


# ----------------------------------------------------------------------
# Sanity: the rewrite chain itself is what we think it is
# ----------------------------------------------------------------------


class TestRewriteChainDiscovery:
    """The rewrite chain discovery is the single source of truth the
    preflight uses to decide PROCEED vs FAIL_CLOSED. These tests lock
    the discovery against the on-disk graph so a future migration
    rename cannot silently turn a valid ancestor into an unknown.
    """

    def test_chain_contains_every_known_rewrite_revision(self):
        chain, head = _load_rewrite_chain()
        # The current rewrite chain must contain at least the baseline
        # plus every migration that has been merged into the rewrite
        # branch. Adding a new migration is a deliberate developer
        # action; the test does NOT pin the *exact* set because we do
        # not want CI to fail every time a migration is added — but it
        # DOES pin that the baseline + the two follow-ups that were
        # already merged are present.
        assert EXPECTED_REVISION in chain
        assert "0002_renewal_pipeline" in chain
        assert "0003_units_cap" in chain

    def test_head_is_the_latest_known_revision(self):
        _, head = _load_rewrite_chain()
        assert head == "0003_units_cap"

    def test_legacy_revision_is_not_in_rewrite_chain(self):
        """The retired legacy revision must NOT be discoverable from
        ``alembic/versions/`` — otherwise the LEGACY_RESET branch
        would never fire because the recorded legacy revision would
        already classify as a valid rewrite ancestor.
        """
        chain, _ = _load_rewrite_chain()
        assert LEGACY_REVISION not in chain


# ----------------------------------------------------------------------
# PROCEED outcomes
# ----------------------------------------------------------------------


class TestProceedOutcomes:
    """Recorded revisions that are part of the rewrite chain (or empty)
    must classify as PROCEED so ``alembic upgrade head`` can run.
    """

    def test_empty_alembic_version_proceeds(self, preflight_db):
        """Fresh DB: no ``alembic_version`` row yet, no schema.
        PROCEED so the first ``alembic upgrade head`` can build the
        baseline from scratch.
        """
        # The fixture already dropped public; nothing else to do.
        decision = run_preflight(preflight_db.url.render_as_string(hide_password=False))
        assert decision.action == PROCEED
        assert decision.reason == "empty_alembic_version"
        assert decision.head == "0003_units_cap"
        assert decision.recorded == ()
        assert decision.dropped == 0

    def test_recorded_rewrite_head_proceeds(self, preflight_db):
        """Recorded revision = current head ``0003_units_cap`` →
        PROCEED with reason ``valid_rewrite_head`` (already at head).
        """
        _plant_alembic_version(preflight_db, ["0003_units_cap"])
        decision = run_preflight(preflight_db.url.render_as_string(hide_password=False))
        assert decision.action == PROCEED
        assert decision.reason == "valid_rewrite_head"
        assert decision.recorded == ("0003_units_cap",)
        assert decision.dropped == 0

    def test_recorded_first_ancestor_proceeds(self, preflight_db):
        """Recorded revision = ``0001_baseline`` (first rewrite
        ancestor) → PROCEED with reason ``valid_rewrite_ancestor``.
        This is the case that was being rejected by the old
        hard-coded preflight and broke the real deploy run #8.
        """
        _plant_alembic_version(preflight_db, ["0001_baseline"])
        decision = run_preflight(preflight_db.url.render_as_string(hide_password=False))
        assert decision.action == PROCEED
        assert decision.reason == "valid_rewrite_ancestor"
        assert decision.recorded == ("0001_baseline",)
        assert decision.dropped == 0

    def test_recorded_intermediate_ancestor_proceeds(self, preflight_db):
        """Recorded revision = ``0002_renewal_pipeline`` (intermediate
        ancestor) → PROCEED so ``alembic upgrade head`` can apply the
        remaining ``0003_units_cap`` migration.
        """
        _plant_alembic_version(preflight_db, ["0002_renewal_pipeline"])
        decision = run_preflight(preflight_db.url.render_as_string(hide_password=False))
        assert decision.action == PROCEED
        assert decision.reason == "valid_rewrite_ancestor"
        assert decision.recorded == ("0002_renewal_pipeline",)
        assert decision.dropped == 0

    @pytest.mark.parametrize(
        "rev",
        ["0001_baseline", "0002_renewal_pipeline", "0003_units_cap"],
    )
    def test_each_rewrite_ancestor_or_head_proceeds(self, preflight_db, rev):
        """Every revision in the live rewrite chain classifies as
        PROCEED. Parameterised so a future rename of any single
        revision surfaces as a focused test failure here.
        """
        _plant_alembic_version(preflight_db, [rev])
        decision = run_preflight(preflight_db.url.render_as_string(hide_password=False))
        assert decision.action == PROCEED
        assert decision.head == "0003_units_cap"
        assert decision.recorded == (rev,)
        assert decision.dropped == 0


# ----------------------------------------------------------------------
# LEGACY_RESET — including real PostgreSQL drop validation
# ----------------------------------------------------------------------


class TestLegacyReset:
    """The exact retired legacy revision triggers LEGACY_RESET. The
    legacy relation objects in the ``public`` schema must be dropped
    (preserving the schema owner / ACLs / default privileges) so that
    ``alembic upgrade head`` can install the rewrite chain.
    """

    def test_exact_legacy_revision_resets_schema(self, preflight_db):
        """Recorded = ``r3_grant_all_public_20260828`` →
        LEGACY_RESET; legacy relation objects are dropped.
        """
        _plant_alembic_version(preflight_db, [LEGACY_REVISION])
        created = _create_legacy_fixture(preflight_db)
        # ``_plant_alembic_version`` also created the
        # ``alembic_version`` table (one TABLE relation), so the
        # total before the reset is ``created + 1``.
        before = sum(_count_relations(preflight_db).values())
        assert before == created + 1, (
            f"unexpected pre-reset relation count: {before!r}"
        )

        decision = run_preflight(preflight_db.url.render_as_string(hide_password=False))
        assert decision.action == LEGACY_RESET
        assert decision.reason == "exact_legacy_revision"
        assert decision.recorded == (LEGACY_REVISION,)
        # Every legacy relation was dropped; ``alembic_version`` is
        # also dropped (it was a legacy relation in the public
        # schema) so ``alembic upgrade head`` will install the
        # rewrite chain from ``0001_baseline``.
        assert decision.dropped == before

        # Sanity: every legacy relation is gone, including
        # ``alembic_version``.
        assert _count_relations(preflight_db) == {}
        with preflight_db.begin() as conn:
            has_av = conn.execute(
                sa.text("SELECT to_regclass('public.alembic_version')"),
            ).scalar()
        assert has_av is None

    def test_legacy_reset_then_alembic_upgrade_head_succeeds(
        self, preflight_db,
    ):
        """End-to-end LEGACY_RESET + alembic upgrade head.

        This is the headline proof the deploy step relies on:
        1. Plant legacy revision + legacy relation objects.
        2. Run preflight → LEGACY_RESET.
        3. Run ``alembic upgrade head`` against the same DB.
        4. Verify ``alembic current`` reports the new head
           ``0003_units_cap`` and the rewrite chain is installed.
        """
        url = preflight_db.url.render_as_string(hide_password=False)
        _plant_alembic_version(preflight_db, [LEGACY_REVISION])
        _create_legacy_fixture(preflight_db)

        decision = run_preflight(url)
        assert decision.action == LEGACY_RESET

        proc = _alembic("upgrade", "head", url=url)
        assert proc.returncode == 0, (
            f"alembic upgrade head failed after LEGACY_RESET "
            f"(rc={proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout[-2000:]}\n"
            f"STDERR:\n{proc.stderr[-2000:]}"
        )

        # ``alembic current`` must report the new head.
        proc = _alembic("current", url=url)
        assert proc.returncode == 0, proc.stderr
        assert "0003_units_cap" in proc.stdout, (
            f"unexpected alembic current output: {proc.stdout!r}"
        )

    def test_dry_run_does_not_drop_legacy_schema(self, preflight_db):
        """``dry_run=True`` classifies correctly but must NOT actually
        drop legacy relations. Future CI sanity / dry-run workflows
        depend on this so the deploy step cannot accidentally mutate
        the DB outside the deploy run.
        """
        url = preflight_db.url.render_as_string(hide_password=False)
        _plant_alembic_version(preflight_db, [LEGACY_REVISION])
        _create_legacy_fixture(preflight_db)

        decision = run_preflight(url, dry_run=True)
        assert decision.action == LEGACY_RESET
        assert decision.dropped == 0, (
            "dry_run=True must not record any drops"
        )

        # All legacy relations are still present.
        after = _count_relations(preflight_db)
        assert sum(after.values()) >= 3, (
            f"dry_run should preserve relations; got {after!r}"
        )

        # And the recorded revision is unchanged.
        with preflight_db.begin() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT version_num FROM alembic_version "
                    "ORDER BY version_num"
                ),
            ).scalars().all()
        assert tuple(row) == (LEGACY_REVISION,)

    def test_legacy_revision_via_explicit_override(self, preflight_db):
        """The ``legacy_revision`` override is honoured. When the
        override does NOT match the recorded revision, the recorded
        revision is classified as off-graph and the preflight fails
        closed without mutating the DB.
        """
        _plant_alembic_version(preflight_db, [LEGACY_REVISION])
        url = preflight_db.url.render_as_string(hide_password=False)

        with pytest.raises(SystemExit) as excinfo:
            run_preflight(url, legacy_revision="some_other_legacy")
        # The recorded ``LEGACY_REVISION`` is not the override, so it
        # is treated as an unexpected off-graph revision.
        assert LEGACY_REVISION in str(excinfo.value)

        # Recorded revision is unchanged.
        with preflight_db.begin() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT version_num FROM alembic_version "
                    "ORDER BY version_num"
                ),
            ).scalars().all()
        assert tuple(row) == (LEGACY_REVISION,)


# ----------------------------------------------------------------------
# FAIL_CLOSED outcomes
# ----------------------------------------------------------------------


class TestFailClosed:
    """The deploy must refuse to proceed on unknown revisions and on
    multi-row ``alembic_version``. Both paths raise ``SystemExit`` so
    the deploy step's ``set -euo pipefail`` catches the failure and
    nothing destructive runs on the DB.
    """

    def test_unknown_single_revision_fails_closed(self, preflight_db):
        """A recorded revision that is neither an ancestor/head of the
        rewrite chain nor the exact retired legacy revision must
        fail closed without mutating the DB.
        """
        _plant_alembic_version(preflight_db, ["9999_unknown_garbage"])
        url = preflight_db.url.render_as_string(hide_password=False)

        with pytest.raises(SystemExit) as excinfo:
            run_preflight(url)
        assert "9999_unknown_garbage" in str(excinfo.value)

        # The DB is unchanged: recorded revision is still the
        # unknown garbage.
        with preflight_db.begin() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT version_num FROM alembic_version "
                    "ORDER BY version_num"
                ),
            ).scalars().all()
        assert tuple(row) == ("9999_unknown_garbage",)

    def test_multi_row_alembic_version_fails_closed(self, preflight_db):
        """Two or more rows in ``alembic_version`` must fail closed.
        ``alembic upgrade head`` cannot reason about multiple current
        revisions safely; the preflight must refuse before any DDL
        runs.
        """
        _plant_alembic_version(
            preflight_db,
            ["0001_baseline", "0002_renewal_pipeline"],
        )
        url = preflight_db.url.render_as_string(hide_password=False)

        with pytest.raises(SystemExit) as excinfo:
            run_preflight(url)
        msg = str(excinfo.value)
        assert "multi-row" in msg.lower() or "multi_row" in msg.lower()
        assert "0001_baseline" in msg
        assert "0002_renewal_pipeline" in msg

        # The DB is unchanged.
        with preflight_db.begin() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT version_num FROM alembic_version "
                    "ORDER BY version_num"
                ),
            ).scalars().all()
        assert set(rows) == {"0001_baseline", "0002_renewal_pipeline"}

    def test_unknown_revision_does_not_drop_relations(self, preflight_db):
        """FAIL_CLOSED must not perform any DDL. We plant a table
        beside the unknown revision and verify it survives after the
        preflight raises SystemExit.
        """
        _plant_alembic_version(preflight_db, ["9999_unknown"])
        with preflight_db.begin() as conn:
            conn.execute(sa.text("CREATE TABLE sentinel (id INT)"))
        before = _count_relations(preflight_db)
        assert before.get("r", 0) >= 1

        url = preflight_db.url.render_as_string(hide_password=False)
        with pytest.raises(SystemExit):
            run_preflight(url)

        after = _count_relations(preflight_db)
        assert after == before, (
            f"preflight mutated relations on FAIL_CLOSED: "
            f"before={before!r} after={after!r}"
        )


# ----------------------------------------------------------------------
# Decision dataclass shape
# ----------------------------------------------------------------------


class TestDecisionShape:
    """The decision dataclass is part of the script's external API
    (tests assert on its fields). Pin the shape so a future refactor
    cannot accidentally change field names without surfacing the
    test failure.
    """

    def test_decision_fields_present(self, preflight_db):
        decision = run_preflight(
            preflight_db.url.render_as_string(hide_password=False),
        )
        # Frozen dataclass — attribute names are part of the contract.
        assert hasattr(decision, "action")
        assert hasattr(decision, "reason")
        assert hasattr(decision, "head")
        assert hasattr(decision, "recorded")
        assert hasattr(decision, "dropped")

    def test_action_constant_values(self):
        """The decision constants are part of the deploy-step parser's
        contract. Pin the exact string values so deploy.yml / future
        consumers stay in sync.
        """
        assert PROCEED == "PROCEED"
        assert LEGACY_RESET == "LEGACY_RESET"
        assert FAIL_CLOSED == "FAIL_CLOSED"

    def test_legacy_revision_constant_is_frozen(self):
        """The retired legacy revision is the only off-graph revision
        we explicitly accept. Pin the value so a typo cannot silently
        turn an unknown revision into a recognised one.
        """
        assert LEGACY_REVISION == "r3_grant_all_public_20260828"


# ----------------------------------------------------------------------
# End-to-end: fresh DB → migrate from empty
# ----------------------------------------------------------------------


class TestFreshDatabaseEndToEnd:
    """The end-to-end happy path the deploy step relies on: empty
    DB → preflight PROCEED → ``alembic upgrade head`` installs the
    full rewrite chain.
    """

    def test_empty_db_to_head_via_preflight_then_upgrade(self, preflight_db):
        url = preflight_db.url.render_as_string(hide_password=False)

        decision = run_preflight(url)
        assert decision.action == PROCEED
        assert decision.reason == "empty_alembic_version"

        proc = _alembic("upgrade", "head", url=url)
        assert proc.returncode == 0, (
            f"alembic upgrade head failed on empty DB (rc={proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout[-2000:]}\n"
            f"STDERR:\n{proc.stderr[-2000:]}"
        )

        # Now the preflight must classify again as PROCEED with
        # reason ``valid_rewrite_head``.
        decision = run_preflight(url)
        assert decision.action == PROCEED
        assert decision.reason == "valid_rewrite_head"
        assert decision.recorded == ("0003_units_cap",)