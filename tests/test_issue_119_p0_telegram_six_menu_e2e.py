"""Issue #119 P0 — Telegram six-menu V1 contract repair E2E.

The Owner screenshot fingerprint on 2026-09-08 was:

    08:45 ``🏘 房源`` -> ``获取数据失败: Missing API key``
    (after credential provisioning/redeploy)
    08:54 / 08:57 ``🏘 房源`` -> ``获取数据失败: Not Found``

That ``Not Found`` is the production failure of the V1 surface: the
bot's six frozen Owner bottom-menu routes (首页 / 房源 / 待办 / 租金 /
支出 / 档案) call ``PasayApiClient`` methods that hit endpoints which
    existed in legacy ``app/api/routers/operations.py`` /
    ``reports.py`` but were NOT migrated to ``app/v1/api/``.

This test exercises the EXACT production FastAPI app (the factory the
Dockerfile CMD runs — ``app.v1.main:create_v1_app``) through the
production HUMAN credential contract (created by ``scripts/create_v1_api_
key.py``) and proves, end-to-end, that all six menu API calls return
non-404 with an OWNER credential.

The six routes audited by this test (handler -> PasayApiClient ->
/api/v1 endpoint):

  Menu        Handler                           ApiClient method           Endpoint
  ----------  --------------------------------  -------------------------  -----------------------------------------
  首页        show_home                         get_financial_summary       GET /api/v1/reports/financial-summary
  首页        show_home                         get_overdue_rents           GET /api/v1/reports/overdue-rents
  首页        show_home                         get_units                   GET /api/v1/units
  首页        show_home                         get_digest                  GET /api/v1/operations/digest
  首页        show_home                         get_quick_rent              GET /api/v1/operations/quick/rent
  首页        show_home                         get_quick_expense           GET /api/v1/operations/quick/expense
  房源        show_quick_properties             get_quick_properties        GET /api/v1/operations/quick/properties
  待办        show_today_digest                 get_digest                  GET /api/v1/operations/digest
  租金        show_quick_rent                   get_quick_rent              GET /api/v1/operations/quick/rent
  支出        show_quick_expense                get_quick_expense           GET /api/v1/operations/quick/expense
  档案        show_archive_launcher             (settings only)             n/a

Auth boundaries exercised (one test each):

  * OWNER (SECRETARY-tier manager is rejected where the menu path
    requires OWNER — currently none do, so SECRETARY is accepted on
    every menu path; the test proves the membership gate is enforced
    regardless of role).
  * No bearer → 401 on every endpoint.
  * Wrong bearer → 401 on every endpoint.
  * Cross-org access →  403 / empty (org-scope fail-closed).
  * SYSTEM job key →  401 (SYSTEM is rejected by ``get_current_
    principal``; SYSTEM's own ``/operations/digest`` +
    ``/operations/quick/tasks`` remain reachable via
    ``get_system_principal``).

Real-data assertions: each menu endpoint returns the exact shape the
bot's render layer consumes (``pasay_bot.render.cards``). We seed
real V1 data (workspace + property + occupied unit + overdue
RentDueSchedule + pending ExpenseClaim) and assert the non-404
responses carry the fields the bot needs (no ``None`` for the
critical counts).

Reuses ``tests.v1_support`` fixtures; no new harness, no new
workflow, no DB writes outside the test engine.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Iterable

import pytest
from fastapi.testclient import TestClient

from app.core.permissions import Role
from app.core.security import generate_api_key, hash_api_key
from app.db.session import get_session_factory, reset_engine_cache
from app.v1.main import create_v1_app
from app.v1.models.base import (
    LeaseState,
    MembershipState,
    OperationState,
    TaskState,
    V1Base,
    V1PrincipalType,
)
from app.v1.models.expense import (
    ExpenseClaim,
    ExpenseClaimStatus,
)
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
from tests.v1_support import (
    Workspace,
    seed_workspace,
    v1_engine_ctx,
)


SYSTEM_SENTINEL_USERNAME = "v1-system-scheduler-menu-e2e"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bootstrap_owner(db, *, workspace: Workspace, username: str) -> str:
    """Mirror ``scripts/create_v1_api_key.py`` HUMAN OWNER path on an
    existing workspace.

    Returns the raw key — production code captures the raw key on stdout
    exactly once and discards it; this test needs it to authenticate.
    """
    raw = generate_api_key()
    user = db.query(User).filter(User.username == username).one_or_none()
    if user is None:
        user = User(
            username=username,
            display_name=username,
            default_language="en-US",
        )
        db.add(user)
        db.flush()
    # The workspace's OWNER membership belongs to the seeded user; we
    # add the new owner as a SECOND OWNER member of the same workspace
    # so the credential lookup + membership gate both succeed.
    mship = (
        db.query(Membership)
        .filter(
            Membership.org_id == workspace.org_id,
            Membership.user_id == user.id,
        )
        .order_by(Membership.id.asc())
        .first()
    )
    if mship is None:
        mship = Membership(
            org_id=workspace.org_id, user_id=user.id, role=Role.OWNER.value,
            state=MembershipState.ACTIVE.value,
        )
        db.add(mship)
        db.flush()
    # Deactivate any prior active HUMAN credential for this user.
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
    db.add(
        ApiCredential(
            user_id=user.id,
            key_hash=hash_api_key(raw),
            is_active=True,
            principal_type=V1PrincipalType.HUMAN.value,
        )
    )
    db.commit()
    return raw


def _bootstrap_secretary(db, *, workspace: Workspace, username: str) -> str:
    """SECRETARY-tier manager mirror on an existing workspace."""
    raw = generate_api_key()
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
        .filter(
            Membership.org_id == workspace.org_id,
            Membership.user_id == user.id,
        )
        .order_by(Membership.id.asc())
        .first()
    )
    if mship is None:
        mship = Membership(
            org_id=workspace.org_id, user_id=user.id, role=Role.SECRETARY.value,
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
    db.add(
        ApiCredential(
            user_id=user.id,
            key_hash=hash_api_key(raw),
            is_active=True,
            principal_type=V1PrincipalType.HUMAN.value,
        )
    )
    db.commit()
    return raw


def _bootstrap_system(db, *, trusted_organization_id: int | None) -> str:
    raw = generate_api_key()
    user = (
        db.query(User)
        .filter(User.username == SYSTEM_SENTINEL_USERNAME)
        .one_or_none()
    )
    if user is None:
        user = User(
            username=SYSTEM_SENTINEL_USERNAME,
            display_name="V1 SYSTEM sentinel",
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
    db.add(
        ApiCredential(
            user_id=user.id,
            key_hash=hash_api_key(raw),
            is_active=True,
            principal_type=V1PrincipalType.SYSTEM.value,
            purpose="internal:scheduler",
            trusted_organization_id=trusted_organization_id,
        )
    )
    db.commit()
    return raw


def _seed_workspace_with_overdue_rent_and_pending_expense(
    db, *, name: str,
) -> Workspace:
    """Workspace + one overdue RentDueSchedule + one pending ExpenseClaim.

    The expense has status SUBMITTED (not yet verified) so it counts as
    ``pending_approval_count`` but NOT ``payable``; the digest gets a
    real ``rent_overdue`` row so the Owner Home view's overdue counter
    is non-zero.
    """
    workspace = seed_workspace(db, name=name)
    # Overdue rent schedule (read by /reports/overdue-rents + /reports/financial-summary)
    overdue = RentDueSchedule(
        org_id=workspace.org_id,
        lease_id=workspace.lease_id,
        period_start=date.today().replace(day=1) - timedelta(days=60),
        due_date=date.today() - timedelta(days=15),
        amount_due=Decimal("12000.00"),
        state=RentDueState.OVERDUE.value,
    )
    db.add(overdue)
    db.flush()
    # Operation + Task pair (read by /operations/digest + /operations/quick/tasks)
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
    # Pending approval expense (read by /operations/quick/expense pending count).
    db.add(
        ExpenseClaim(
            org_id=workspace.org_id,
            title="Plumbing fix",
            category="REPAIRS",
            claimed_amount=Decimal("2500.00"),
            status=ExpenseClaimStatus.SUBMITTED.value,
            idempotency_key=f"e2e-{workspace.org_id}-pending",
            payload_hash="0" * 64,
        )
    )
    db.commit()
    return workspace


def _production_app_client() -> TestClient:
    """TestClient against ``app.v1.main:create_v1_app()`` — the EXACT
    factory the Dockerfile CMD runs. No fakes, no test-only overrides.
    """
    return TestClient(create_v1_app())


# ---------------------------------------------------------------------------
# 1) Properties 🏘  →  GET /operations/quick/properties
# ---------------------------------------------------------------------------


def test_menu_properties_returns_non_404_with_owner_credential():
    """Owner pressing ``🏘 房源`` reaches the V1 ``/operations/quick/
    properties`` endpoint and gets a non-404 response carrying the
    stable ``unit_code`` / ``property_name`` / ``status`` /
    ``tenant_name`` fields the bot's ``properties_quick_card`` reads.
    """
    with v1_engine_ctx():
        db = get_session_factory()()
        try:
            workspace = _seed_workspace_with_overdue_rent_and_pending_expense(
                db, name="e2e-menu-properties",
            )
            owner_key = _bootstrap_owner(
                db, workspace=workspace,
                username="e2e-menu-properties-owner",
            )
            client = _production_app_client()
            r = client.get(
                f"/api/v1/operations/quick/properties?org_id={workspace.org_id}",
                headers={"Authorization": f"Bearer {owner_key}"},
            )
            assert r.status_code == 200, r.text
            rows = r.json()
            assert isinstance(rows, list), rows
            assert any(
                row.get("status") == "occupied"
                and row.get("tenant_name")
                and row.get("unit_code") == "7777"
                for row in rows
            ), rows
        finally:
            db.close()
            reset_engine_cache()


# ---------------------------------------------------------------------------
# 2) Tasks ✅  →  GET /operations/digest
# ---------------------------------------------------------------------------


def test_menu_tasks_returns_non_404_with_owner_credential():
    """Owner pressing ``✅ 待办`` reaches the V1 ``/operations/digest``
    endpoint and gets a non-404 response carrying the act_now /
    upcoming / done_today sections the bot's
    ``cards.active_tasks_digest_card`` reads.
    """
    with v1_engine_ctx():
        db = get_session_factory()()
        try:
            workspace = _seed_workspace_with_overdue_rent_and_pending_expense(
                db, name="e2e-menu-tasks",
            )
            owner_key = _bootstrap_owner(
                db, workspace=workspace,
                username="e2e-menu-tasks-owner",
            )
            client = _production_app_client()
            r = client.get(
                f"/api/v1/operations/digest?org_id={workspace.org_id}",
                headers={"Authorization": f"Bearer {owner_key}"},
            )
            assert r.status_code == 200, r.text
            digest = r.json()
            # Legacy keys that the bot's card falls back to must exist.
            for key in ("pending", "in_progress", "recently_completed"):
                assert key in digest, f"digest missing legacy key {key!r}"
            # Real overdue rent must surface in act_now.
            assert any(
                row.get("kind") == "rent_overdue"
                for row in digest.get("act_now", [])
            ), digest
        finally:
            db.close()
            reset_engine_cache()


# ---------------------------------------------------------------------------
# 3) Rent 💰  →  GET /operations/quick/rent
# ---------------------------------------------------------------------------


def test_menu_rent_returns_non_404_with_owner_credential():
    """Owner pressing ``💰 租金`` reaches the V1 ``/operations/quick/
    rent`` endpoint and gets a non-404 response carrying the overdue
    list + outstanding_total + current-month stats the bot's
    ``rent_quick_card`` reads.
    """
    with v1_engine_ctx():
        db = get_session_factory()()
        try:
            workspace = _seed_workspace_with_overdue_rent_and_pending_expense(
                db, name="e2e-menu-rent",
            )
            owner_key = _bootstrap_owner(
                db, workspace=workspace,
                username="e2e-menu-rent-owner",
            )
            client = _production_app_client()
            r = client.get(
                f"/api/v1/operations/quick/rent?org_id={workspace.org_id}",
                headers={"Authorization": f"Bearer {owner_key}"},
            )
            assert r.status_code == 200, r.text
            data = r.json()
            assert isinstance(data, dict), data
            for key in (
                "overdue", "outstanding_total", "expected_rent_total",
                "collected_rent", "outstanding_rent", "collection_rate",
                "month", "unpaid_unit_count",
            ):
                assert key in data, f"rent quick view missing key {key!r}"
            # Real overdue rent row exists.
            overdue_list = data["overdue"]
            assert isinstance(overdue_list, list)
            assert len(overdue_list) >= 1, data
            first = overdue_list[0]
            for key in (
                "unit", "unit_code", "amount", "unpaid_periods",
                "monthly_rent", "overdue_days",
            ):
                assert key in first, f"rent overdue row missing key {key!r}"
        finally:
            db.close()
            reset_engine_cache()


# ---------------------------------------------------------------------------
# 4) Expense 💸  →  GET /operations/quick/expense
# ---------------------------------------------------------------------------


def test_menu_expense_returns_non_404_with_owner_credential():
    """Owner pressing ``💸 支出`` reaches the V1 ``/operations/quick/
    expense`` endpoint and gets a non-404 response carrying the
    month_total / pending_approval_count / payable[] keys the bot's
    ``expense_quick_card`` reads.
    """
    with v1_engine_ctx():
        db = get_session_factory()()
        try:
            workspace = _seed_workspace_with_overdue_rent_and_pending_expense(
                db, name="e2e-menu-expense",
            )
            owner_key = _bootstrap_owner(
                db, workspace=workspace,
                username="e2e-menu-expense-owner",
            )
            client = _production_app_client()
            r = client.get(
                f"/api/v1/operations/quick/expense?org_id={workspace.org_id}",
                headers={"Authorization": f"Bearer {owner_key}"},
            )
            assert r.status_code == 200, r.text
            data = r.json()
            assert isinstance(data, dict), data
            for key in (
                "month_total", "current_month_total", "month",
                "payable", "pending_approval_count",
                "pending_approval_amount", "records",
            ):
                assert key in data, f"expense quick view missing key {key!r}"
            # Pending SUBMITTED expense must show in pending_approval_count.
            assert int(data["pending_approval_count"]) >= 1, data
        finally:
            db.close()
            reset_engine_cache()


# ---------------------------------------------------------------------------
# 5) Home 🏠  →  GET /reports/financial-summary + /reports/overdue-rents + /units
# ---------------------------------------------------------------------------


def test_menu_home_financial_summary_returns_non_404_with_owner_credential():
    """Owner pressing ``🏠 首页`` triggers ``show_home`` which calls
    ``GET /reports/financial-summary``; that endpoint MUST NOT 404 on
    V1 (this is the deterministic Home fail-closed path: when fin is
    empty / errored the bot renders ``⚠️⚠️ 获取数据失败``).
    """
    with v1_engine_ctx():
        db = get_session_factory()()
        try:
            workspace = _seed_workspace_with_overdue_rent_and_pending_expense(
                db, name="e2e-menu-home-fs",
            )
            owner_key = _bootstrap_owner(
                db, workspace=workspace,
                username="e2e-menu-home-fs-owner",
            )
            client = _production_app_client()
            r = client.get(
                f"/api/v1/reports/financial-summary?org_id={workspace.org_id}",
                headers={"Authorization": f"Bearer {owner_key}"},
            )
            assert r.status_code == 200, r.text
            data = r.json()
            for key in (
                "month", "expected_rent_total", "collected_rent",
                "outstanding_rent", "total_income", "total_expense",
                "net_income", "units_count", "occupied_units",
                "vacant_units",
            ):
                assert key in data, f"financial summary missing key {key!r}"
            # Real unit count: 1 occupied unit.
            assert int(data["units_count"]) >= 1
            assert int(data["occupied_units"]) >= 1
        finally:
            db.close()
            reset_engine_cache()


def test_menu_home_overdue_rents_returns_non_404_with_owner_credential():
    """Owner pressing ``🏠 首页`` triggers ``show_home`` which calls
    ``GET /reports/overdue-rents``; that endpoint MUST NOT 404.

    Issue #119 P0 (Telegram six-menu V1 contract repair): the bot's
    ``PasayApiClient.get_overdue_rents()`` iterates the raw response
    as a flat list and calls ``OverdueRent.from_dict(d)`` per row, so
    the V1 endpoint returns a flat list (NOT the legacy
    ``Paginated[OverdueRent]`` envelope). We assert the flat-list
    shape here.
    """
    with v1_engine_ctx():
        db = get_session_factory()()
        try:
            workspace = _seed_workspace_with_overdue_rent_and_pending_expense(
                db, name="e2e-menu-home-overdue",
            )
            owner_key = _bootstrap_owner(
                db, workspace=workspace,
                username="e2e-menu-home-overdue-owner",
            )
            client = _production_app_client()
            r = client.get(
                f"/api/v1/reports/overdue-rents?org_id={workspace.org_id}",
                headers={"Authorization": f"Bearer {owner_key}"},
            )
            assert r.status_code == 200, r.text
            data = r.json()
            assert isinstance(data, list), (
                f"overdue-rents must be a flat list (bot iterates it "
                f"directly); got {type(data).__name__}: {data!r}"
            )
            assert len(data) >= 1, data
            first = data[0]
            for key in (
                "lease_id", "unit_id", "tenant_id", "unit", "tenant",
                "overdue_months", "overdue_periods", "amount_per_month",
                "total_outstanding", "oldest_due_date", "overdue_days",
                "outstanding", "days_overdue",
            ):
                assert key in first, f"overdue-rents row missing key {key!r}"
        finally:
            db.close()
            reset_engine_cache()


def test_menu_home_units_returns_non_404_with_owner_credential():
    """Owner pressing ``🏠 首页`` triggers ``show_home`` which calls
    ``GET /units`` (no ``/properties/`` prefix); that endpoint MUST
    NOT 404.
    """
    with v1_engine_ctx():
        db = get_session_factory()()
        try:
            workspace = _seed_workspace_with_overdue_rent_and_pending_expense(
                db, name="e2e-menu-home-units",
            )
            owner_key = _bootstrap_owner(
                db, workspace=workspace,
                username="e2e-menu-home-units-owner",
            )
            client = _production_app_client()
            r = client.get(
                f"/api/v1/units?org_id={workspace.org_id}",
                headers={"Authorization": f"Bearer {owner_key}"},
            )
            assert r.status_code == 200, r.text
            data = r.json()
            assert isinstance(data, list)
            assert any(
                u.get("id") == workspace.unit_id for u in data
            ), data
        finally:
            db.close()
            reset_engine_cache()


# ---------------------------------------------------------------------------
# 6) Archive 📁  →  no API call (settings-only launcher)
#
# No test needed: show_archive_launcher renders from ``settings`` and
# never calls PasayApiClient. The menu path is purely UI; the only way
# it can 404 against the V1 app is if the bot itself is broken, which
# is out of scope for this contract repair.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Auth boundaries
# ---------------------------------------------------------------------------


_MENU_PATHS: Iterable[tuple[str, str]] = (
    ("properties", "/api/v1/operations/quick/properties"),
    ("digest", "/api/v1/operations/digest"),
    ("rent", "/api/v1/operations/quick/rent"),
    ("expense", "/api/v1/operations/quick/expense"),
    ("financial_summary", "/api/v1/reports/financial-summary"),
    ("overdue_rents", "/api/v1/reports/overdue-rents"),
    ("units", "/api/v1/units"),
)


def test_menu_paths_all_reachable_for_owner_and_secretary():
    """Every frozen menu path returns 200 for both OWNER and SECRETARY
    credentials (the bot currently sends SECRETARY for Secretary
    private chats — failure here would mean the Secretary sees the
    same ``Not Found`` fingerprint the Owner screenshot captured).
    """
    with v1_engine_ctx():
        db = get_session_factory()()
        try:
            workspace = _seed_workspace_with_overdue_rent_and_pending_expense(
                db, name="e2e-menu-both-roles",
            )
            owner_key = _bootstrap_owner(
                db, workspace=workspace,
                username="e2e-menu-both-roles-owner",
            )
            secretary_key = _bootstrap_secretary(
                db, workspace=workspace,
                username="e2e-menu-both-roles-sec",
            )
            client = _production_app_client()
            for label, path in _MENU_PATHS:
                for role_label, key in (("OWNER", owner_key), ("SECRETARY", secretary_key)):
                    r = client.get(
                        f"{path}?org_id={workspace.org_id}",
                        headers={"Authorization": f"Bearer {key}"},
                    )
                    assert r.status_code == 200, (
                        f"{role_label} {label} -> {r.status_code} {r.text}"
                    )
        finally:
            db.close()
            reset_engine_cache()


def test_menu_paths_reject_missing_bearer():
    """Every frozen menu path returns 401 when the bearer is missing —
    the legacy router dependency does the same fail-closed check.
    """
    with v1_engine_ctx():
        db = get_session_factory()()
        try:
            workspace = _seed_workspace_with_overdue_rent_and_pending_expense(
                db, name="e2e-menu-no-bearer",
            )
            client = _production_app_client()
            for _label, path in _MENU_PATHS:
                r = client.get(f"{path}?org_id={workspace.org_id}")
                assert r.status_code == 401, (
                    f"{path} -> {r.status_code} {r.text}"
                )
        finally:
            db.close()
            reset_engine_cache()


def test_menu_paths_reject_wrong_bearer():
    """Every frozen menu path returns 401 when the bearer is well-formed
    but does not match an active row.
    """
    with v1_engine_ctx():
        db = get_session_factory()()
        try:
            workspace = _seed_workspace_with_overdue_rent_and_pending_expense(
                db, name="e2e-menu-wrong-bearer",
            )
            client = _production_app_client()
            bogus = "pasay_v1_bogus_" + "x" * 32
            for _label, path in _MENU_PATHS:
                r = client.get(
                    f"{path}?org_id={workspace.org_id}",
                    headers={"Authorization": f"Bearer {bogus}"},
                )
                assert r.status_code == 401, (
                    f"{path} -> {r.status_code} {r.text}"
                )
        finally:
            db.close()
            reset_engine_cache()


def test_menu_paths_reject_cross_org_access():
    """Every frozen menu path enforces org-scope fail-closed: an OWNER
    of another org gets 403 / empty when probing a workspace they
    don't belong to.
    """
    with v1_engine_ctx():
        db = get_session_factory()()
        try:
            workspace = _seed_workspace_with_overdue_rent_and_pending_expense(
                db, name="e2e-menu-cross-org",
            )
            # Build a SECOND workspace + OWNER + SECRETARY.
            other_workspace = seed_workspace(db, name="e2e-menu-cross-org-other")
            other_owner = _bootstrap_owner(
                db, workspace=other_workspace,
                username="e2e-menu-cross-org-other-owner",
            )
            client = _production_app_client()
            for label, path in _MENU_PATHS:
                r = client.get(
                    f"{path}?org_id={workspace.org_id}",
                    headers={"Authorization": f"Bearer {other_owner}"},
                )
                # ``require_org_scope`` raises 403 (the same fail-closed
                # contract the V1 surface uses everywhere else).
                assert r.status_code == 403, (
                    f"cross-org {label} -> {r.status_code} {r.text}"
                )
        finally:
            db.close()
            reset_engine_cache()


def test_menu_paths_reject_system_credential():
    """SYSTEM credentials authenticate through the unified
    ``get_human_or_system_principal`` dep ONLY for the two SYSTEM
    surfaces (``/operations/digest`` + ``/operations/quick/tasks``)
    where the scheduled-job path needs to coexist with the
    Owner-menu path. Every other menu path is HUMAN-only and MUST
    reject SYSTEM with 401 — otherwise a leaked ``PASSAY_JOB_API_KEY``
    could reach Owner-only data through the menu surface.

    The pin is enforced by every endpoint using
    ``get_current_principal`` directly (not the unified dep); the two
    SYSTEM surfaces explicitly route through the unified dep instead.
    """
    with v1_engine_ctx():
        db = get_session_factory()()
        try:
            workspace = _seed_workspace_with_overdue_rent_and_pending_expense(
                db, name="e2e-menu-syskey",
            )
            job_key = _bootstrap_system(
                db, trusted_organization_id=workspace.org_id,
            )
            client = _production_app_client()
            # Two surfaces MUST accept SYSTEM (scheduled-job path is
            # production-grade; Owner menu path is on the same
            # endpoint so the system principal must be allowed).
            system_ok = {
                "/api/v1/operations/digest",
                "/api/v1/operations/quick/tasks",
            }
            for label, path in _MENU_PATHS:
                r = client.get(
                    f"{path}?org_id={workspace.org_id}",
                    headers={"Authorization": f"Bearer {job_key}"},
                )
                if path in system_ok:
                    assert r.status_code == 200, (
                        f"SYSTEM {label} (expected 200 on shared surface) -> "
                        f"{r.status_code} {r.text}"
                    )
                else:
                    assert r.status_code == 401, (
                        f"SYSTEM {label} -> {r.status_code} {r.text}"
                    )
        finally:
            db.close()
            reset_engine_cache()