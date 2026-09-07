"""Alembic round-trip for the Issue #119 P0 credential bootstrap migrations.

Tests that ``0005_v1_api_cred_system`` and ``0006_v1_orm_alignment``
apply, downgrade, and re-apply cleanly on a fresh PostgreSQL DB.

These tests are intentionally narrow: each migration is exercised
individually so a future regression in the upgrade / downgrade paths
is caught at the unit boundary. The single-head invariant
(``alembic heads`` returns exactly one revision) is also asserted.

These tests do NOT depend on any V1 ORM seed / test harness — they
talk to alembic directly so they catch the migration code itself,
not the V1 ORM shape.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa


REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


def _alembic_env() -> dict:
    """Subprocess env with the CI test DATABASE_URL."""
    env = os.environ.copy()
    # The CI postgres service exports DATABASE_URL=postgresql+psycopg2://...
    env.setdefault(
        "DATABASE_URL",
        "postgresql+psycopg2://pasay:pasay@localhost:5432/pasay",
    )
    return env


def _alembic(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), *args],
        cwd=str(REPO_ROOT),
        capture_output=True, text=True,
        env=_alembic_env(),
    )


def _drop_public_schema(database_url: str) -> None:
    parsed = sa.engine.make_url(database_url)
    admin_engine = sa.create_engine(
        parsed.set(database="postgres"), isolation_level="AUTOCOMMIT",
    )
    try:
        with admin_engine.connect() as conn:
            conn.execute(sa.text(
                f"SELECT pg_terminate_backend(pid) "
                f"FROM pg_stat_activity WHERE datname = '{parsed.database}' "
                f"AND pid <> pg_backend_pid()"
            ))
            conn.execute(sa.text(f"DROP DATABASE IF EXISTS {parsed.database}"))
            conn.execute(sa.text(f"CREATE DATABASE {parsed.database}"))
    finally:
        admin_engine.dispose()


def _has_column(engine, table: str, column: str) -> bool:
    insp = sa.inspect(engine)
    return column in {c["name"] for c in insp.get_columns(table)}


def _check_constraint_exists(engine, table: str, constraint_name: str) -> bool:
    insp = sa.inspect(engine)
    for c in insp.get_check_constraints(table):
        if c.get("name") == constraint_name:
            return True
    return False


@pytest.fixture()
def fresh_db():
    """Drop + recreate the test DB before each test for hermetic isolation."""
    db_url = os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg2://pasay:pasay@localhost:5432/pasay",
    )
    _drop_public_schema(db_url)
    yield db_url


def test_alembic_heads_single_revision_at_0006(fresh_db):
    """Fresh DB upgrade head → exactly one head (no chain branches)."""
    result = _alembic("upgrade", "head")
    assert result.returncode == 0, result.stderr
    heads = _alembic("heads")
    assert heads.returncode == 0, heads.stderr
    # exactly one non-empty line in `heads` output
    head_lines = [
        line.strip() for line in heads.stdout.splitlines() if line.strip()
    ]
    assert len(head_lines) == 1, (
        f"alembic heads must report exactly one revision; got {head_lines}"
    )
    assert head_lines[0].startswith("0007_v1_system_trusted_org"), (
        f"head must be the new 0007 migration, got {head_lines[0]!r}"
    )


def test_0005_principal_type_and_purpose_columns_present(fresh_db):
    """After upgrade head, v1_api_credentials has principal_type + purpose
    columns + the compound index."""
    _alembic("upgrade", "head")
    engine = sa.create_engine(fresh_db)
    try:
        assert _has_column(engine, "v1_api_credentials", "principal_type")
        assert _has_column(engine, "v1_api_credentials", "purpose")
        insp = sa.inspect(engine)
        idx = {ix["name"] for ix in insp.get_indexes("v1_api_credentials")}
        assert "ix_v1_api_credentials_principal_type_purpose" in idx
        assert _check_constraint_exists(
            engine, "v1_api_credentials",
            "ck_v1_api_credentials_principal_type",
        )
    finally:
        engine.dispose()


def test_0005_round_trip_downgrade_then_upgrade(fresh_db):
    """0005 must upgrade, downgrade, and re-upgrade without errors.

    After downgrade, the new columns must be GONE; after re-upgrade they
    must come BACK with the same defaults / constraints.
    """
    _alembic("upgrade", "0004_legacy_telegram_runtime")
    # Upgrade to 0005.
    assert _alembic("upgrade", "0005_v1_api_cred_system").returncode == 0
    engine = sa.create_engine(fresh_db)
    try:
        assert _has_column(engine, "v1_api_credentials", "principal_type")
    finally:
        engine.dispose()
    # Downgrade to 0004 — columns must be gone.
    assert _alembic("downgrade", "0004_legacy_telegram_runtime").returncode == 0
    engine = sa.create_engine(fresh_db)
    try:
        assert not _has_column(engine, "v1_api_credentials", "principal_type")
        assert not _has_column(engine, "v1_api_credentials", "purpose")
    finally:
        engine.dispose()
    # Re-upgrade — columns must come back.
    assert _alembic("upgrade", "0005_v1_api_cred_system").returncode == 0
    engine = sa.create_engine(fresh_db)
    try:
        assert _has_column(engine, "v1_api_credentials", "principal_type")
        assert _has_column(engine, "v1_api_credentials", "purpose")
    finally:
        engine.dispose()


def test_0006_aligns_v1_users_with_orm(fresh_db):
    """After 0006, v1_users.telegram_user_id (renamed) + default_language."""
    _alembic("upgrade", "head")
    engine = sa.create_engine(fresh_db)
    try:
        assert _has_column(engine, "v1_users", "telegram_user_id")
        assert not _has_column(engine, "v1_users", "telegram_id")
        assert _has_column(engine, "v1_users", "default_language")
    finally:
        engine.dispose()


def test_0006_aligns_v1_memberships_role_state_with_orm(fresh_db):
    """After 0006, role CHECK accepts ONLY OWNER/SECRETARY; state CHECK
    excludes SUSPENDED.

    Each expected-failure INSERT is wrapped in a SAVEPOINT so a
    triggered CHECK constraint does not poison the outer transaction.
    """
    _alembic("upgrade", "head")
    engine = sa.create_engine(fresh_db)
    try:
        assert _check_constraint_exists(
            engine, "v1_memberships", "ck_v1_memberships_role",
        )
        assert _check_constraint_exists(
            engine, "v1_memberships", "ck_v1_memberships_state",
        )
        with engine.connect() as conn:
            try:
                conn.execute(sa.text(
                    "INSERT INTO v1_organizations (name) VALUES "
                    f"('roundtrip-{os.getpid()}')"
                ))
                conn.execute(sa.text(
                    "INSERT INTO v1_users (default_language) VALUES "
                    "('en-US')"
                ))
                # 'OWNER' uppercase is accepted.
                conn.execute(sa.text(
                    "INSERT INTO v1_memberships "
                    "(org_id, user_id, role, state) "
                    "VALUES (1, 1, 'OWNER', 'ACTIVE')"
                ))
                # Lowercase 'owner' is rejected (CHECK constraint).
                with pytest.raises(sa.exc.DBAPIError):
                    with conn.begin_nested():
                        conn.execute(sa.text(
                            "INSERT INTO v1_memberships "
                            "(org_id, user_id, role, state) "
                            "VALUES (1, 1, 'owner', 'ACTIVE')"
                        ))
                # SUSPENDED state is rejected.
                with pytest.raises(sa.exc.DBAPIError):
                    with conn.begin_nested():
                        conn.execute(sa.text(
                            "INSERT INTO v1_memberships "
                            "(org_id, user_id, role, state) "
                            "VALUES (1, 1, 'OWNER', 'SUSPENDED')"
                        ))
            finally:
                conn.rollback()
    finally:
        engine.dispose()


def test_0006_round_trip_downgrade_then_upgrade(fresh_db):
    """0006 must upgrade, downgrade, and re-upgrade without errors.

    Note: downgrade intentionally does NOT lowercase existing rows
    (DATA_CONTRACT §2.5 forbids silently mapping UPPERCASE → lowercase
    for state because SUSPENDED has no direct lowercase target).
    Therefore the downgrade path requires the operator to clean up
    rows first; we exercise the path on an EMPTY table here.
    """
    _alembic("upgrade", "0005_v1_api_cred_system")
    assert _alembic("upgrade", "0006_v1_orm_alignment").returncode == 0
    engine = sa.create_engine(fresh_db)
    try:
        assert _has_column(engine, "v1_users", "telegram_user_id")
    finally:
        engine.dispose()
    assert _alembic("downgrade", "0005_v1_api_cred_system").returncode == 0
    engine = sa.create_engine(fresh_db)
    try:
        # On downgrade the column is renamed BACK to telegram_id.
        assert _has_column(engine, "v1_users", "telegram_id")
        assert not _has_column(engine, "v1_users", "telegram_user_id")
    finally:
        engine.dispose()
    assert _alembic("upgrade", "0006_v1_orm_alignment").returncode == 0
    engine = sa.create_engine(fresh_db)
    try:
        assert _has_column(engine, "v1_users", "telegram_user_id")
        assert _has_column(engine, "v1_users", "default_language")
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 0007_v1_system_trusted_org (Issue #119 P0 follow-up)
# ---------------------------------------------------------------------------


def test_alembic_heads_single_revision_at_0007(fresh_db):
    """Fresh DB upgrade head → exactly one head (0007_v1_system_trusted_org)."""
    result = _alembic("upgrade", "head")
    assert result.returncode == 0, result.stderr
    heads = _alembic("heads")
    assert heads.returncode == 0, heads.stderr
    head_lines = [
        line.strip() for line in heads.stdout.splitlines() if line.strip()
    ]
    assert len(head_lines) == 1, (
        f"alembic heads must report exactly one revision; got {head_lines}"
    )
    assert head_lines[0].startswith("0007_v1_system_trusted_org"), (
        f"head must be 0007_v1_system_trusted_org, got {head_lines[0]!r}"
    )


def test_0007_adds_trusted_organization_id_with_fk_and_index(fresh_db):
    """0007 must add the trusted_organization_id column with a FK to
    v1_organizations and the (principal_type, purpose,
    trusted_organization_id) compound index."""
    _alembic("upgrade", "head")
    engine = sa.create_engine(fresh_db)
    try:
        assert _has_column(
            engine, "v1_api_credentials", "trusted_organization_id",
        )
        insp = sa.inspect(engine)
        idx = {ix["name"] for ix in insp.get_indexes("v1_api_credentials")}
        assert "ix_v1_api_credentials_system_org_binding" in idx
        fks = {
            fk["name"]
            for fk in insp.get_foreign_keys("v1_api_credentials")
        }
        assert "fk_v1_api_credentials_trusted_organization_id" in fks
    finally:
        engine.dispose()


def test_0007_round_trip_downgrade_then_upgrade(fresh_db):
    """0007 must upgrade, downgrade, and re-upgrade without errors.
    On downgrade, the column, FK, and index are dropped in inverse
    order. Re-upgrade brings them back.
    """
    _alembic("upgrade", "0006_v1_orm_alignment")
    assert _alembic("upgrade", "0007_v1_system_trusted_org").returncode == 0
    engine = sa.create_engine(fresh_db)
    try:
        assert _has_column(
            engine, "v1_api_credentials", "trusted_organization_id",
        )
    finally:
        engine.dispose()
    assert _alembic("downgrade", "0006_v1_orm_alignment").returncode == 0
    engine = sa.create_engine(fresh_db)
    try:
        assert not _has_column(
            engine, "v1_api_credentials", "trusted_organization_id",
        )
        insp = sa.inspect(engine)
        idx = {ix["name"] for ix in insp.get_indexes("v1_api_credentials")}
        assert "ix_v1_api_credentials_system_org_binding" not in idx
    finally:
        engine.dispose()
    assert _alembic("upgrade", "0007_v1_system_trusted_org").returncode == 0
    engine = sa.create_engine(fresh_db)
    try:
        assert _has_column(
            engine, "v1_api_credentials", "trusted_organization_id",
        )
    finally:
        engine.dispose()
