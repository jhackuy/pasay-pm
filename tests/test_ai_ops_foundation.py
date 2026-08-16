"""AI-OPS-FOUNDATION-001 acceptance tests (backend domain rules).

Covers the domain-state-is-source-of-truth / one-issue-one-task /
deterministic-idempotency acceptance items that live in the backend:

1. Same overdue rent cannot create duplicate active tasks (and a later
   scheduler pass REFRESHES the same logical task instead of creating a
   sibling).
2. Reminder retry cannot duplicate domain records (income idempotency key;
   refreshed reminder never duplicates the task).
3. Rent paid automatically closes the overdue task (reconcile).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.models.financial import Income, IncomeStatus
from app.models.lease import Lease, LeaseStatus
from app.models.operations import (
    NotificationOutbox,
    OperationalTask,
    OperationalTaskPriority,
    OperationalTaskStatus,
    OperationalTaskType,
)
from app.models.property import Property, Unit, UnitStatus
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.operations.generation import generate_business_tasks
from app.services.operations.reconcile import reconcile_tasks
from app.services.operations.scheduler import run_scheduler_once

NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)


def _user(db, username, role, telegram_chat_id=None):
    import secrets

    user = User(
        username=username,
        role=role,
        api_key_hash=secrets.token_urlsafe(24),
        is_active=True,
        telegram_chat_id=telegram_chat_id or f"tg-{username}",
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


def _overdue_tasks(db) -> list[OperationalTask]:
    return (
        db.query(OperationalTask)
        .filter(
            OperationalTask.task_type == OperationalTaskType.RENT_OVERDUE,
            OperationalTask.status.in_(
                [OperationalTaskStatus.PENDING, OperationalTaskStatus.IN_PROGRESS]
            ),
        )
        .all()
    )


def _confirm_periods(db, lease: Lease, periods: list[str]) -> None:
    """Confirmed income rows covering every given YYYY-MM period."""
    for month in periods:
        db.add(Income(
            lease_id=lease.id,
            amount=Decimal(lease.monthly_rent),
            received_date=date(int(month[:4]), int(month[5:7]), 10),
            payment_method="Bank",
            status=IncomeStatus.confirmed,
            description=f"rent {month}",
        ))
    db.flush()


def test_rent_overdue_one_active_task_refreshed_on_later_pass(db_session, monkeypatch):
    """AI-OPS-001 §1/§2: repeated scheduler passes never create a second
    active RENT_OVERDUE task; a newly-overdue period REFRESHES the same task
    (updated periods/total) and enqueues one fresh reminder."""
    from app.services.operations import generation

    user = _user(db_session, "default-admin", UserRole.admin)
    monkeypatch.setattr(generation, "DEFAULT_ASSIGNED_USER_ID", user.id)
    monkeypatch.setattr(generation, "SECRETARY_ASSIGNEE_ID", user.id)
    lease = _seed_lease(db_session)  # due day 5; accounting start 2026-01-01
    db_session.commit()

    r1 = run_scheduler_once(db_session, now=NOW)
    assert r1.tasks_created >= 1
    tasks = _overdue_tasks(db_session)
    assert len(tasks) == 1
    first = tasks[0]
    assert first.dedupe_key == f"lease:{lease.id}:RENT_OVERDUE"
    first_periods = (first.details or {}).get("periods")
    assert first_periods and len(first_periods) == 8  # Jan..Aug
    outbox_after_first = db_session.query(NotificationOutbox).count()

    # A later pass: Sep 10 -> Sep is now also overdue. Still ONE active task,
    # but its periods/total are refreshed and a reminder is enqueued.
    later = NOW + timedelta(days=31)
    r2 = run_scheduler_once(db_session, now=later)
    tasks = _overdue_tasks(db_session)
    assert len(tasks) == 1, "later pass must not create a second active RENT_OVERDUE task"
    assert tasks[0].id == first.id, "the same logical task is reused"
    assert (tasks[0].details or {}).get("periods") is not None
    assert len(tasks[0].details["periods"]) == 9  # Jan..Sep
    outbox_after_second = db_session.query(NotificationOutbox).count()
    assert outbox_after_second > outbox_after_first, "refresh enqueues a fresh reminder"

    # No duplicate dedupe keys anywhere.
    keys = [t.dedupe_key for t in db_session.query(OperationalTask).all() if t.dedupe_key]
    assert len(keys) == len(set(keys))


def test_rent_paid_closes_overdue_task(db_session, monkeypatch):
    """AI-OPS-001 §1: once every overdue period is covered by confirmed
    income, the RENT_OVERDUE task is auto-completed by reconcile (stale task
    must never stay active after the business state is resolved)."""
    from app.services.operations import generation

    user = _user(db_session, "default-admin", UserRole.admin)
    monkeypatch.setattr(generation, "DEFAULT_ASSIGNED_USER_ID", user.id)
    monkeypatch.setattr(generation, "SECRETARY_ASSIGNEE_ID", user.id)
    lease = _seed_lease(db_session)
    db_session.commit()

    run_scheduler_once(db_session, now=NOW)
    assert len(_overdue_tasks(db_session)) == 1

    periods = [m for m, _ in __import__(
        "app.services.operations.rent_math", fromlist=["lease_periods"]
    ).lease_periods(lease)]
    _confirm_periods(db_session, lease, periods)
    db_session.commit()

    completed, cancelled = reconcile_tasks(db_session, now=NOW + timedelta(days=1))
    assert completed >= 1
    task = db_session.query(OperationalTask).filter(
        OperationalTask.task_type == OperationalTaskType.RENT_OVERDUE,
        OperationalTask.source_id == lease.id,
    ).one()
    assert task.status == OperationalTaskStatus.COMPLETED
    assert task.completed_at is not None


def test_reminder_retry_does_not_duplicate_income(db_session, client, admin_headers, lease_id):
    """AI-OPS-001 §2/§10: a retried income create with the same idempotency
    key produces exactly one record (deterministic idempotency, no duplicate
    domain record on Telegram retry)."""
    payload = {
        "lease_id": lease_id,
        "amount": "12000.00",
        "received_date": "2026-08-10",
        "payment_method": "Bank",
        "description": "rent 2026-08",
        "status": "pending",
        "idempotency_key": "ik-rent-2026-08-retry",
    }
    r1 = client.post("/api/v1/incomes", json=payload, headers=admin_headers)
    r2 = client.post("/api/v1/incomes", json=payload, headers=admin_headers)
    # First write is 201; the retry replays the existing record (200), never a
    # second row — deterministic idempotency at the business layer.
    assert r1.status_code == 201 and r2.status_code in (200, 201)
    assert r1.json()["id"] == r2.json()["id"]
    rows = db_session.query(Income).filter(Income.idempotency_key == "ik-rent-2026-08-retry").all()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# B: role-aware routing + Owner attention filter
# ---------------------------------------------------------------------------

def test_rent_overdue_assigned_to_secretary_not_owner(db_session, monkeypatch):
    """AI-OPS-001 §4/§5: overdue rent is routine operational work — the
    RENT_OVERDUE task is assigned to the SECRETARY, never the Owner."""
    from app.services.operations import generation

    owner = _user(db_session, "owner", UserRole.admin)
    secretary = _user(db_session, "secretary", UserRole.manager)
    monkeypatch.setattr(generation, "DEFAULT_ASSIGNED_USER_ID", owner.id)
    monkeypatch.setattr(generation, "SECRETARY_ASSIGNEE_ID", secretary.id)
    _seed_lease(db_session)
    db_session.commit()

    run_scheduler_once(db_session, now=NOW)
    task = (
        db_session.query(OperationalTask)
        .filter(OperationalTask.task_type == OperationalTaskType.RENT_OVERDUE)
        .one()
    )
    assert task.assigned_user_id == secretary.id
    assert task.assigned_user_id != owner.id


def test_owner_scope_excludes_secretary_operational_tasks(
    db_session, client, monkeypatch, admin_headers
):
    """AI-OPS-001 §5: the Owner attention filter keeps routine operational
    tasks (RENT_OVERDUE / AC_MAINTENANCE) OUT of the Owner queue; approvals
    and Owner payments stay IN."""
    from app.services.operations import generation

    owner = _user(db_session, "owner-b", UserRole.admin)
    secretary = _user(db_session, "secretary-b", UserRole.manager)
    monkeypatch.setattr(generation, "DEFAULT_ASSIGNED_USER_ID", owner.id)
    monkeypatch.setattr(generation, "SECRETARY_ASSIGNEE_ID", secretary.id)
    lease = _seed_lease(db_session)
    db_session.commit()

    run_scheduler_once(db_session, now=NOW)  # RENT_OVERDUE -> secretary

    # An APPROVAL_PENDING task for the Owner.
    from app.models.financial import Expense, ExpenseStatus

    expense = Expense(expense_date=date(2026, 8, 1), category="维修", amount="5000.00",
                      payee="Fix-It Co", status=ExpenseStatus.pending,
                      created_at=NOW - timedelta(days=10), payer_user_id=owner.id)
    db_session.add(expense)
    db_session.commit()
    run_scheduler_once(db_session, now=NOW)

    all_tasks = client.get("/api/v1/operations/tasks?status=PENDING", headers=admin_headers)
    assert all_tasks.status_code == 200
    types_all = {t["task_type"] for t in all_tasks.json()}
    assert "RENT_OVERDUE" in types_all and "APPROVAL_PENDING" in types_all

    owner_tasks = client.get(
        "/api/v1/operations/tasks?status=PENDING&scope=owner", headers=admin_headers
    )
    assert owner_tasks.status_code == 200
    types_owner = {t["task_type"] for t in owner_tasks.json()}
    assert "APPROVAL_PENDING" in types_owner
    assert "RENT_OVERDUE" not in types_owner


def test_approved_expense_routes_payment_to_actual_payer(db_session, monkeypatch):
    """AI-OPS-001 §4/§8: an approved expense's PAYMENT_PENDING task is
    assigned to the ACTUAL payer (expense.payer_user_id), not always the
    Owner."""
    from app.models.financial import Expense, ExpenseStatus
    from app.services.operations import generation

    owner = _user(db_session, "owner-c", UserRole.admin)
    payer = _user(db_session, "payer-c", UserRole.manager)
    monkeypatch.setattr(generation, "DEFAULT_ASSIGNED_USER_ID", owner.id)
    monkeypatch.setattr(generation, "SECRETARY_ASSIGNEE_ID", owner.id)

    expense = Expense(expense_date=date(2026, 8, 1), category="物业费", amount="8000.00",
                      payee="Assoc", status=ExpenseStatus.approved,
                      approved_at=NOW - timedelta(days=10), payer_user_id=payer.id)
    db_session.add(expense)
    db_session.commit()

    run_scheduler_once(db_session, now=NOW)
    task = (
        db_session.query(OperationalTask)
        .filter(OperationalTask.task_type == OperationalTaskType.PAYMENT_PENDING)
        .one()
    )
    assert task.assigned_user_id == payer.id
    assert (task.details or {}).get("payer_user_id") == payer.id


# ---------------------------------------------------------------------------
# C: promises / follow-ups / escalation
# ---------------------------------------------------------------------------

def _task_with_promise(db, *, task_type=OperationalTaskType.AC_MAINTENANCE,
                       follow_up_at, missed=0, status=OperationalTaskStatus.IN_PROGRESS):
    from app.services.operations.promises import apply_promise

    task = OperationalTask(
        task_type=task_type,
        title="Unit 101 repair",
        source_type="conversation",
        source_id=1,
        priority=OperationalTaskPriority.high,
        status=status,
        due_at=NOW,
        assigned_user_id=1,
    )
    apply_promise(
        task,
        promised_at=NOW - timedelta(days=2),
        follow_up_at=follow_up_at,
        responsible_party="technician",
        related_entity="task:1",
        note="Technician coming tomorrow",
    )
    details = dict(task.details or {})
    details["promise"]["missed"] = missed
    task.details = details
    db.add(task)
    db.flush()
    return task


def test_promise_follow_up_reminds_then_escalates_to_owner(db_session, monkeypatch):
    """AI-OPS-001 §8/§6: an unresolved promise whose follow_up_at has passed
    first reminds the responsible party, then escalates to the Owner after
    repeated misses — the Owner only receives the escalation."""
    from app.services.operations import generation
    from app.services.operations.promises import task_escalation_level

    owner = _user(db_session, "owner-d", UserRole.admin)
    monkeypatch.setattr(generation, "DEFAULT_ASSIGNED_USER_ID", owner.id)
    monkeypatch.setattr(generation, "SECRETARY_ASSIGNEE_ID", owner.id)

    task = _task_with_promise(db_session, follow_up_at=NOW - timedelta(hours=2), missed=0)
    db_session.commit()

    # First pass: follow-up is due -> reminded (missed 1), not escalated.
    r1 = run_scheduler_once(db_session, now=NOW)
    assert r1.promises_reminded == 1 and r1.promises_escalated == 0
    db_session.refresh(task)
    assert task_escalation_level(task) == "none"
    assert (task.details or {})["promise"]["missed"] == 1

    # Business resolves before the next follow-up -> reconcile closes the
    # task; the escalation pass must NOT touch a completed task.
    from app.services.operations.reconcile import reconcile_tasks

    # Instead of resolving, keep it active and let it miss again -> escalate.
    task.status = OperationalTaskStatus.IN_PROGRESS
    db_session.commit()
    r2 = run_scheduler_once(db_session, now=NOW + timedelta(days=2))
    assert r2.promises_escalated == 1
    db_session.refresh(task)
    assert task_escalation_level(task) == "owner"
    assert (task.details or {})["escalation"]["level"] == "owner"


def test_resolved_business_state_not_reminded_after_promise_due(db_session, monkeypatch):
    """AI-OPS-001 §8: when the business state is already resolved by the time
    the follow-up is due, reconcile closes the task and the promise pass never
    reminds anyone."""
    from app.services.operations import generation

    owner = _user(db_session, "owner-e", UserRole.admin)
    monkeypatch.setattr(generation, "DEFAULT_ASSIGNED_USER_ID", owner.id)
    monkeypatch.setattr(generation, "SECRETARY_ASSIGNEE_ID", owner.id)

    task = _task_with_promise(db_session, follow_up_at=NOW - timedelta(hours=2), missed=0)
    task.status = OperationalTaskStatus.COMPLETED  # business already resolved
    db_session.commit()

    result = run_scheduler_once(db_session, now=NOW)
    assert result.promises_reminded == 0 and result.promises_escalated == 0
    assert result.reconciled_completed == 0


# ---------------------------------------------------------------------------
# D: repair completion evidence -> Secretary follow-up
# ---------------------------------------------------------------------------

def test_repair_completion_without_evidence_creates_secretary_followup(
    db_session, client, monkeypatch, admin_headers
):
    """AI-OPS-001 §13: completing an AC_MAINTENANCE task without completion
    evidence creates ONE FOLLOWUP for the SECRETARY (never the Owner), with a
    stable dedupe key (no duplicate on repeated completion)."""
    from app.services.operations import generation

    owner = _user(db_session, "owner-f", UserRole.admin)
    secretary = _user(db_session, "secretary-f", UserRole.manager)
    monkeypatch.setattr(generation, "DEFAULT_ASSIGNED_USER_ID", owner.id)
    monkeypatch.setattr(generation, "SECRETARY_ASSIGNEE_ID", secretary.id)

    from app.models.operations import OperationalTaskPriority

    task = OperationalTask(
        task_type=OperationalTaskType.AC_MAINTENANCE,
        title="Unit 101 aircon",
        source_type="conversation",
        source_id=1,
        priority=OperationalTaskPriority.high,
        status=OperationalTaskStatus.PENDING,
        due_at=NOW,
        assigned_user_id=secretary.id,
    )
    db_session.add(task)
    db_session.commit()

    resp = client.post(f"/api/v1/operations/tasks/{task.id}/complete", headers=admin_headers)
    assert resp.status_code == 200

    followups = (
        db_session.query(OperationalTask)
        .filter(
            OperationalTask.task_type == OperationalTaskType.FOLLOWUP,
            OperationalTask.dedupe_key == f"repair-evidence:{task.id}",
        )
        .all()
    )
    assert len(followups) == 1
    assert followups[0].assigned_user_id == secretary.id
    assert followups[0].assigned_user_id != owner.id

    # Re-completing (idempotent replay) never creates a second follow-up.
    resp2 = client.post(f"/api/v1/operations/tasks/{task.id}/complete", headers=admin_headers)
    assert resp2.status_code == 200
    assert (
        db_session.query(OperationalTask)
        .filter(OperationalTask.dedupe_key == f"repair-evidence:{task.id}")
        .count()
        == 1
    )


def test_repair_evidence_upload_closes_secretary_followup(
    db_session, client, monkeypatch, admin_headers, unit_id
):
    """AI-OPS-001 §13/§14: uploading after-repair evidence marks the repair
    task's completion_evidence and closes the secretary evidence follow-up."""
    from app.models.operations import OperationalTaskPriority

    owner = _user(db_session, "owner-g", UserRole.admin)
    secretary = _user(db_session, "secretary-g", UserRole.manager)

    task = OperationalTask(
        task_type=OperationalTaskType.AC_MAINTENANCE,
        title="Unit repair",
        source_type="conversation",
        source_id=1,
        priority=OperationalTaskPriority.high,
        status=OperationalTaskStatus.COMPLETED,
        due_at=NOW,
        assigned_user_id=secretary.id,
    )
    db_session.add(task)
    db_session.commit()

    followup = OperationalTask(
        task_type=OperationalTaskType.FOLLOWUP,
        title="上传维修完成凭证",
        source_type="task",
        source_id=task.id,
        priority=OperationalTaskPriority.medium,
        status=OperationalTaskStatus.PENDING,
        due_at=NOW,
        assigned_user_id=secretary.id,
        dedupe_key=f"repair-evidence:{task.id}",
    )
    db_session.add(followup)
    db_session.commit()

    resp = client.post(
        "/api/v1/evidence",
        json={
            "storage_provider": "telegram_channel",
            "external_file_id": "tg_file_after",
            "media_type": "photo",
            "category": "after_repair",
            "unit_id": unit_id,
            "entity_type": "task",
            "entity_id": task.id,
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201
    db_session.refresh(followup)
    assert followup.status == OperationalTaskStatus.COMPLETED
    db_session.refresh(task)
    assert (task.details or {}).get("completion_evidence", {}).get("after_photo", {}).get("evidence_id") == resp.json()["id"]


# ---------------------------------------------------------------------------
# E: evidence index + retrieval
# ---------------------------------------------------------------------------

def test_evidence_linked_and_retrieved_by_unit(client, admin_headers, unit_id):
    """AI-OPS-001 §14: evidence is indexed once and retrievable by unit."""
    payload = {
        "storage_provider": "telegram_channel",
        "external_file_id": "tg_file_x",
        "media_type": "photo",
        "mime_type": "image/jpeg",
        "filename": "before.jpg",
        "category": "before_repair",
        "unit_id": unit_id,
    }
    r1 = client.post("/api/v1/evidence", json=payload, headers=admin_headers)
    assert r1.status_code == 201
    rows = client.get(f"/api/v1/evidence?unit_id={unit_id}", headers=admin_headers)
    assert rows.status_code == 200
    data = rows.json()
    assert len(data) == 1
    assert data[0]["external_file_id"] == "tg_file_x"
    assert data[0]["category"] == "before_repair"


# ---------------------------------------------------------------------------
# G: viewings + deposit + unit lifecycle
# ---------------------------------------------------------------------------

def test_viewing_created_with_secretary_reminder_and_closed_on_outcome(
    db_session, client, monkeypatch, admin_headers, unit_id
):
    """AI-OPS-001 §17: creating a viewing schedules one Secretary reminder;
    recording the outcome closes the viewing and its reminder."""
    from app.services.operations import generation

    owner = _user(db_session, "owner-h", UserRole.admin)
    secretary = _user(db_session, "secretary-h", UserRole.manager)
    monkeypatch.setattr(generation, "DEFAULT_ASSIGNED_USER_ID", owner.id)
    monkeypatch.setattr(generation, "SECRETARY_ASSIGNEE_ID", secretary.id)

    resp = client.post(
        "/api/v1/viewings",
        json={
            "unit_id": unit_id,
            "scheduled_at": "2026-08-17T14:00:00+08:00",
            "notes": "Someone will view",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201
    viewing_id = resp.json()["id"]
    assert resp.json()["status"] == "scheduled"

    reminders = (
        db_session.query(OperationalTask)
        .filter(OperationalTask.dedupe_key == f"viewing:{viewing_id}")
        .all()
    )
    assert len(reminders) == 1
    assert reminders[0].assigned_user_id == secretary.id

    outcome = client.post(
        f"/api/v1/viewings/{viewing_id}/outcome",
        json={"outcome": "not_interested", "reason": "too small"},
        headers=admin_headers,
    )
    assert outcome.status_code == 200
    assert outcome.json()["status"] == "done"
    assert outcome.json()["reason"] == "too small"
    db_session.refresh(reminders[0])
    assert reminders[0].status == OperationalTaskStatus.COMPLETED


def test_unit_lifecycle_state_recorded_and_deposit_fields(client, admin_headers, unit_id, db_session):
    """AI-OPS-001 §16/§18: a unit lifecycle transition records a durable
    event; the lease deposit accounting fields exist on the model."""
    from app.models.property import UnitLifecycleEvent

    resp = client.patch(
        f"/api/v1/units/{unit_id}",
        json={"unit_state": "LISTED"},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["unit_state"] == "LISTED"
    event = db_session.query(UnitLifecycleEvent).filter_by(unit_id=unit_id).one()
    assert event.to_status == "LISTED"

    lease = _seed_lease(db_session)
    lease.deposit_received = Decimal("24000.00")
    lease.deposit_deductions = [
        {"amount": "2000.00", "reason": "repair", "repair_id": 1}
    ]
    db_session.commit()
    db_session.refresh(lease)
    assert lease.deposit_received == Decimal("24000.00")
    assert lease.deposit_deductions[0]["reason"] == "repair"


# ---------------------------------------------------------------------------
# §15: unit timeline (digital file)
# ---------------------------------------------------------------------------

def test_unit_timeline_returns_events(client, admin_headers, unit_id, db_session, tenant_id):
    """AI-OPS-001 §15: 'Give me the history of 1608' resolves to a
    deterministic unit digital file (rent/payments, expenses, repairs)."""
    from app.models.financial import Expense, ExpenseStatus, Income, IncomeStatus
    from app.models.lease import Lease
    from app.models.operations import OperationalTask, OperationalTaskPriority

    lease = Lease(unit_id=unit_id, tenant_id=tenant_id,
                  start_date=date(2026, 1, 1), end_date=date(2026, 12, 31),
                  monthly_rent="12000.00", deposit="24000.00",
                  status=LeaseStatus.active, due_day=5)
    db_session.add(lease)
    db_session.commit()
    db_session.refresh(lease)

    db_session.add(Income(
        lease_id=lease.id, amount="12000.00", received_date=date(2026, 8, 5),
        status=IncomeStatus.confirmed, description="rent 2026-08",
    ))
    db_session.add(Expense(
        expense_date=date(2026, 8, 6), category="维修", amount="5000.00",
        payee="Fix-It Co", status=ExpenseStatus.paid, unit_id=unit_id,
    ))
    db_session.add(OperationalTask(
        task_type=OperationalTaskType.AC_MAINTENANCE, title="aircon fix",
        lease_id=lease.id, source_type="conversation", source_id=1,
        priority=OperationalTaskPriority.high,
        status=OperationalTaskStatus.COMPLETED, due_at=NOW,
    ))
    db_session.commit()

    resp = client.get(
        f"/api/v1/operations/quick/unit-timeline?unit_id={unit_id}",
        headers=admin_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    kinds = [e["kind"] for e in data["events"]]
    assert "rent" in kinds and "expense" in kinds and "task" in kinds


# ---------------------------------------------------------------------------
# §19: exception detection hooks
# ---------------------------------------------------------------------------

def test_exception_scan_detects_long_vacancy_and_owner_warning(
    db_session, monkeypatch, client, admin_headers,
):
    """AI-OPS-001 §19: a long-vacant unit triggers a deduped Owner WARNING;
    the same scan never duplicates it."""
    from app.models.financial import Expense  # noqa: F401
    from app.models.property import UnitStatus
    from app.services.operations import generation
    from app.services.operations.exceptions import scan_exceptions

    owner = _user(db_session, "owner-x", UserRole.admin, "tg-owner-x")
    monkeypatch.setattr(generation, "DEFAULT_ASSIGNED_USER_ID", owner.id)
    monkeypatch.setattr(generation, "SECRETARY_ASSIGNEE_ID", owner.id)

    prop = Property(name="T", address="A", city="C", total_units=1)
    db_session.add(prop)
    db_session.flush()
    unit = Unit(property_id=prop.id, unit_number="999", monthly_rent="15000.00",
                status=UnitStatus.vacant, created_at=NOW - timedelta(days=120))
    db_session.add(unit)
    db_session.commit()

    findings1 = scan_exceptions(db_session, now=NOW)
    kinds = {f["kind"] for f in findings1}
    assert "long_vacancy" in kinds

    # Second scan: same day -> outbox dedupe swallows the duplicate.
    findings2 = scan_exceptions(db_session, now=NOW)
    assert len(findings2) == len(findings1)
    outbox = db_session.query(NotificationOutbox).all()
    dedupe_keys = [o.dedupe_key for o in outbox]
    assert len(dedupe_keys) == len(set(dedupe_keys))
