"""TELEGRAM-OPS-REAL-WORLD-CLOSURE-005 — backend regression tests.

Pins:
- /operations/secretary-target resolves the canonical Secretary DM target
  (fail closed: 404 when no Secretary destination exists);
- Tasks board RENT_OVERDUE rows surface the TOTAL arrears (month × uncovered
  periods), never a bare monthly rent — cross-view with Rent detail/overview;
- the Rent overview's "last follow-up" reflects the Secretary's EXECUTED
  (completed_at) date, never the mere creation of an assigned task.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.models.financial import Expense, ExpenseStatus
from app.models.lease import Lease, LeaseStatus
from app.models.property import Property, Unit, UnitStatus
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.operations.config import SECRETARY_ASSIGNEE_ID
from app.services.operations.quick import build_quick_properties, build_quick_rent, build_quick_tasks
from tests.conftest import ensure_default_org, seed_property, seed_unit, seed_tenant, seed_expense  # noqa: F401 (seed helpers shared via conftest)

API = "/api/v1"
NOW = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)


def _moneyd(v) -> str:
    from decimal import Decimal
    return str(Decimal(str(v)))


def _seed_fixture(db, *, rent="25000.00", due_day=20):
    """One occupied, 3-period-overdue unit + an active lease + user."""
    from tests.conftest import make_user

    owner, _key = make_user(db, "closure-admin", UserRole.admin)
    db.query(User).filter_by(id=owner.id).update({"telegram_chat_id": "5177241442"})
    db.flush()
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
    return owner, unit, lease


def test_secretary_target_resolves_configured_secretary(client, db_session, admin_headers):
    """The 催租 assign-to-Secretary DM needs the canonical Secretary target."""
    from tests.conftest import make_user
    from app.models.membership import Organization, Membership, MembershipState, OrganizationRole

    sec, _ = make_user(db_session, "closure-secretary", UserRole.manager)
    db_session.query(User).filter_by(id=sec.id).update({"telegram_chat_id": "1083657401"})
    db_session.flush()
    org = ensure_default_org(db_session)
    exists = db_session.query(Membership).filter(
        Membership.organization_id == org.id,
        Membership.user_id == sec.id,
        Membership.removed_at.is_(None),
    ).first()
    if not exists:
        db_session.add(Membership(
            organization_id=org.id,
            user_id=sec.id,
            role=OrganizationRole.SECRETARY,
            state=MembershipState.ACTIVE,
        ))
    db_session.commit()
    resp = client.get(f"{API}/operations/secretary-target", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["telegram_chat_id"].strip() == "1083657401"
    assert resp.json()["principal_id"] == sec.id


def test_secretary_target_404_without_secretary_destination(client, db_session, admin_headers):
    """Fail closed: no Secretary Telegram destination -> 404, so the bot never
    marks a follow-up assigned when it cannot send the DM."""
    from app.models.user import User

    for u in db_session.query(User).filter(User.role == UserRole.manager).all():
        u.telegram_chat_id = None
    db_session.commit()
    resp = client.get(f"{API}/operations/secretary-target", headers=admin_headers)
    assert resp.status_code == 404, resp.text


def test_quick_tasks_rent_overdue_amount_is_total_arrears(db_session, monkeypatch):
    """§11: a RENT_OVERDUE row's ``amount`` must be the TOTAL arrears (monthly
    × 3 uncovered periods), never the bare monthly rent — matching the Rent
    detail / overview truth (cross-view regression)."""
    from app.models.operations import (
        OperationalTask,
        OperationalTaskStatus,
        OperationalTaskType,
    )
    from app.services.operations import generation

    user, unit, lease = _seed_fixture(db_session, rent="25000.00", due_day=20)
    # The scheduler's RENT_OVERDUE generator stores amount=+total_outstanding.
    # A legacy/stale row has only amount=monthly + total_outstanding=total.
    details = {
        "amount": "25000.00",  # the historical bug: month's rent in `amount`
        "total_outstanding": "75000.00",  # the real total arrears
        "unit_number": "1680",
        "periods": ["2026-05", "2026-06", "2026-07"],
    }
    task = OperationalTask(
        task_type=OperationalTaskType.RENT_OVERDUE,
        title="租金逾期 · 3期", description=None,
        property_id=unit.property_id, lease_id=lease.id,
        source_type="lease", source_id=lease.id,
        assigned_user_id=user.id, priority="high",
        status=OperationalTaskStatus.PENDING,
        due_at=NOW - timedelta(days=100),
        next_action="Collect overdue rent.",
        details=details,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    db_session.commit()
    rows = build_quick_tasks(db_session, user, now=NOW)
    rent_row = next((r for r in rows if r["task_type"] == "RENT_OVERDUE"), None)
    assert rent_row is not None
    # The True arrears (₱75,000) wins over the monthly rent (₱25,000).
    assert _moneyd(rent_row["amount"]) == "75000.00"


def test_quick_rent_last_followup_is_executed_date(db_session):
    """§2.5/§4: the Rent overview's ``last_followup_at`` reflects when the
    Secretary EXECUTED (completed_at), never merely when an assigned task was
    created."""
    from app.models.operations import (
        OperationalTask,
        OperationalTaskStatus,
        OperationalTaskType,
    )

    user, unit, lease = _seed_fixture(db_session, rent="25000.00", due_day=1)
    # A follow-up task that was CREATED but never EXECUTED (Secretary not yet
    # contacted) must NOT move last_followup_at.
    pending = OperationalTask(
        task_type=OperationalTaskType.FOLLOWUP, title="Collect rent 1680",
        property_id=unit.property_id, lease_id=lease.id,
        source_type="lease", source_id=lease.id, assigned_user_id=user.id,
        status=OperationalTaskStatus.IN_PROGRESS, due_at=NOW,
        completed_at=None,
        details={"assigned_to": "secretary", "assigned_at": NOW.isoformat()},
    )
    # A previously EXECUTED follow-up (completed_at) is the real last contact.
    executed = OperationalTask(
        task_type=OperationalTaskType.FOLLOWUP, title="Collect rent 1680 past",
        property_id=unit.property_id, lease_id=lease.id,
        source_type="lease", source_id=lease.id, assigned_user_id=user.id,
        status=OperationalTaskStatus.COMPLETED, due_at=NOW - timedelta(days=1),
        completed_at=NOW - timedelta(days=1),
        details={"executed_by": user.id, "executed_at": (NOW - timedelta(days=1)).isoformat()},
    )
    db_session.add_all([pending, executed])
    db_session.commit()

    data = build_quick_rent(db_session, now=NOW)
    row = next((r for r in data["overdue"] if r["unit"] == "1680"), None)
    assert row is not None
    # The last follow-up equals the EXECUTED (completed) timestamp, not the
    # created/assigned task's created_at.
    assert row["last_followup_at"] is not None
    from datetime import timezone
    val = datetime.fromisoformat(row["last_followup_at"])
    assert val.date() == (NOW - timedelta(days=1)).date()
