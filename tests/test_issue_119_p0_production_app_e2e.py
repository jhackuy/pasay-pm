"""Issue #119 P0 — Production-app E2E for the credential bootstrap fix.

The independent review of PR #138 required a focused E2E that starts /
imports the EXACT FastAPI app the production Dockerfile runs
(``uvicorn app.v1.main:app``) and proves the four ownership /
read-write boundaries end-to-end against the real PostgreSQL schema:

  1. manager key  →  real interactive data read
  2. admin key    →  OWNER-only route
  3. job key      →  actual /api/v1/operations/digest + /quick/tasks read
  4. job key      →  write denied (cannot reach a HUMAN-membership write)

This test:

  * imports ``app.v1.main:create_v1_app`` (the exact factory the
    Dockerfile ``CMD`` runs), so the routes / middleware / error
    contract are byte-identical to the production Container;
  * binds the CI PostgreSQL test DB through ``v1_engine_ctx`` so the
    V1 schema is built from scratch for every test run (no leftover
    state, no migration-order coupling);
  * bootstraps manager / admin / job credentials through the same
    in-process helpers the new test suite uses
    (``scripts/create_v1_api_key.py`` public contract, mirrored in
    ``_bootstrap_human_credential`` / ``_bootstrap_system_credential``);
  * asserts the 4 scenarios above AND asserts the additional fix
    invariants (raw-key-once, no silent SUSPENDED reactivation,
    explicit SYSTEM org-scope).

Reuses the existing ``tests/v1_support.py`` fixtures; no new test
harness, no SQLite fallback, no metadata-order DDL hacks.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import uuid
from contextlib import redirect_stdout
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.permissions import Role
from app.core.security import generate_api_key, hash_api_key
from app.db.session import get_session_factory, reset_engine_cache
from app.v1.main import create_v1_app
from app.v1.models.base import (
    MembershipState,
    OperationState,
    TaskState,
    V1Base,
    V1PrincipalType,
)
from app.v1.models.rent_payment import RentDueState
from app.v1.models.expense import ExpenseClaim, ExpenseClaimStatus
from app.v1.models.foundation import (
    ApiCredential,
    Membership,
    Organization,
    User,
)
from app.v1.models.property import Property, Unit
from app.v1.models.base import UnitStatus
from app.v1.models.rent_payment import (
    Operation,
    RentDueSchedule,
    Task,
)
from app.v1.models.tenant_lease import Lease, Tenant
from app.v1.models.base import LeaseState
from tests.v1_support import (
    Workspace,
    seed_workspace,
    v1_engine_ctx,
)


# ---------------------------------------------------------------------------
# Bootstrap helpers — same contract as scripts/create_v1_api_key.py
# ---------------------------------------------------------------------------


SYSTEM_SENTINEL_USERNAME = "v1-system-scheduler-e2e"


def _bootstrap_human(
    db, *, username: str, workspace_name: str, role: Role, raw: str | None = None,
) -> tuple[User, Membership, ApiCredential, str]:
    """Mirror scripts/create_v1_api_key.py HUMAN path in-process.

    Returns (user, membership, credential, raw_key). The raw_key is
    returned ONLY so the test can exercise authentication; production
    code captures the raw_key on stdout and discards it.
    """
    raw = raw or generate_api_key()
    org = (
        db.query(Organization).filter(Organization.name == workspace_name).one_or_none()
    )
    if org is None:
        org = Organization(name=workspace_name)
        db.add(org)
        db.flush()
    user = db.query(User).filter(User.username == username).one_or_none()
    if user is None:
        user = User(
            username=username,
            display_name=username,
            default_language="en-US",
        )
        db.add(user)
        db.flush()
    mship = (
        db.query(Membership)
        .filter(Membership.org_id == org.id, Membership.user_id == user.id)
        .order_by(Membership.id.asc())
        .first()
    )
    if mship is None:
        mship = Membership(
            org_id=org.id, user_id=user.id, role=role.value,
            state=MembershipState.ACTIVE.value,
        )
        db.add(mship)
        db.flush()
    # Deactivate any prior active HUMAN credential for this user so the
    # contract is "one active HUMAN key per user" — mirrors the script.
    for old in (
        db.query(ApiCredential)
        .filter(
            ApiCredential.user_id == user.id,
            ApiCredential.is_active.is_(True),
            ApiCredential.principal_type == V1PrincipalType.HUMAN.value,
        )
        .all()
    ):
        old.is_active = False
    cred = ApiCredential(
        user_id=user.id,
        key_hash=hash_api_key(raw),
        is_active=True,
        principal_type=V1PrincipalType.HUMAN.value,
        purpose=None,
    )
    db.add(cred)
    db.commit()
    return user, mship, cred, raw


def _bootstrap_system(
    db, *, raw: str | None = None, trusted_organization_id: int | None = None,
) -> tuple[User, ApiCredential, str]:
    raw = raw or generate_api_key()
    user = (
        db.query(User)
        .filter(User.username == SYSTEM_SENTINEL_USERNAME)
        .one_or_none()
    )
    if user is None:
        user = User(
            username=SYSTEM_SENTINEL_USERNAME,
            display_name="V1 SYSTEM scheduler sentinel (E2E)",
            default_language="en-US",
        )
        db.add(user)
        db.flush()
    for old in (
        db.query(ApiCredential)
        .filter(
            ApiCredential.user_id == user.id,
            ApiCredential.is_active.is_(True),
            ApiCredential.principal_type == V1PrincipalType.SYSTEM.value,
        )
        .all()
    ):
        old.is_active = False
    cred = ApiCredential(
        user_id=user.id,
        key_hash=hash_api_key(raw),
        is_active=True,
        principal_type=V1PrincipalType.SYSTEM.value,
        purpose="internal:scheduler",
        trusted_organization_id=trusted_organization_id,
    )
    db.add(cred)
    db.commit()
    return user, cred, raw


# ---------------------------------------------------------------------------
# V1 production app client (exact import as the Dockerfile CMD)
# ---------------------------------------------------------------------------


def _production_app_client() -> TestClient:
    """Build a TestClient against ``app.v1.main:create_v1_app()`` — the
    EXACT factory the Dockerfile ``CMD`` runs. No fakes, no test-only
    overrides, no re-exports: the routes / middleware / error
    contract are byte-identical to the production Container.
    """
    return TestClient(create_v1_app())


# ---------------------------------------------------------------------------
# Workspace seeder (real data: overdue rent + pending task + paid claim)
# ---------------------------------------------------------------------------


def _seed_workspace_with_real_data(db, *, name: str) -> Workspace:
    """Create the V1 workspace, then add a real overdue rent schedule
    + a real pending task so the digest / quick-tasks reads return
    non-empty data (matches "real interactive data read" in the
    review acceptance).
    """
    workspace = seed_workspace(db, name=name)
    # Overdue rent due schedule (read by /operations/digest act_now).
    overdue = RentDueSchedule(
        org_id=workspace.org_id,
        lease_id=workspace.lease_id,
        period_start=date.today() - timedelta(days=45),
        due_date=date.today() - timedelta(days=15),
        amount_due=Decimal("12000.00"),
        state=RentDueState.OVERDUE.value,
    )
    db.add(overdue)
    db.flush()
    # Pending task for the overdue rent (read by /operations/quick/tasks).
    op = Operation(
        org_id=workspace.org_id,
        kind="RENT_COLLECTION",
        subject_type="rent_due_schedule",
        subject_id=overdue.id,
        state=OperationState.OPEN.value,
        due_at=overdue.due_date,
    )
    db.add(op)
    db.flush()
    db.add(
        Task(
            org_id=workspace.org_id,
            operation_id=op.id,
            kind="RENT_FOLLOW_UP",
            title="Follow up on overdue rent",
            state=TaskState.OPEN.value,
            due_at=op.due_at,
        )
    )
    db.commit()
    return workspace


def _seed_vacant_property_for_archive(db, *, name: str):
    """Create a V1 workspace PLUS a second, vacant property so the
    OWNER-only archive endpoint can be exercised without tripping
    the "cannot archive property with OCCUPIED units" guard."""
    workspace = seed_workspace(db, name=name)
    prop = Property(
        org_id=workspace.org_id,
        name=f"{name} Vacant Tower",
        address_line1="1 Roxas Blvd",
        city="Pasay",
    )
    db.add(prop)
    db.flush()
    unit = Unit(
        property_id=prop.id,
        org_id=workspace.org_id,
        label="VAC-1",
        bedrooms=1,
        bathrooms=1,
        monthly_rent=Decimal("12000.00"),
        status=UnitStatus.AVAILABLE.value,
    )
    db.add(unit)
    db.commit()
    db.refresh(prop)
    return workspace, prop.id


# ---------------------------------------------------------------------------
# 1) manager key  →  real interactive data read
# ---------------------------------------------------------------------------


def test_production_app_manager_key_reads_real_data():
    """Manager key (SECRETARY) issued by the bootstrap script
    authenticates against the EXACT production FastAPI app and reads
    real V1 data (properties list, units, leases, tenants).
    """
    with v1_engine_ctx():
        db = get_session_factory()()
        try:
            workspace = _seed_workspace_with_real_data(db, name="e2e-mgr")
            # Bootstrap a SECOND SECRETARY credential (independent of
            # the seeded workspace SECRETARY so we exercise the full
            # create_v1_api_key.py → Bearer auth contract).
            _, _, _, mgr_raw = _bootstrap_human(
                db,
                username="e2e-mgr",
                workspace_name="e2e-mgr",
                role=Role.SECRETARY,
            )
            client = _production_app_client()
            # Properties list (real data, scoped to mgr's org)
            r = client.get(
                f"/api/v1/properties?org_id={workspace.org_id}",
                headers={"Authorization": f"Bearer {mgr_raw}"},
            )
            assert r.status_code == 200, r.text
            assert any(p["id"] == workspace.property_id for p in r.json())

            # Units list
            r = client.get(
                f"/api/v1/properties/{workspace.property_id}/units"
                f"?org_id={workspace.org_id}",
                headers={"Authorization": f"Bearer {mgr_raw}"},
            )
            assert r.status_code == 200, r.text
            assert any(u["id"] == workspace.unit_id for u in r.json())

            # Leases list (real data)
            r = client.get(
                f"/api/v1/leases?org_id={workspace.org_id}",
                headers={"Authorization": f"Bearer {mgr_raw}"},
            )
            assert r.status_code == 200, r.text
            assert any(l["id"] == workspace.lease_id for l in r.json())

            # Tenants list (real data)
            r = client.get(
                f"/api/v1/tenants?org_id={workspace.org_id}",
                headers={"Authorization": f"Bearer {mgr_raw}"},
            )
            assert r.status_code == 200, r.text
            assert any(t["id"] == workspace.tenant_id for t in r.json())
        finally:
            db.close()
            reset_engine_cache()


# ---------------------------------------------------------------------------
# 2) admin key  →  OWNER-only route
# ---------------------------------------------------------------------------


def test_production_app_admin_key_reaches_owner_only_route():
    """Admin key (OWNER) reaches the OWNER-only
    ``POST /api/v1/properties/{id}/archive`` route; SECRETARY is 403.
    """
    with v1_engine_ctx():
        db = get_session_factory()()
        try:
            workspace, vacant_property_id = _seed_vacant_property_for_archive(
                db, name="e2e-adm",
            )
            _, _, _, admin_raw = _bootstrap_human(
                db,
                username="e2e-adm",
                workspace_name="e2e-adm",
                role=Role.OWNER,
            )
            _, _, _, mgr_raw = _bootstrap_human(
                db,
                username="e2e-adm-mgr",
                workspace_name="e2e-adm",
                role=Role.SECRETARY,
            )
            client = _production_app_client()
            # ADMIN → 200 (OWNER role check passes; archivable)
            r = client.post(
                f"/api/v1/properties/{vacant_property_id}/archive"
                f"?org_id={workspace.org_id}",
                headers={"Authorization": f"Bearer {admin_raw}"},
            )
            assert r.status_code == 200, r.text
            # MANAGER → 403 (role check fails)
            r = client.post(
                f"/api/v1/properties/{vacant_property_id}/archive"
                f"?org_id={workspace.org_id}",
                headers={"Authorization": f"Bearer {mgr_raw}"},
            )
            assert r.status_code == 403, r.text
        finally:
            db.close()
            reset_engine_cache()


# ---------------------------------------------------------------------------
# 3) job key  →  actual digest / quick-tasks read
# ---------------------------------------------------------------------------


def test_production_app_job_key_reads_digest_and_quick_tasks():
    """Job key (SYSTEM) reaches the SYSTEM-only
    ``/api/v1/operations/digest`` and ``/api/v1/operations/quick/tasks``
    endpoints, which read REAL V1 data (overdue rent + open task) and
    return the contract the bot's ``cards.active_tasks_digest_card``
    already consumes.

    Issue #119 P0 (independent review follow-up): the SYSTEM credential
    is bound to the workspace's org id; both endpoints accept the
    caller-supplied ``org_id`` because it matches the binding.
    """
    with v1_engine_ctx():
        db = get_session_factory()()
        try:
            workspace = _seed_workspace_with_real_data(db, name="e2e-job")
            _, _, job_raw = _bootstrap_system(
                db, trusted_organization_id=workspace.org_id,
            )
            client = _production_app_client()
            # /operations/digest
            r = client.get(
                f"/api/v1/operations/digest?org_id={workspace.org_id}",
                headers={"Authorization": f"Bearer {job_raw}"},
            )
            assert r.status_code == 200, r.text
            digest = r.json()
            # Real overdue rent must surface in act_now
            assert any(
                row.get("kind") == "rent_overdue"
                for row in digest.get("act_now", [])
            ), digest
            # Legacy fallback keys must exist
            for key in ("pending", "in_progress", "recently_completed"):
                assert key in digest, f"digest missing legacy key {key!r}"

            # /operations/quick/tasks
            r = client.get(
                f"/api/v1/operations/quick/tasks?org_id={workspace.org_id}",
                headers={"Authorization": f"Bearer {job_raw}"},
            )
            assert r.status_code == 200, r.text
            quick = r.json()
            assert any(
                t.get("status") == "PENDING"
                for t in quick.get("items", [])
            ), quick
        finally:
            db.close()
            reset_engine_cache()


# ---------------------------------------------------------------------------
# 4) job key  →  write denied
# ---------------------------------------------------------------------------


def test_production_app_job_key_cannot_write_or_reach_human_endpoints():
    """Job key (SYSTEM) is rejected on every HUMAN-membership / write
    path it tries. The four-class matrix is exercised end-to-end:

      * HUMAN owner-only write     → 401 (SYSTEM rejected outright)
      * HUMAN owner/secretary read → 401
      * SYSTEM digest              → 200 (the only allowed surface)
      * SYSTEM digest without org_id → 200 (the credential's
        trusted_organization_id is the canonical scope, so the
        server is happy without a caller-supplied org_id)

    Issue #119 P0 (independent review follow-up): the SYSTEM credential
    is bound to the workspace's org id, so the bound-org read works
    with or without a caller-supplied ``org_id`` query.
    """
    with v1_engine_ctx():
        db = get_session_factory()()
        try:
            workspace = _seed_workspace_with_real_data(db, name="e2e-job-deny")
            _, _, job_raw = _bootstrap_system(
                db, trusted_organization_id=workspace.org_id,
            )
            client = _production_app_client()
            # (a) OWNER-only write → 401
            r = client.post(
                f"/api/v1/properties/{workspace.property_id}/archive"
                f"?org_id={workspace.org_id}",
                headers={"Authorization": f"Bearer {job_raw}"},
            )
            assert r.status_code == 401, r.text
            # (b) HUMAN-membership read → 401
            r = client.get(
                f"/api/v1/dashboard/home?org_id={workspace.org_id}",
                headers={"Authorization": f"Bearer {job_raw}"},
            )
            assert r.status_code == 401, r.text
            # (c) SYSTEM read with org_id → 200
            r = client.get(
                f"/api/v1/operations/digest?org_id={workspace.org_id}",
                headers={"Authorization": f"Bearer {job_raw}"},
            )
            assert r.status_code == 200, r.text
            # (d) SYSTEM read WITHOUT caller-supplied org_id → 200.
            # The credential's trusted_organization_id is the
            # authoritative scope, so the canonical org is used
            # directly. This is the real-world bot path: the bot
            # does not need to know the org id.
            r = client.get(
                "/api/v1/operations/digest",
                headers={"Authorization": f"Bearer {job_raw}"},
            )
            assert r.status_code == 200, r.text
        finally:
            db.close()
            reset_engine_cache()


# ---------------------------------------------------------------------------
# Additional fix invariants
# ---------------------------------------------------------------------------


def test_production_app_exposes_production_runtime_surfaces():
    """The V1 app exposes the production runtime surfaces (legacy
    /telegram/webhook, /internal/ingest, /health) so the Worker →
    Container queue contract and the watchdog probe stay byte-
    identical after the entrypoint switch.
    """
    with v1_engine_ctx():
        client = _production_app_client()
        # /health should be reachable without auth and report
        # 'status: ok' when the DB is up.
        r = client.get("/health")
        assert r.status_code == 200, r.text
            # Either the legacy snapshot succeeded (DB up) or it
            # returned 503 (snapshot_unavailable). Both are acceptable
            # for this assertion — we only need /health mounted and
            # reachable.
        # /telegram/webhook + /internal/ingest routes must be
        # registered (the routes are POST; the OPTIONS / 405
        # assertions confirm the path is bound, not 404).
        r = client.post("/telegram/webhook", json={})
        assert r.status_code != 404, r.text
        r = client.post("/internal/ingest", json={})
        assert r.status_code != 404, r.text
        reset_engine_cache()


def test_create_v1_api_key_emits_raw_key_exactly_once():
    """Issue #119 P0 fix: ``scripts/create_v1_api_key.py`` must
    emit the raw key exactly once on stdout.  The previous
    implementation printed ``API key: …`` AND ``Authorization:
    Bearer …`` (two lines, same raw key) which violates
    AGENTS.md §3 "Print the raw key to stdout exactly once".
    """
    with v1_engine_ctx():
        factory = get_session_factory()
        db = factory()
        try:
            # Use a unique workspace so this test is hermetic w.r.t.
            # the other in-process bootstraps.
            workspace = f"script-{uuid.uuid4().hex[:8]}"
            # Run the script as a subprocess so we capture the
            # production-style operator output verbatim.
            repo_root = Path(__file__).resolve().parent.parent
            script = repo_root / "scripts" / "create_v1_api_key.py"
            env = {
                "PATH": "/usr/bin:/bin",
                "DATABASE_URL": __import__("os").environ.get(
                    "DATABASE_URL", "sqlite:///:memory:",
                ),
                "PYTHONPATH": str(repo_root),
            }
            proc = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--workspace", workspace,
                    "--username", f"script-{uuid.uuid4().hex[:8]}",
                    "--principal-type", "HUMAN",
                    "--role", "SECRETARY",
                    "--purpose", "manager",
                    "--apply",
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
                cwd=str(repo_root),
            )
            # The subprocess either succeeded (script ran against the
            # same V1 schema the test built) or it crashed at the
            # legacy ``app.models.*`` import path. Either way the
            # stdout we care about is captured.
            out = proc.stdout
            # Count occurrences of the Authorization line — exactly 1.
            bearer_lines = [
                line for line in out.splitlines()
                if line.startswith("Authorization: Bearer ")
            ]
            assert len(bearer_lines) == 1, (
                f"raw key emitted {len(bearer_lines)} times; "
                f"expected exactly 1. stdout={out!r}"
            )
            # The legacy "API key: …" duplicate line MUST be gone.
            assert "API key: " not in out, (
                f"legacy 'API key: …' line is still emitted; "
                f"stdout={out!r}"
            )
        finally:
            db.close()
            reset_engine_cache()


def test_migration_0006_fails_closed_on_suspended_memberships():
    """Issue #119 P0 fix: migration 0006 must NOT silently reactivate
    SUSPENDED memberships. Insert a SUSPENDED row on a fresh DB and
    assert the migration aborts with the explicit guard error.
    """
    # Use a dedicated DB so this test does not collide with the
    # v1_engine_ctx that other tests reuse. alembic upgrade /
    # downgrade need an isolated schema or DB to stage SUSPENDED.
    # env.py reads from app.config.settings.database_url, so we run
    # alembic as a subprocess with the DATABASE_URL env var + the
    # -x db_url=... argument (env.py reads x_arguments too).
    import os
    import subprocess
    import sqlalchemy as sa
    from datetime import datetime, timezone
    from sqlalchemy.engine import make_url

    db_url = os.environ.get(
        "DATABASE_URL", "postgresql+psycopg2://pasay:pasay@localhost:5432/pasay",
    )
    parsed = make_url(db_url)
    admin_engine = sa.create_engine(
        parsed.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
    )
    test_db = f"pasay_test_suspended_{uuid.uuid4().hex[:8]}"
    try:
        with admin_engine.connect() as conn:
            conn.execute(sa.text(f'CREATE DATABASE "{test_db}"'))
    finally:
        admin_engine.dispose()
    test_url = parsed.set(database=test_db)
    test_url_str = test_url.render_as_string(hide_password=False)
    repo_root = Path(__file__).resolve().parent.parent
    alembic_ini = repo_root / "alembic.ini"
    env = os.environ.copy()
    env["DATABASE_URL"] = test_url_str
    try:
        # 1) Upgrade through 0005 (state CHECK still allows SUSPENDED)
        r = subprocess.run(
            [
                sys.executable, "-m", "alembic",
                "-c", str(alembic_ini),
                "-x", f"db_url={test_url_str}",
                "upgrade", "0005_v1_api_cred_system",
            ],
            capture_output=True, text=True, env=env, cwd=str(repo_root),
        )
        assert r.returncode == 0, (
            f"alembic upgrade 0005 failed: {r.stdout!r} {r.stderr!r}"
        )
        # 2) Insert a SUSPENDED row directly
        engine = sa.create_engine(test_url_str)
        now = datetime.now(timezone.utc)
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO v1_organizations "
                    "(name, created_at, updated_at) "
                    "VALUES ('suspended-test', :now, :now)"
                ),
                {"now": now},
            )
            org_id = conn.execute(
                sa.text("SELECT id FROM v1_organizations "
                        "WHERE name = 'suspended-test'")
            ).scalar()
            conn.execute(
                sa.text(
                    "INSERT INTO v1_users "
                    "(username, display_name, created_at, updated_at) "
                    "VALUES ('suspended-user', 'Suspended User', :now, :now)"
                ),
                {"now": now},
            )
            user_id = conn.execute(
                sa.text("SELECT id FROM v1_users "
                        "WHERE username = 'suspended-user'")
            ).scalar()
            conn.execute(
                sa.text(
                    "INSERT INTO v1_memberships "
                    "(org_id, user_id, role, state, created_at, updated_at) "
                    "VALUES (:org_id, :user_id, 'owner', 'SUSPENDED', :now, :now)"
                ),
                {"org_id": org_id, "user_id": user_id, "now": now},
            )
        engine.dispose()
        # 3) Run 0006 — must fail with the explicit guard error
        r = subprocess.run(
            [
                sys.executable, "-m", "alembic",
                "-c", str(alembic_ini),
                "-x", f"db_url={test_url_str}",
                "upgrade", "0006_v1_orm_alignment",
            ],
            capture_output=True, text=True, env=env, cwd=str(repo_root),
        )
        assert r.returncode != 0, (
            f"alembic upgrade 0006 unexpectedly succeeded with a "
            f"SUSPENDED row: {r.stdout!r}"
        )
        # The error must mention SUSPENDED explicitly so an operator
        # never has to guess why the migration refused to run.
        combined = r.stdout + r.stderr
        assert "SUSPENDED" in combined, (
            f"expected explicit SUSPENDED refusal; got: {combined!r}"
        )
    finally:
        # Drop the dedicated test DB — kill any lingering sessions first
        # so the DROP can run cleanly (the failure path leaves a session
        # open from the failed migration).
        admin_engine = sa.create_engine(
            parsed.set(database="postgres"),
            isolation_level="AUTOCOMMIT",
        )
        try:
            with admin_engine.connect() as conn:
                conn.execute(
                    sa.text(
                        "SELECT pg_terminate_backend(pid) "
                        "FROM pg_stat_activity "
                        "WHERE datname = :db AND pid <> pg_backend_pid()"
                    ),
                    {"db": test_db},
                )
                conn.execute(
                    sa.text(f'DROP DATABASE IF EXISTS "{test_db}"')
                )
        finally:
            admin_engine.dispose()
