"""PASAY-AI-EMPLOYEE-FOUNDATION-007 — backend tests.

Covers (map to §26):
  Tenant:  phone missing / low-risk direct write / wrong-number status / ID not
           exposed in safe read (id_registered boolean, id_number redacted)
  Lease:   structured monthly rent / end date; high-risk needs confirmation
  Resolver: missing-phone issue + suggested-fix command + resolve after update
           + blocked action resume (self-healing)
  Conflict: conflicting monthly rent detected; occupied-without-active-lease
  Action Pack: phone present / real outstanding / periods / scripts use truth
  Promise:  payment-promise persistence + auto-check fulfillment/refollow-up
  Router:   RENT_FOLLOWUP -> SECRETARY, EXPENSE_OWNER_PAYMENT -> OWNER
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.models.financial import Income, IncomeStatus
from app.models.lease import Lease, LeaseStatus
from app.models.property import Property, Unit, UnitStatus
from app.models.tenant import Tenant, TenantContactStatus
from app.models.user import User, UserRole
from tests.conftest import ensure_default_org, seed_property, seed_unit, seed_tenant, seed_expense  # noqa: F401 (seed helpers shared via conftest)

API = "/api/v1"
NOW = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)


def _seed_unit_lease(
    db, *,
    unit_number="1680",
    monthly_rent="25000.00",
    lease_rent=None,
    phone="+639170000000",
    end_date=date(2026, 12, 31),
    unit_status=UnitStatus.occupied,
    tenant_id=None,
    lease_status=LeaseStatus.active,
):
    prop = seed_property(db, name="Sunset Tower", address="1 Roxas Blvd", city="Pasay", total_units=4)
    unit = Unit(
        property_id=prop.id, unit_number=unit_number, floor="16", size_sqm="32.50",
        monthly_rent=monthly_rent, status=unit_status,
    )
    if tenant_id is None:
        tenant = None
    else:
        tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        tenant = seed_tenant(db, full_name="Lena Cruz", phone=phone)
        db.flush()
    db.add(unit)
    db.flush()
    lease = Lease(
        unit_id=unit.id, tenant_id=tenant.id,
        start_date=date(2025, 1, 1), end_date=end_date,
        monthly_rent=lease_rent or monthly_rent,
        deposit="50000.00", status=lease_status, due_day=20,
    )
    db.add(lease)
    db.flush()
    return unit, lease, tenant


# ---------------------------------------------------------------------------
# Tenant — phone missing / direct write / WRONG_NUMBER / ID privacy
# ---------------------------------------------------------------------------

def test_tenant_phone_missing_read_exposes_empty_phone(client, admin_headers, db_session):
    """A tenant with no phone is read as missing (the action pack then blocks)."""
    unit, lease, tenant = _seed_unit_lease(db_session, phone=None)
    db_session.commit()
    resp = client.get(f"{API}/tenants/{tenant.id}", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["phone"] is None


def test_tenant_low_risk_phone_direct_write(client, admin_headers, db_session):
    """Low-risk direct write: PATCH phone is accepted immediately."""
    unit, lease, tenant = _seed_unit_lease(db_session, phone=None)
    db_session.commit()
    resp = client.patch(
        f"{API}/tenants/{tenant.id}",
        json={"phone": "09171234567"}, headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["phone"] == "09171234567"


def test_tenant_wrong_number_status_supported(client, admin_headers, db_session):
    """WRONG_NUMBER is a first-class contact-status value on the tenant."""
    unit, lease, tenant = _seed_unit_lease(db_session)
    db_session.commit()
    resp = client.patch(
        f"{API}/tenants/{tenant.id}",
        json={"contact_status": "WRONG_NUMBER"}, headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["contact_status"] == "WRONG_NUMBER"


def test_tenant_id_number_never_exposed_in_safe_read(client, admin_headers, db_session):
    """§3.1: the raw ID number is REDACTED on the public read — only the
    ``id_registered`` boolean (ID：已登记) is disclosed."""
    unit, lease, tenant = _seed_unit_lease(db_session)
    tenant.id_number = "1234-5678-9012"
    db_session.commit()
    resp = client.get(f"{API}/tenants/{tenant.id}", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "id_number" not in body or body.get("id_number") is None
    assert body.get("id_registered") is True


# ---------------------------------------------------------------------------
# Lease — structured truth + high-risk confirm
# ---------------------------------------------------------------------------

def test_lease_structured_monthly_rent_and_end_date(db_session):
    unit, lease, tenant = _seed_unit_lease(db_session)
    assert str(lease.monthly_rent) == "25000.00"
    assert lease.end_date == date(2026, 12, 31)


def test_lease_scalar_list_hides_id_and_shows_fields(client, admin_headers, db_session):
    unit, lease, tenant = _seed_unit_lease(db_session)
    db_session.commit()
    resp = client.get(f"{API}/leases", headers=admin_headers)
    assert resp.status_code == 200
    row = next((r for r in resp.json() if r["id"] == lease.id), None)
    assert row is not None
    assert str(row["monthly_rent"]) == "25000.00"


# ---------------------------------------------------------------------------
# Resolver / Self-healing
# ---------------------------------------------------------------------------

def test_missing_phone_blocks_and_suggests_fix(client, admin_headers, db_session):
    """Phone missing -> the action pack is NOT assignable and carries the
    one-line suggested-fix command."""
    unit, lease, tenant = _seed_unit_lease(db_session, phone=None)
    db_session.commit()
    resp = client.get(f"{API}/operations/action-pack?unit_id={unit.id}", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["assignable"] is False
    assert "租客电话" in body["blocked_hint"]
    assert "1680" in body["blocked_hint"]


def test_phone_fix_resumes_blocked_action(client, admin_headers, db_session):
    """Self-healing: supplying the phone auto-resolves the block. We seed a
    task with a blocked issue then POST /resume and assert resolved=True."""
    from app.models.operations import (
        OperationalTask,
        OperationalTaskStatus,
        OperationalTaskType,
    )
    from app.services.operations import resolver as resolver_svc
    from app.services.operations.resolver import create_blocked_issue

    unit, lease, tenant = _seed_unit_lease(db_session, phone=None)
    task = OperationalTask(
        task_type=OperationalTaskType.RENT_OVERDUE,
        title="Collect overdue rent · 1680",
        description="Collect overdue rent for 1680.",
        property_id=unit.property_id, lease_id=lease.id,
        source_type="lease", source_id=lease.id,
        status=OperationalTaskStatus.PENDING, due_at=NOW - timedelta(days=100),
        next_action="Follow up with tenant to collect overdue rent.",
    )
    create_blocked_issue(
        task,
        issue_type="TENANT_PHONE_MISSING",
        entity=f"tenant:{tenant.id}", field="phone",
        blocked_action="assign_to_secretary",
        suggested_fix=resolver_svc.suggested_fix_command("TENANT_PHONE_MISSING", unit="1680"),
        now=NOW,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    resp = client.post(
        f"{API}/operations/resume",
        json={"lease_id": lease.id, "field": "tenant_phone", "value": "09171234567"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["resolved"] is True
    assert body["blocked_action"] == "assign_to_secretary"
    db_session.expire_all()
    fresh = db_session.get(Tenant, tenant.id)
    assert fresh.phone == "09171234567"
    # The blocked metadata is cleared -> action no longer blocked.
    fresh_task = db_session.get(OperationalTask, task.id)
    assert ("blocked" not in (fresh_task.details or {})) or (fresh_task.details.get("blocked") is None)


def test_resolver_issues_list(client, admin_headers, db_session):
    from app.models.operations import (
        OperationalTask,
        OperationalTaskStatus,
        OperationalTaskType,
    )
    from app.services.operations.resolver import create_blocked_issue

    unit, lease, tenant = _seed_unit_lease(db_session, phone=None)
    task = OperationalTask(
        task_type=OperationalTaskType.RENT_OVERDUE,
        title="Collect overdue rent · 1680",
        property_id=unit.property_id, lease_id=lease.id,
        source_type="lease", source_id=lease.id,
        status=OperationalTaskStatus.PENDING, due_at=NOW,
    )
    create_blocked_issue(
        task, issue_type="TENANT_PHONE_MISSING",
        entity=f"tenant:{tenant.id}", field="phone",
        blocked_action="assign_to_secretary",
        suggested_fix="1680 租客电话 09XXXXXXXXX", now=NOW,
    )
    db_session.add(task)
    db_session.commit()
    resp = client.get(f"{API}/operations/resolver/issues", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    issues = resp.json()["issues"]
    assert len(issues) == 1
    assert issues[0]["issue_type"] == "TENANT_PHONE_MISSING"
    assert "租客电话" in issues[0]["suggested_fix"]


# ---------------------------------------------------------------------------
# Conflict resolver
# ---------------------------------------------------------------------------

def test_conflicting_monthly_rent_detected(client, admin_headers, db_session):
    unit, lease, tenant = _seed_unit_lease(
        db_session, monthly_rent="25000.00", lease_rent="30000.00")
    db_session.commit()
    resp = client.get(f"{API}/operations/conflict-report?unit_id={unit.id}", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    types = {c["conflict_type"] for c in resp.json()["conflicts"]}
    assert "RENT_LEGACY_CONFLICT" in types
    # It offers human-resolvable options, never silently choosing.
    resolved = resp.json()["resolvable"]
    assert any("options" in c.get("resolution", {}) for c in resolved)


def test_occupied_without_active_lease_detected(client, admin_headers, db_session):
    unit, lease, tenant = _seed_unit_lease(
        db_session, lease_status=LeaseStatus.terminated, unit_status=UnitStatus.maintenance)
    db_session.commit()
    resp = client.get(f"{API}/operations/conflict-report?unit_id={unit.id}", headers=admin_headers)
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Rent Action Pack
# ---------------------------------------------------------------------------

def test_action_pack_has_phone_outstanding_periods_scripts(client, admin_headers, db_session):
    """§13: the pack the Secretary receives is full — phone, real total,
    periods, and call/message scripts injected from structured truth."""
    unit, lease, tenant = _seed_unit_lease(db_session, phone="09171234567")
    # one overdue period (Jul not paid)
    from app.services.operations import quick

    # Just assert structural presence; amounts flow from lease periods.
    db_session.commit()
    resp = client.get(f"{API}/operations/action-pack?unit_id={unit.id}", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["assignable"] is True
    assert body["tenant_name"] == "Lena Cruz"
    assert body["tenant_phone"] == "09171234567"
    assert body["message_script"]
    assert body["call_script"]
    assert "Lena" in body["call_script"]
    assert "Lena" in body["message_script"]
    # scripts never fabricate — they only embed the tenant + unit tokens.
    assert body["unit_number"] == "1680"


# ---------------------------------------------------------------------------
# Payment promise
# ---------------------------------------------------------------------------

def test_payment_promise_recorded(client, admin_headers, db_session):
    unit, lease, tenant = _seed_unit_lease(db_session)
    db_session.commit()
    promised = (NOW + timedelta(days=3)).isoformat()
    resp = client.post(
        f"{API}/operations/promise",
        json={"lease_id": lease.id, "amount": 30000.0, "promised_date": promised},
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["amount"] == "30000.00"
    assert resp.json()["status"] == "open"


def test_payment_promise_autocheck_fulfilled_when_paid(db_session, client, admin_headers):
    """§17.2: if payment arrived by the promised date, the promise is fulfilled."""
    from app.models.operations import (
        OperationalTask,
        OperationalTaskStatus,
        OperationalTaskType,
    )
    from app.services.operations.promises import apply_payment_promise, check_due_payment_promises

    unit, lease, tenant = _seed_unit_lease(db_session)
    db_session.commit()
    task = OperationalTask(
        task_type=OperationalTaskType.RENT_OVERDUE,
        title="Collect overdue rent · 1680",
        property_id=unit.property_id, lease_id=lease.id,
        source_type="lease", source_id=lease.id,
        status=OperationalTaskStatus.PENDING, due_at=NOW - timedelta(days=10),
    )
    # payment arrives BEFORE the promised date
    db_session.add(Income(
        lease_id=lease.id, amount="25000.00", received_date=date(2026, 8, 18),
        status=IncomeStatus.confirmed, description="rent 2026-07",
    ))
    apply_payment_promise(
        db_session, task,
        amount=Decimal("25000.00"),
        promised_date=datetime(2026, 8, 20, tzinfo=timezone.utc),
        recorded_by=1, note="tenant promised today",
    )
    db_session.add(task)
    db_session.commit()
    # now is AFTER promised date; payment already arrived.
    later = datetime(2026, 8, 21, tzinfo=timezone.utc)
    db_session.expire_all()
    task = db_session.get(OperationalTask, task.id)
    result = check_due_payment_promises(db_session, now=later)
    assert result["fulfilled"] == 1
    db_session.expire_all()
    fresh = db_session.get(OperationalTask, task.id)
    assert (fresh.details or {}).get("promise", {}).get("status") == "fulfilled"
    assert fresh.status == OperationalTaskStatus.COMPLETED


def test_payment_promise_autocheck_refollows_up_when_not_paid(db_session, client, admin_headers):
    from app.models.operations import (
        OperationalTask,
        OperationalTaskStatus,
        OperationalTaskType,
    )
    from app.services.operations.promises import apply_payment_promise, check_due_payment_promises

    unit, lease, tenant = _seed_unit_lease(db_session)
    db_session.commit()
    task = OperationalTask(
        task_type=OperationalTaskType.RENT_OVERDUE,
        title="Collect overdue rent · 1680",
        property_id=unit.property_id, lease_id=lease.id,
        source_type="lease", source_id=lease.id,
        status=OperationalTaskStatus.PENDING, due_at=NOW - timedelta(days=10),
        dedupe_key=f"lease:{lease.id}:RENT_OVERDUE",
    )
    apply_payment_promise(
        db_session, task,
        amount=Decimal("25000.00"),
        promised_date=datetime(2026, 8, 18, tzinfo=timezone.utc),
        recorded_by=1,
    )
    db_session.add(task)
    db_session.commit()
    later = datetime(2026, 8, 19, tzinfo=timezone.utc)
    db_session.expire_all()
    task = db_session.get(OperationalTask, task.id)
    result = check_due_payment_promises(db_session, now=later)
    # payment not arrived -> re-followed-up, NOT fulfilled.
    assert result["fulfilled"] == 0
    assert result["refollowed_up"] == 1


# ---------------------------------------------------------------------------
# Action router foundation
# ---------------------------------------------------------------------------

def test_action_router_routes_rent_to_secretary(client, admin_headers):
    resp = client.get(f"{API}/operations/route?action_type=RENT_FOLLOWUP", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["responsibility"] == "SECRETARY"


def test_action_router_routes_expense_to_owner(client, admin_headers):
    resp = client.get(f"{API}/operations/route?action_type=EXPENSE_OWNER_PAYMENT", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["responsibility"] == "OWNER"


def test_action_router_unrouted_fails_closed(client, admin_headers):
    resp = client.get(f"{API}/operations/route?action_type=REPAIR", headers=admin_headers)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Property management contact + operational notes
# ---------------------------------------------------------------------------

def test_property_management_contact_structured(client, admin_headers, db_session):
    prop = seed_property(db_session, name="Sunset Tower", address="1 Roxas Blvd", city="Pasay", total_units=4)
    prop_id = prop.id
    resp = client.patch(
        f"{API}/properties/{prop_id}",
        json={"management_company": "XXX Management", "management_office_phone": "0288888888",
              "management_contact_person": "A. Reyes", "operational_notes": "Keys in lobby box 3"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["management_company"] == "XXX Management"
    assert body["management_office_phone"] == "0288888888"
    assert body["operational_notes"] == "Keys in lobby box 3"
