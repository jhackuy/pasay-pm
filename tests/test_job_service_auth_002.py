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

from app.core.security import hash_api_key
from app.models.financial import Income, IncomeStatus
from app.models.identity import (
    ApiCredential,
    CredentialState,
    Principal,
    PrincipalType,
    TelegramIdentityBinding,
)
from app.models.operations import OperationalTask, OperationalTaskStatus, OperationalTaskType
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


def _seed_task(db, *, status=OperationalTaskStatus.PENDING):
    task = OperationalTask(
        task_type=OperationalTaskType.AC_MAINTENANCE,
        title="SYSTEM job test task",
        source_type="conversation",
        source_id=1,
        status=status,
        due_at=NOW,
        dedupe_key="job-service-auth-002",
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
    assert len(body["pending"]) == 1
    assert len(body["in_progress"]) == 1

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
    assert len(resp.json()["pending"]) == 1

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
