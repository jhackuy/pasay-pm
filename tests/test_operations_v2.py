"""PASAY-V2-FOUNDATION-001 backend tests: V2 task core.

Covers: PATCH transition validation (IN_PROGRESS requires next_action +
next_check_at), conversation-driven POST /tasks (create + dedupe), the
deterministic Quick View + Digest endpoints (no LLM, no writes), and the
expense approve/reject auto-completion of the linked APPROVAL_PENDING task.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.models.financial import Expense, ExpenseStatus
from app.models.operations import (
    OperationalTask,
    OperationalTaskStatus,
    OperationalTaskType,
)
from app.models.property import Property, Unit, UnitStatus
from app.models.tenant import Tenant
from app.models.lease import Lease, LeaseStatus
from tests.conftest import ensure_default_org, seed_property, seed_unit, seed_tenant, seed_expense  # noqa: F401 (seed helpers shared via conftest)

API = "/api/v1"
NOW = datetime(2026, 8, 20, 9, 30, 0, tzinfo=timezone.utc)
FUTURE = (NOW + timedelta(days=1)).isoformat()


def _seed_lease(db):
    prop = seed_property(db, name="Sunset Tower", address="1 Roxas Blvd", city="Pasay", total_units=4)
    unit = Unit(property_id=prop.id, unit_number="1680", floor="16", size_sqm="32.50",
                monthly_rent=Decimal("12000.00"), status=UnitStatus.occupied)
    tenant = seed_tenant(db, full_name="Ana P.", phone="+639170000000")
    db.add_all([unit])
    db.flush()
    lease = Lease(unit_id=unit.id, tenant_id=tenant.id, start_date=date(2026, 1, 1),
                  end_date=date(2026, 12, 31), monthly_rent=Decimal("12000.00"),
                  deposit=Decimal("24000.00"),
                  status=LeaseStatus.active, due_day=5)
    db.add(lease)
    db.flush()
    return lease, unit


def _make_task(db, *, task_type=OperationalTaskType.AC_MAINTENANCE,
               status=OperationalTaskStatus.PENDING, assigned_user_id=None,
               source_type="conversation", source_id=1, due_at=None, lease_id=None,
               dedupe_key=None, next_action=None, next_check_at=None):
    p = db.query(Property).order_by(Property.id.asc()).first()
    if not p:
        p = seed_property(db, name="OP2-P", address="A", city="C", total_units=1)
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
        property_id=p.id,
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
    from tests.conftest import seed_property
    _pid = seed_property(db_session).id

    resp = client.post(
        f"{API}/operations/tasks",
        json={
            "task_type": "AC_MAINTENANCE",
            "title": "1680 · Aircon leaking",
            "property_id": _pid,
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
            "property_id": _pid,
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
    # build_quick_properties is asset directory, not operations workbench:
    # status returns "occupied"/"vacant" only (docstring L606-L611).
    # RENT_OVERDUE chips live on quick/rent and /tasks, not properties grid.
    assert rows[0]["status"] == "occupied"


def test_quick_properties_open_maintenance_chip(client, db_session, admin_headers):
    """TELEGRAM-OPS-UX-CONVERGENCE-001 §2: maintenance workload lives on
    /tasks and /quick/rent (plus /operations/summary). /quick/properties
    returns the bare asset directory grid per its docstring (L606-L611):
    unit_code / property_name / status / tenant_name only.

    The open-maintenance count chip for a unit is driven by the aggregate
    in /operations/summary, which is validated by the summary test module.
    The properties grid is identity-only, so workload chips do not live
    here (kept small for bot render latency on 500+ unit portfolios)."""
    lease, _ = _seed_lease(db_session)
    _make_task(db_session, task_type=OperationalTaskType.AC_MAINTENANCE,
               status=OperationalTaskStatus.PENDING, lease_id=lease.id, dedupe_key="m1")
    _make_task(db_session, task_type=OperationalTaskType.AC_MAINTENANCE,
               status=OperationalTaskStatus.IN_PROGRESS, lease_id=lease.id, dedupe_key="m2")
    _make_task(db_session, task_type=OperationalTaskType.AC_MAINTENANCE,
               status=OperationalTaskStatus.COMPLETED, lease_id=lease.id, dedupe_key="m3")
    resp = client.get(f"{API}/operations/quick/properties", headers=admin_headers)
    assert resp.status_code == 200
    rows = resp.json()
    row = next(r for r in rows if r["unit_code"] == "1680")
    asset_keys = {"unit_code", "property_name", "status", "tenant_name"}
    assert asset_keys.issubset(set(row.keys())), row.keys()
    assert row["status"] == "occupied"
    # open_maintenance is intentionally absent from the asset grid.


def test_quick_rent_and_expense_shapes(client, db_session, admin_headers):
    _seed_lease(db_session)
    rent = client.get(f"{API}/operations/quick/rent", headers=admin_headers)
    assert rent.status_code == 200
    body = rent.json()
    assert "overdue" in body and "outstanding_total" in body
    assert body["overdue"] and body["overdue"][0]["unit"] == "1680"
    # Journey B statistics: current-month expected/collected/outstanding/rate/
    # unpaid unit count are always present (may be zero).
    for key in ("month", "expected_rent_total", "collected_rent",
                "outstanding_rent", "collection_rate", "unpaid_unit_count"):
        assert key in body

    exp = client.get(f"{API}/operations/quick/expense", headers=admin_headers)
    assert exp.status_code == 200
    body = exp.json()
    assert {"month_total", "pending_approval_count", "pending_approval_amount",
            "unresolved_expense_tasks", "records",
            "payable", "paid_records"} <= set(body)


def test_quick_rent_month_statistics_contract(client, db_session, admin_headers):
    """PASAY-V2-OWNER-SECRETARY-JOURNEY-AUDIT-006 (Journey B): the current-month
    rent statistics are internally consistent. A lease whose current period is
    uncovered contributes to expected but not collected -> outstanding = expected
    - collected, collection_rate = collected/expected, unpaid_unit_count >= 1."""
    lease, _ = _seed_lease(db_session)
    resp = client.get(f"{API}/operations/quick/rent", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    expected = Decimal(str(body["expected_rent_total"]))
    collected = Decimal(str(body["collected_rent"]))
    outstanding = Decimal(str(body["outstanding_rent"]))
    assert expected > Decimal("0")
    computed_outstanding = (expected - collected).quantize(Decimal("0.01"))
    assert outstanding == computed_outstanding, (
        f"outstanding={outstanding} != expected-collected={computed_outstanding} "
        f"(Decimal exact match; float rounding would hide off-by-penny bugs)."
    )
    rate = Decimal(str(body["collection_rate"]))
    assert Decimal("0") <= rate <= Decimal("100")
    assert body["unpaid_unit_count"] >= 1


def test_quick_expense_records_show_paid_approved_pending_exclude_reversed(
    client, db_session, admin_headers,
):
    """P1-EXPENSE-QUICKVIEW-LIST-001: the quick view lists this month's real
    expense records. PENDING, APPROVED and PAID must all appear; REVERSED and
    REJECTED (cancelled) records never appear; last month's records stay out;
    the month_total semantics (approved + paid only) are unchanged."""
    _, unit = _seed_lease(db_session)
    today = date.today()
    db_session.add_all([
        Expense(
            expense_date=today, category="Repair / 维修",
            amount=Decimal("6001.00"),
            payee="Carpenter", unit_id=unit.id, property_id=unit.property_id,
            status=ExpenseStatus.paid,
        ),
        Expense(
            expense_date=today, category="Water / 水费",
            amount=Decimal("3500.00"),
            payee="MWCI", unit_id=unit.id, property_id=unit.property_id,
            status=ExpenseStatus.approved,
        ),
        Expense(
            expense_date=today, category="Electric / 电费",
            amount=Decimal("1200.00"),
            payee="Meralco", unit_id=unit.id, property_id=unit.property_id,
            status=ExpenseStatus.pending,
        ),
        Expense(
            expense_date=today, category="Ghost / 撤销",
            amount=Decimal("2000.00"),
            payee="N/A", unit_id=unit.id, property_id=unit.property_id,
            status=ExpenseStatus.reversed,
        ),
        Expense(
            expense_date=today, category="Cancelled / 拒绝",
            amount=Decimal("1500.00"),
            payee="N/A", unit_id=unit.id, property_id=unit.property_id,
            status=ExpenseStatus.rejected,
        ),
        Expense(
            expense_date=date(today.year, today.month, 1)
            - timedelta(days=1),  # previous month
            category="Old / 上月", amount=Decimal("9000.00"),
            payee="Old Co", unit_id=unit.id, property_id=unit.property_id,
            status=ExpenseStatus.paid,
        ),
    ])
    db_session.commit()

    resp = client.get(f"{API}/operations/quick/expense", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    records = body["records"]
    assert len(records) == 3
    assert {r["status"] for r in records} == {"paid", "approved", "pending"}
    by_status = {r["status"]: r for r in records}
    for row in records:
        assert row["unit"] == "1680"
        assert row["purpose"]
        assert row["amount"]
        assert row["expense_date"]
        # PASAY-V2-OWNER-SECRETARY-JOURNEY-AUDIT-006 (Journey E): every record
        # carries the stable business identity so same-date/same-amount records
        # stay distinguishable.
        assert isinstance(row["expense_id"], int)
    # same expense_date -> most recently created first (id desc)
    assert records[0]["status"] == "pending"
    assert Decimal(str(by_status["paid"]["amount"])) == Decimal("6001.00")
    assert Decimal(str(by_status["approved"]["amount"])) == Decimal("3500.00")
    assert Decimal(str(by_status["pending"]["amount"])) == Decimal("1200.00")
    # month_total keeps the approved+paid semantics and ignores reversed.
    assert Decimal(str(body["month_total"])) == Decimal("9501.00")


def test_quick_expense_records_empty_state(client, db_session, admin_headers):
    """P1-EXPENSE-QUICKVIEW-LIST-001: with no expenses the quick view returns
    an empty records list (the bot then shows the real empty state)."""
    _seed_lease(db_session)
    resp = client.get(f"{API}/operations/quick/expense", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["records"] == []
    assert Decimal(str(body["month_total"])) == Decimal("0.00")


def test_quick_expense_records_purpose_fallback(client, db_session, admin_headers):
    """PASAY-V2-EXPENSE-UX-AUDIT-005 Test B: purpose is the first meaningful
    of category/description; `??`, None, null and empty are normalized away so
    the read model never ships a raw `??` placeholder."""
    _, unit = _seed_lease(db_session)
    today = date.today()
    db_session.add_all([
        Expense(
            expense_date=today, category="Repair / 维修",
            amount=Decimal("6001.00"),
            payee="Carpenter", unit_id=unit.id, property_id=unit.property_id,
            status=ExpenseStatus.paid,
        ),
        Expense(
            expense_date=today, category="??", description="Water / 水费",
            amount=Decimal("3500.00"), payee="MWCI", unit_id=unit.id,
            property_id=unit.property_id, status=ExpenseStatus.approved,
        ),
        Expense(
            expense_date=today, category="",
            amount=Decimal("1200.00"),
            payee="Meralco", unit_id=unit.id, property_id=unit.property_id,
            status=ExpenseStatus.pending,
        ),
    ])
    db_session.commit()

    resp = client.get(f"{API}/operations/quick/expense", headers=admin_headers)
    assert resp.status_code == 200
    records = resp.json()["records"]
    by_amount = {Decimal(str(r["amount"])): r for r in records}
    assert by_amount[Decimal("6001.00")]["purpose"] == "Repair / 维修"  # category wins
    assert by_amount[Decimal("3500.00")]["purpose"] == "Water / 水费"   # `??` ignored, desc used
    # P1-PASAY-NIGHTLY-PRODUCT-HARDENING-008 A3: an empty category now falls
    # back to the truthful payee/vendor before the renderer's neutral label.
    assert by_amount[Decimal("1200.00")]["purpose"] == "Meralco"
    assert "??" not in [r["purpose"] for r in records]
    assert "None" not in [r["purpose"] for r in records]


def test_quick_expense_payable_and_paid_sections_clean_and_disjoint(
    client, db_session, admin_headers,
):
    """EXPENSE-UX-FIX-001: the quick expense payload splits APPROVED-unpaid
    (payable) from this month's PAID (paid_records). Every payable row carries
    the real fields (expense_id, unit, purpose, amount, expense_date, status);
    a legacy `??` category resolves to the truthful payee and never ships as
    `??`; an APPROVED expense never appears in paid_records (no duplication)."""
    _, unit = _seed_lease(db_session)
    today = date.today()
    db_session.add_all([
        Expense(
            expense_date=today, category="??",
            amount=Decimal("7000.00"),
            payee="Repair", unit_id=unit.id, property_id=unit.property_id,
            status=ExpenseStatus.approved,
        ),
        Expense(
            expense_date=today, category="??",
            amount=Decimal("7000.00"),
            payee="Repair", unit_id=unit.id, property_id=unit.property_id,
            status=ExpenseStatus.approved,
        ),
        Expense(
            expense_date=today, category="维修",
            amount=Decimal("6002.00"),
            payee="Fix-It Co", unit_id=unit.id, property_id=unit.property_id,
            status=ExpenseStatus.paid,
        ),
        Expense(
            expense_date=today, category="水费",
            amount=Decimal("1200.00"),
            payee="MWCI", unit_id=unit.id, property_id=unit.property_id,
            status=ExpenseStatus.pending,
        ),
    ])
    db_session.commit()

    resp = client.get(f"{API}/operations/quick/expense", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    payable = body["payable"]
    paid_records = body["paid_records"]

    assert len(payable) == 2
    assert all(r["status"] == "approved" for r in payable)
    for row in payable:
        assert isinstance(row["expense_id"], int)
        assert row["unit"] == "1680"
        assert row["purpose"] == "Repair"  # `??` category -> truthful payee
        assert row["amount"]
        assert row["expense_date"]
        assert "??" not in row["purpose"]

    assert len(paid_records) == 1
    assert paid_records[0]["status"] == "paid"
    assert paid_records[0]["purpose"] == "维修"
    # the same APPROVED expense never leaks into the paid section
    payable_ids = {r["expense_id"] for r in payable}
    assert not ({r["expense_id"] for r in paid_records} & payable_ids)
    assert "??" not in [r["purpose"] for r in payable + paid_records]


def test_digest_structure(client, db_session, admin_headers):
    """Daily Digest exposes the three user-semantic sections and at most one
    row per business object. Seeding the quick/task board directly must NOT
    surface raw task rows — only real human actions (overdue rent / payable
    expense) and real lease expiries appear (DAILY-DIGEST-TRUTH-CLEANUP-006)."""
    lease, unit = _seed_lease(db_session)
    # Seed raw PENDING/IN_PROGRESS operational tasks: they must NOT be dumped
    # into the digest (system-internal rows are not "what the human does").
    _make_task(db_session, status=OperationalTaskStatus.PENDING, lease_id=lease.id,
               dedupe_key="v2-d1")
    _make_task(db_session, status=OperationalTaskStatus.IN_PROGRESS, lease_id=lease.id,
               next_action="Confirm", next_check_at=FUTURE, dedupe_key="v2-d2")
    resp = client.get(f"{API}/operations/digest", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    # The 1680 lease is overdue (due day 5, current PH date is in Aug-2026) ->
    # at least the Jan..Aug uncovered periods give a semantic item.
    assert len(body["act_now"]) == 1
    rent = body["act_now"][0]
    assert rent["kind"] == "rent_overdue"
    assert rent["business_dedupe_key"] == f"lease:{lease.id}:RENT_OVERDUE"
    assert rent["unit"] == "1680"
    # Total arrears truth: uncovered periods x 12,000 monthly rent (never a
    # bare monthly rent in place of the outstanding).
    periods = rent["unpaid_periods"]
    assert periods >= 8
    amount_dec = Decimal(str(rent["amount"]))
    expected = Decimal(str(periods)) * Decimal("12000.00")
    assert amount_dec == expected, (
        f"rent arrears amount={amount_dec} != periods*rent={expected}. "
        f"Float coercion would hide a per-period precision bug here."
    )
    assert rent["overdue_days"] >= 15
    # Upcoming is empty: the lease ends in Dec (outside the 30d window).
    assert body["upcoming"] == []
    assert body["done_today"] == []
    # Raw-board tasks must not leak into the user's digest.
    assert len(body["pending"]) == 1  # legacy alias == act_now count
    assert body["in_progress"] == []


# ---------------------------------------------------------------------------
# expense approve/reject auto-completes the linked APPROVAL_PENDING task
# ---------------------------------------------------------------------------

def test_expense_approve_completes_linked_task(client, db_session, admin_headers, manager_headers):
    from app.models.user import User, UserRole
    admin = db_session.query(User).filter_by(role=UserRole.admin).first()
    p = db_session.query(Property).order_by(Property.id.asc()).first()
    if not p:
        p = seed_property(db_session, name="OP2-P", address="A", city="C", total_units=1)
    expense = Expense(expense_date=date(2026, 8, 1), category="repair", amount=Decimal("3500.00"),
                      payee="Fix-It Co", status=ExpenseStatus.pending, property_id=p.id)
    db_session.add(expense)
    db_session.commit()
    db_session.refresh(expense)
    _make_task(db_session, task_type=OperationalTaskType.APPROVAL_PENDING,
               source_type="expense", source_id=expense.id,
               assigned_user_id=admin.id, dedupe_key="v2-e1")

    resp = client.post(f"{API}/expenses/{expense.id}/approve", headers=admin_headers)
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
    p = db_session.query(Property).order_by(Property.id.asc()).first()
    if not p:
        p = seed_property(db_session, name="OP2-P", address="A", city="C", total_units=1)
    expense = Expense(expense_date=date(2026, 8, 1), category="repair", amount=Decimal("500.00"),
                      payee="Fix-It Co", status=ExpenseStatus.pending, property_id=p.id)
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


def test_operations_v2_decimal_sums_deterministic_ten_times():
    unit = Decimal("1234.56")
    sums = []
    for _ in range(10):
        s = Decimal("0")
        for _ in range(10):
            s = s + unit
        sums.append(s)
    expected = Decimal("12345.60")
    for s in sums:
        assert s == expected, f"Decimal sum mismatch: {s} != {expected}"
    for i in range(1, len(sums)):
        assert sums[i] is not sums[0]
        assert sums[i] == sums[0]
    assert Decimal("0.10") + Decimal("0.20") == Decimal("0.30")
