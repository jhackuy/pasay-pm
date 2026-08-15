"""PASAY-V2-FOUNDATION-001 backend tests: V2 task core.

Covers: PATCH transition validation (IN_PROGRESS requires next_action +
next_check_at), conversation-driven POST /tasks (create + dedupe), the
deterministic Quick View + Digest endpoints (no LLM, no writes), and the
expense approve/reject auto-completion of the linked APPROVAL_PENDING task.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.models.financial import Expense, ExpenseStatus
from app.models.operations import (
    OperationalTask,
    OperationalTaskStatus,
    OperationalTaskType,
)
from app.models.property import Property, Unit, UnitStatus
from app.models.tenant import Tenant
from app.models.lease import Lease, LeaseStatus

API = "/api/v1"
NOW = datetime(2026, 8, 20, 9, 30, 0, tzinfo=timezone.utc)
FUTURE = (NOW + timedelta(days=1)).isoformat()


def _seed_lease(db):
    prop = Property(name="Sunset Tower", address="1 Roxas Blvd", city="Pasay", total_units=4)
    db.add(prop)
    db.flush()
    unit = Unit(property_id=prop.id, unit_number="1680", floor="16", size_sqm="32.50",
                monthly_rent="12000.00", status=UnitStatus.occupied)
    tenant = Tenant(full_name="Ana P.", phone="+639170000000")
    db.add_all([unit, tenant])
    db.flush()
    lease = Lease(unit_id=unit.id, tenant_id=tenant.id, start_date=date(2026, 1, 1),
                  end_date=date(2026, 12, 31), monthly_rent="12000.00", deposit="24000.00",
                  status=LeaseStatus.active, due_day=5)
    db.add(lease)
    db.flush()
    return lease, unit


def _make_task(db, *, task_type=OperationalTaskType.AC_MAINTENANCE,
               status=OperationalTaskStatus.PENDING, assigned_user_id=None,
               source_type="conversation", source_id=1, due_at=None, lease_id=None,
               dedupe_key=None, next_action=None, next_check_at=None):
    task = OperationalTask(
        task_type=task_type,
        title="1680 · Aircon repair",
        source_type=source_type,
        source_id=source_id,
        assigned_user_id=assigned_user_id,
        status=status,
        due_at=due_at or NOW,
        lease_id=lease_id,
        dedupe_key=dedupe_key,
        next_action=next_action,
        next_check_at=next_check_at,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


# ---------------------------------------------------------------------------
# PATCH /operations/tasks/{id}: V2 transition rules
# ---------------------------------------------------------------------------

def test_patch_in_progress_requires_next_action_and_next_check(client, db_session, admin_headers):
    task = _make_task(db_session, dedupe_key="v2-p1")
    # missing next_action -> 422
    resp = client.patch(
        f"{API}/operations/tasks/{task.id}",
        json={"status": "IN_PROGRESS", "next_check_at": FUTURE},
        headers=admin_headers,
    )
    assert resp.status_code == 422
    # missing next_check_at -> 422
    resp = client.patch(
        f"{API}/operations/tasks/{task.id}",
        json={"status": "IN_PROGRESS", "next_action": "Confirm repair tomorrow"},
        headers=admin_headers,
    )
    assert resp.status_code == 422
    # both present -> 200 IN_PROGRESS
    resp = client.patch(
        f"{API}/operations/tasks/{task.id}",
        json={
            "status": "IN_PROGRESS",
            "next_action": "Confirm repair tomorrow",
            "next_check_at": FUTURE,
            "context": "Technician scheduled",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["task"]
    assert data["status"] == "IN_PROGRESS"
    assert data["next_action"] == "Confirm repair tomorrow"
    assert data["next_check_at"] is not None
    assert data["context"] == "Technician scheduled"
    db_session.refresh(task)
    assert task.status == OperationalTaskStatus.IN_PROGRESS


def test_patch_completed_sets_completed_at_and_stops_reminders(client, db_session, admin_headers):
    task = _make_task(db_session, status=OperationalTaskStatus.IN_PROGRESS,
                      next_action="Confirm", next_check_at=FUTURE, dedupe_key="v2-p2")
    resp = client.patch(
        f"{API}/operations/tasks/{task.id}",
        json={"status": "COMPLETED"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["task"]
    assert data["status"] == "COMPLETED"
    assert data["completed_at"] is not None
    db_session.refresh(task)
    assert task.completed_by is not None


def test_patch_cancelled_is_terminal(client, db_session, admin_headers):
    task = _make_task(db_session, status=OperationalTaskStatus.CANCELLED, dedupe_key="v2-p3")
    resp = client.patch(
        f"{API}/operations/tasks/{task.id}",
        json={"status": "IN_PROGRESS", "next_action": "x", "next_check_at": FUTURE},
        headers=admin_headers,
    )
    assert resp.status_code == 409


def test_patch_agent_scoped(client, db_session, agent_headers, admin_headers):
    from app.models.user import User, UserRole
    admin = db_session.query(User).filter_by(role=UserRole.admin).first()
    task = _make_task(db_session, assigned_user_id=admin.id, dedupe_key="v2-p4")
    # agent cannot update a task not assigned to them
    resp = client.patch(
        f"{API}/operations/tasks/{task.id}",
        json={"next_action": "x"},
        headers=agent_headers,
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /operations/tasks: conversation-driven creation + dedupe
# ---------------------------------------------------------------------------

def test_create_task_from_conversation(client, db_session, admin_headers, lease_id):
    resp = client.post(
        f"{API}/operations/tasks",
        json={
            "task_type": "AC_MAINTENANCE",
            "title": "1680 · Aircon leaking",
            "property_id": None,
            "description": "Aircon leaking again",
            "context": "1680 aircon leaking; tenant reported",
            "source_event": "secretary: 1680 aircon leaking",
            "completion_condition": "Repair confirmed done",
            "dedupe_key": "conversation:1680-aircon-1",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["task"]
    assert data["status"] == "PENDING"
    assert data["task_type"] == "AC_MAINTENANCE"
    assert data["source_event"] == "secretary: 1680 aircon leaking"
    assert data["completion_condition"] == "Repair confirmed done"

    # same dedupe_key -> existing task, not a duplicate
    resp2 = client.post(
        f"{API}/operations/tasks",
        json={
            "task_type": "AC_MAINTENANCE",
            "title": "1680 · Aircon leaking again",
            "dedupe_key": "conversation:1680-aircon-1",
        },
        headers=admin_headers,
    )
    assert resp2.status_code == 201, resp2.text
    assert resp2.json()["task"]["id"] == data["id"]
    assert db_session.query(OperationalTask).count() == 1


def test_create_task_in_progress_requires_fields(client, db_session, admin_headers):
    resp = client.post(
        f"{API}/operations/tasks",
        json={
            "task_type": "AC_MAINTENANCE",
            "title": "bad in-progress",
            "status": "IN_PROGRESS",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Quick Views + Digest: deterministic, no LLM, read-only
# ---------------------------------------------------------------------------

def test_quick_tasks_deterministic(client, db_session, admin_headers):
    lease, unit = _seed_lease(db_session)
    _make_task(db_session, status=OperationalTaskStatus.PENDING, lease_id=lease.id,
               dedupe_key="v2-q1")
    _make_task(db_session, status=OperationalTaskStatus.IN_PROGRESS, lease_id=lease.id,
               next_action="Confirm", next_check_at=FUTURE, dedupe_key="v2-q2")
    resp = client.get(f"{API}/operations/quick/tasks", headers=admin_headers)
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 2
    codes = {r["property_code"] for r in rows}
    assert codes == {"1680"}
    assert {r["status"] for r in rows} == {"PENDING", "IN_PROGRESS"}


def test_quick_properties_anomaly_first(client, db_session, admin_headers):
    lease, unit = _seed_lease(db_session)
    resp = client.get(f"{API}/operations/quick/properties", headers=admin_headers)
    assert resp.status_code == 200
    rows = resp.json()
    assert rows and rows[0]["unit_code"] == "1680"
    assert rows[0]["status"] == "overdue_rent"  # due day 5, now Aug 20 -> overdue


def test_quick_rent_and_expense_shapes(client, db_session, admin_headers):
    _seed_lease(db_session)
    rent = client.get(f"{API}/operations/quick/rent", headers=admin_headers)
    assert rent.status_code == 200
    body = rent.json()
    assert "overdue" in body and "outstanding_total" in body
    assert body["overdue"] and body["overdue"][0]["unit"] == "1680"

    exp = client.get(f"{API}/operations/quick/expense", headers=admin_headers)
    assert exp.status_code == 200
    body = exp.json()
    assert {"month_total", "pending_approval_count", "pending_approval_amount",
            "unresolved_expense_tasks"} <= set(body)


def test_quick_expense_recent_records_include_pending_and_paid(
    client, db_session, admin_headers,
):
    """PASAY-V2-EXPENSE-LIST-003: 💸 Expense returns recent/current-month
    records across statuses (PENDING + PAID). A paid expense must remain
    visible in expense history."""
    _, unit = _seed_lease(db_session)
    today = date.today()
    db_session.add_all([
        Expense(
            expense_date=today, category="Repair / 维修", amount="6001.00",
            payee="Carpenter", unit_id=unit.id, status=ExpenseStatus.paid,
        ),
        Expense(
            expense_date=today, category="Water / 水费", amount="3500.00",
            payee="MWCI", unit_id=unit.id, status=ExpenseStatus.pending,
        ),
    ])
    db_session.commit()

    resp = client.get(f"{API}/operations/quick/expense", headers=admin_headers)
    assert resp.status_code == 200
    recent = resp.json()["recent_expenses"]
    assert len(recent) == 2
    statuses = {r["status"] for r in recent}
    assert statuses == {"paid", "pending"}
    for row in recent:
        # Case C: every row exposes unit/purpose/amount/date/status.
        assert row["unit"] == "1680"
        assert row["purpose"]
        assert row["amount"]
        assert row["expense_date"]
        assert row["status"]


def test_digest_structure(client, db_session, admin_headers):
    lease, _ = _seed_lease(db_session)
    _make_task(db_session, status=OperationalTaskStatus.PENDING, lease_id=lease.id,
               dedupe_key="v2-d1")
    _make_task(db_session, status=OperationalTaskStatus.IN_PROGRESS, lease_id=lease.id,
               next_action="Confirm", next_check_at=FUTURE, dedupe_key="v2-d2")
    resp = client.get(f"{API}/operations/digest", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["pending"]) == 1
    assert len(body["in_progress"]) == 1
    assert body["recently_completed"] == []


# ---------------------------------------------------------------------------
# expense approve/reject auto-completes the linked APPROVAL_PENDING task
# ---------------------------------------------------------------------------

def test_expense_approve_completes_linked_task(client, db_session, admin_headers, manager_headers):
    from app.models.user import User, UserRole
    admin = db_session.query(User).filter_by(role=UserRole.admin).first()
    expense = Expense(expense_date=date(2026, 8, 1), category="repair", amount="3500.00",
                      payee="Fix-It Co", status=ExpenseStatus.pending)
    db_session.add(expense)
    db_session.commit()
    db_session.refresh(expense)
    _make_task(db_session, task_type=OperationalTaskType.APPROVAL_PENDING,
               source_type="expense", source_id=expense.id,
               assigned_user_id=admin.id, dedupe_key="v2-e1")

    resp = client.post(f"{API}/expenses/{expense.id}/approve", headers=manager_headers)
    assert resp.status_code == 200, resp.text
    task = (
        db_session.query(OperationalTask)
        .filter(
            OperationalTask.source_type == "expense",
            OperationalTask.source_id == expense.id,
        )
        .one()
    )
    assert task.status == OperationalTaskStatus.COMPLETED
    assert task.completed_at is not None


def test_expense_reject_completes_linked_task(client, db_session, admin_headers):
    from app.models.user import User, UserRole
    admin = db_session.query(User).filter_by(role=UserRole.admin).first()
    expense = Expense(expense_date=date(2026, 8, 1), category="repair", amount="500.00",
                      payee="Fix-It Co", status=ExpenseStatus.pending)
    db_session.add(expense)
    db_session.commit()
    db_session.refresh(expense)
    _make_task(db_session, task_type=OperationalTaskType.APPROVAL_PENDING,
               source_type="expense", source_id=expense.id,
               assigned_user_id=admin.id, dedupe_key="v2-e2")

    resp = client.post(f"{API}/expenses/{expense.id}/reject", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    task = (
        db_session.query(OperationalTask)
        .filter(
            OperationalTask.source_type == "expense",
            OperationalTask.source_id == expense.id,
        )
        .one()
    )
    assert task.status == OperationalTaskStatus.COMPLETED
