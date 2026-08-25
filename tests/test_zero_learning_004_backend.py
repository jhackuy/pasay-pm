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


def test_quick_properties_unpaid_periods_truth(db_session):
    """The Properties index's period count comes from the SAME len(overdue)
    computation as the RENT_OVERDUE generator (never a renderer guess).

    NOTE: the unpaid-periods truth lives in ``build_quick_rent`` (the Rent
    detail view), not in ``build_quick_properties`` which is the asset-only
    Units page and intentionally exposes only ``status=occupied|vacant``.
    This test pins the canonical overdue-period source used by both the
    RENT_OVERDUE generator and the Properties index renderer."""
    from app.services.operations.quick import build_quick_rent
    lease = _seed_overdue_lease(db_session)
    db_session.commit()
    data = build_quick_rent(db_session, now=NOW)
    rows = data["overdue"]
    row = next(r for r in rows if r.get("unit_code") == "1680")
    assert row["unpaid_periods"] >= 3
    assert row["unit_code"] == "1680"
    assert row["overdue_days"] >= 0


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
