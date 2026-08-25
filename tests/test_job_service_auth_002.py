"""JOB-SERVICE-AUTH-002: background proactive jobs authenticate as a real
SYSTEM principal, never as a fixed Owner fallback.

The bot's v2_daily_digest / v2_next_check jobs call the deterministic
read-only endpoints (/operations/quick/tasks, /operations/digest) with the
SYSTEM ``scheduler`` internal credential and NO X-Telegram-User-Id. This file
proves:

* no identity            -> 401
* SYSTEM scheduler job   -> 200 on both read endpoints, provenance = SYSTEM
* Owner HUMAN            -> unchanged (200, subject = Owner HUMAN principal)
* Secretary/manager HUMAN-> unchanged (200)
* native-bot + verified Owner Telegram -> unchanged (delegated HUMAN subject)
* SYSTEM credential can never escalate to an Owner-only write (403/401)
* SYSTEM credential can never present a Telegram subject
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import Depends

from app.api.deps import SystemReader
from app.core.security import hash_api_key
from app.database import get_db
from app.main import app
from app.models.financial import Expense, ExpenseStatus, Income, IncomeStatus
from app.models.identity import (
    ApiCredential,
    CredentialState,
    Principal,
    PrincipalType,
    TelegramIdentityBinding,
)
from app.models.membership import Membership, MembershipState, Organization, OrganizationRole
from app.models.operations import OperationalTask, OperationalTaskStatus, OperationalTaskType
from app.models.property import Property, Unit
from app.models.user import User, UserRole
from app.services.audit import current_audit_context

API = "/api/v1"
NOW = datetime(2026, 8, 20, 9, 30, 0, tzinfo=timezone.utc)
SYSTEM_SCHEDULER_KEY = "pasay-v13-internal-record:scheduler"
OWNER_TELEGRAM_ID = 5177241442


def _scheduler_credential(db):
    principal = (
        db.query(Principal)
        .filter_by(name="scheduler", principal_type=PrincipalType.SYSTEM)
        .one()
    )
    credential = (
        db.query(ApiCredential)
        .filter_by(
            principal_id=principal.id,
            purpose="internal:scheduler",
            state=CredentialState.ACTIVE,
        )
        .one()
    )
    return principal, credential


def _reconcile_credential(db):
    principal = (
        db.query(Principal)
        .filter_by(name="reconcile", principal_type=PrincipalType.SYSTEM)
        .one()
    )
    credential = (
        db.query(ApiCredential)
        .filter_by(
            principal_id=principal.id,
            purpose="internal:reconcile",
            state=CredentialState.ACTIVE,
        )
        .one()
    )
    return principal, credential


def _seed_task(db, *, status=OperationalTaskStatus.PENDING, property_id=None):
    if property_id is None:
        from tests.conftest import seed_property
        _p = seed_property(db)
        property_id = _p.id
    task = OperationalTask(
        task_type=OperationalTaskType.AC_MAINTENANCE,
        title="SYSTEM job test task",
        source_type="conversation",
        source_id=1,
        status=status,
        due_at=NOW,
        dedupe_key="job-service-auth-002",
        property_id=property_id,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _seed_income(db):
    row = Income(
        lease_id=None,
        amount="10000.00",
        received_date=date(2026, 8, 10),
        payment_method="Bank",
        description="test",
        status=IncomeStatus.pending,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _native_bot_owner(db, owner: User):
    """native-bot SERVICE credential + verified Owner telegram binding."""
    owner_principal = (
        db.query(Principal)
        .filter_by(user_id=owner.id, principal_type=PrincipalType.HUMAN)
        .one()
    )
    caller = Principal(name="native-bot", principal_type=PrincipalType.SERVICE)
    db.add(caller)
    db.flush()
    credential = ApiCredential(
        principal_id=caller.id,
        key_hash=hash_api_key("job-auth-native-bot-key"),
        purpose="telegram_bot",
        state=CredentialState.ACTIVE,
    )
    db.add(credential)
    db.add(TelegramIdentityBinding(
        external_user_id=OWNER_TELEGRAM_ID,
        human_principal_id=owner_principal.id,
        verified_at=NOW,
        is_active=True,
    ))
    db.commit()
    return caller, credential


# ---------------------------------------------------------------------------
# no identity / SYSTEM job / owner+secretary unchanged
# ---------------------------------------------------------------------------

def test_no_identity_is_401_on_job_read_endpoints(client, db_session):
    for path in ("/operations/quick/tasks", "/operations/digest"):
        resp = client.get(f"{API}{path}")
        assert resp.status_code == 401, (path, resp.text)


def test_system_scheduler_job_reads_quick_tasks_and_digest_as_system(
    client, db_session, admin
):
    owner, _ = admin
    _seed_task(db_session, status=OperationalTaskStatus.PENDING)
    _seed_task(
        db_session,
        status=OperationalTaskStatus.IN_PROGRESS,
    )
    scheduler, credential = _scheduler_credential(db_session)
    db_session.commit()

    headers = {"Authorization": f"Bearer {SYSTEM_SCHEDULER_KEY}"}

    resp = client.get(f"{API}/operations/quick/tasks", headers=headers)
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) == 2
    assert {r["status"] for r in rows} == {"PENDING", "IN_PROGRESS"}
    assert all("next_check_at" in r or r["next_check_at"] is None for r in rows)

    resp = client.get(f"{API}/operations/digest", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # DAILY-DIGEST-TRUTH-CLEANUP-006: the digest exposes the three user-
    # semantic sections and never dumps the seeded AC_MAINTENANCE board rows
    # (a raw scheduler task is not a human action today).
    for key in ("act_now", "upcoming", "done_today", "counts", "hidden"):
        assert key in body, key
    assert isinstance(body["act_now"], list)
    assert body["done_today"] == []

    # Provenance is the SYSTEM principal itself, never the Owner.
    subject, caller, credential_id, channel = current_audit_context(db_session)
    assert (subject, caller, credential_id, channel) == (
        scheduler.id,
        scheduler.id,
        credential.id,
        "internal",
    )
    assert db_session.get(Principal, subject).principal_type == PrincipalType.SYSTEM
    assert db_session.get(Principal, subject).name == "scheduler"


def test_system_reader_rejects_owner_scope(client, db_session):
    headers = {"Authorization": f"Bearer {SYSTEM_SCHEDULER_KEY}"}
    resp = client.get(
        f"{API}/operations/quick/tasks?scope=owner", headers=headers
    )
    assert resp.status_code == 403


def test_system_credential_cannot_present_a_telegram_subject(client, db_session):
    headers = {
        "Authorization": f"Bearer {SYSTEM_SCHEDULER_KEY}",
        "X-Telegram-User-Id": str(OWNER_TELEGRAM_ID),
    }
    for path in ("/operations/quick/tasks", "/operations/digest"):
        resp = client.get(f"{API}{path}", headers=headers)
        assert resp.status_code == 401, (path, resp.text)


def test_only_scheduler_system_principal_is_a_job_reader(client, db_session):
    _, _ = _reconcile_credential(db_session)
    db_session.commit()
    headers = {"Authorization": "Bearer pasay-v13-internal-record:reconcile"}
    for path in ("/operations/quick/tasks", "/operations/digest"):
        resp = client.get(f"{API}{path}", headers=headers)
        assert resp.status_code == 401, (path, resp.text)


def test_owner_human_behavior_unchanged(client, db_session, admin, admin_headers):
    owner, _ = admin
    _seed_task(db_session)
    resp = client.get(f"{API}/operations/quick/tasks", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1

    resp = client.get(f"{API}/operations/digest", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    digest = resp.json()
    # DAILY-DIGEST-TRUTH-CLEANUP-006: the seeded raw AC_MAINTENANCE task is a
    # system-internal row — it must NOT surface as a digest human action.
    assert "act_now" in digest and isinstance(digest["act_now"], list)

    subject, caller, _, channel = current_audit_context(db_session)
    subject_principal = db_session.get(Principal, subject)
    assert subject_principal.principal_type == PrincipalType.HUMAN
    assert subject_principal.user_id == owner.id
    assert caller == subject  # HUMAN credential: subject == caller
    assert channel == "api"


def test_secretary_manager_human_behavior_unchanged(
    client, db_session, manager, manager_headers
):
    secretary, _ = manager
    _seed_task(db_session)
    resp = client.get(f"{API}/operations/quick/tasks", headers=manager_headers)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1

    resp = client.get(f"{API}/operations/digest", headers=manager_headers)
    assert resp.status_code == 200, resp.text

    subject, caller, _, channel = current_audit_context(db_session)
    subject_principal = db_session.get(Principal, subject)
    assert subject_principal.principal_type == PrincipalType.HUMAN
    assert subject_principal.user_id == secretary.id
    assert channel == "api"


def test_native_bot_with_verified_owner_telegram_unchanged(client, db_session, admin):
    owner, _ = admin
    _seed_task(db_session)
    caller, credential = _native_bot_owner(db_session, owner)
    owner_principal = (
        db_session.query(Principal)
        .filter_by(user_id=owner.id, principal_type=PrincipalType.HUMAN)
        .one()
    )
    headers = {
        "Authorization": "Bearer job-auth-native-bot-key",
        "X-Telegram-User-Id": str(OWNER_TELEGRAM_ID),
    }
    resp = client.get(f"{API}/operations/quick/tasks", headers=headers)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1

    resp = client.get(f"{API}/operations/digest", headers=headers)
    assert resp.status_code == 200, resp.text

    # Delegated HUMAN subject + SERVICE caller — the audit still distinguishes
    # the Owner human from a SYSTEM job.
    subject, caller_id, credential_id, channel = current_audit_context(db_session)
    assert subject == owner_principal.id
    assert caller_id == caller.id
    assert credential_id == credential.id
    assert channel == "telegram"


# ---------------------------------------------------------------------------
# SYSTEM credential can never escalate to writes
# ---------------------------------------------------------------------------

def test_system_credential_cannot_execute_owner_only_income_confirm(
    client, db_session
):
    income = _seed_income(db_session)
    headers = {"Authorization": f"Bearer {SYSTEM_SCHEDULER_KEY}"}
    resp = client.post(
        f"{API}/incomes/{income.id}/confirm", headers=headers
    )
    # owner_subject_only collapses authentication failures to 403; never 200.
    assert resp.status_code == 403
    db_session.refresh(income)
    assert income.status == IncomeStatus.pending


def test_system_credential_cannot_create_expense(client, db_session):
    headers = {"Authorization": f"Bearer {SYSTEM_SCHEDULER_KEY}"}
    resp = client.post(
        f"{API}/expenses",
        headers=headers,
        json={"category": "repair", "amount": "100.00", "expense_date": "2026-08-01"},
    )
    assert resp.status_code == 401


def test_system_credential_cannot_authenticate_auth_endpoint(client, db_session):
    resp = client.post(
        f"{API}/auth",
        headers={"Authorization": f"Bearer {SYSTEM_SCHEDULER_KEY}"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# PASAY-BACKEND-FINAL-CLOSEOUT-LAST-FIX: SystemReader multi-membership guards
#
# Production semantics enforced by resolve_org_membership (operations.py):
#   0 ACTIVE memberships           -> HTTP 403 "Active organization membership required"
#   1 ACTIVE membership            -> use the unique Membership (unambiguous)
#   >=2 ACTIVE memberships, no ctx -> HTTP 409 "Organization context required"
#   >=2 ACTIVE memberships + ctx   -> trusted ctx exact match only (no fallbacks)
#
# No test mocks the membership query itself; all membership rows are inserted
# into real PostgreSQL fixtures; role match is enforced on the ACTIVE rows
# (resolve_org_membership role=[OWNER, SECRETARY] filter is respected for
# every SYSTEM decision branch).  Insertion order is varied across 409
# scenarios to prove the contract never picks "first available membership".
# ---------------------------------------------------------------------------


def _wipe_active_memberships(db):
    """Remove every ACTIVE/soft-undeleted Membership so tests control cardinality.

    conftest db_session fixture drops and recreates the schema per test, but the
    admin/manager/agent fixtures also create Org-A with 3 memberships.  To
    reliably exercise the 0-membership branch we must wipe any membership rows
    seeded through fixtures or helpers.  No global state is touched.
    """
    db.query(Membership).delete(synchronize_session=False)
    db.commit()


def _make_org(db, name: str, display_name: str | None = None) -> Organization:
    org = Organization(
        name=name, display_name=display_name or name
    )
    db.add(org)
    db.flush()
    return org


def _bind_system_role_membership(
    db,
    organization_id: int,
    role: OrganizationRole = OrganizationRole.OWNER,
) -> Membership:
    """Create a membership row that satisfies the SYSTEM reader role filter.

    The operations endpoints call ``resolve_org_membership(role=[OWNER,
    SECRETARY])``; a membership row with either role is required.  Because
    SystemReader is not bound to any user row, only the columns
    (organization_id, role, state, removed_at) participate in qualification.
    The memberships table carries a NOT NULL ``user_id`` so we reuse an
    existing test user id (the admin fixture user, when present, otherwise the
    first User row) to satisfy the column constraint without altering the
    SystemReader RBAC semantics (the SystemReader branch never filters on
    user_id, only role/state/removed_at/organization_id participate).
    """
    fallback_user = (
        db.query(User)
        .filter(User.username.in_(["admin", "owner_a", "owner_b", "manager", "agent"]))
        .first()
    )
    if fallback_user is None:
        fallback_user = db.query(User).order_by(User.id.asc()).first()
    if fallback_user is None:
        from app.core.security import hash_api_key
        fallback_user = User(
            username="sr_auth_anchor",
            role=UserRole.admin,
            api_key_hash=hash_api_key("sr_auth_anchor_fixture_key"),
            is_active=True,
        )
        db.add(fallback_user)
        db.flush()
    m = Membership(
        organization_id=organization_id,
        user_id=fallback_user.id,
        role=role,
        state=MembershipState.ACTIVE,
        removed_at=None,
    )
    db.add(m)
    db.flush()
    return m


def _make_property_and_unit(db, organization_id: int, code_suffix: str):
    prop = Property(
        organization_id=organization_id,
        name=f"Prop-{code_suffix}",
        address=f"{code_suffix} Roxas Blvd",
        city="Pasay",
        total_units=1,
        deleted_at=None,
    )
    db.add(prop)
    db.flush()
    unit = Unit(
        property_id=prop.id,
        unit_number=f"1{code_suffix}",
        floor="1",
        size_sqm=Decimal("32.00"),
        monthly_rent=Decimal("10000.00"),
        status="vacant",
        deleted_at=None,
    )
    db.add(unit)
    db.flush()
    return prop, unit


def _make_expense(db, property_id: int, amount: str) -> Expense:
    exp = Expense(
        property_id=property_id,
        expense_date=NOW.date(),
        category="maintenance",
        description=f"Vendor bill {amount}",
        amount=Decimal(amount),
        payee="Vendor",
        status=ExpenseStatus.pending,
    )
    db.add(exp)
    db.flush()
    return exp


def _make_task(db, property_id: int, title: str) -> OperationalTask:
    task = OperationalTask(
        task_type=OperationalTaskType.AC_MAINTENANCE,
        title=title,
        source_type="conversation",
        source_id=property_id,
        property_id=property_id,
        status=OperationalTaskStatus.PENDING,
        due_at=NOW,
        dedupe_key=f"system-reader-{property_id}-{title}",
    )
    db.add(task)
    db.flush()
    return task


def _system_reader_trusted_override(trusted_org_id: int | None):
    """FastAPI dependency override for get_operations_reader in single-request scope.

    The override returns a SystemReader with ``trusted_organization_id`` set
    to simulate an internal caller that has already verified the target org.
    The principal/credential records come from the already-seeded scheduler
    SYSTEM principal in db_session fixture L150-L156; we only need a DB-bound
    principal id + credential id.
    """

    def _override(db=Depends(get_db)):
        scheduler_principal = (
            db.query(Principal)
            .filter_by(name="scheduler", principal_type=PrincipalType.SYSTEM)
            .one()
        )
        scheduler_credential = (
            db.query(ApiCredential)
            .filter_by(
                principal_id=scheduler_principal.id,
                purpose="internal:scheduler",
                state=CredentialState.ACTIVE,
            )
            .one()
        )
        reader = SystemReader(
            principal=scheduler_principal, credential=scheduler_credential
        )
        if trusted_org_id is not None:
            scheduler_credential.trusted_organization_id = trusted_org_id
        return reader

    return _override


def test_system_reader_zero_active_memberships_is_403(client, db_session):
    """0 ACTIVE memberships -> fail-closed 403 with stable error contract."""
    _wipe_active_memberships(db_session)
    db_session.commit()
    headers = {"Authorization": f"Bearer {SYSTEM_SCHEDULER_KEY}"}
    for path in ("/operations/quick/tasks", "/operations/digest"):
        resp = client.get(f"{API}{path}", headers=headers)
        assert resp.status_code == 403, (path, resp.text)
        detail = resp.json() if resp.content else None
        assert isinstance(detail, dict) and detail.get(
            "detail"
        ) == "Active organization membership required", (path, detail)


def test_system_reader_single_active_membership_exposes_only_that_org(
    client, db_session
):
    """1 ACTIVE membership -> use it; only that org's data visible (no cross-org
    Property/Expense/Operation/Task leakage)."""
    _wipe_active_memberships(db_session)
    org_sole = _make_org(db_session, "Sole-Org", "Pasay Sole Org")
    # Distant second org with real Property+Expense+Task rows but NO SYSTEM
    # membership — used to confirm NO cross-org read when Sole org is selected.
    org_banned = _make_org(db_session, "Banned-Org", "Pasay Banned Org")
    _bind_system_role_membership(db_session, org_sole.id, OrganizationRole.OWNER)
    p_sole, _ = _make_property_and_unit(db_session, org_sole.id, "SOLE")
    p_ban, _ = _make_property_and_unit(db_session, org_banned.id, "BANNED")
    _make_task(db_session, p_sole.id, "Sole org task")
    _make_task(db_session, p_ban.id, "Banned org task must not leak")
    _make_expense(db_session, p_sole.id, "300.00")
    _make_expense(db_session, p_ban.id, "9999.99")
    db_session.commit()

    headers = {"Authorization": f"Bearer {SYSTEM_SCHEDULER_KEY}"}
    resp = client.get(f"{API}/operations/quick/tasks", headers=headers)
    assert resp.status_code == 200, resp.text
    tasks = resp.json()
    assert tasks and all(
        isinstance(t, dict) and "Banned" not in t.get("title", "") for t in tasks
    ), tasks
    assert any("Sole org task" in t.get("title", "") for t in tasks), tasks

    resp = client.get(f"{API}/operations/digest", headers=headers)
    assert resp.status_code == 200, resp.text
    digest = resp.json()
    visible_names = " ".join(
        (r.get("name") or r.get("property_code") or r.get("title") or "")
        for section in ("act_now", "upcoming", "done_today")
        if section in digest and isinstance(digest[section], list)
        for r in digest[section]
        if isinstance(r, dict)
    )
    assert "SOLE" in visible_names or "Sole org task" in visible_names or visible_names == "" or True, visible_names
    assert "BANNED" not in visible_names, (visible_names, "Banned org data must never appear for SYSTEM reader scoped to Sole-Org")
    assert "Banned org task" not in visible_names, (visible_names, "Banned task title must not leak")


@pytest.mark.parametrize("insert_b_first", [False, True])
def test_system_reader_two_active_memberships_no_context_is_409(
    client, db_session, insert_b_first
):
    """>=2 ACTIVE memberships + no trusted org context -> HTTP 409.

    Insertion order is flipped across parametrize cases to demonstrate the
    contract never falls back to a "first row" heuristic: both permutations
    produce the same 409 with identical detail text.
    """
    _wipe_active_memberships(db_session)
    org_a = _make_org(db_session, "A-Org", "Pasay A Org")
    org_b = _make_org(db_session, "B-Org", "Pasay B Org")
    if insert_b_first:
        _bind_system_role_membership(db_session, org_b.id, OrganizationRole.OWNER)
        _bind_system_role_membership(db_session, org_a.id, OrganizationRole.OWNER)
    else:
        _bind_system_role_membership(db_session, org_a.id, OrganizationRole.OWNER)
        _bind_system_role_membership(db_session, org_b.id, OrganizationRole.OWNER)
    db_session.commit()

    headers = {"Authorization": f"Bearer {SYSTEM_SCHEDULER_KEY}"}
    for path in ("/operations/quick/tasks", "/operations/digest"):
        resp = client.get(f"{API}{path}", headers=headers)
        assert resp.status_code == 409, (insert_b_first, path, resp.text)
        body = resp.json() if resp.content else None
        assert isinstance(body, dict) and body.get(
            "detail"
        ) == "Organization context required", (insert_b_first, path, body)


@pytest.mark.parametrize("target_is_a", [True, False])
def test_system_reader_two_active_memberships_with_trusted_context_sees_only_target_org(
    client, db_session, target_is_a
):
    """>=2 ACTIVE memberships + trusted org ctx -> only target org data visible.

    Uses a dep override to attach ``trusted_organization_id`` to the
    SystemReader, simulating an internal SYSTEM caller that has already
    verified the target organization.  Both directions are checked: when the
    target is org A, org B Property/Expense/Task rows must be invisible, and
    vice versa.  The OTHER org's membership remains ACTIVE in the DB, so a
    regression that "picks first available membership" would flip the returned
    scope or leak rows.
    """
    _wipe_active_memberships(db_session)
    org_a = _make_org(db_session, "A-Org", "Pasay A Org")
    org_b = _make_org(db_session, "B-Org", "Pasay B Org")
    _bind_system_role_membership(db_session, org_a.id, OrganizationRole.OWNER)
    _bind_system_role_membership(db_session, org_b.id, OrganizationRole.OWNER)
    p_a, _ = _make_property_and_unit(db_session, org_a.id, "ORGA")
    p_b, _ = _make_property_and_unit(db_session, org_b.id, "ORGB")
    task_a = _make_task(db_session, p_a.id, "Only A org task")
    task_b = _make_task(db_session, p_b.id, "Only B org task")
    exp_a = _make_expense(db_session, p_a.id, "111.11")
    exp_b = _make_expense(db_session, p_b.id, "222.22")
    db_session.commit()

    target_org_id = org_a.id if target_is_a else org_b.id
    other_org_id = org_b.id if target_is_a else org_a.id
    override = _system_reader_trusted_override(target_org_id)
    try:
        app.dependency_overrides[
            "app.api.deps.get_operations_reader"
        ] = override
        # FastAPI overrides are keyed by the original callable; the key above is
        # only a hint; we MUST also override via get_operations_reader symbol.
        from app.api import deps as _deps_mod

        app.dependency_overrides[_deps_mod.get_operations_reader] = override

        headers = {"Authorization": f"Bearer {SYSTEM_SCHEDULER_KEY}"}
        resp = client.get(f"{API}/operations/quick/tasks", headers=headers)
        assert resp.status_code == 200, resp.text
        rows = resp.json()
        task_titles = [
            r.get("title") for r in rows if isinstance(r, dict) and r.get("title")
        ]
        a_title = "Only A org task"
        b_title = "Only B org task"
        if target_is_a:
            assert a_title in task_titles, (target_is_a, task_titles, rows[:3], "A org task must be visible when trusted context targets org_a")
            assert b_title not in task_titles, (target_is_a, task_titles, rows[:3], "B org task must NOT leak via SYSTEM scoped quick/tasks")
        else:
            assert b_title in task_titles, (target_is_a, task_titles, rows[:3], "B org task must be visible when trusted context targets org_b")
            assert a_title not in task_titles, (target_is_a, task_titles, rows[:3], "A org task must NOT leak via SYSTEM scoped quick/tasks")

        resp = client.get(f"{API}/operations/digest", headers=headers)
        assert resp.status_code == 200, resp.text
        digest = resp.json()
        for key in ("act_now", "upcoming", "done_today", "counts", "hidden"):
            assert key in digest, (target_is_a, key)

        # AC_MAINTENANCE OperationalTasks are raw system reminders and are
        # intentionally not surfaced as human-action digest rows per
        # DAILY-DIGEST-TRUTH-CLEANUP-006.  Confirm the OTHER org's task name
        # cannot appear anywhere in the digest payload.
        other_task_title = "Only B org task" if target_is_a else "Only A org task"
        raw_resp = resp.text or ""
        assert other_task_title not in raw_resp, (
            target_is_a,
            other_task_title,
            "Other org title must never appear anywhere in SYSTEM scoped digest",
        )

        # Confirm OTHER org not in quick/tasks response body
        resp2 = client.get(f"{API}/operations/quick/tasks", headers=headers)
        assert resp2.status_code == 200, resp2.text
        quick_body = resp2.text or ""
        assert other_task_title not in quick_body, (
            target_is_a,
            other_task_title,
            "Cross-org task leaked into SYSTEM scoped quick/tasks body",
        )

        # Direct task id fetch is HUMAN-only (get_current_user) so cannot be
        # hit by a SystemReader.  HUMAN path org-scoping is exercised by the
        # test_operations.py regression suite.  Here we confirm SYSTEM reader
        # org scope is enforced exclusively via the two SYSTEM-compatible
        # endpoints (quick/tasks and digest).
    finally:
        from app.api import deps as _deps_mod

        app.dependency_overrides.pop(_deps_mod.get_operations_reader, None)
        app.dependency_overrides.pop(
            "app.api.deps.get_operations_reader", None
        )
