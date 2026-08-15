"""PASAY-V2-EXPENSE-PAYABLE-TASK-006 backend tests.

Covers the canonical ``PENDING -> APPROVED -> PAID`` rule at the data layer:
- every APPROVED (unpaid) Expense is an Owner (admin) quick-task row with a
  stable business identity (expense_id) and distinguishable unit/purpose;
- a PAID expense never appears as a payable task;
- the possible-duplicate matcher uses MULTIPLE strong fields (unit identity,
  amount, purpose/category, relevant date window) and NEVER matches on amount
  alone; it is advisory and returns both existing + current identity.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from app.models.financial import Expense, ExpenseStatus
from app.models.property import Property, Unit, UnitStatus
from app.models.user import UserRole
from app.services.operations import quick as quick_svc

API = "/api/v1"
TODAY = date(2026, 8, 15)
NOW = datetime(2026, 8, 15, 9, 0, 0, tzinfo=timezone.utc)


def _make_user(db, username, role):
    from tests.conftest import make_user
    user, _key = make_user(db, username, role)
    db.refresh(user)
    return user


def _seed_unit(db):
    prop = Property(name="DEV Modern", address="8 Roxas Blvd", city="Pasay", total_units=2)
    db.add(prop)
    db.flush()
    u1680 = Unit(property_id=prop.id, unit_number="1680", floor="16", size_sqm="40.00",
                 monthly_rent="55000.00", status=UnitStatus.occupied)
    u1608 = Unit(property_id=prop.id, unit_number="1608", floor="16", size_sqm="35.00",
                 monthly_rent="45000.00", status=UnitStatus.occupied)
    db.add_all([u1680, u1608])
    db.flush()
    return u1680, u1608


def _add_expense(db, *, unit_id, category, amount, status, expense_date=TODAY,
                 exp_id=None):
    exp = Expense(
        expense_date=expense_date, category=category, amount=amount,
        payee="Fix-It Co", unit_id=unit_id, status=status,
    )
    if exp_id is not None:
        exp.id = exp_id
    db.add(exp)
    db.flush()
    return exp


def test_quick_tasks_include_approved_not_paid_for_admin(db_session):
    u1680, _u1608 = _seed_unit(db_session)
    admin = _make_user(db_session, "admin_pay", UserRole.admin)
    _add_expense(db_session, unit_id=u1680.id, category="维修", amount="7000.00",
                 status=ExpenseStatus.approved)      # payable
    _add_expense(db_session, unit_id=u1680.id, category="维修", amount="7000.00",
                 status=ExpenseStatus.paid)          # NOT payable
    rows = quick_svc.build_quick_tasks(db_session, admin, now=NOW)
    payable = [r for r in rows if r.get("kind") == "payable_expense"]
    assert len(payable) == 1
    row = payable[0]
    # stable business identity visible + distinguishable fields.
    assert row["expense_id"]  # #E{id} source
    assert row["status"] == "approved"
    assert row["unit"] == "1680"
    assert row["purpose"] == "维修"
    assert int(row["amount"]) == 7000


def test_quick_tasks_not_payable_for_manager(db_session):
    u1680, _ = _seed_unit(db_session)
    manager = _make_user(db_session, "mgr_pay", UserRole.manager)
    _add_expense(db_session, unit_id=u1680.id, category="维修", amount="7000.00",
                 status=ExpenseStatus.approved)
    rows = quick_svc.build_quick_tasks(db_session, manager, now=NOW)
    assert all(r.get("kind") != "payable_expense" for r in rows)


def test_quick_tasks_exclude_rejected_and_reversed_for_admin(db_session):
    u1680, _ = _seed_unit(db_session)
    admin = _make_user(db_session, "admin_pay2", UserRole.admin)
    _add_expense(db_session, unit_id=u1680.id, category="维修", amount="7000.00",
                 status=ExpenseStatus.rejected)
    _add_expense(db_session, unit_id=u1680.id, category="维修", amount="5000.00",
                 status=ExpenseStatus.reversed)
    rows = quick_svc.build_quick_tasks(db_session, admin, now=NOW)
    assert all(r.get("kind") != "payable_expense" for r in rows)


def test_duplicate_matcher_matches_strong_fields_not_amount_alone(db_session):
    u1680, u1608 = _seed_unit(db_session)
    # Current APPROVED expense to be paid (#E1031-like).
    current = _add_expense(db_session, unit_id=u1680.id, category="维修",
                           amount="7000.00", status=ExpenseStatus.approved)
    # Highly similar PAID (#E1027-like): same unit, amount, purpose, day.
    similar = _add_expense(db_session, unit_id=u1680.id, category="维修",
                           amount="7000.00", status=ExpenseStatus.paid)
    # Different purpose + unit, same amount -> must NOT be a match.
    _add_expense(db_session, unit_id=u1608.id, category="水费",
                 amount="7000.00", status=ExpenseStatus.paid)
    matches = quick_svc.find_similar_paid_expenses(db_session, current, now=NOW)
    assert len(matches) == 1
    assert matches[0]["expense_id"] == similar.id
    # The single-matching amount-only record (different unit/purpose) is absent.
    assert all(m["expense_id"] == similar.id for m in matches)


def test_duplicate_matcher_requires_same_day_window(db_session):
    u1680, _ = _seed_unit(db_session)
    current = _add_expense(db_session, unit_id=u1680.id, category="维修",
                           amount="7000.00", status=ExpenseStatus.approved,
                           expense_date=TODAY)
    # Same strong fields but far outside the relevant date window.
    _add_expense(db_session, unit_id=u1680.id, category="维修",
                 amount="7000.00", status=ExpenseStatus.paid,
                 expense_date=TODAY.replace(year=2025))
    matches = quick_svc.find_similar_paid_expenses(db_session, current, now=NOW)
    assert matches == []


def test_duplicate_endpoint_advisory_and_ids(client, db_session, admin_headers):
    u1680, _ = _seed_unit(db_session)
    current = _add_expense(db_session, unit_id=u1680.id, category="维修",
                           amount="7000.00", status=ExpenseStatus.approved)
    similar = _add_expense(db_session, unit_id=u1680.id, category="维修",
                           amount="7000.00", status=ExpenseStatus.paid)
    resp = client.get(
        f"{API}/operations/quick/expense-duplicates?expense_id={current.id}",
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert [r["expense_id"] for r in rows] == [similar.id]
    # The current expense row is left untouched (advisory only).
    cur = client.get(f"{API}/expenses/{current.id}", headers=admin_headers)
    assert cur.json()["status"] == "approved"
