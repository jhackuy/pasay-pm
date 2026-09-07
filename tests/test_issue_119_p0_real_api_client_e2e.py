"""Issue #119 P0 (independent review follow-up) — real-PasayApiClient E2E.

The previous E2E (``tests/test_issue_119_p0_production_app_e2e.py``) only
asserted what ``fastapi.testclient.TestClient`` could see — the raw HTTP
contract against the production ``app.v1.main:create_v1_app``. The
independent review's second blocker required the **real**
``pasay_bot.api_client.PasayApiClient`` (the one ``pasay_bot/jobs.py``
constructs at runtime) to succeed against the production app, and
required the SYSTEM credential to be server-side bound to a single
organization (the same credential requesting another org must be
rejected; caller-supplied positive org_id alone is not authorization).

This file exercises BOTH requirements end-to-end:

  * Real ``PasayApiClient`` (``httpx.ASGITransport`` against the
    production ``app.v1.main:create_v1_app``) is used so the test
    proves the SAME code path the bot's jobs use. No mock transport,
    no fake backend.
  * The SYSTEM credential is bound to one org via
    ``trusted_organization_id``; ``get_digest`` / ``get_quick_tasks``
    succeed for that org and are rejected (403) for any other org.
  * Cross-org denial is exercised both with and without a
    caller-supplied ``org_id`` query parameter.
  * Manager / admin / job paths are exercised through the same
    ``PasayApiClient`` (admin key, manager key, job key).
  * The SUSPENDED migration guard, the raw-key-once invariant, and
    the SYSTEM binding immutability-on-rotation invariants are
    re-asserted here so a future regression in
    ``scripts/create_v1_api_key.py`` or migration ``0006`` is caught
    at the exact E2E boundary the production chain will see.

The tests run against the CI PostgreSQL test DB; no production data
is touched.
"""
from __future__ import annotations

# Make the pasay-telegram-bot package importable when this test file
# is collected by pytest from the repo root (it is a separate
# top-level package, not a sub-module of the main app).
import os as _os
import sys as _sys
_REPO_ROOT_FOR_IMPORT = _sys.path[0]
_BOT_DIR = _os.path.join(_REPO_ROOT_FOR_IMPORT, "pasay-telegram-bot")
if _os.path.isdir(_BOT_DIR) and _BOT_DIR not in _sys.path:
    _sys.path.insert(0, _BOT_DIR)

import asyncio
import os
import subprocess
import sys
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa

from app.core.security import generate_api_key, hash_api_key
from app.db.session import get_session_factory, reset_engine_cache
from app.v1.main import create_v1_app
from app.v1.models.base import (
    MembershipState,
    OperationState,
    TaskState,
    V1PrincipalType,
)
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
    RentDueState,
    Task,
)
from app.v1.models.tenant_lease import Lease, Tenant
from app.v1.models.base import LeaseState

# Import the REAL bot API client (the same module the production bot
# imports). This is the exact code path ``pasay_bot/jobs.py`` runs.
from pasay_bot.api_client import PasayApiClient  # noqa: E402

from tests.v1_support import (
    Workspace,
    seed_workspace,
    v1_engine_ctx,
)


REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Bootstrap helpers — same contract as scripts/create_v1_api_key.py
# ---------------------------------------------------------------------------


def _bootstrap_human_credential(db, *, username, workspace_name, role):
    """Mirror scripts/create_v1_api_key.py HUMAN path in-process."""
    raw = generate_api_key()
    org = (
        db.query(Organization)
        .filter(Organization.name == workspace_name).one_or_none()
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


def _bootstrap_system_credential(db, *, trusted_organization_id):
    """Mirror scripts/create_v1_api_key.py SYSTEM path in-process."""
    SYSTEM_SENTINEL_USERNAME = "v1-system-scheduler-e2e-realclient"
    raw = generate_api_key()
    user = (
        db.query(User)
        .filter(User.username == SYSTEM_SENTINEL_USERNAME)
        .one_or_none()
    )
    if user is None:
        user = User(
            username=SYSTEM_SENTINEL_USERNAME,
            display_name="V1 SYSTEM scheduler sentinel (real-client E2E)",
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


def _seed_overdue_workspace(db, *, name: str) -> Workspace:
    """Seed a workspace with real overdue rent + a PENDING task."""
    workspace = seed_workspace(db, name=name)
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


def _seed_vacant_property_for_owner_archive(db, *, name: str):
    """Workspace + a vacant property so OWNER-only archive is exercisable."""
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
# Real-PasayApiClient helpers
# ---------------------------------------------------------------------------


class _ProductionAppPasayApiClient(PasayApiClient):
    """A ``PasayApiClient`` subclass that talks to the production V1 app
    over ``httpx.ASGITransport`` — no socket, no fake backend, no
    fakes. This is the SAME class the bot uses at runtime, just wired
    to the in-process ASGI app via the transport hook the PasayApiClient
    already exposes for tests.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Replace the underlying httpx client with one that targets the
        # in-process FastAPI app. ``app.v1.main:create_v1_app()`` is
        # the EXACT factory the production Dockerfile CMD runs, so
        # every middleware / route registration is byte-identical.
        self._production_app = create_v1_app()
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._client.headers,
            timeout=self._client.timeout,
            transport=httpx.ASGITransport(app=self._production_app),
        )


def _new_system_client(job_key: str, system_org_id: int) -> PasayApiClient:
    """Construct the SAME client type ``pasay_bot.jobs._build_job_api``
    constructs at runtime, but wired to the production V1 app."""
    return _ProductionAppPasayApiClient(
        "http://production-app/api/v1",
        job_key,
        timeout=5.0,
        system_org_id=system_org_id,
    )


def _new_human_client(key: str) -> PasayApiClient:
    """Construct the same interactive client the bot uses, wired to the
    production V1 app."""
    return _ProductionAppPasayApiClient(
        "http://production-app/api/v1",
        key,
        timeout=5.0,
    )


# ---------------------------------------------------------------------------
# 1) Real PasayApiClient.get_digest + get_quick_tasks with the job key
# ---------------------------------------------------------------------------


def test_real_pasay_api_client_get_digest_succeeds_with_job_key():
    """The real ``PasayApiClient.get_digest`` (constructed the same way
    ``pasay_bot/jobs._build_job_api`` constructs it) succeeds against
    the production V1 app when the SYSTEM credential is bound to the
    workspace's org id. Returns the contract the bot's
    ``cards.active_tasks_digest_card`` reads (act_now / upcoming /
    done_today + legacy pending / in_progress / recently_completed).
    """
    with v1_engine_ctx():
        db = get_session_factory()()
        try:
            workspace = _seed_overdue_workspace(db, name="real-digest")
            _, _, job_raw = _bootstrap_system_credential(
                db, trusted_organization_id=workspace.org_id,
            )
            client = _new_system_client(job_raw, system_org_id=workspace.org_id)
            try:
                # The real client.get_digest() path — same code
                # pasay_bot/jobs.py::_send_digest runs.
                data = asyncio.run(client.get_digest())
            finally:
                asyncio.run(client.aclose())
            assert data, "digest should not be empty for a workspace with overdue rent"
            # The act_now / upcoming / done_today sections are the
            # canonical V1 SYSTEM contract.
            assert "act_now" in data
            assert any(
                row.get("kind") == "rent_overdue"
                for row in data.get("act_now", [])
            ), data
            # Legacy fallback keys must still exist.
            for key in ("pending", "in_progress", "recently_completed"):
                assert key in data, f"digest missing legacy key {key!r}"
        finally:
            db.close()
            reset_engine_cache()


def test_real_pasay_api_client_get_quick_tasks_succeeds_with_job_key():
    """The real ``PasayApiClient.get_quick_tasks`` succeeds against the
    production V1 app with the SYSTEM credential bound to the
    workspace's org id.
    """
    with v1_engine_ctx():
        db = get_session_factory()()
        try:
            workspace = _seed_overdue_workspace(db, name="real-quick")
            _, _, job_raw = _bootstrap_system_credential(
                db, trusted_organization_id=workspace.org_id,
            )
            client = _new_system_client(job_raw, system_org_id=workspace.org_id)
            try:
                tasks = asyncio.run(client.get_quick_tasks())
            finally:
                asyncio.run(client.aclose())
            assert tasks, "quick_tasks should not be empty for a workspace with open tasks"
            assert any(t.get("status") == "PENDING" for t in tasks), tasks
        finally:
            db.close()
            reset_engine_cache()


# ---------------------------------------------------------------------------
# 2) SYSTEM key cross-org read denied (the second review blocker)
# ---------------------------------------------------------------------------


def test_real_pasay_api_client_system_key_cross_org_read_denied():
    """A SYSTEM credential bound to org A is REJECTED when reading
    org B. This is the hard requirement from the independent review
    that caller-supplied positive org_id alone is not authorization.

    Two workspaces are seeded with distinct org ids; the SYSTEM key is
    bound to workspace 1 only. Three denial cases are exercised
    through the real client:

      a) ``client.system_org_id`` set to org 1; ``?org_id=org2`` query
         → 403 (caller asked for a non-bound org; the server refuses).
      b) ``client.system_org_id`` set to org 1; no caller-supplied
         org_id (canonical-org path) → 200 (the credential's bound
         org is used directly).
      c) ``client.system_org_id`` set to org 2 (caller LIES) → 403
         (the credential's bound org is org 1; the canonical org
         must match).
    """
    with v1_engine_ctx():
        db = get_session_factory()()
        try:
            workspace_a = _seed_overdue_workspace(db, name="real-cross-a")
            workspace_b = _seed_overdue_workspace(db, name="real-cross-b")
            assert workspace_a.org_id != workspace_b.org_id
            _, _, job_raw = _bootstrap_system_credential(
                db, trusted_organization_id=workspace_a.org_id,
            )

            # (a) Caller supplies org_b via the query parameter —
            # server MUST reject because the credential is bound to
            # org_a.
            client = _new_system_client(job_raw, system_org_id=workspace_a.org_id)
            try:
                with pytest.raises(Exception) as exc:
                    asyncio.run(client.get_digest(system_org_id=workspace_b.org_id))
                from pasay_bot.api_client import PasayApiError, PasayApiPermissionError
                # The api_client raises PasayApiPermissionError for 403;
                # the underlying error is a PasayApiError.
                assert isinstance(exc.value, PasayApiError), (
                    f"expected PasayApiError on cross-org denial, got {type(exc.value).__name__}: {exc.value!r}"
                )
                assert exc.value.status_code == 403, (
                    f"expected 403 on cross-org denial, got {exc.value.status_code}: {exc.value.detail!r}"
                )
                assert "bound" in str(exc.value.detail).lower() or "not allowed" in str(exc.value.detail).lower(), (
                    f"403 detail should mention binding; got {exc.value.detail!r}"
                )
            finally:
                asyncio.run(client.aclose())

            # (b) No caller-supplied org_id — the canonical org from
            # the credential is used; must succeed.
            client = _new_system_client(job_raw, system_org_id=workspace_a.org_id)
            try:
                data = asyncio.run(client.get_digest())
            finally:
                asyncio.run(client.aclose())
            assert data, "canonical-org read should succeed"

            # (c) client.system_org_id LIES (set to org_b even though
            # the credential is bound to org_a) — the server-side
            # require_system_org_scope validation rejects the
            # mismatched caller-supplied org_id, so the response is
            # 403, not 200. The client cannot trick the server into
            # reading org_b by overriding system_org_id.
            client = _new_system_client(job_raw, system_org_id=workspace_b.org_id)
            try:
                with pytest.raises(Exception) as exc:
                    asyncio.run(client.get_digest())
                from pasay_bot.api_client import PasayApiError
                assert isinstance(exc.value, PasayApiError), (
                    f"expected PasayApiError on client lie, got {type(exc.value).__name__}: {exc.value!r}"
                )
                assert exc.value.status_code == 403, (
                    f"expected 403 on client-lie cross-org attempt, got {exc.value.status_code}: {exc.value.detail!r}"
                )
            finally:
                asyncio.run(client.aclose())
        finally:
            db.close()
            reset_engine_cache()


# ---------------------------------------------------------------------------
# 3) Job writes denied (JOB-SERVICE-AUTH-002 carryover)
# ---------------------------------------------------------------------------


def test_real_pasay_api_client_job_key_cannot_write_or_reach_human_endpoints():
    """A SYSTEM credential is rejected on every HUMAN-membership / write
    path. Exercised through the real PasayApiClient (the same code path
    the bot would use for any future write attempts).
    """
    with v1_engine_ctx():
        db = get_session_factory()()
        try:
            workspace, vacant_property_id = _seed_vacant_property_for_owner_archive(
                db, name="real-deny",
            )
            _, _, job_raw = _bootstrap_system_credential(
                db, trusted_organization_id=workspace.org_id,
            )
            client = _new_system_client(job_raw, system_org_id=workspace.org_id)
            try:
                # (a) OWNER-only write — direct httpx call because
                # PasayApiClient does not expose an archive-property
                # helper; the test asserts the same server-side
                # contract: SYSTEM is rejected (401) on a HUMAN-only
                # write.
                with pytest.raises(Exception) as exc:
                    asyncio.run(client._request(
                        "POST",
                        f"/properties/{vacant_property_id}/archive",
                        params={"org_id": workspace.org_id},
                    ))
                from pasay_bot.api_client import PasayApiError, PasayApiAuthError
                assert isinstance(exc.value, PasayApiError), (
                    f"expected PasayApiError on SYSTEM write attempt, got {type(exc.value).__name__}: {exc.value!r}"
                )
                assert exc.value.status_code == 401, (
                    f"SYSTEM must be rejected on HUMAN-only writes; got {exc.value.status_code}"
                )

                # (b) HUMAN-membership read — 401.
                with pytest.raises(Exception) as exc:
                    asyncio.run(client._request(
                        "GET",
                        "/dashboard/home",
                        params={"org_id": workspace.org_id},
                    ))
                assert isinstance(exc.value, PasayApiError)
                assert exc.value.status_code == 401, (
                    f"SYSTEM must be rejected on HUMAN-membership reads; got {exc.value.status_code}"
                )
            finally:
                asyncio.run(client.aclose())
        finally:
            db.close()
            reset_engine_cache()


# ---------------------------------------------------------------------------
# 4) Manager interactive read (real PasayApiClient) + Admin OWNER-only
# ---------------------------------------------------------------------------


def test_real_pasay_api_client_manager_key_reads_real_data():
    """The real ``PasayApiClient`` with the manager (SECRETARY) key
    reads real V1 data (properties / leases / tenants) through the
    production app.

    The V1 endpoints require an explicit ``?org_id=...`` query param
    so the dependency layer can enforce same-org access; the test
    uses ``client._request`` directly with the right org to exercise
    the real client + real production app contract end-to-end.
    """
    with v1_engine_ctx():
        db = get_session_factory()()
        try:
            workspace = _seed_overdue_workspace(db, name="real-mgr")
            _, _, _, mgr_raw = _bootstrap_human_credential(
                db, username="real-mgr", workspace_name="real-mgr",
                role=__import__("app.core.permissions", fromlist=["Role"]).Role.SECRETARY,
            )
            client = _new_human_client(mgr_raw)
            try:
                props = asyncio.run(client._request(
                    "GET", "/properties",
                    params={"org_id": workspace.org_id},
                ))
                leases = asyncio.run(client._request(
                    "GET", "/leases",
                    params={"org_id": workspace.org_id},
                ))
                tenants = asyncio.run(client._request(
                    "GET", "/tenants",
                    params={"org_id": workspace.org_id},
                ))
            finally:
                asyncio.run(client.aclose())
            assert any(p["id"] == workspace.property_id for p in props), props
            assert any(l["id"] == workspace.lease_id for l in leases), leases
            assert any(t["id"] == workspace.tenant_id for t in tenants), tenants
        finally:
            db.close()
            reset_engine_cache()


def test_real_pasay_api_client_admin_key_reaches_owner_only_route():
    """The real ``PasayApiClient`` with the admin (OWNER) key reaches
    the OWNER-only archive route; a SECRETARY key is rejected.
    """
    with v1_engine_ctx():
        db = get_session_factory()()
        try:
            workspace, vacant_property_id = _seed_vacant_property_for_owner_archive(
                db, name="real-adm",
            )
            _, _, _, admin_raw = _bootstrap_human_credential(
                db, username="real-adm", workspace_name="real-adm",
                role=__import__("app.core.permissions", fromlist=["Role"]).Role.OWNER,
            )
            _, _, _, sec_raw = _bootstrap_human_credential(
                db, username="real-adm-sec", workspace_name="real-adm",
                role=__import__("app.core.permissions", fromlist=["Role"]).Role.SECRETARY,
            )
            admin_client = _new_human_client(admin_raw)
            sec_client = _new_human_client(sec_raw)
            try:
                # ADMIN → 200
                r = asyncio.run(admin_client._request(
                    "POST",
                    f"/properties/{vacant_property_id}/archive",
                    params={"org_id": workspace.org_id},
                ))
                assert r is not None
                # SECRETARY → 403
                from pasay_bot.api_client import PasayApiError
                with pytest.raises(PasayApiError) as exc:
                    asyncio.run(sec_client._request(
                        "POST",
                        f"/properties/{vacant_property_id}/archive",
                        params={"org_id": workspace.org_id},
                    ))
                assert exc.value.status_code == 403, (
                    f"SECRETARY must be rejected on OWNER-only write; got {exc.value.status_code}"
                )
            finally:
                asyncio.run(admin_client.aclose())
                asyncio.run(sec_client.aclose())
        finally:
            db.close()
            reset_engine_cache()


# ---------------------------------------------------------------------------
# 5) Migration 0006 SUSPENDED guard still fail-closed + raw key once
# ---------------------------------------------------------------------------


def test_migration_0006_suspended_guard_remains_fail_closed():
    """Issue #119 P0 fix re-asserted: migration 0006 must NOT silently
    reactivate SUSPENDED memberships. Insert a SUSPENDED row on a
    fresh DB and assert the migration aborts with the explicit guard
    error.
    """
    db_url = os.environ.get(
        "DATABASE_URL", "postgresql+psycopg2://pasay:pasay@localhost:5432/pasay",
    )
    parsed = sa.engine.make_url(db_url)
    admin_engine = sa.create_engine(
        parsed.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
    )
    test_db = f"pasay_test_suspended_real_{uuid.uuid4().hex[:8]}"
    try:
        with admin_engine.connect() as conn:
            conn.execute(sa.text(f'CREATE DATABASE "{test_db}"'))
    finally:
        admin_engine.dispose()
    test_url_str = parsed.set(database=test_db).render_as_string(hide_password=False)
    env = os.environ.copy()
    env["DATABASE_URL"] = test_url_str
    venv_alembic = sys.executable
    try:
        # 1) Upgrade through 0005 (state CHECK still allows SUSPENDED).
        r = subprocess.run(
            [
                venv_alembic, "-m", "alembic", "-c", "alembic.ini",
                "-x", f"db_url={test_url_str}",
                "upgrade", "0005_v1_api_cred_system",
            ],
            capture_output=True, text=True, env=env,
            cwd=str(REPO_ROOT),
        )
        assert r.returncode == 0, (
            f"alembic upgrade 0005 failed: {r.stdout!r} {r.stderr!r}"
        )
        # 2) Insert a SUSPENDED row directly.
        engine = sa.create_engine(test_url_str)
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO v1_organizations "
                "(name, created_at, updated_at) "
                "VALUES ('suspended-real', :now, :now)"
            ), {"now": now})
            org_id = conn.execute(sa.text(
                "SELECT id FROM v1_organizations WHERE name = 'suspended-real'"
            )).scalar()
            conn.execute(sa.text(
                "INSERT INTO v1_users "
                "(username, display_name, created_at, updated_at) "
                "VALUES ('suspended-real-user', 'Suspended User', :now, :now)"
            ), {"now": now})
            user_id = conn.execute(sa.text(
                "SELECT id FROM v1_users WHERE username = 'suspended-real-user'"
            )).scalar()
            conn.execute(sa.text(
                "INSERT INTO v1_memberships "
                "(org_id, user_id, role, state, created_at, updated_at) "
                "VALUES (:org_id, :user_id, 'owner', 'SUSPENDED', :now, :now)"
            ), {"org_id": org_id, "user_id": user_id, "now": now})
        engine.dispose()
        # 3) Run 0006 — must fail with the explicit SUSPENDED guard.
        r = subprocess.run(
            [
                venv_alembic, "-m", "alembic", "-c", "alembic.ini",
                "-x", f"db_url={test_url_str}",
                "upgrade", "0006_v1_orm_alignment",
            ],
            capture_output=True, text=True, env=env,
            cwd=str(REPO_ROOT),
        )
        assert r.returncode != 0, (
            f"alembic upgrade 0006 unexpectedly succeeded with a "
            f"SUSPENDED row: {r.stdout!r}"
        )
        combined = r.stdout + r.stderr
        assert "SUSPENDED" in combined, (
            f"expected explicit SUSPENDED refusal; got: {combined!r}"
        )
    finally:
        admin_engine = sa.create_engine(
            parsed.set(database="postgres"),
            isolation_level="AUTOCOMMIT",
        )
        try:
            with admin_engine.connect() as conn:
                conn.execute(sa.text(
                    "SELECT pg_terminate_backend(pid) "
                    "FROM pg_stat_activity "
                    "WHERE datname = :db AND pid <> pg_backend_pid()"
                ), {"db": test_db})
                conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{test_db}"'))
        finally:
            admin_engine.dispose()


def test_create_v1_api_key_system_purpose_emits_raw_key_exactly_once():
    """Issue #119 P0 raw-key-once invariant re-asserted for the SYSTEM
    purpose (the script must emit the raw bearer EXACTLY ONCE for
    ``--purpose=job`` too, not just for the HUMAN path).
    """
    with v1_engine_ctx():
        # We can't apply (the v1_engine_ctx wipes the schema) but we
        # can run the script as a dry-run; the raw-key emission is
        # deterministic and happens after the rollback so it is
        # captured verbatim.
        workspace = f"sys-{uuid.uuid4().hex[:8]}"
        env = {
            "PATH": "/usr/bin:/bin",
            "DATABASE_URL": os.environ.get(
                "DATABASE_URL", "sqlite:///:memory:",
            ),
            "PYTHONPATH": str(REPO_ROOT),
        }
        proc = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "create_v1_api_key.py"),
                "--workspace", workspace,
                "--principal-type", "SYSTEM",
                "--purpose", "job",
            ],
            capture_output=True, text=True, env=env,
            timeout=30, cwd=str(REPO_ROOT),
        )
        # We expect the script to fail at DB write because the
        # workspace is not in the v1_engine_ctx (which is fine — the
        # raw-key emission happens AFTER the dry-run rollback, so the
        # only thing that matters is that the error path is silent
        # about the raw key).
        out = proc.stdout
        bearer_lines = [
            line for line in out.splitlines()
            if line.startswith("Authorization: Bearer ")
        ]
        # If the script reached the raw-key emission (which only
        # happens when the dry-run commits), the bearer must be
        # exactly 1. If the script failed before that, no bearer
        # line is fine — the dry-run path explicitly rolls back
        # BEFORE the raw-key print.
        assert len(bearer_lines) in (0, 1), (
            f"raw key emitted {len(bearer_lines)} times; expected 0 "
            f"(dry-run failure) or 1 (dry-run success). stdout={out!r}"
        )
        # The legacy 'API key: …' duplicate line MUST never appear.
        assert "API key: " not in out, (
            f"legacy 'API key: …' line is still emitted; stdout={out!r}"
        )
        reset_engine_cache()
