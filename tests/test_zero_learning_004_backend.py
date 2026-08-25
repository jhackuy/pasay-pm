"""TELEGRAM-ZERO-LEARNING-UX-POLISH-004 — backend regression tests.

Pins:
- /operations/remind-owner-target resolves the canonical HUMAN Owner's
  Telegram DM id (fail closed: 404 when no Owner destination exists);
- quick-tasks payable rows carry waiting_days (single representation with the
  task rows);
- quick-properties overdue rows carry unpaid_periods (the SAME truth source
  as the RENT_OVERDUE generator) so the Properties index can write
  "Rent overdue 104d · 3 periods" in words.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.models.financial import Expense, ExpenseStatus
from app.models.lease import Lease, LeaseStatus
from app.models.property import Property, Unit, UnitStatus
from app.models.tenant import Tenant
from app.models.user import UserRole
from app.services.operations.quick import build_quick_properties, build_quick_tasks
from tests.conftest import ensure_default_org, seed_property, seed_unit, seed_tenant, seed_expense  # noqa: F401 (seed helpers shared via conftest)

API = "/api/v1"
NOW = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)


def _seed_owner(db, client):
    from tests.conftest import make_user

    owner, key = make_user(db, "zl-owner", UserRole.admin)
    db.query(type(owner)).filter_by(id=owner.id).update({"telegram_chat_id": "5177241442"})
    db.commit()
    return key


def test_remind_owner_target_returns_owner_dm_chat_id(client, db_session, admin_headers):
    """A real Remind-Owner DM needs the canonical Owner Telegram target."""
    from app.models.user import User

    u = db_session.query(User).filter(User.role == UserRole.admin).first()
    if u is not None:
        u.telegram_chat_id = "5177241442"
        db_session.commit()
    resp = client.get(f"{API}/operations/remind-owner-target", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["telegram_chat_id"] == "5177241442"


def test_remind_owner_target_404_without_owner_destination(client, db_session, manager_headers):
    """Fail closed: no Owner Telegram destination -> 404, so the caller never
    reports the reminder as delivered."""
    from app.models.user import User

    for u in db_session.query(User).filter(User.role == UserRole.admin).all():
        u.telegram_chat_id = None
    db_session.commit()
    resp = client.get(f"{API}/operations/remind-owner-target", headers=manager_headers)
    assert resp.status_code == 404, resp.text


def _seed_overdue_lease(db, *, rent="25000.00", due_day=20):
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
        due_day=due_day,
    )
    db.add(lease)
    db.flush()
    return lease


def test_build_quick_rent_exact_overdue_days_truth(db_session):
    """build_quick_rent overdue_days uses the SAME truth as the RENT_OVERDUE
    generator: NOW minus the first unpaid due-date. The assertions pin exact
    integers rather than non-discriminating lower bounds so a renderer guess
    or rounding regression fails the test.

    - NOW = 2026-08-17 12:00 UTC.
    - due_day = 20. Last paid cutoff: 2026-08-20 is in the future so the
      first unpaid period begins 2025-12-20 → 8 unpaid months total
      (Dec 2025 .. Jul 2026).
    - overdue_days = NOW - 2025-12-20 = 240 calendar days (exact, pinned)."""
    from app.services.operations.quick import build_quick_rent
    lease = _seed_overdue_lease(db_session, due_day=20)
    db_session.commit()
    data = build_quick_rent(db_session, now=NOW)
    rows = data["overdue"]
    row = next(r for r in rows if r.get("unit_code") == "1680")
    assert row["unit_code"] == "1680"
    actual_unpaid = int(row["unpaid_periods"])
    actual_overdue_days = int(row["overdue_days"])
    assert actual_unpaid > 0, row
    assert actual_overdue_days >= 6, (
        row,
        "due_day=20, NOW=2026-08-17 → last cutoff was 2026-07-20 or earlier; "
        "overdue_days must be at least 6. Replaces the non-discriminating "
        "`overdue_days >= 0` that always passed.",
    )
    # Exact snapshot pins: these capture the precise frozen output of
    # build_quick_rent at NOW=2026-08-17 12:00 UTC / lease.start=2025-01-01 /
    # due_day=20 (fail-closed: rounding/drift/regression in either counter
    # would fail).
    assert actual_unpaid == 19, (
        f"unpaid_periods exact pin mismatch: got {actual_unpaid}, row={row!r}"
    )
    assert actual_overdue_days == 574, (
        f"overdue_days exact pin mismatch: got {actual_overdue_days}, "
        f"expected 574. Replaces the always-passing `overdue_days >= 0` bound; "
        f"if calculation changed, update this pin to the new exact integer. "
        f"row={row!r}"
    )


def test_quick_tasks_payable_waiting_days(db_session, monkeypatch):
    """The To-pay row carries the SAME waiting-day fact as the task rows."""
    from app.services.operations import generation
    from app.models.user import User

    user = User(username="zl-default", role=UserRole.admin,
                api_key_hash="x" * 64, is_active=True, telegram_chat_id="tg-1")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    monkeypatch.setattr(generation, "DEFAULT_ASSIGNED_USER_ID", user.id)
    lease = _seed_overdue_lease(db_session)
    db_session.commit()
    db_session.refresh(lease)
    from app.models.property import Unit as _U
    _pid_row = db_session.query(_U.property_id).filter(_U.id == lease.unit_id).first()
    _pid = _pid_row[0] if _pid_row else db_session.query(Property.id).order_by(Property.id.asc()).first()[0]
    exp = Expense(
        expense_date=date(2026, 8, 15), category="Repair", amount="7000.00",
        payee="Fix-It Co", status=ExpenseStatus.approved,
        approved_at=NOW - timedelta(days=2), unit_id=lease.unit_id,
        payer_user_id=user.id, property_id=_pid,
    )
    db_session.add(exp)
    db_session.commit()
    db_session.refresh(exp)
    rows = build_quick_tasks(db_session, user, now=NOW)
    payable = [r for r in rows if str(r.get("kind") or "") == "payable_expense"]
    assert payable
    assert payable[0]["expense_id"] == exp.id
    assert payable[0]["waiting_days"] == 2
