"""TELEGRAM-OPS-UX-CONVERGENCE-003 — Phase 1 P0 reminder + data-truth tests.

Pins the frozen reminder product rules (§1.3-§1.7):
A. same-day continuous scans (10x) -> exactly ONE dispatch;
B. first send + runtime restart -> no additional dispatch;
C. two concurrent workers -> exactly ONE dispatch;
D. next day, still incomplete -> one NEW dispatch allowed (then dedup again);
E. Acknowledge -> same-day scans dispatch 0 more;
F. Completed -> cross-day scans dispatch 0 more;
G. two different business objects -> each may dispatch once.

Also pins: the RENT_DUE create->supersede loop fix (no per-pass task spam),
the notifier send-time guard, the legacy `??` expense READ path (no 500), and
the arrears truth source (unpaid_periods in quick rent == RENT_OVERDUE count).
"""
from __future__ import annotations

import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy.orm import sessionmaker

from app.models.financial import Expense, ExpenseStatus, Income, IncomeStatus
from app.models.lease import Lease, LeaseStatus
from app.models.operations import (
    NotificationOutbox,
    NotificationStatus,
    OperationalTask,
    OperationalTaskStatus,
    OperationalTaskType,
)
from app.models.property import Property, Unit, UnitStatus
from app.models.tenant import Tenant
from app.models.user import UserRole
from app.services.operations.daily_dedup import claim_daily_dedup, philippines_local_date
from app.services.operations.generation import generate_business_tasks
from app.services.operations.notifier import process_notifications_once
from app.services.operations.quick import build_quick_rent, build_quick_tasks
from app.services.operations.scheduler import run_scheduler_once
from tests.conftest import ensure_default_org, seed_property, seed_unit, seed_tenant, seed_expense  # noqa: F401 (seed helpers shared via conftest)

API = "/api/v1"
# 2026-08-17 12:00 UTC = the live P0 window; PH date = 2026-08-17.
NOW = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)


def _user(db, username, role, telegram_chat_id=None):
    from tests.conftest import make_user
    return make_user(db, username, role)


def _seed_default_assignee(db, monkeypatch):
    """Pin DEFAULT + SECRETARY assignee to a real user with a telegram chat id
    so business-source tasks resolve a notification recipient."""
    from app.services.operations import generation

    user = _user(db, "default-admin", UserRole.admin)
    db.flush()
    # telegram destination: the identity layer resolves via user.telegram_chat_id
    from app.models.user import User
    db.query(User).filter(User.id == user[0].id).update({"telegram_chat_id": "tg-default"})
    db.commit()
    monkeypatch.setattr(generation, "DEFAULT_ASSIGNED_USER_ID", user[0].id)
    monkeypatch.setattr(generation, "SECRETARY_ASSIGNEE_ID", user[0].id)
    return user[0]


def _seed_overdue_plus_upcoming_lease(db, *, rent="25000.00"):
    """A lease with BOTH overdue periods and an upcoming period within the
    RENT_DUE_ADVANCE_DAYS window — the exact live P0 shape (DEV-BAY-1680:
    overdue 104d + 2026-08 due on the 20th, scanned 2026-08-17)."""
    prop = seed_property(db, name="Sunset Tower", address="1 Roxas Blvd", city="Pasay", total_units=4)
    unit = Unit(property_id=prop.id, unit_number="1680", floor="16", size_sqm="32.50",
                monthly_rent=rent, status=UnitStatus.occupied)
    tenant = seed_tenant(db, full_name="Carlo Reyes", phone="+639170000000")
    db.add_all([unit])
    db.flush()
    lease = Lease(
        unit_id=unit.id, tenant_id=tenant.id,
        start_date=date(2025, 1, 1), end_date=date(2026, 12, 31),
        monthly_rent=rent, deposit="50000.00", status=LeaseStatus.active,
        due_day=20,
    )
    db.add(lease)
    db.flush()
    return lease


class _OkSender:
    def __init__(self):
        self.sent = []

    def send(self, recipient, text, reply_markup=None):
        self.sent.append((recipient, text, reply_markup))
        return "777"


def _outbox_pending(db) -> int:
    return (
        db.query(NotificationOutbox)
        .filter(NotificationOutbox.status == NotificationStatus.PENDING)
        .count()
    )


# ---------------------------------------------------------------------------
# A. same-day 10 scans -> dispatch == 1 (the live P0)
# ---------------------------------------------------------------------------

def test_same_day_ten_scans_single_dispatch(db_session, monkeypatch):
    """The exact live P0 shape: a lease with overdue + upcoming periods.
    Ten scheduler passes on the same PH day must create the RENT_OVERDUE task
    ONCE, never a RENT_DUE (no create->supersede loop), and dispatch exactly
    ONE notification."""
    _seed_default_assignee(db_session, monkeypatch)
    lease = _seed_overdue_plus_upcoming_lease(db_session)
    db_session.commit()

    sender = _OkSender()
    first = run_scheduler_once(db_session, now=NOW)
    assert first.tasks_created == 1, first  # RENT_OVERDUE only, no RENT_DUE
    # No RENT_DUE task may exist (superseded noise must not be re-created).
    due_tasks = (
        db_session.query(OperationalTask)
        .filter(OperationalTask.task_type == OperationalTaskType.RENT_DUE)
        .all()
    )
    assert due_tasks == []

    for _ in range(9):
        result = run_scheduler_once(db_session, now=NOW)
        assert result.tasks_created == 0, result

    assert _outbox_pending(db_session) == 1
    result = process_notifications_once(db_session, sender, now=NOW)
    assert result["sent"] == 1
    assert len(sender.sent) == 1
    text = sender.sent[0][1]
    assert "房号" in text and "租金逾期" in text
    # second notifier pass: nothing left to send
    result2 = process_notifications_once(db_session, _OkSender(), now=NOW)
    assert result2["sent"] == 0


def test_same_day_scans_no_rent_due_recreation_loop(db_session, monkeypatch):
    """Even without the daily dedup, the generation fix alone must not recreate
    RENT_DUE every pass (that loop was the per-minute spam source)."""
    _seed_default_assignee(db_session, monkeypatch)
    _seed_overdue_plus_upcoming_lease(db_session)
    db_session.commit()
    for _ in range(5):
        run_scheduler_once(db_session, now=NOW)
    types = sorted(
        t.task_type.value
        for t in db_session.query(OperationalTask).all()
    )
    assert types == ["RENT_OVERDUE"]
    assert _outbox_pending(db_session) == 1


# ---------------------------------------------------------------------------
# B. runtime restart (new Session) -> no additional dispatch
# ---------------------------------------------------------------------------

def test_restart_same_day_no_extra_dispatch(db_session, test_engine, monkeypatch):
    _seed_default_assignee(db_session, monkeypatch)
    _seed_overdue_plus_upcoming_lease(db_session)
    db_session.commit()
    run_scheduler_once(db_session, now=NOW)
    outbox_after_first = _outbox_pending(db_session)

    # Simulate a runtime restart: brand-new session/engine connection.
    Session = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)
    db2 = Session()
    try:
        for _ in range(5):
            run_scheduler_once(db2, now=NOW)
    finally:
        db2.close()
    db_session.expire_all()
    assert _outbox_pending(db_session) == outbox_after_first == 1


# ---------------------------------------------------------------------------
# C. two concurrent workers -> one dispatch
# ---------------------------------------------------------------------------

def test_concurrent_workers_single_dispatch(db_session, test_engine, monkeypatch):
    _seed_default_assignee(db_session, monkeypatch)
    _seed_overdue_plus_upcoming_lease(db_session)
    db_session.commit()

    Session = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def _run():
        db = Session()
        try:
            barrier.wait(timeout=20)
            run_scheduler_once(db, now=NOW)
        except BaseException as exc:  # noqa: BLE001 - surface in main thread
            errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=_run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    db_session.expire_all()
    assert _outbox_pending(db_session) == 1


# ---------------------------------------------------------------------------
# D. next day (still incomplete) -> exactly one NEW dispatch allowed
# ---------------------------------------------------------------------------

def test_next_day_allows_exactly_one_resend(db_session, monkeypatch):
    _seed_default_assignee(db_session, monkeypatch)
    _seed_overdue_plus_upcoming_lease(db_session)
    db_session.commit()
    run_scheduler_once(db_session, now=NOW)
    sender = _OkSender()
    process_notifications_once(db_session, sender, now=NOW)
    assert len(sender.sent) == 1

    next_day = NOW + timedelta(days=1)
    sender2 = _OkSender()
    run_scheduler_once(db_session, now=next_day)
    assert _outbox_pending(db_session) == 1  # one fresh reminder slot
    process_notifications_once(db_session, sender2, now=next_day)
    assert len(sender2.sent) == 1

    # later scans the SAME day -> no more
    sender3 = _OkSender()
    run_scheduler_once(db_session, now=next_day + timedelta(hours=3))
    process_notifications_once(db_session, sender3, now=next_day + timedelta(hours=3))
    assert len(sender3.sent) == 0


def test_daily_dedup_key_uses_philippines_local_date():
    """A UTC date flip must not cause two sends on one PH day: the boundary is
    Asia/Manila. 2026-08-17 15:59 UTC == 2026-08-17 23:59 PH; +2min UTC ==
    2026-08-18 00:01 PH (a different dedupe day)."""
    from datetime import timezone as tz
    d1 = datetime(2026, 8, 17, 15, 59, tzinfo=tz.utc)
    d2 = datetime(2026, 8, 17, 16, 1, tzinfo=tz.utc)
    assert philippines_local_date(d1) == "2026-08-17"
    assert philippines_local_date(d2) == "2026-08-18"


# ---------------------------------------------------------------------------
# E/F. Acknowledge / Completed -> reminders stop
# ---------------------------------------------------------------------------

def test_acknowledge_then_same_day_scans_no_more_dispatch(db_session, client, monkeypatch):
    _seed_default_assignee(db_session, monkeypatch)
    lease = _seed_overdue_plus_upcoming_lease(db_session)
    db_session.commit()
    run_scheduler_once(db_session, now=NOW)
    task = (
        db_session.query(OperationalTask)
        .filter(OperationalTask.task_type == OperationalTaskType.RENT_OVERDUE)
        .one()
    )

    from tests.conftest import make_user, ensure_default_org
    from app.models.membership import Membership, OrganizationRole, MembershipState
    user, key = make_user(db_session, "ack-admin", UserRole.admin)
    org = ensure_default_org(db_session)
    db_session.add(Membership(
        organization_id=org.id,
        user_id=user.id,
        role=OrganizationRole.OWNER,
        state=MembershipState.ACTIVE,
    ))
    db_session.commit()
    headers = {"Authorization": f"Bearer {key}"}
    resp = client.post(f"{API}/operations/tasks/{task.id}/acknowledge", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["status"] == "IN_PROGRESS"
    # idempotent repeat tap
    resp2 = client.post(f"{API}/operations/tasks/{task.id}/acknowledge", headers=headers)
    assert resp2.status_code == 200 and resp2.json()["detail"] == "Task already acknowledged"

    # notifier drops any remaining outbox row for the acknowledged task
    sender = _OkSender()
    result = process_notifications_once(db_session, sender, now=NOW)
    assert result["sent"] == 0 and len(sender.sent) == 0
    # further same-day scans stay silent (task IN_PROGRESS -> refresh no-ops)
    run_scheduler_once(db_session, now=NOW)
    assert _outbox_pending(db_session) == 0


def test_completed_task_cross_day_no_more_dispatch(db_session, monkeypatch):
    """F: the business item became PAID -> reconcile completes the RENT_OVERDUE
    task; cross-day scans never re-remind a resolved item."""
    _seed_default_assignee(db_session, monkeypatch)
    lease = _seed_overdue_plus_upcoming_lease(db_session, rent="25000.00")
    db_session.commit()
    run_scheduler_once(db_session, now=NOW)
    task = (
        db_session.query(OperationalTask)
        .filter(OperationalTask.task_type == OperationalTaskType.RENT_OVERDUE)
        .one()
    )
    # Pay every overdue period (confirmed incomes) -> reconcile completes the
    # task with reason "rent_paid".
    from app.services.operations.rent_math import lease_periods
    for month, _due in lease_periods(lease):
        db_session.add(Income(
            lease_id=lease.id, amount="25000.00", received_date=date(2026, 8, 10),
            payment_method="bank", status=IncomeStatus.confirmed, description=month,
        ))
    db_session.commit()

    run_scheduler_once(db_session, now=NOW)  # reconcile completes it; no re-create
    db_session.refresh(task)
    assert task.status == OperationalTaskStatus.COMPLETED
    assert task.completed_at is not None

    # cross-day scans: the item is resolved -> never reminded again
    for day in range(3):
        run_scheduler_once(db_session, now=NOW + timedelta(days=day + 1))
    assert (
        db_session.query(OperationalTask)
        .filter(OperationalTask.task_type == OperationalTaskType.RENT_OVERDUE,
                OperationalTask.status == OperationalTaskStatus.PENDING)
        .count()
    ) == 0
    sender = _OkSender()
    process_notifications_once(db_session, sender, now=NOW + timedelta(days=3))
    assert len(sender.sent) == 0
    assert _outbox_pending(db_session) == 0


# ---------------------------------------------------------------------------
# G. two different business objects -> each may dispatch once
# ---------------------------------------------------------------------------

def test_two_leases_each_dispatch_once(db_session, monkeypatch):
    _seed_default_assignee(db_session, monkeypatch)
    l1 = _seed_overdue_plus_upcoming_lease(db_session, rent="25000.00")
    # second lease, different unit number
    prop = seed_property(db_session, name="Bay Tower", address="2 EDSA", city="Pasay", total_units=2)
    unit = Unit(property_id=prop.id, unit_number="2208", floor="22", size_sqm="30.00",
                monthly_rent="55000.00", status=UnitStatus.occupied)
    db_session.add(unit)
    db_session.flush()
    lease2 = Lease(unit_id=unit.id, tenant_id=l1.tenant_id,
                   start_date=date(2025, 1, 1), end_date=date(2026, 12, 31),
                   monthly_rent="55000.00", deposit="110000.00",
                   status=LeaseStatus.active, due_day=20)
    db_session.add(lease2)
    db_session.commit()

    run_scheduler_once(db_session, now=NOW)
    run_scheduler_once(db_session, now=NOW)  # same-day repeat
    assert _outbox_pending(db_session) == 2  # one per business object
    sender = _OkSender()
    process_notifications_once(db_session, sender, now=NOW)
    assert len(sender.sent) == 2


# ---------------------------------------------------------------------------
# quick-rent truth source (§7) + expense read path (§4) + task context (§8)
# ---------------------------------------------------------------------------

def test_quick_rent_unpaid_periods_matches_overdue_count(db_session, monkeypatch):
    """Rent detail must show the SAME period count as the RENT_OVERDUE task
    generator (the Tasks board): quick-rent row unpaid_periods == len(overdue)."""
    _seed_default_assignee(db_session, monkeypatch)
    lease = _seed_overdue_plus_upcoming_lease(db_session, rent="25000.00")
    db_session.commit()
    # 2026-08-17: overdue periods = every month whose due date (the 20th) is
    # <= today = 2025-01 .. 2026-07 = 19 periods (no incomes -> all uncovered).
    data = build_quick_rent(db_session, now=NOW)
    row = data["overdue"][0]
    assert row["unpaid_periods"] == 19
    assert Decimal(str(row["amount"])) == Decimal("25000.00") * 19
    assert Decimal(str(row["monthly_rent"])) == Decimal("25000.00")


def test_expense_read_legacy_placeholder_category_no_500(client, admin_headers, db_session):
    """CONVERGENCE-003 §4.2: GET /expenses/{id} on a legacy `??` category row
    must return 200 (read-through), never ResponseValidationError 500."""
    from app.models.financial import Expense
    from tests.conftest import seed_property
    _p = seed_property(db_session)
    exp = Expense(
        expense_date=date(2026, 8, 15), category="??", amount="7000.00",
        payee="Repair Co", status=ExpenseStatus.approved, property_id=_p.id,
    )
    db_session.add(exp)
    db_session.commit()
    db_session.refresh(exp)
    resp = client.get(f"{API}/expenses/{exp.id}", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["category"] == "??"  # raw read-through; renderers clean


def test_quick_tasks_expense_row_has_business_context(db_session, monkeypatch):
    """CONVERGENCE-003 §8: a PAYMENT_PENDING task row carries expense_id /
    amount / purpose so the Tasks card can render
    ``💸 E{id} · unit · purpose · ₱amount · waiting Nd`` (never two identical
    ``待付款支出 · overdue 2d`` rows)."""
    _seed_default_assignee(db_session, monkeypatch)
    lease = _seed_overdue_plus_upcoming_lease(db_session, rent="25000.00")
    db_session.commit()
    from app.models.financial import Expense
    from app.models.property import Unit
    from app.models.user import User
    admin_user = db_session.query(User).filter(User.role == UserRole.admin).first()
    unit = db_session.get(Unit, lease.unit_id)
    exp = Expense(
        expense_date=date(2026, 8, 15), due_date=date(2026, 8, 15),
        category="Repair", amount="7000.00", payee="Repair Co",
        status=ExpenseStatus.approved, approved_at=NOW - timedelta(days=2),
        unit_id=lease.unit_id, payer_user_id=admin_user.id,
        property_id=unit.property_id if unit else None,
    )
    db_session.add(exp)
    db_session.commit()
    db_session.refresh(exp)
    run_scheduler_once(db_session, now=NOW)
    rows = build_quick_tasks(db_session, admin_user, now=NOW)
    payable = [r for r in rows if str(r.get("kind") or "") == "payable_expense"]
    assert payable and payable[0]["expense_id"] == exp.id
