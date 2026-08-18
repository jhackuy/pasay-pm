"""DAILY-DIGEST-TRUTH-CLEANUP-006 backend tests.

Covers the Daily Tasks Digest contract changes:
- three user-semantic sections (act_now / upcoming / done_today)
- one row per business object (business_dedupe_key) at most
- SYSTEM completions (scheduler / supersede / reconcile / generator
  replacement / duplicate cleanup) NEVER enter "done_today"; only real HUMAN
  completions do
- overdue rent uses the TRUE total-arrears truth (never a bare monthly rent)
- an overdue lease rent + its generated follow-up collapse into ONE user action
- near-term lease expiry lands in 🟡 upcoming, not 🔴 act_now
- payable expenses read as an explicit PAY action
- deterministic ordering + hard per-section caps with overflows.

Tests call ``build_digest`` directly with a controlled ``now`` so the rent
period math is deterministic (the endpoint itself calls the same function with
the real clock; the direct-call tests are representative and clock-free).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.models.financial import Expense, ExpenseStatus, Income, IncomeStatus
from app.models.lease import Lease, LeaseStatus
from app.models.operations import (
    OperationalTask,
    OperationalTaskStatus,
    OperationalTaskType,
)
from app.models.property import Property, Unit, UnitStatus
from app.models.tenant import Tenant
from app.services.operations.quick import build_digest

NOW = datetime(2026, 8, 20, 9, 30, 0, tzinfo=timezone.utc)


def _seed_property_unit_tenant(db, unit_number="1680"):
    prop = Property(name="Sunset Tower", address="1 Roxas Blvd", city="Pasay", total_units=4)
    db.add(prop)
    db.flush()
    unit = Unit(property_id=prop.id, unit_number=unit_number, floor="16",
                size_sqm="32.50", monthly_rent="12000.00", status=UnitStatus.occupied)
    tenant = Tenant(full_name="Ana P.", phone="+639170000000")
    db.add_all([unit, tenant])
    db.flush()
    return unit, tenant


def _seed_lease(db, *, unit, tenant, start, end, monthly="25000.00", due_day=1):
    lease = Lease(unit_id=unit.id, tenant_id=tenant.id, start_date=start, end_date=end,
                  monthly_rent=monthly, deposit="50000.00",
                  status=LeaseStatus.active, due_day=due_day)
    db.add(lease)
    db.flush()
    return lease


def _seed_user(db):
    """A fake HUMAN actor principal id for `completed_by`. `completed_by` is a
    plain BigInteger column (non-null == a human did it); the value does not
    need a real row."""
    return 424242


def _completed_task(db, *, lease=None, task_type=OperationalTaskType.FOLLOWUP,
                    completed_by, completed_at, details=None, dedupe_key=None):
    task = OperationalTask(
        task_type=task_type, title="Follow up", source_type="lease",
        source_id=lease.id if lease else 1, lease_id=lease.id if lease else None,
        status=OperationalTaskStatus.COMPLETED, due_at=NOW,
        completed_by=completed_by, completed_at=completed_at,
        dedupe_key=dedupe_key, details=details,
    )
    db.add(task)
    db.flush()
    return task


# ---------------------------------------------------------------------------
# 1) one business_dedupe_key with many history rows -> at most ONE digest line
# ---------------------------------------------------------------------------

def test_same_business_key_20_rows_so_digest_shows_one(db_session):
    unit, tenant = _seed_property_unit_tenant(db_session)
    lease = _seed_lease(db_session, unit=unit, tenant=tenant,
                        start=date(2026, 6, 1), end=date(2026, 12, 31),
                        monthly="25000.00", due_day=1)
    user = _seed_user(db_session)
    # 20 COMPLETED rows for the SAME business fact all "done today" by a human.
    for i in range(20):
        _completed_task(
            db_session, lease=lease, completed_by=user,
            completed_at=NOW, dedupe_key=f"lease:{lease.id}:RENT_OVERDUE",
        )
    db_session.commit()

    digest = build_digest(db_session, user, now=NOW)
    # A human already completed today's follow-up for this lease, so that same
    # logical action is no longer active in today's red queue.
    assert digest["act_now"] == []
    # Despite 20 history rows for the same business key, done_today = 1.
    assert len(digest["done_today"]) == 1
    assert digest["done_today"][0]["business_dedupe_key"] \
        == f"lease:{lease.id}:RENT_OVERDUE"


# ---------------------------------------------------------------------------
# 2) SYSTEM completions (superseded / auto-complete / scheduler) never enter done_today
# ---------------------------------------------------------------------------

def test_superseded_by_rent_overdue_hidden_from_done_today(db_session):
    unit, tenant = _seed_property_unit_tenant(db_session)
    lease = _seed_lease(db_session, unit=unit, tenant=tenant,
                        start=date(2026, 6, 1), end=date(2026, 12, 31),
                        monthly="25000.00", due_day=1)
    user = _seed_user(db_session)
    # A supersede-completed RENT_DUE task: completed_by is NULL (system).
    _completed_task(
        db_session, lease=lease, task_type=OperationalTaskType.RENT_DUE,
        completed_by=None, completed_at=NOW, dedupe_key=f"lease:{lease.id}:RENT_DUE:2026-08",
    )
    # A human-completed follow-up for the same lease -> eligible.
    _completed_task(
        db_session, lease=lease, completed_by=user, completed_at=NOW,
        dedupe_key=f"lease:{lease.id}:RENT_OVERDUE",
    )
    db_session.commit()
    digest = build_digest(db_session, user, now=NOW)
    done_kinds = {r["business_dedupe_key"] for r in digest["done_today"]}
    assert f"lease:{lease.id}:RENT_DUE:2026-08" not in done_kinds
    assert f"lease:{lease.id}:RENT_OVERDUE" in done_kinds


def test_system_actor_completed_hidden_from_done_today(db_session):
    unit, tenant = _seed_property_unit_tenant(db_session)
    lease = _seed_lease(db_session, unit=unit, tenant=tenant,
                        start=date(2026, 6, 1), end=date(2026, 12, 31),
                        monthly="25000.00", due_day=1)
    user = _seed_user(db_session)
    # auto_transition (reconcile) leaves completed_by NULL.
    _completed_task(db_session, lease=lease, completed_by=None, completed_at=NOW,
                    dedupe_key="auto:" + str(lease.id))
    db_session.commit()
    digest = build_digest(db_session, user, now=NOW)
    assert digest["done_today"] == []


def test_scheduler_completed_hidden_from_done_today(db_session):
    unit, tenant = _seed_property_unit_tenant(db_session)
    lease = _seed_lease(db_session, unit=unit, tenant=tenant,
                        start=date(2026, 6, 1), end=date(2026, 12, 31),
                        monthly="25000.00", due_day=1)
    user = _seed_user(db_session)
    _completed_task(db_session, lease=lease, completed_by=None, completed_at=NOW,
                    dedupe_key="scheduler-gen:" + str(lease.id))
    db_session.commit()
    digest = build_digest(db_session, user, now=NOW)
    assert digest["done_today"] == []


def test_lot_system_duplicates_hidden_from_done_today(db_session):
    """Mass SYSTEM-completed duplicate history for DEV-BAY-2208 must never show
    as 20+ 'Recently completed' entries (the observed live bug)."""
    unit, tenant = _seed_property_unit_tenant(db_session)
    lease = _seed_lease(db_session, unit=unit, tenant=tenant,
                        start=date(2026, 6, 1), end=date(2026, 12, 31),
                        monthly="25000.00", due_day=1)
    user = _seed_user(db_session)
    for i in range(20):
        _completed_task(db_session, lease=lease, completed_by=None, completed_at=NOW,
                        dedupe_key=f"lease:{lease.id}:RENT_OVERDUE")
    db_session.commit()
    digest = build_digest(db_session, user, now=NOW)
    assert digest["done_today"] == []


# ---------------------------------------------------------------------------
# HUMAN completions are visible
# ---------------------------------------------------------------------------

def test_human_secretary_completed_followup_visible(db_session):
    unit, tenant = _seed_property_unit_tenant(db_session)
    lease = _seed_lease(db_session, unit=unit, tenant=tenant,
                        start=date(2026, 6, 1), end=date(2026, 12, 31),
                        monthly="25000.00", due_day=1)
    user = _seed_user(db_session)
    _completed_task(db_session, lease=lease, completed_by=user, completed_at=NOW,
                    dedupe_key=f"lease:{lease.id}:RENT_OVERDUE")
    db_session.commit()
    digest = build_digest(db_session, user, now=NOW)
    assert len(digest["done_today"]) == 1
    assert digest["done_today"][0]["kind"] == "rent_followup"


def test_followed_up_today_leases_absent_from_act_now(db_session):
    """A rent follow-up completed today removes that same logical action from
    today's active red queue while keeping the completion visible."""
    user = _seed_user(db_session)
    leases: list[Lease] = []
    for unit_number in ("7789", "9950"):
        unit, tenant = _seed_property_unit_tenant(db_session, unit_number=unit_number)
        lease = _seed_lease(
            db_session,
            unit=unit,
            tenant=tenant,
            start=date(2026, 6, 1),
            end=date(2026, 12, 31),
            monthly="25000.00",
            due_day=1,
        )
        leases.append(lease)
        _completed_task(
            db_session,
            lease=lease,
            completed_by=user,
            completed_at=NOW,
            dedupe_key=f"lease:{lease.id}:RENT_OVERDUE",
        )
    db_session.commit()

    digest = build_digest(db_session, user, now=NOW)

    act_units = {row["unit"] for row in digest["act_now"] if row["kind"] == "rent_overdue"}
    done_units = {row["unit"] for row in digest["done_today"] if row["kind"] == "rent_followup"}
    assert "7789" not in act_units
    assert "9950" not in act_units
    assert {"7789", "9950"}.issubset(done_units)


def test_human_owner_payment_completed_visible(db_session):
    unit, tenant = _seed_property_unit_tenant(db_session)
    _seed_lease(db_session, unit=unit, tenant=tenant,
                start=date(2026, 6, 1), end=date(2026, 12, 31),
                monthly="25000.00", due_day=1)
    user = _seed_user(db_session)
    expense = Expense(expense_date=date(2026, 8, 1), category="repair",
                      amount="7000.00", payee="Repair Co", status=ExpenseStatus.approved,
                      approved_at=NOW)
    db_session.add(expense)
    db_session.flush()
    _completed_task(
        db_session, task_type=OperationalTaskType.PAYMENT_PENDING,
        completed_by=user, completed_at=NOW,
        dedupe_key=f"expense:{expense.id}:PAYMENT_PENDING",
        details={"expense_id": expense.id, "amount": "7000.00"},
    )
    db_session.commit()
    digest = build_digest(db_session, user, now=NOW)
    done = digest["done_today"]
    assert len(done) == 1
    assert done[0]["kind"] == "expense_paid"
    assert done[0]["expense_id"] == expense.id


# ---------------------------------------------------------------------------
# overdue rent + generated follow-up -> single user action (no double row)
# ---------------------------------------------------------------------------

def test_overdue_rent_and_followup_collapse_to_one_action(db_session):
    unit, tenant = _seed_property_unit_tenant(db_session)
    lease = _seed_lease(db_session, unit=unit, tenant=tenant,
                        start=date(2026, 6, 1), end=date(2026, 12, 31),
                        monthly="25000.00", due_day=1)
    user = _seed_user(db_session)
    # A FOLLOWUP task exists for the same lease (in PENDING) as well as the
    # overdue rent fact -> the digest shows ONE red user action, not two.
    for tt, dk in ((OperationalTaskType.RENT_OVERDUE, f"lease:{lease.id}:RENT_OVERDUE"),
                   (OperationalTaskType.FOLLOWUP, f"followup:lease:{lease.id}:FOLLOWUP")):
        db_session.add(OperationalTask(
            task_type=tt, title="Follow up", source_type="lease", source_id=lease.id,
            lease_id=lease.id, status=OperationalTaskStatus.PENDING, due_at=NOW,
            dedupe_key=dk, details={"total_outstanding": "75000.00", "periods": ["2026-08", "2026-07", "2026-06"]},
        ))
    db_session.commit()
    digest = build_digest(db_session, user, now=NOW)
    red = digest["act_now"]
    # Both PENDING task rows exist, but act_now is computed from LEASE truth,
    # so the lease appears once (no separate FOLLOWUP red line).
    assert len([r for r in red if r["kind"] == "rent_overdue"]) == 1
    assert len([r for r in red if r["kind"] in ("followup", "rent_due")]) == 0


# ---------------------------------------------------------------------------
# rent outstanding uses the true arrears truth (never a bare monthly rent)
# ---------------------------------------------------------------------------

def test_1680_outstanding_is_true_total_arrears(db_session):
    unit, tenant = _seed_property_unit_tenant(db_session)
    lease = _seed_lease(db_session, unit=unit, tenant=tenant,
                        start=date(2026, 6, 1), end=date(2026, 12, 31),
                        monthly="25000.00", due_day=1)
    user = _seed_user(db_session)
    db_session.commit()
    digest = build_digest(db_session, user, now=NOW)
    red = [r for r in digest["act_now"] if r["kind"] == "rent_overdue"]
    assert len(red) == 1
    item = red[0]
    # Only 3 periods overdue under the lease (Jun/Aug due day 1 <= Aug 20).
    assert item["unpaid_periods"] == 3
    # True outstanding = 3 periods x 25,000 = 75,000 �?never the bare monthly.
    assert float(item["amount"]) == 75000.0


# ---------------------------------------------------------------------------
# upcoming lease -> 🟡 not 🔴
# ---------------------------------------------------------------------------

def test_lease_expiring_soon_is_upcoming_not_act_now(db_session):
    unit, tenant = _seed_property_unit_tenant(db_session)
    # Lease ending in 18 days -> near-term expiry, rent paid in full (no rent
    # income seeded means it IS overdue too; use a fully-covered short lease).
    lease = _seed_lease(db_session, unit=unit, tenant=tenant,
                        start=date(2026, 1, 1), end=date(2026, 9, 7),
                        monthly="12000.00", due_day=25)
    # Mark every period covered so nothing is overdue (only expiry is relevant).
    from app.services.operations.quick import _lease_periods
    for month, due in _lease_periods(lease):
        db_session.add(Income(
            lease_id=lease.id, amount=lease.monthly_rent, received_date=due,
            payment_method="Bank", description=month, status=IncomeStatus.confirmed,
        ))
    user = _seed_user(db_session)
    db_session.commit()
    digest = build_digest(db_session, user, now=NOW)
    assert digest["act_now"] == []
    assert len(digest["upcoming"]) == 1
    assert digest["upcoming"][0]["days_to_expiry"] == 18


# ---------------------------------------------------------------------------
# expense E7 renders an explicit PAY action (backend carries purpose/amount)
# ---------------------------------------------------------------------------

def test_payable_expense_carries_pay_action_fields(db_session):
    unit, tenant = _seed_property_unit_tenant(db_session)
    _seed_lease(db_session, unit=unit, tenant=tenant,
                start=date(2026, 6, 1), end=date(2026, 12, 31),
                monthly="12000.00", due_day=25)
    user = _seed_user(db_session)
    expense = Expense(expense_date=date(2026, 8, 15), category="repair",
                      amount="7000.00", payee="Repair Co", unit_id=unit.id,
                      status=ExpenseStatus.approved, approved_at=NOW)
    db_session.add(expense)
    db_session.commit()
    digest = build_digest(db_session, user, now=NOW)
    pay = [r for r in digest["act_now"] if r["kind"] == "payable_expense"]
    assert len(pay) == 1
    assert pay[0]["expense_id"] == expense.id
    assert float(pay[0]["amount"]) == 7000.0
    assert pay[0].get("purpose")  # the actionable purpose, not a bare category


# ---------------------------------------------------------------------------
# deterministic ordering: most severe overdue first, then payable expenses
# ---------------------------------------------------------------------------

def test_act_now_sorted_by_severity_then_ties(db_session):
    unit_a, tenant_a = _seed_property_unit_tenant(db_session, unit_number="1680")
    lease_a = _seed_lease(db_session, unit=unit_a, tenant=tenant_a,
                          start=date(2026, 1, 1), end=date(2026, 12, 31),
                          monthly="25000.00", due_day=1)
    unit_b, tenant_b = _seed_property_unit_tenant(db_session, unit_number="2208")
    lease_b = _seed_lease(db_session, unit=unit_b, tenant=tenant_b,
                          start=date(2026, 6, 1), end=date(2026, 12, 31),
                          monthly="25000.00", due_day=1)
    user = _seed_user(db_session)
    db_session.commit()
    digest = build_digest(db_session, user, now=NOW)
    red = digest["act_now"]
    # lease_a has MORE overdue periods (longer history) -> it must sort first.
    assert red[0]["business_dedupe_key"] == f"lease:{lease_a.id}:RENT_OVERDUE"


# ---------------------------------------------------------------------------
# hard caps: act_now max 8, upcoming max 5, done_today max 3 + overflows
# ---------------------------------------------------------------------------

def test_act_now_cap_hidden_overflow(db_session):
    user = _seed_user(db_session)
    for n in range(10):
        unit, tenant = _seed_property_unit_tenant(db_session, unit_number=f"{1000 + n}")
        _seed_lease(db_session, unit=unit, tenant=tenant,
                    start=date(2026, 6, 1), end=date(2026, 12, 31),
                    monthly="25000.00", due_day=1)
    db_session.commit()
    digest = build_digest(db_session, user, now=NOW)
    assert len(digest["act_now"]) == 8
    assert digest["hidden"]["act_now"] == 2
    assert digest["counts"]["act_now"] == 10


def test_upcoming_cap_hidden_overflow(db_session):
    user = _seed_user(db_session)
    for n in range(7):
        unit, tenant = _seed_property_unit_tenant(db_session, unit_number=f"{2000 + n}")
        _seed_lease(db_session, unit=unit, tenant=tenant,
                    start=date(2026, 1, 1), end=date(2026, 9, int(1 + n)),
                    monthly="12000.00", due_day=28)
        # cover all periods so only expiry matters
        from app.services.operations.quick import _lease_periods
        lease = db_session.query(Lease).filter_by(unit_id=unit.id).one()
        for month, due in _lease_periods(lease):
            db_session.add(Income(
                lease_id=lease.id, amount=lease.monthly_rent, received_date=due,
                payment_method="Bank", description=month, status=IncomeStatus.confirmed,
            ))
    db_session.commit()
    digest = build_digest(db_session, user, now=NOW)
    assert len(digest["upcoming"]) == 5
    assert digest["hidden"]["upcoming"] == 2


def test_done_today_cap_hidden_overflow(db_session):
    unit, tenant = _seed_property_unit_tenant(db_session)
    lease = _seed_lease(db_session, unit=unit, tenant=tenant,
                        start=date(2026, 6, 1), end=date(2026, 12, 31),
                        monthly="25000.00", due_day=1)
    user = _seed_user(db_session)
    for i in range(5):
        _completed_task(db_session, lease=lease, completed_by=user,
                        completed_at=NOW,
                        dedupe_key=f"lease:{lease.id}:#{i}" if i else f"lease:{lease.id}:RENT_OVERDUE")
    db_session.commit()
    digest = build_digest(db_session, user, now=NOW)
    assert len(digest["done_today"]) == 3
    assert digest["hidden"]["done_today"] == 2
