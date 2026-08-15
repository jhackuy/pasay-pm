"""V1.2 PROACTIVE OPERATIONS tests (real PostgreSQL pasay_pm_test).

Covers: scheduler idempotency + real-PG concurrency, SKIP LOCKED crash
recovery, notification retry/backoff/dedupe, task state machine (snooze /
complete / cancel), RBAC (admin/manager/agent, agent 403), reconciliation,
recurring rules, Alembic upgrade/downgrade, and the financial-write-path
invariant (task handlers never UPDATE expenses/incomes directly).
"""
from __future__ import annotations

import concurrent.futures
import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models.attachment import Attachment
from app.models.audit_log import AuditAction, AuditLog
from app.models.commission import (
    CommissionRule,
    CommissionRuleType,
    CommissionSettlement,
    CommissionSettlementStatus,
)
from app.models.financial import Expense, ExpenseStatus, Income, IncomeStatus
from app.models.lease import Lease, LeaseStatus
from app.models.operations import (
    NotificationOutbox,
    NotificationStatus,
    OperationalTask,
    OperationalTaskStatus,
    OperationalTaskType,
    Recurrence,
    RecurringRule,
)
from app.models.property import Property, Unit, UnitStatus
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.models.identity import ApiCredential, CredentialState, Principal, PrincipalType
from app.services.operations.generation import generate_business_tasks
from app.services.operations.notifier import claim_pending_notifications, process_notifications_once
from app.services.operations.outbox import enqueue_notification
from app.services.operations.scheduler import run_scheduler_once
from app.services.operations.reconcile import reconcile_tasks
from app.services.operations.backfill import (
    backfill_unassigned_business_tasks,
    enqueue_missing_notifications,
)
from app.services.operations.assignee import validate_default_assignee

API = "/api/v1"
NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _user(db, username, role, telegram_chat_id=None):
    user = User(
        username=username,
        role=role,
        api_key_hash=__import__("secrets").token_urlsafe(24),
        is_active=True,
        telegram_chat_id=telegram_chat_id,
    )
    db.add(user)
    db.flush()
    return user


def _seed_lease(db, *, start="2026-01-01", end="2026-12-31", due_day=5,
                rent="12000.00", status=LeaseStatus.active):
    prop = Property(name="Sunset Tower", address="1 Roxas Blvd", city="Pasay", total_units=4)
    db.add(prop)
    db.flush()
    unit = Unit(property_id=prop.id, unit_number="101", floor="1", size_sqm="32.50",
                monthly_rent=rent, status=UnitStatus.occupied)
    tenant = Tenant(full_name="Juan Dela Cruz", phone="+639170000000")
    db.add_all([unit, tenant])
    db.flush()
    lease = Lease(unit_id=unit.id, tenant_id=tenant.id, start_date=date.fromisoformat(start),
                  end_date=date.fromisoformat(end), monthly_rent=rent, deposit="24000.00",
                  status=status, due_day=due_day)
    db.add(lease)
    db.flush()
    return lease


def _seed_rule(db, *, rule_type=OperationalTaskType.AC_MAINTENANCE,
               recurrence=Recurrence.quarterly, next_run_at=None, user_id=None):
    rule = RecurringRule(
        rule_type=rule_type,
        title="季度空调保养",
        recurrence=recurrence,
        next_run_at=next_run_at or (NOW - timedelta(days=1)),
        enabled=True,
        assigned_user_id=user_id,
    )
    db.add(rule)
    db.flush()
    return rule


def _seed_default_assignee(db, monkeypatch):
    """Pin the fallback assignee to a real user so business-source tasks
    (and notification recipients) resolve in tests."""
    from app.services.operations import generation

    user = _user(db, "default-admin", UserRole.admin, "tg-default")
    monkeypatch.setattr(generation, "DEFAULT_ASSIGNED_USER_ID", user.id)
    return user


def _task_count(db) -> int:
    return db.query(OperationalTask).count()


def _outbox_count(db) -> int:
    return db.query(NotificationOutbox).count()


def _audit_count(db, action: str) -> int:
    return (
        db.query(__import__("app.models.audit_log", fromlist=["AuditLog"]).AuditLog)
        .filter_by(action=action)
        .count()
    )


class _FailingSender:
    def __init__(self, error="telegram down"):
        self.error = error
        self.calls = 0

    def send(self, recipient, text, reply_markup=None):
        self.calls += 1
        raise RuntimeError(self.error)


class _OkSender:
    def __init__(self):
        self.sent = []

    def send(self, recipient, text, reply_markup=None):
        self.sent.append((recipient, text, reply_markup))
        return "777"


# ---------------------------------------------------------------------------
# scheduler: idempotency + recurring rules
# ---------------------------------------------------------------------------

def test_scheduler_repeat_run_creates_no_duplicates(db_session, monkeypatch):
    _seed_default_assignee(db_session, monkeypatch)
    _seed_lease(db_session)  # rent overdue (due day 5, today Aug 10)
    _seed_rule(db_session, user_id=_user(db_session, "m1", UserRole.manager, "tg1").id)
    db_session.commit()

    r1 = run_scheduler_once(db_session, now=NOW)
    assert r1.tasks_created >= 2  # RENT_OVERDUE + rule task
    tasks_after_first = _task_count(db_session)
    outbox_after_first = _outbox_count(db_session)

    r2 = run_scheduler_once(db_session, now=NOW)
    assert r2.tasks_created == 0
    assert _task_count(db_session) == tasks_after_first
    assert _outbox_count(db_session) == outbox_after_first


def test_recurring_rule_generates_next_period_and_advances(db_session):
    user = _user(db_session, "m2", UserRole.manager, "tg2")
    rule = _seed_rule(db_session, recurrence=Recurrence.monthly, user_id=user.id)
    rule.next_run_at = NOW
    db_session.commit()

    run_scheduler_once(db_session, now=NOW)
    db_session.refresh(rule)
    assert rule.next_run_at == NOW + timedelta(days=31)  # add_months Aug 10 -> Sep 10
    task1 = db_session.query(OperationalTask).one()
    assert task1.dedupe_key == f"recurring:{rule.id}:2026-08"

    later = rule.next_run_at + timedelta(seconds=1)
    run_scheduler_once(db_session, now=later)
    keys = sorted(t.dedupe_key for t in db_session.query(OperationalTask).all())
    assert keys == [f"recurring:{rule.id}:2026-08", f"recurring:{rule.id}:2026-09"]


def test_recurring_rule_quarterly_period_key(db_session):
    rule = _seed_rule(db_session, recurrence=Recurrence.quarterly, next_run_at=NOW)
    db_session.commit()
    run_scheduler_once(db_session, now=NOW)
    key = db_session.query(OperationalTask).one().dedupe_key
    assert key.endswith(":2026-Q3")


def test_disabled_rule_not_claimed(db_session):
    rule = _seed_rule(db_session, next_run_at=NOW - timedelta(days=1))
    rule.enabled = False
    db_session.commit()
    result = run_scheduler_once(db_session, now=NOW)
    assert result.rules_claimed == 0
    assert _task_count(db_session) == 0


# ---------------------------------------------------------------------------
# scheduler: real-PG concurrency
# ---------------------------------------------------------------------------

def test_two_schedulers_concurrent_no_duplicates(db_session, test_engine, monkeypatch):
    _seed_default_assignee(db_session, monkeypatch)
    _seed_lease(db_session)
    _seed_rule(db_session, user_id=_user(db_session, "m3", UserRole.manager, "tg3").id)
    db_session.commit()

    Session = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)
    results: list[dict] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def _run():
        db = Session()
        try:
            barrier.wait(timeout=20)
            results.append(run_scheduler_once(db, now=NOW).model_dump())
        except BaseException as exc:  # noqa: BLE001 - surface in main thread
            errors.append(exc)
        finally:
            db.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_run) for _ in range(2)]
        for f in futures:
            f.result(timeout=60)
    assert not errors, errors

    db = Session()
    try:
        dedupe_keys = [t.dedupe_key for t in db.query(OperationalTask).all()]
        assert len(dedupe_keys) == len(set(dedupe_keys)), "duplicate active tasks"
        rule_tasks = [k for k in dedupe_keys if k.startswith("recurring:")]
        assert len(rule_tasks) == 1
        outbox_keys = [o.dedupe_key for o in db.query(NotificationOutbox).all()]
        assert len(outbox_keys) == len(set(outbox_keys)), "duplicate outbox rows"
        # next_run_at advanced exactly once (one of the two claims won)
        rule = db.query(RecurringRule).one()
        assert rule.next_run_at > NOW
    finally:
        db.close()


def test_rule_claim_skip_locked_excludes_concurrent_worker(db_session, test_engine):
    rule = _seed_rule(db_session, next_run_at=NOW)
    db_session.commit()

    Session = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)
    db_a = Session()
    db_b = Session()
    try:
        claimed_a = claim_due_rules_public(db_a, now=NOW)
        assert len(claimed_a) == 1
        claimed_b = claim_due_rules_public(db_b, now=NOW)
        assert claimed_b == [], "SKIP LOCKED must exclude the row claimed by worker A"
        db_a.rollback()  # simulate worker crash -> claim released
        claimed_b2 = claim_due_rules_public(db_b, now=NOW)
        assert len(claimed_b2) == 1, "released claim must be re-claimable"
        db_b.rollback()
    finally:
        db_a.close()
        db_b.close()


def claim_due_rules_public(db, *, now, batch=20):
    from app.services.operations.scheduler import claim_due_rules
    return claim_due_rules(db, now=now, batch=batch)


# ---------------------------------------------------------------------------
# notification outbox + notifier
# ---------------------------------------------------------------------------

def test_notification_dedupe(db_session):
    task = OperationalTask(
        task_type=OperationalTaskType.AC_MAINTENANCE,
        title="t", source_type="recurring_rule", source_id=1,
        status=OperationalTaskStatus.PENDING, due_at=NOW,
    )
    db_session.add(task)
    db_session.flush()
    ok1 = enqueue_notification(
        db_session, task_id=task.id, channel="telegram", recipient="tg1",
        payload={"message": "x"}, dedupe_key="task:1:telegram:tg1",
    )
    ok2 = enqueue_notification(
        db_session, task_id=task.id, channel="telegram", recipient="tg1",
        payload={"message": "x"}, dedupe_key="task:1:telegram:tg1",
    )
    db_session.commit()
    assert ok1 is True
    assert ok2 is False
    assert _outbox_count(db_session) == 1


def test_notification_retry_exponential_backoff_then_failed(db_session):
    item = NotificationOutbox(
        channel="telegram", recipient="tg1",
        payload={"message": "hello"},
        status=NotificationStatus.PENDING, attempts=0,
    )
    db_session.add(item)
    db_session.commit()

    sender = _FailingSender()
    t0 = NOW
    r1 = process_notifications_once(db_session, sender, now=t0, max_attempts=3, backoff_base=30)
    assert r1 == {"claimed": 1, "sent": 0, "retried": 1, "failed": 0}
    db_session.refresh(item)
    assert item.attempts == 1
    assert item.status == NotificationStatus.PENDING
    assert item.next_attempt_at == t0 + timedelta(seconds=30)
    assert item.last_error == "telegram down"

    r2 = process_notifications_once(db_session, sender, now=t0 + timedelta(seconds=31),
                                    max_attempts=3, backoff_base=30)
    assert r2["retried"] == 1
    db_session.refresh(item)
    assert item.attempts == 2
    assert item.next_attempt_at == t0 + timedelta(seconds=31 + 60)

    r3 = process_notifications_once(db_session, sender, now=t0 + timedelta(seconds=120),
                                    max_attempts=3, backoff_base=30)
    assert r3 == {"claimed": 1, "sent": 0, "retried": 0, "failed": 1}
    db_session.refresh(item)
    assert item.attempts == 3
    assert item.status == NotificationStatus.FAILED
    assert item.next_attempt_at is None


def test_notification_success_marks_sent_and_records_message_id(db_session):
    item = NotificationOutbox(
        channel="telegram", recipient="tg1",
        payload={"message": "hello"},
        status=NotificationStatus.PENDING, attempts=0,
    )
    db_session.add(item)
    db_session.commit()
    result = process_notifications_once(db_session, _OkSender(), now=NOW)
    assert result == {"claimed": 1, "sent": 1, "retried": 0, "failed": 0}
    db_session.refresh(item)
    assert item.status == NotificationStatus.SENT
    assert item.sent_at == NOW
    assert item.telegram_message_id == 777


def test_notifier_passes_reply_markup_through(db_session):
    """V1.3: process_notifications_once forwards the outbox payload's
    reply_markup to the Telegram sender (approve/reject buttons)."""
    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "✅ 批准", "callback_data": "v1:exa:5:abc12345:1700000000"},
                {"text": "❌ 拒绝", "callback_data": "v1:exr:5:abc12345:1700000000"},
            ],
            [{"text": "📎 查看凭证", "callback_data": "v1:exd:5"}],
        ]
    }
    item = NotificationOutbox(
        channel="telegram", recipient="tg1",
        payload={"message": "x", "reply_markup": reply_markup},
        status=NotificationStatus.PENDING, attempts=0,
    )
    db_session.add(item)
    db_session.commit()
    sender = _OkSender()
    result = process_notifications_once(db_session, sender, now=NOW)
    assert result == {"claimed": 1, "sent": 1, "retried": 0, "failed": 0}
    assert sender.sent[0][2] == reply_markup


def test_expense_approval_task_human_notification_with_actions(db_session, monkeypatch):
    """V1.3: APPROVAL_PENDING tasks get a human title (no #expense_id), resolve
    to the Owner, and their outbox notification carries approve/reject/detail
    reply_markup plus a humanized message (no internal enums)."""
    from app.services.operations import generation

    admin = _user(db_session, "exp-owner", UserRole.admin, "tg-owner")
    db_session.commit()
    monkeypatch.setattr(generation, "DEFAULT_ASSIGNED_USER_ID", admin.id)
    expense = Expense(
        expense_date=date(2026, 8, 1), category="repair", amount="5000.00",
        payee="Fix-It Co", status=ExpenseStatus.pending,
        created_at=NOW - timedelta(days=10),
    )
    db_session.add(expense)
    db_session.commit()

    run_scheduler_once(db_session, now=NOW)
    task = db_session.query(OperationalTask).filter_by(
        task_type=OperationalTaskType.APPROVAL_PENDING
    ).one()
    assert task.title == "待批准支出 · repair"
    assert "#" not in task.title
    assert task.assigned_user_id == admin.id

    outbox = db_session.query(NotificationOutbox).filter_by(task_id=task.id).one()
    assert outbox.recipient == "tg-owner"
    message = outbox.payload["message"]
    assert "待办提醒" in message
    assert "待批准支出 · repair" in message
    assert "金额：₱5,000" in message
    assert "金额：5000.00" not in message
    for banned in ("APPROVAL_PENDING", "REPAIR", f"#{expense.id}"):
        assert banned not in message
    kb = outbox.payload["reply_markup"]
    callbacks = [
        button["callback_data"]
        for row in kb["inline_keyboard"]
        for button in row
    ]
    assert any(c.startswith("v1:exa:") for c in callbacks)
    assert any(c.startswith("v1:exr:") for c in callbacks)
    assert any(c == f"v1:exd:{expense.id}" for c in callbacks)
    # no receipt attached -> generic 查看详情, never 查看凭证
    secondary = kb["inline_keyboard"][1]
    assert secondary[0]["text"] == "查看详情"
    assert secondary[0]["callback_data"] == f"v1:exd:{expense.id}"


def test_notification_money_formatting():
    from app.services.operations.generation import _money
    assert _money("5000") == "₱5,000"
    assert _money("5000.50") == "₱5,000.50"
    assert _money("0") == "₱0"
    assert _money(None) == "₱0"


def test_expense_notification_detail_button_shows_receipt_label(db_session, monkeypatch):
    """V1.3: an expense WITH a receipt attachment gets the 📎 查看凭证 label on
    its detail button; the callback data stays v1:exd:<id>."""
    from app.services.operations import generation

    admin = _user(db_session, "exp-owner-2", UserRole.admin, "tg-owner-2")
    db_session.commit()
    monkeypatch.setattr(generation, "DEFAULT_ASSIGNED_USER_ID", admin.id)
    attachment = Attachment(
        filedata="/tmp/fix-it-receipt.pdf",
        original_filename="fix-it-receipt.pdf",
    )
    db_session.add(attachment)
    db_session.flush()
    expense = Expense(
        expense_date=date(2026, 8, 1), category="repair", amount="5000.00",
        payee="Fix-It Co", status=ExpenseStatus.pending,
        created_at=NOW - timedelta(days=10),
        receipt_attachment_id=attachment.id,
    )
    db_session.add(expense)
    db_session.commit()

    run_scheduler_once(db_session, now=NOW)
    task = db_session.query(OperationalTask).filter_by(
        task_type=OperationalTaskType.APPROVAL_PENDING
    ).one()
    outbox = db_session.query(NotificationOutbox).filter_by(task_id=task.id).one()
    kb = outbox.payload["reply_markup"]
    secondary = kb["inline_keyboard"][1]
    assert secondary[0]["text"] == "📎 查看凭证"
    assert secondary[0]["callback_data"] == f"v1:exd:{expense.id}"


def test_worker_crash_after_claim_recovers_via_skip_locked(db_session, test_engine):
    item = NotificationOutbox(
        channel="telegram", recipient="tg1",
        payload={"message": "hello"},
        status=NotificationStatus.PENDING, attempts=0,
    )
    db_session.add(item)
    db_session.commit()

    Session = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)
    db_a = Session()
    db_b = Session()
    try:
        # worker A claims but "crashes" before commit/send
        claimed = claim_pending_notifications(db_a, now=NOW)
        assert [i.id for i in claimed] == [item.id]
        db_a.rollback()

        # worker B re-claims the same row and delivers it
        result = process_notifications_once(db_b, _OkSender(), now=NOW)
        assert result["sent"] == 1
        # `item` is bound to db_session, so re-fetch through worker B's session
        recovered = db_b.get(NotificationOutbox, item.id)
        assert recovered is not None
        assert recovered.status == NotificationStatus.SENT
    finally:
        db_a.close()
        db_b.close()


# ---------------------------------------------------------------------------
# task state machine + RBAC (through the API)
# ---------------------------------------------------------------------------

def _make_task(db, *, task_type=OperationalTaskType.AC_MAINTENANCE,
               status=OperationalTaskStatus.PENDING, assigned_user_id=None,
               source_type="recurring_rule", source_id=1, due_at=None, dedupe_key=None):
    task = OperationalTask(
        task_type=task_type,
        title="季度空调保养",
        source_type=source_type,
        source_id=source_id,
        assigned_user_id=assigned_user_id,
        status=status,
        due_at=due_at or NOW,
        dedupe_key=dedupe_key,
        details={"amount": "12000.00"},
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def test_complete_snooze_cancel_state_machine(client, db_session, admin_headers):
    task = _make_task(db_session)
    task_id = task.id

    # complete
    resp = client.post(f"{API}/operations/tasks/{task_id}/complete", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()["task"]
    assert data["status"] == "COMPLETED"
    assert data["completed_at"] is not None
    assert data["completed_by"] == db_session.query(User).filter_by(role=UserRole.admin).first().id
    assert _audit_count(db_session, "task_completed") == 1

    # replay idempotent
    resp = client.post(f"{API}/operations/tasks/{task_id}/complete", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["task"]["status"] == "COMPLETED"
    assert _audit_count(db_session, "task_completed") == 1

    # cannot cancel a completed task
    resp = client.post(f"{API}/operations/tasks/{task_id}/cancel", headers=admin_headers)
    assert resp.status_code == 409


def test_snooze_presets(client, db_session, admin_headers, monkeypatch):
    task = _make_task(db_session)
    task_id = task.id

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr("app.api.routers.operations.datetime", _FrozenDatetime)

    for preset, expected_hour in (("1h", 13), ("today_afternoon", 17),
                                  ("tomorrow_morning", 9), ("3d", None)):
        task.snoozed_until = None
        db_session.commit()
        resp = client.post(
            f"{API}/operations/tasks/{task_id}/snooze",
            json={"preset": preset}, headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        until = resp.json()["task"]["snoozed_until"]
        assert until is not None
        assert resp.json()["task"]["status"] == "PENDING"
        # DB session renders the offset as +08:00; normalize to UTC for asserts
        parsed = datetime.fromisoformat(until).astimezone(timezone.utc)
        if preset == "3d":
            assert parsed.date() == (NOW + timedelta(days=3)).date()
        else:
            assert parsed.hour == expected_hour

    # explicit until
    resp = client.post(
        f"{API}/operations/tasks/{task_id}/snooze",
        json={"until": "2026-08-20T10:00:00+08:00"}, headers=admin_headers,
    )
    assert resp.status_code == 200
    explicit = datetime.fromisoformat(resp.json()["task"]["snoozed_until"])
    assert explicit.astimezone(timezone.utc).date() == date(2026, 8, 20)

    # past until rejected
    resp = client.post(
        f"{API}/operations/tasks/{task_id}/snooze",
        json={"until": "2020-01-01T00:00:00+08:00"}, headers=admin_headers,
    )
    assert resp.status_code == 422
    assert _audit_count(db_session, "task_snoozed") == 5


def test_cancel_and_replay(client, db_session, admin_headers):
    task = _make_task(db_session)
    resp = client.post(f"{API}/operations/tasks/{task.id}/cancel", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["task"]["status"] == "CANCELLED"
    resp = client.post(f"{API}/operations/tasks/{task.id}/cancel", headers=admin_headers)
    assert resp.status_code == 200  # replay
    assert _audit_count(db_session, "task_cancelled") == 1
    resp = client.post(f"{API}/operations/tasks/{task.id}/complete", headers=admin_headers)
    assert resp.status_code == 409


def test_rbac_agent_sees_only_own_tasks(client, db_session, admin_headers, agent_headers, manager_headers):
    admin = db_session.query(User).filter_by(role=UserRole.admin).first()
    manager = db_session.query(User).filter_by(role=UserRole.manager).first()
    agent = db_session.query(User).filter_by(role=UserRole.agent).first()
    _make_task(db_session, assigned_user_id=admin.id, dedupe_key="a1")
    mine = _make_task(db_session, assigned_user_id=agent.id, dedupe_key="a2")

    # agent list: only own
    resp = client.get(f"{API}/operations/tasks", headers=agent_headers)
    assert resp.status_code == 200
    ids = [t["id"] for t in resp.json()]
    assert ids == [mine.id]

    # agent detail of another user's task -> 403
    other = _make_task(db_session, assigned_user_id=manager.id, dedupe_key="a3")
    resp = client.get(f"{API}/operations/tasks/{other.id}", headers=agent_headers)
    assert resp.status_code == 403

    # agent complete another user's task -> 403
    resp = client.post(f"{API}/operations/tasks/{other.id}/complete", headers=agent_headers)
    assert resp.status_code == 403
    db_session.refresh(other)
    assert other.status == OperationalTaskStatus.PENDING

    # agent can complete own task
    resp = client.post(f"{API}/operations/tasks/{mine.id}/complete", headers=agent_headers)
    assert resp.status_code == 200
    assert resp.json()["task"]["status"] == "COMPLETED"

    # manager/admin can see everything and act on any task
    resp = client.get(f"{API}/operations/tasks", headers=manager_headers)
    assert len(resp.json()) == 3
    resp = client.post(f"{API}/operations/tasks/{other.id}/complete", headers=admin_headers)
    assert resp.status_code == 200


def test_rbac_rules_agent_forbidden(client, agent_headers, manager_headers, admin_headers):
    resp = client.get(f"{API}/operations/rules", headers=agent_headers)
    assert resp.status_code == 403
    resp = client.post(
        f"{API}/operations/rules",
        json={
            "rule_type": "AC_MAINTENANCE",
            "title": "季度保养",
            "recurrence": "quarterly",
        },
        headers=agent_headers,
    )
    assert resp.status_code == 403
    resp = client.post(
        f"{API}/operations/rules",
        json={
            "rule_type": "AC_MAINTENANCE",
            "title": "季度保养",
            "recurrence": "quarterly",
        },
        headers=manager_headers,
    )
    assert resp.status_code == 201


def test_rules_crud_and_disable(client, db_session, manager_headers):
    resp = client.post(
        f"{API}/operations/rules",
        json={
            "rule_type": "AC_MAINTENANCE",
            "title": "季度空调保养",
            "recurrence": "quarterly",
            "interval_months": 3,
        },
        headers=manager_headers,
    )
    assert resp.status_code == 201
    rule_id = resp.json()["id"]
    assert resp.json()["next_run_at"] is not None
    assert _audit_count(db_session, "rule_created") == 1

    resp = client.patch(
        f"{API}/operations/rules/{rule_id}",
        json={"title": "季度空调保养 v2", "enabled": True},
        headers=manager_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "季度空调保养 v2"
    assert _audit_count(db_session, "rule_updated") == 1

    resp = client.post(f"{API}/operations/rules/{rule_id}/disable", headers=manager_headers)
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    assert _audit_count(db_session, "rule_disabled") == 1

    resp = client.get(f"{API}/operations/rules?enabled=true", headers=manager_headers)
    assert all(r["enabled"] is True for r in resp.json())


def test_summary_scoped_to_agent(client, db_session, agent_headers, admin_headers, monkeypatch):
    # Freeze the real-time clock used by /operations/summary to the test's fixed
    # NOW so the overdue / due_today bucketing is deterministic on any run day
    # (previously date-fragile: it drifted once the real calendar moved past 2026-08-10).
    import app.services.operations.summary as summary_mod
    import datetime as _dt

    class _FrozenDatetime:
        @staticmethod
        def now(tz):
            return NOW

        @staticmethod
        def combine(*args, **kwargs):
            return _dt.datetime.combine(*args, **kwargs)

    monkeypatch.setattr(summary_mod, "datetime", _FrozenDatetime)
    admin = db_session.query(User).filter_by(role=UserRole.admin).first()
    agent = db_session.query(User).filter_by(role=UserRole.agent).first()
    _make_task(db_session, assigned_user_id=admin.id, due_at=NOW - timedelta(days=2), dedupe_key="s1")
    _make_task(db_session, assigned_user_id=admin.id, due_at=NOW, dedupe_key="s2")
    _make_task(db_session, assigned_user_id=agent.id, due_at=NOW + timedelta(days=2), dedupe_key="s3")

    resp = client.get(f"{API}/operations/summary", headers=agent_headers)
    assert resp.status_code == 200
    assert resp.json() == {"overdue": 0, "due_today": 0, "due_7_days": 1, "pending_total": 1}

    resp = client.get(f"{API}/operations/summary", headers=admin_headers)
    assert resp.json() == {"overdue": 1, "due_today": 1, "due_7_days": 2, "pending_total": 3}


def test_scheduler_run_endpoint(client, db_session, manager_headers, monkeypatch):
    from app.services.operations import config as ops_config
    from app.services.operations import generation

    admin = _seed_valid_default_admin(db_session, "tg-sched")  # valid notifiable default
    # endpoint validates against config DEFAULT_ASSIGNED_USER_ID; pin it to the
    # seeded valid admin so the manager-triggered pass is allowed.
    monkeypatch.setattr(ops_config, "DEFAULT_ASSIGNED_USER_ID", admin.id)
    monkeypatch.setattr(generation, "DEFAULT_ASSIGNED_USER_ID", admin.id)
    _seed_lease(db_session)
    db_session.commit()
    resp = client.post(f"{API}/operations/scheduler/run", headers=manager_headers)
    assert resp.status_code == 200
    assert resp.json()["tasks_created"] >= 1


def test_scheduler_run_endpoint_fails_fast_on_invalid_default(client, db_session, manager_headers, monkeypatch):
    """A manager-triggered scheduler pass with a broken default assignee must NOT
    silently create un-notifiable tasks (backend review finding #1). It fails fast
    instead of creating PENDING tasks with an unresolvable recipient."""
    import pytest

    from app.services.operations import config as ops_config

    _seed_lease(db_session)
    db_session.commit()
    # Pin the default to a user id that does not exist -> validate fails.
    monkeypatch.setattr(ops_config, "DEFAULT_ASSIGNED_USER_ID", 999999)
    with pytest.raises(RuntimeError) as excinfo:
        client.post(f"{API}/operations/scheduler/run", headers=manager_headers)
    assert "no user with this id" in str(excinfo.value)
    assert _task_count(db_session) == 0, "no business task may be created behind a broken default"


def test_audit_action_enum_append_only(db_session):
    """Old V1.1 actions are untouched; V1.2 actions are appended."""
    old = ["create", "update", "soft_delete", "confirm", "approve", "reject", "pay", "reverse"]
    new = ["task_created", "task_completed", "task_cancelled", "task_snoozed",
           "rule_created", "rule_updated", "rule_disabled",
           "task_auto_completed", "task_auto_cancelled"]
    values = [a.value for a in AuditAction]
    assert values[: len(old)] == old
    for item in new:
        assert item in values


# ---------------------------------------------------------------------------
# reconciliation
# ---------------------------------------------------------------------------

def test_reconciliation_payment_pending_completes_when_paid(client, db_session, admin_headers, monkeypatch):
    _seed_default_assignee(db_session, monkeypatch)
    admin = db_session.query(User).filter_by(role=UserRole.admin).first()
    expense = Expense(expense_date=date(2026, 8, 1), category="repair", amount="5000.00",
                      payee="Fix-It Co", status=ExpenseStatus.approved,
                      approved_at=NOW - timedelta(days=10), approved_by=admin.id)
    db_session.add(expense)
    db_session.commit()

    run_scheduler_once(db_session, now=NOW)
    task = db_session.query(OperationalTask).filter_by(
        task_type=OperationalTaskType.PAYMENT_PENDING
    ).one()
    assert task.status == OperationalTaskStatus.PENDING

    # Source state changes (expense becomes paid) OUTSIDE the API pay handler,
    # so the task stays PENDING and the scheduler's reconcile pass is the one
    # that auto-completes it (P0-EXPENSE-PAID-CLOSEOUT-001 closes linked tasks
    # at API pay-time, which would preempt reconcile; here we exercise the
    # reconcile auto-completion + task_auto_completed audit path instead).
    expense.status = ExpenseStatus.paid
    db_session.commit()
    assert expense.status == ExpenseStatus.paid  # source is paid, task still PENDING

    run_scheduler_once(db_session, now=NOW)
    db_session.refresh(task)
    assert task.status == OperationalTaskStatus.COMPLETED
    assert task.completed_at is not None
    assert _audit_count(db_session, "task_auto_completed") >= 1


def test_financial_write_path_not_bypassed(client, db_session, admin_headers, monkeypatch):
    _seed_default_assignee(db_session, monkeypatch)
    """Completing a PAYMENT_PENDING task must NOT change the expense."""
    admin = db_session.query(User).filter_by(role=UserRole.admin).first()
    expense = Expense(expense_date=date(2026, 8, 1), category="repair", amount="5000.00",
                      payee="Fix-It Co", status=ExpenseStatus.approved,
                      approved_at=NOW - timedelta(days=10), approved_by=admin.id)
    db_session.add(expense)
    db_session.commit()
    run_scheduler_once(db_session, now=NOW)
    task = db_session.query(OperationalTask).filter_by(
        task_type=OperationalTaskType.PAYMENT_PENDING
    ).one()

    resp = client.post(f"{API}/operations/tasks/{task.id}/complete", headers=admin_headers)
    assert resp.status_code == 200

    db_session.refresh(expense)
    assert expense.status == ExpenseStatus.approved, "task handler must not touch expenses"
    assert expense.amount == Decimal("5000.00")


def test_reconciliation_approval_pending_cancelled_when_rejected(db_session, monkeypatch):
    _seed_default_assignee(db_session, monkeypatch)
    expense = Expense(expense_date=date(2026, 8, 1), category="repair", amount="5000.00",
                      payee="Fix-It Co", status=ExpenseStatus.pending,
                      created_at=NOW - timedelta(days=10))
    db_session.add(expense)
    db_session.commit()

    run_scheduler_once(db_session, now=NOW)
    task = db_session.query(OperationalTask).filter_by(
        task_type=OperationalTaskType.APPROVAL_PENDING
    ).one()
    assert task.status == OperationalTaskStatus.PENDING

    expense.status = ExpenseStatus.rejected
    db_session.commit()
    run_scheduler_once(db_session, now=NOW)
    db_session.refresh(task)
    assert task.status == OperationalTaskStatus.CANCELLED
    assert _audit_count(db_session, "task_auto_cancelled") >= 1


def test_reconciliation_rent_due_completes_when_covered(db_session, monkeypatch):
    _seed_default_assignee(db_session, monkeypatch)
    _seed_lease(db_session, due_day=13)  # Aug 13 inside the 3-day advance window
    db_session.commit()
    run_scheduler_once(db_session, now=NOW)
    task = db_session.query(OperationalTask).filter_by(
        task_type=OperationalTaskType.RENT_DUE
    ).one()
    assert task.status == OperationalTaskStatus.PENDING

    lease = db_session.query(Lease).one()
    income = Income(lease_id=lease.id, amount="12000.00", received_date=date(2026, 8, 10),
                    status=IncomeStatus.confirmed, description="rent 2026-08")
    db_session.add(income)
    db_session.commit()

    run_scheduler_once(db_session, now=NOW)
    db_session.refresh(task)
    assert task.status == OperationalTaskStatus.COMPLETED


def test_reconciliation_lease_expiring_cancelled_when_terminated(db_session, monkeypatch):
    _seed_default_assignee(db_session, monkeypatch)
    _seed_lease(db_session, start="2025-01-01", end="2026-08-20")
    db_session.commit()
    run_scheduler_once(db_session, now=NOW)
    task = db_session.query(OperationalTask).filter_by(
        task_type=OperationalTaskType.LEASE_EXPIRING
    ).one()
    assert task.status == OperationalTaskStatus.PENDING

    lease = db_session.query(Lease).one()
    lease.status = LeaseStatus.terminated
    db_session.commit()
    run_scheduler_once(db_session, now=NOW)
    db_session.refresh(task)
    assert task.status == OperationalTaskStatus.CANCELLED


def test_reconciliation_lease_expiring_completes_when_renewed(db_session, monkeypatch):
    _seed_default_assignee(db_session, monkeypatch)
    lease = _seed_lease(db_session, start="2025-01-01", end="2026-08-20")
    db_session.commit()
    run_scheduler_once(db_session, now=NOW)
    task = db_session.query(OperationalTask).filter_by(
        task_type=OperationalTaskType.LEASE_EXPIRING
    ).one()

    lease.status = LeaseStatus.expired
    renewed = Lease(unit_id=lease.unit_id, tenant_id=lease.tenant_id,
                    start_date=date(2026, 8, 21), end_date=date(2027, 8, 20),
                    monthly_rent="12000.00", deposit="24000.00",
                    status=LeaseStatus.active, due_day=5)
    db_session.add(renewed)
    db_session.commit()

    run_scheduler_once(db_session, now=NOW)
    db_session.refresh(task)
    assert task.status == OperationalTaskStatus.COMPLETED


def test_settlement_pending_task_and_reconciliation(db_session, monkeypatch):
    _seed_default_assignee(db_session, monkeypatch)
    agent = _user(db_session, "ag1", UserRole.agent, "tg-agent")
    lease = _seed_lease(db_session)
    rule = CommissionRule(name="出租", rule_type=CommissionRuleType.percentage,
                          value="50.00", agent_role="出租")
    db_session.add(rule)
    db_session.flush()
    settlement = CommissionSettlement(agent_id=agent.id, lease_id=lease.id, rule_id=rule.id,
                                      computed_amount="6000.00",
                                      status=CommissionSettlementStatus.pending,
                                      created_at=NOW - timedelta(days=5))
    db_session.add(settlement)
    db_session.commit()

    run_scheduler_once(db_session, now=NOW)
    task = db_session.query(OperationalTask).filter_by(
        task_type=OperationalTaskType.SETTLEMENT_PENDING
    ).one()
    assert task.assigned_user_id == agent.id
    outbox = db_session.query(NotificationOutbox).filter_by(task_id=task.id).one()
    assert outbox.recipient == "tg-agent"

    settlement.status = CommissionSettlementStatus.confirmed
    db_session.commit()
    run_scheduler_once(db_session, now=NOW)
    db_session.refresh(task)
    assert task.status == OperationalTaskStatus.COMPLETED


def test_business_task_no_assignee_defaults_and_enqueues_notification(db_session, monkeypatch):
    from app.services.operations import generation

    admin = _user(db_session, "admin-tg", UserRole.admin, "tg-admin")
    db_session.commit()
    monkeypatch.setattr(generation, "DEFAULT_ASSIGNED_USER_ID", admin.id)

    _seed_lease(db_session)  # due day 5 -> RENT_OVERDUE (no explicit assignee)
    db_session.commit()

    run_scheduler_once(db_session, now=NOW)
    task = db_session.query(OperationalTask).filter_by(
        task_type=OperationalTaskType.RENT_OVERDUE
    ).one()
    assert task.assigned_user_id == admin.id
    outbox = db_session.query(NotificationOutbox).one()
    assert outbox.task_id == task.id
    assert outbox.recipient == "tg-admin"
    assert outbox.status == NotificationStatus.PENDING


def test_recurring_rule_task_keeps_rule_assignee_as_is(db_session):
    _seed_rule(db_session, next_run_at=NOW)  # rule has no assigned_user_id
    db_session.commit()

    run_scheduler_once(db_session, now=NOW)
    task = db_session.query(OperationalTask).one()
    assert task.source_type == "recurring_rule"
    assert task.assigned_user_id is None
    assert _outbox_count(db_session) == 0


# ---------------------------------------------------------------------------
# Alembic migration upgrade / rollback
# ---------------------------------------------------------------------------

def test_alembic_migration_upgrade_downgrade(monkeypatch, test_engine):
    """Real Alembic upgrade head -> downgrade base on a scratch database."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect
    from sqlalchemy.engine import make_url

    original_url = settings.database_url
    scratch = "pasay_pm_test_mig_v12"

    def _admin_engine():
        return create_engine(
            make_url(settings.database_url).set(database="postgres"),
            isolation_level="AUTOCOMMIT",
        )

    def _drop_scratch():
        engine = _admin_engine()
        try:
            with engine.connect() as conn:
                # kill leftover sessions from previous runs before dropping
                conn.execute(text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ), {"name": scratch})
                conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch}"'))
        finally:
            engine.dispose()

    _drop_scratch()
    admin_engine = _admin_engine()
    try:
        with admin_engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{scratch}"'))
    finally:
        admin_engine.dispose()

    scratch_url = make_url(settings.database_url).set(database=scratch)
    monkeypatch.setattr(
        settings, "database_url", scratch_url.render_as_string(hide_password=False)
    )

    repo_root = Path(__file__).resolve().parents[1]
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "alembic"))
    try:
        command.upgrade(cfg, "head")
        engine = create_engine(scratch_url)
        try:
            insp = inspect(engine)
            tables = set(insp.get_table_names())
            for name in ("operational_tasks", "recurring_rules", "notification_outbox"):
                assert name in tables, name
            cols = {c["name"] for c in insp.get_columns("operational_tasks")}
            assert {"dedupe_key", "metadata", "due_at", "status"} <= cols
            user_cols = {c["name"] for c in insp.get_columns("users")}
            assert "telegram_chat_id" in user_cols
            idxs = {i["name"] for i in insp.get_indexes("operational_tasks")}
            assert "uq_operational_tasks_active_dedupe" in idxs
            # partial unique index condition is enforced by PostgreSQL
            partial = engine.connect().execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname='uq_operational_tasks_active_dedupe'")
            ).scalar()
            assert "WHERE" in partial and "PENDING" in partial
        finally:
            engine.dispose()

        command.downgrade(cfg, "base")
        engine = create_engine(scratch_url)
        try:
            names = inspect(engine).get_table_names()
            assert "operational_tasks" not in names
            assert "notification_outbox" not in names
        finally:
            engine.dispose()
    finally:
        monkeypatch.setattr(settings, "database_url", original_url)
        _drop_scratch()


# ---------------------------------------------------------------------------
# V1.3 explicit SYSTEM provenance
# ---------------------------------------------------------------------------

def test_all_four_workers_record_explicit_system_provenance(db_session):
    def assert_system(action, record_id, name):
        audit = db_session.query(AuditLog).filter_by(
            action=action, record_id=record_id
        ).order_by(AuditLog.id.desc()).first()
        assert audit is not None
        principal = db_session.query(Principal).filter_by(
            name=name, principal_type=PrincipalType.SYSTEM
        ).one()
        credential = db_session.query(ApiCredential).filter_by(
            principal_id=principal.id,
            purpose=f"internal:{name}",
            state=CredentialState.ACTIVE,
        ).one()
        assert audit.actor_id is None
        assert audit.subject_principal_id == principal.id
        assert audit.caller_principal_id == principal.id
        assert audit.credential_id == credential.id
        assert audit.channel == "internal"

    # scheduler
    assignee = _user(db_session, "system-provenance-admin", UserRole.admin, "tg-system")
    _seed_rule(db_session, user_id=assignee.id)
    db_session.commit()
    run_scheduler_once(db_session, now=NOW)
    scheduled_task = db_session.query(OperationalTask).filter_by(
        source_type="recurring_rule"
    ).one()
    assert_system("task_created", scheduled_task.id, "scheduler")

    # reconcile
    expense = Expense(
        expense_date=date(2026, 8, 1),
        category="repair",
        amount="5000.00",
        payee="Vendor",
        status=ExpenseStatus.paid,
    )
    db_session.add(expense)
    db_session.flush()
    reconcile_task = _make_task(
        db_session,
        task_type=OperationalTaskType.PAYMENT_PENDING,
        source_type="expense",
        source_id=expense.id,
        dedupe_key="system-provenance-reconcile",
    )
    db_session.commit()
    reconcile_tasks(db_session, now=NOW)
    db_session.commit()
    assert_system("task_auto_completed", reconcile_task.id, "reconcile")

    # notifier: a stale snooze reminder is durably dropped before any send.
    stale_task = _make_task(
        db_session,
        status=OperationalTaskStatus.CANCELLED,
        assigned_user_id=assignee.id,
        dedupe_key="system-provenance-stale-task",
    )
    stale_outbox = NotificationOutbox(
        task_id=stale_task.id,
        channel="telegram",
        recipient="tg-system",
        payload={"message": "stale"},
        status=NotificationStatus.PENDING,
        dedupe_key=f"snooze-redelivery:{stale_task.id}:0:{NOW.isoformat()}",
    )
    db_session.add(stale_outbox)
    db_session.commit()
    process_notifications_once(db_session, _OkSender(), now=NOW)
    db_session.refresh(stale_outbox)
    assert stale_outbox.status == NotificationStatus.DROPPED
    assert_system("outbox_dropped", stale_outbox.id, "notifier")

    # backfill: assignee is ownership only and is never substituted as actor.
    backfill_task = _business_task(
        db_session,
        dedupe_key="system-provenance-backfill",
    )
    db_session.commit()
    backfill_unassigned_business_tasks(
        db_session,
        default_assignee_id=assignee.id,
        now=NOW,
    )
    assert_system("task_backfilled", backfill_task.id, "backfill")


# ---------------------------------------------------------------------------
# V1.2 data-hardening: default-assignee backfill + notification re-enqueue
# ---------------------------------------------------------------------------

def _seed_valid_default_admin(db, telegram_chat_id="tg-default"):
    """Create a valid default assignee (active admin with a telegram chat id)."""
    return _user(db, "admin-backfill", UserRole.admin, telegram_chat_id)


def _business_task(db, *, assigned_user_id=None, status=OperationalTaskStatus.PENDING,
                   source_type="lease", dedupe_key=None):
    """A PENDING business-source task (source_type in BUSINESS_SOURCE_TYPES)."""
    return _make_task(
        db,
        task_type=OperationalTaskType.RENT_DUE,
        status=status,
        assigned_user_id=assigned_user_id,
        source_type=source_type,
        source_id=1,
        dedupe_key=dedupe_key,
    )


def test_backfill_assigns_unassigned_business_task_and_enqueues(db_session):
    admin = _seed_valid_default_admin(db_session, "tg-default")
    task = _business_task(db_session, dedupe_key="b1")
    db_session.commit()

    report = backfill_unassigned_business_tasks(
        db_session, default_assignee_id=admin.id, now=NOW
    )
    assert report.tasks_backfilled == [task.id]
    assert report.tasks_skipped_already_assigned == 0
    assert report.tasks_missing_notification == [task.id]
    assert report.notifications_enqueued == 1

    db_session.refresh(task)
    assert task.assigned_user_id == admin.id
    outbox = db_session.query(NotificationOutbox).filter_by(task_id=task.id).one()
    assert outbox.recipient == "tg-default"
    assert outbox.status == NotificationStatus.PENDING
    assert _audit_count(db_session, "task_backfilled") == 1
    audit = (
        db_session.query(__import__("app.models.audit_log", fromlist=["AuditLog"]).AuditLog)
        .filter_by(action="task_backfilled", record_id=task.id)
        .one()
    )
    assert audit.changed_fields["assigned_user_id"] == [None, admin.id]
    assert audit.actor_id is None


def test_backfill_does_not_overwrite_already_assigned_task(db_session):
    admin = _seed_valid_default_admin(db_session, "tg-default")
    other = _user(db_session, "manager-owner", UserRole.manager, "tg-mgr")
    _ = other
    task = _business_task(db_session, assigned_user_id=other.id, dedupe_key="b2")
    db_session.commit()

    report = backfill_unassigned_business_tasks(
        db_session, default_assignee_id=admin.id, now=NOW
    )
    assert report.tasks_backfilled == []
    assert report.tasks_skipped_already_assigned == 1
    db_session.refresh(task)
    assert task.assigned_user_id == other.id, "backfill must not overwrite ownership"
    assert _audit_count(db_session, "task_backfilled") == 0


def test_backfill_rerun_is_idempotent(db_session):
    admin = _seed_valid_default_admin(db_session, "tg-default")
    task = _business_task(db_session, dedupe_key="b3")
    db_session.commit()

    r1 = backfill_unassigned_business_tasks(db_session, default_assignee_id=admin.id, now=NOW)
    assert r1.tasks_backfilled == [task.id]
    assert r1.notifications_enqueued == 1
    outbox_after_first = _outbox_count(db_session)

    # Re-run: finds nothing to do, produces no new audit / no duplicate outbox.
    r2 = backfill_unassigned_business_tasks(db_session, default_assignee_id=admin.id, now=NOW)
    assert r2.tasks_backfilled == []
    assert r2.tasks_skipped_already_assigned == 1
    assert r2.notifications_enqueued == 0
    db_session.refresh(task)
    assert task.assigned_user_id == admin.id
    assert _audit_count(db_session, "task_backfilled") == 1
    assert _outbox_count(db_session) == outbox_after_first == 1


def test_enqueue_missing_notifications_no_dupe_when_sent_exists(db_session):
    admin = _seed_valid_default_admin(db_session, "tg-default")
    task = _business_task(db_session, assigned_user_id=admin.id, dedupe_key="b4")
    # a SENT outbox row already exists for this task
    enqueue_notification(
        db_session, task_id=task.id, channel="telegram", recipient="tg-default",
        payload={"message": "x"}, dedupe_key=f"task:{task.id}:telegram:tg-default",
    )
    sent = db_session.query(NotificationOutbox).filter_by(task_id=task.id).one()
    sent.status = NotificationStatus.SENT
    sent.sent_at = NOW
    db_session.commit()

    missing, enqueued = enqueue_missing_notifications(db_session)
    assert missing == []
    assert enqueued == 0
    assert (
        db_session.query(NotificationOutbox).filter_by(task_id=task.id).count() == 1
    ), "SENT outbox must not be duplicated"


def test_enqueue_missing_notifications_creates_one_row_when_none(db_session):
    admin = _seed_valid_default_admin(db_session, "tg-default")
    task = _business_task(db_session, assigned_user_id=admin.id, dedupe_key="b5")
    db_session.commit()
    assert _outbox_count(db_session) == 0

    missing, enqueued = enqueue_missing_notifications(db_session)
    assert missing == [task.id]
    assert enqueued == 1
    assert _outbox_count(db_session) == 1
    outbox = db_session.query(NotificationOutbox).filter_by(task_id=task.id).one()
    assert outbox.recipient == "tg-default"
    assert outbox.status == NotificationStatus.PENDING


def test_enqueue_missing_notifications_does_not_repair_existing_pending_outbox(db_session):
    """A PENDING (not-yet-sent) outbox row blocks a duplicate via the dedupe index."""
    admin = _seed_valid_default_admin(db_session, "tg-default")
    task = _business_task(db_session, assigned_user_id=admin.id, dedupe_key="b6")
    enqueue_notification(
        db_session, task_id=task.id, channel="telegram", recipient="tg-default",
        payload={"message": "x"}, dedupe_key=f"task:{task.id}:telegram:tg-default",
    )
    db_session.commit()

    missing, enqueued = enqueue_missing_notifications(db_session)
    # Task is considered "missing a SENT notification" (its row is still PENDING) but the
    # unique dedupe index makes the re-enqueue a no-op -> no duplicate row.
    assert missing == [task.id]
    assert enqueued == 0
    assert _outbox_count(db_session) == 1


def test_validate_default_assignee_missing_user_raises(db_session):
    import pytest

    with pytest.raises(RuntimeError) as excinfo:
        validate_default_assignee(db_session, 999999)
    assert "no user with this id" in str(excinfo.value)


def test_validate_default_assignee_no_telegram_chat_id_raises(db_session):
    import pytest

    admin = _user(db_session, "admin-no-tg", UserRole.admin, telegram_chat_id=None)
    db_session.commit()
    with pytest.raises(RuntimeError) as excinfo:
        validate_default_assignee(db_session, admin.id)
    assert "telegram_chat_id" in str(excinfo.value) or "Telegram chat id" in str(excinfo.value)


def test_validate_default_assignee_inactive_user_raises(db_session):
    import pytest

    admin = User(
        username="admin-inactive", role=UserRole.admin,
        api_key_hash=__import__("secrets").token_urlsafe(24),
        is_active=False, telegram_chat_id="tg-x",
    )
    db_session.add(admin)
    db_session.commit()
    with pytest.raises(RuntimeError) as excinfo:
        validate_default_assignee(db_session, admin.id)
    assert "inactive" in str(excinfo.value)


def test_validate_default_assignee_agent_role_raises(db_session):
    import pytest

    agent = _user(db_session, "agent-default", UserRole.agent, "tg-agent")
    db_session.commit()
    with pytest.raises(RuntimeError) as excinfo:
        validate_default_assignee(db_session, agent.id)
    assert "admin" in str(excinfo.value) or "manager" in str(excinfo.value)


def test_backfill_validates_default_assignee_before_touching_data(db_session):
    import pytest

    task = _business_task(db_session, dedupe_key="b7")
    db_session.commit()
    with pytest.raises(RuntimeError):
        backfill_unassigned_business_tasks(db_session, default_assignee_id=999999, now=NOW)
    db_session.refresh(task)
    assert task.assigned_user_id is None
    assert _audit_count(db_session, "task_backfilled") == 0
    assert _outbox_count(db_session) == 0


def test_concurrent_missing_notifications_single_outbox(db_session, test_engine):
    """Two concurrent enqueue processes on one unassigned business task -> ONE outbox row."""
    admin = _seed_valid_default_admin(db_session, "tg-default")
    task = _business_task(db_session, assigned_user_id=admin.id, dedupe_key="b8")
    db_session.commit()

    Session = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def _run():
        db = Session()
        try:
            barrier.wait(timeout=20)
            enqueue_missing_notifications(db)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            db.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_run) for _ in range(2)]
        for f in futures:
            f.result(timeout=60)
    assert not errors, errors
    assert (
        db_session.query(NotificationOutbox).filter_by(task_id=task.id).count() == 1
    ), "concurrent enqueue must produce exactly one outbox row"


def test_concurrent_backfill_assignment_converges(db_session, test_engine):
    """Two concurrent backfills of the same unassigned task converge: one owner, one audit,
    no duplicate outbox."""
    admin = _seed_valid_default_admin(db_session, "tg-default")
    task = _business_task(db_session, dedupe_key="b9")
    db_session.commit()

    Session = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def _run():
        db = Session()
        try:
            barrier.wait(timeout=20)
            backfill_unassigned_business_tasks(db, default_assignee_id=admin.id, now=NOW)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            db.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_run) for _ in range(2)]
        for f in futures:
            f.result(timeout=60)
    assert not errors, errors

    db_session.refresh(task)
    assert task.assigned_user_id == admin.id
    # exactly one owner; audit rows may be 1..2 depending on the race, but ownership +
    # outbox must be unique. The atomic conditional UPDATE guarantees one winner claims
    # assignment, so exactly one task_backfilled audit row is written.
    assert _audit_count(db_session, "task_backfilled") == 1
    assert _outbox_count(db_session) == 1
    outbox = db_session.query(NotificationOutbox).filter_by(task_id=task.id).one()
    assert outbox.recipient == "tg-default"
