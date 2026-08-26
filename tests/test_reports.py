from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.services.dates import add_months, month_range


def _patch_reports_date_today(monkeypatch, target: date):
    """Override ``date.today`` and ``_today_utc`` inside app.api.routers.reports.

    The router originally used ``from datetime import date`` + ``date.today()``
    which we override via a ``date`` subclass. It also calls a module-level
    helper ``_today_utc()`` which we override directly to *target* so
    endpoints compute overdue against *current_date* fixture (not real today).
    Without this, Dec→Jan case would compute overdue from real today (e.g. Aug
    2026) and produce ~16 months instead of the expected 9.
    """
    import app.api.routers.reports as _reports_mod
    import datetime as _std_dt

    class _FixedDate(_std_dt.date):
        @classmethod
        def today(cls):
            return target

    monkeypatch.setattr(_reports_mod, "date", _FixedDate)
    monkeypatch.setattr(_reports_mod, "_today_utc", lambda: target)


@pytest.fixture()
def owner_a_headers(owner_a):
    return {"Authorization": f"Bearer {owner_a[1]}"}

API = "/api/v1"


def _month():
    return date.today().strftime("%Y-%m")


def test_financial_summary(client, owner_a, lease_id, unit_id):
    h = {"Authorization": f"Bearer {owner_a[1]}"}
    target_month = "2026-06"
    resp = client.post(
        f"{API}/incomes",
        json={
            "lease_id": lease_id,
            "amount": "12000.00",
            "received_date": "2026-06-15",
            "payment_method": "cash",
            "status": "confirmed",
            "description": "rent 2026-06",
        },
        headers=h,
    )
    assert resp.status_code == 201, resp.text

    resp = client.post(
        f"{API}/expenses",
        json={
            "expense_date": "2026-06-15",
            "category": "repair",
            "amount": "2000.00",
            "payee": "Fix-It Co",
            "status": "approved",
            "unit_id": unit_id,
        },
        headers=h,
    )
    expense_id = resp.json()["id"]
    client.post(f"{API}/expenses/{expense_id}/pay", headers=h)

    resp = client.get(
        f"{API}/reports/financial-summary?month={target_month}", headers=h
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["month"] == target_month
    assert data["expected_rent_total"] == "12000.00"
    assert data["collected_rent"] == "12000.00"
    assert data["outstanding_rent"] == "0.00"
    assert data["total_income"] == "12000.00"
    assert data["total_expense"] == "2000.00"
    assert data["net_income"] == "10000.00"
    assert data["units_count"] == 1
    assert data["occupied_units"] == 1
    assert data["vacant_units"] == 0


def test_financial_summary_unit_filter(client, owner_a, unit_id, tenant_id):
    h = {"Authorization": f"Bearer {owner_a[1]}"}
    today = date.today()
    resp = client.post(
        f"{API}/leases",
        json={
            "unit_id": unit_id,
            "tenant_id": tenant_id,
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
            "monthly_rent": "12000.00",
            "deposit": "0.00",
            "status": "active",
        },
        headers=h,
    )
    assert resp.status_code == 201, resp.text

    resp = client.get(
        f"{API}/reports/financial-summary?month={_month()}&unit_id=999999",
        headers=h,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["expected_rent_total"] == "0.00"
    assert data["units_count"] == 0

    resp = client.get(
        f"{API}/reports/financial-summary?month={_month()}&unit_id={unit_id}",
        headers=h,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["expected_rent_total"] == "12000.00"
    assert data["units_count"] == 1
    assert data["occupied_units"] == 1


def test_financial_summary_agent_forbidden(client, agent, db_session):
    from app.models.membership import Membership, MembershipState, Organization
    from app.models.user import UserRole

    agent_user, agent_key = agent
    if agent_user.role == UserRole.agent:
        default_org = db_session.query(Organization).order_by(Organization.id.asc()).first()
        if default_org is not None:
            ms = db_session.query(Membership).filter(
                Membership.user_id == agent_user.id,
                Membership.organization_id == default_org.id,
                Membership.state == MembershipState.ACTIVE,
            ).all()
            for m in ms:
                db_session.delete(m)
            db_session.commit()
    h = {"Authorization": f"Bearer {agent_key}"}
    resp = client.get(f"{API}/reports/financial-summary", headers=h)
    assert resp.status_code == 403


def test_overdue_rents_report(client, owner_a_headers, unit_id, tenant_id):
    today = date.today()
    start = today.replace(day=1)
    resp = client.post(
        f"{API}/leases",
        json={
            "unit_id": unit_id,
            "tenant_id": tenant_id,
            "start_date": start.isoformat(),
            "end_date": f"{today.year + 1}-12-31",
            "monthly_rent": "5000.00",
            "deposit": "0.00",
            "status": "active",
            "due_day": 1,
        },
        headers=owner_a_headers,
    )
    assert resp.status_code == 201, resp.text
    new_lease_id = resp.json()["id"]

    resp = client.get(f"{API}/reports/overdue-rents", headers=owner_a_headers)
    assert resp.status_code == 200
    rows = [r for r in resp.json() if r["lease_id"] == new_lease_id]
    assert len(rows) == 1
    row = rows[0]
    assert row["unit"] == "101"
    assert row["tenant"] == "Juan Dela Cruz"
    assert row["outstanding"] == "5000.00"
    assert row["days_overdue"] >= 0


def _month_key(value: date) -> str:
    return value.strftime("%Y-%m")


def _shift_month(value: date, delta: int) -> date:
    month_index = value.month - 1 + delta
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    return value.replace(year=year, month=month, day=1)


def _create_rent_lease(
    client, owner_a_headers, unit_id, tenant_id, start_date, end_date,
    monthly_rent="65000.00", due_day=None, accounting_start_date=None,
):
    payload = {
        "unit_id": unit_id,
        "tenant_id": tenant_id,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "monthly_rent": monthly_rent,
        "deposit": "0.00",
        "status": "active",
    }
    if due_day is not None:
        payload["due_day"] = due_day
    if accounting_start_date is not None:
        payload["accounting_start_date"] = accounting_start_date.isoformat()
    resp = client.post(f"{API}/leases", json=payload, headers=owner_a_headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _overdue_row(client, owner_a_headers, lease_id):
    resp = client.get(f"{API}/reports/overdue-rents", headers=owner_a_headers)
    assert resp.status_code == 200
    rows = [r for r in resp.json() if r["lease_id"] == lease_id]
    assert len(rows) == 1
    return rows[0]


def _post_income(client, owner_a_headers, lease_id, amount, received_date,
                 status="confirmed", description=None):
    payload = {
        "lease_id": lease_id,
        "amount": amount,
        "received_date": received_date.isoformat(),
        "payment_method": "cash",
        "status": status,
    }
    if description is not None:
        payload["description"] = description
    resp = client.post(f"{API}/incomes", json=payload, headers=owner_a_headers)
    assert resp.status_code == 201, resp.text


def test_overdue_rents_current_month(client, owner_a_headers, unit_id, tenant_id):
    today = date.today()
    start = today.replace(day=1)
    lease_id = _create_rent_lease(
        client, owner_a_headers, unit_id, tenant_id,
        start_date=start,
        end_date=date(start.year + 1, 12, 31),
        due_day=1,
    )
    row = _overdue_row(client, owner_a_headers, lease_id)
    assert row["unit"] == "101"
    assert row["tenant"] == "Juan Dela Cruz"
    assert row["overdue_months"] == 1
    assert row["amount_per_month"] == "65000.00"
    assert row["total_outstanding"] == "65000.00"
    assert row["outstanding"] == "65000.00"
    assert row["overdue_periods"] == [{"month": _month_key(start), "amount": "65000.00"}]
    assert row["oldest_due_date"] == start.isoformat()
    assert row["overdue_days"] == (today - start).days
    assert row["days_overdue"] == row["overdue_days"]


def test_overdue_rents_consecutive_months(client, owner_a_headers, unit_id, tenant_id):
    today = date.today()
    start = _shift_month(today, -2)
    lease_id = _create_rent_lease(
        client, owner_a_headers, unit_id, tenant_id,
        start_date=start,
        end_date=date(start.year + 1, 12, 31),
        due_day=1,
    )
    row = _overdue_row(client, owner_a_headers, lease_id)
    assert row["overdue_months"] == 3
    assert row["total_outstanding"] == "195000.00"
    assert row["overdue_periods"] == [
        {"month": _month_key(_shift_month(today, -2)), "amount": "65000.00"},
        {"month": _month_key(_shift_month(today, -1)), "amount": "65000.00"},
        {"month": _month_key(today), "amount": "65000.00"},
    ]
    assert row["oldest_due_date"] == start.isoformat()


def test_overdue_rents_one_month_paid(client, owner_a_headers, unit_id, tenant_id):
    today = date.today()
    start = _shift_month(today, -2)
    lease_id = _create_rent_lease(
        client, owner_a_headers, unit_id, tenant_id,
        start_date=start,
        end_date=date(start.year + 1, 12, 31),
        due_day=1,
    )
    paid_month = _shift_month(today, -1)
    _post_income(
        client, owner_a_headers, lease_id, "65000.00", today,
        description=f"rent {_month_key(paid_month)}",
    )
    row = _overdue_row(client, owner_a_headers, lease_id)
    assert row["overdue_months"] == 2
    assert row["total_outstanding"] == "130000.00"
    assert [p["month"] for p in row["overdue_periods"]] == [
        _month_key(start),
        _month_key(today),
    ]


def test_overdue_rents_received_month_fallback(client, owner_a_headers, unit_id, tenant_id):
    today = date.today()
    start = _shift_month(today, -2)
    lease_id = _create_rent_lease(
        client, owner_a_headers, unit_id, tenant_id,
        start_date=start,
        end_date=date(start.year + 1, 12, 31),
        due_day=1,
    )
    paid_month = _shift_month(today, -1)
    _post_income(client, owner_a_headers, lease_id, "65000.00", paid_month)
    row = _overdue_row(client, owner_a_headers, lease_id)
    assert row["overdue_months"] == 2
    assert [p["month"] for p in row["overdue_periods"]] == [
        _month_key(start),
        _month_key(today),
    ]


def test_overdue_rents_pending_not_counted(client, owner_a_headers, unit_id, tenant_id):
    today = date.today()
    start = today.replace(day=1)
    lease_id = _create_rent_lease(
        client, owner_a_headers, unit_id, tenant_id,
        start_date=start,
        end_date=date(start.year + 1, 12, 31),
        due_day=1,
    )
    _post_income(
        client, owner_a_headers, lease_id, "65000.00", today,
        status="pending", description=f"rent {_month_key(start)}",
    )
    row = _overdue_row(client, owner_a_headers, lease_id)
    assert row["overdue_months"] == 1
    assert row["total_outstanding"] == "65000.00"


def test_overdue_rents_advance_future_payment(client, owner_a_headers, unit_id, tenant_id):
    today = date.today()
    start = _shift_month(today, -2)
    lease_id = _create_rent_lease(
        client, owner_a_headers, unit_id, tenant_id,
        start_date=start,
        end_date=date(start.year + 1, 12, 31),
        due_day=1,
    )
    future_month = _shift_month(today, 1)
    _post_income(
        client, owner_a_headers, lease_id, "65000.00", today,
        description=f"rent {_month_key(future_month)}",
    )
    row = _overdue_row(client, owner_a_headers, lease_id)
    assert row["overdue_months"] == 3
    assert row["total_outstanding"] == "195000.00"
    assert _month_key(future_month) not in [p["month"] for p in row["overdue_periods"]]


def test_overdue_rents_lease_started_midterm(client, owner_a_headers, unit_id, tenant_id):
    today = date.today()
    start = today - timedelta(days=15)
    lease_id = _create_rent_lease(
        client, owner_a_headers, unit_id, tenant_id,
        start_date=start,
        end_date=date(start.year + 1, 12, 31),
    )
    row = _overdue_row(client, owner_a_headers, lease_id)
    assert row["overdue_months"] == 1
    assert row["total_outstanding"] == "65000.00"
    assert row["overdue_periods"] == [{"month": _month_key(start), "amount": "65000.00"}]
    assert row["oldest_due_date"] == start.isoformat()


def test_overdue_rents_lease_ended(client, owner_a_headers, unit_id, tenant_id):
    today = date.today()
    start = _shift_month(today, -5)
    end = _shift_month(today, -2) - timedelta(days=1)
    lease_id = _create_rent_lease(
        client, owner_a_headers, unit_id, tenant_id,
        start_date=start,
        end_date=end,
        due_day=1,
    )
    row = _overdue_row(client, owner_a_headers, lease_id)
    assert row["overdue_months"] == 3
    assert row["total_outstanding"] == "195000.00"
    assert [p["month"] for p in row["overdue_periods"]] == [
        _month_key(_shift_month(today, -5)),
        _month_key(_shift_month(today, -4)),
        _month_key(_shift_month(today, -3)),
    ]


def test_monthly_report(client, owner_a, unit_id, tenant_id):
    h = {"Authorization": f"Bearer {owner_a[1]}"}
    resp = client.post(
        f"{API}/leases",
        json={
            "unit_id": unit_id,
            "tenant_id": tenant_id,
            "start_date": f"{date.today().year}-01-01",
            "end_date": f"{date.today().year}-12-31",
            "monthly_rent": "12000.00",
            "deposit": "0.00",
            "status": "active",
        },
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    _lease_id = resp.json()["id"]
    resp = client.get(f"{API}/reports/monthly?month={_month()}", headers=h)
    assert resp.status_code == 200
    rows = [r for r in resp.json() if r["lease_id"] == _lease_id]
    assert len(rows) == 1
    row = rows[0]
    assert row["unit"] == "101"
    assert row["tenant"] == "Juan Dela Cruz"
    assert row["expected"] == "12000.00"
    assert row["collected"] == "0.00"
    assert row["outstanding"] == "12000.00"


def test_commission_report(client, owner_a_headers, manager_headers, agent, lease_id):
    rule = client.post(
        f"{API}/commission/rules",
        json={
            "name": "Rent 5%",
            "rule_type": "percentage",
            "value": "5.00",
            "agent_role": "出租",
        },
        headers=owner_a_headers,
    ).json()
    resp = client.post(
        f"{API}/commission/settlements",
        json={
            "agent_id": agent[0].id,
            "lease_id": lease_id,
            "rule_id": rule["id"],
        },
        headers=manager_headers,
    )
    assert resp.status_code == 201, resp.text

    resp = client.get(
        f"{API}/reports/commission?month={_month()}", headers=owner_a_headers
    )
    assert resp.status_code == 200
    rows = [r for r in resp.json() if r["agent_id"] == agent[0].id]
    assert len(rows) == 1
    row = rows[0]
    assert row["agent"] == "agent"
    assert row["rule"] == "Rent 5%"
    assert row["computed_total"] == "600.00"
    assert row["settlements"] == 1


def test_tasks_report(client, owner_a, unit_id, db_session):
    from app.models.property import Property, Unit

    h = {"Authorization": f"Bearer {owner_a[1]}"}
    _u = db_session.query(Unit).filter(Unit.id == unit_id).first()
    _pid = _u.property_id if _u is not None else (
        db_session.query(Property.id).order_by(Property.id.asc()).first()[0]
    )
    today = date.today()
    from datetime import datetime, timezone
    resp = client.post(
        f"{API}/operations/tasks",
        json={
            "task_type": "AC_MAINTENANCE",
            "title": "Fix A",
            "description": "fixup",
            "property_id": _pid,
            "due_at": (datetime.combine(today - timedelta(days=3), datetime.min.time(), tzinfo=timezone.utc)).isoformat(),
        },
        headers=h,
    )
    assert resp.status_code == 201, resp.text

    resp = client.post(
        f"{API}/operations/tasks",
        json={
            "task_type": "FOLLOWUP",
            "title": "Scheduled A",
            "description": "scheduled follow-up",
            "property_id": _pid,
            "priority": "medium",
            "due_at": (datetime.combine(today + timedelta(days=10), datetime.min.time(), tzinfo=timezone.utc)).isoformat(),
        },
        headers=h,
    )
    assert resp.status_code == 201, resp.text

    resp = client.get(
        f"{API}/reports/tasks?status=pending&overdue=true", headers=h
    )
    assert resp.status_code == 200
    titles = [r["title"] for r in resp.json()]
    assert "Fix A" in titles
    assert "Scheduled A" not in titles

    resp = client.get(f"{API}/reports/tasks?status=scheduled", headers=h)
    assert resp.status_code == 200
    assert [r["title"] for r in resp.json()] == ["Scheduled A"]

    # within_days window: Scheduled A is due in +10 days -> excluded at 5d, included at 15d
    resp = client.get(f"{API}/reports/tasks?status=scheduled&within_days=5", headers=h)
    assert resp.status_code == 200
    assert [r["title"] for r in resp.json()] == []
    resp = client.get(f"{API}/reports/tasks?status=scheduled&within_days=15", headers=h)
    assert resp.status_code == 200
    assert [r["title"] for r in resp.json()] == ["Scheduled A"]


def test_expenses_report(client, owner_a_headers, unit_id):
    today = date.today()
    for category, amount in (("repair", "3000.00"), ("utilities", "1500.00")):
        resp = client.post(
            f"{API}/expenses",
            json={
                "expense_date": today.isoformat(),
                "category": category,
                "amount": amount,
                "payee": "Vendor Co",
                "status": "approved",
                "unit_id": unit_id,
            },
            headers=owner_a_headers,
        )
        expense_id = resp.json()["id"]
        client.post(f"{API}/expenses/{expense_id}/pay", headers=owner_a_headers)

    resp = client.get(
        f"{API}/reports/expenses?month={_month()}", headers=owner_a_headers
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["month"] == _month()
    assert data["total_amount"] == "4500.00"
    by_category = {r["category"]: r["amount"] for r in data["by_category"]}
    assert by_category == {"repair": "3000.00", "utilities": "1500.00"}
    assert len(data["by_unit"]) == 1
    assert data["by_unit"][0]["unit_id"] == unit_id
    assert data["by_unit"][0]["unit"] == "101"
    assert data["by_unit"][0]["amount"] == "4500.00"

    resp = client.get(
        f"{API}/reports/expenses?month={_month()}&category=repair", headers=owner_a_headers
    )
    assert resp.status_code == 200
    assert resp.json()["total_amount"] == "3000.00"


def test_overdue_rents_no_accounting_start_regression(
    client, owner_a_headers, unit_id, tenant_id
):
    # 旧租约不传 accounting_start_date → 行为完全不变（回归）
    today = date.today()
    start = _shift_month(today, -2)
    lease_id = _create_rent_lease(
        client, owner_a_headers, unit_id, tenant_id,
        start_date=start,
        end_date=date(start.year + 1, 12, 31),
        due_day=1,
    )
    row = _overdue_row(client, owner_a_headers, lease_id)
    assert row["overdue_months"] == 3
    assert row["total_outstanding"] == "195000.00"
    assert [p["month"] for p in row["overdue_periods"]] == [
        _month_key(_shift_month(today, -2)),
        _month_key(_shift_month(today, -1)),
        _month_key(today),
    ]


def test_overdue_rents_accounting_start_excludes_history(
    client, owner_a_headers, unit_id, tenant_id
):
    # 租约历史已半年、accounting_start_date=本月 → 历史月份不报欠租
    today = date.today()
    start = _shift_month(today, -6)
    lease_id = _create_rent_lease(
        client, owner_a_headers, unit_id, tenant_id,
        start_date=start,
        end_date=date(start.year + 1, 12, 31),
        due_day=1,
        accounting_start_date=today.replace(day=1),
    )
    row = _overdue_row(client, owner_a_headers, lease_id)
    assert row["overdue_months"] == 1
    assert row["total_outstanding"] == "65000.00"
    assert row["overdue_periods"] == [{"month": _month_key(today), "amount": "65000.00"}]


def test_overdue_rents_accounting_start_unpaid_after(
    client, owner_a_headers, unit_id, tenant_id
):
    # accounting_start_date 之后未付月份 → 正常 overdue
    today = date.today()
    start = _shift_month(today, -3)
    accounting_start = _shift_month(today, -2)
    lease_id = _create_rent_lease(
        client, owner_a_headers, unit_id, tenant_id,
        start_date=start,
        end_date=date(start.year + 1, 12, 31),
        due_day=1,
        accounting_start_date=accounting_start,
    )
    row = _overdue_row(client, owner_a_headers, lease_id)
    assert row["overdue_months"] == 3
    assert row["total_outstanding"] == "195000.00"
    assert [p["month"] for p in row["overdue_periods"]] == [
        _month_key(_shift_month(today, -2)),
        _month_key(_shift_month(today, -1)),
        _month_key(today),
    ]


def test_overdue_rents_income_before_accounting_start_ignored(
    client, owner_a_headers, unit_id, tenant_id
):
    # accounting_start_date 之前的 confirmed income 不影响之后的月份
    today = date.today()
    start = _shift_month(today, -3)
    accounting_start = _shift_month(today, -2)
    lease_id = _create_rent_lease(
        client, owner_a_headers, unit_id, tenant_id,
        start_date=start,
        end_date=date(start.year + 1, 12, 31),
        due_day=1,
        accounting_start_date=accounting_start,
    )
    _post_income(
        client, owner_a_headers, lease_id, "65000.00", start,
        description=f"rent {_month_key(start)}",
    )
    row = _overdue_row(client, owner_a_headers, lease_id)
    assert row["overdue_months"] == 3
    assert row["total_outstanding"] == "195000.00"


def test_overdue_rents_income_after_accounting_start_covers_period(
    client, owner_a_headers, unit_id, tenant_id
):
    # accounting_start_date 之后的 confirmed income 正确覆盖对应 period
    today = date.today()
    start = _shift_month(today, -3)
    accounting_start = _shift_month(today, -2)
    lease_id = _create_rent_lease(
        client, owner_a_headers, unit_id, tenant_id,
        start_date=start,
        end_date=date(start.year + 1, 12, 31),
        due_day=1,
        accounting_start_date=accounting_start,
    )
    paid_month = _shift_month(today, -1)
    _post_income(
        client, owner_a_headers, lease_id, "65000.00", today,
        description=f"rent {_month_key(paid_month)}",
    )
    row = _overdue_row(client, owner_a_headers, lease_id)
    assert row["overdue_months"] == 2
    assert [p["month"] for p in row["overdue_periods"]] == [
        _month_key(_shift_month(today, -2)),
        _month_key(today),
    ]


def _expected_overdue_months(start_month: date, end_month: date) -> tuple[int, set[str], list[str]]:
    """Compute reference overdue months using REAL calendar math (add_months).

    Returns (count, year_set, ordered_month_keys)."""
    months: list[str] = []
    cursor = start_month.replace(day=1)
    end_norm = end_month.replace(day=1)
    while cursor <= end_norm:
        months.append(_month_key(cursor))
        cursor = add_months(cursor, 1)
    year_set = {m.split("-")[0] for m in months}
    return len(months), year_set, months


def test_overdue_rents_accounting_start_cross_year(
    client, owner_a_headers, unit_id, tenant_id
):
    # 跨年租约：accounting 起点在上一年，应收周期跨两个年份
    # FIXED (was May-Aug only): compute reference data with actual calendar
    # math using app.services.dates.add_months so the assertions are correct
    # in EVERY month of the year — including Jan/Feb vs Nov/Dec boundaries.
    today = date.today()
    start = _shift_month(today, -14)
    accounting_start = _shift_month(today, -8)
    end_date = add_months(today, 12).replace(month=12, day=31)
    lease_id = _create_rent_lease(
        client, owner_a_headers, unit_id, tenant_id,
        start_date=start,
        end_date=end_date,
        due_day=1,
        accounting_start_date=accounting_start,
    )
    row = _overdue_row(client, owner_a_headers, lease_id)

    # Reference computed via app.services.dates.add_months calendar math
    expected_count, expected_years, expected_month_keys = _expected_overdue_months(
        accounting_start, today
    )

    assert row["overdue_months"] == expected_count, (
        f"overdue_months={row['overdue_months']} expected={expected_count} "
        f"today={today.isoformat()} accounting_start={accounting_start.isoformat()}"
    )
    assert row["overdue_periods"][0]["month"] == expected_month_keys[0]
    assert row["overdue_periods"][-1]["month"] == expected_month_keys[-1]
    actual_years = {p["month"].split("-")[0] for p in row["overdue_periods"]}
    assert actual_years == expected_years, (
        f"year set mismatch: actual={actual_years} expected={expected_years} "
        f"months={[p['month'] for p in row['overdue_periods']]}"
    )


@pytest.mark.parametrize(
    "report_month_date,current_date,months_back,expected_cross_year",
    [
        # Dedicated boundary case: report=December Y, current=January 1st Y+1
        (
            date(2025, 12, 15),
            date(2026, 1, 1),
            8,
            True,
        ),
        # Another boundary: report=November Y, current=February Y+1
        (
            date(2025, 11, 10),
            date(2026, 2, 1),
            8,
            True,
        ),
        # Same-year case: report=February, current=September (no cross)
        (
            date(2026, 2, 1),
            date(2026, 9, 1),
            8,
            False,
        ),
    ],
    ids=["Dec→Jan_boundary", "Nov→Feb_boundary", "Feb→Sep_same_year"],
)
def test_overdue_rents_accounting_start_cross_year_dedicated_cases(
    client, owner_a_headers, unit_id, tenant_id, monkeypatch,
    report_month_date, current_date, months_back, expected_cross_year,
):
    """Dedicated parametrize cases with fixed dates + calendar math reference.

    The critical case here is month=December / current_date=January 1st next
    year: the old today.year-1 style reference was off-by-one and incorrectly
    asserted a cross-year where none existed (or vice versa depending on what
    month the test was run in). We use add_months for the golden reference.

    CRITICAL: we patch ``app.api.routers.reports.date.today`` so the endpoint
    computes overdue against *current_date* fixture (not real today) — this
    is what makes the fixed-date assertions deterministic regardless of when
    CI runs. Without this, Dec→Jan case would compute overdue from real today
    (e.g. Aug 2026) and produce ~16 months instead of the expected 9.
    """
    _patch_reports_date_today(monkeypatch, current_date)
    today = current_date
    start = add_months(today, -14)
    accounting_start = add_months(today, -months_back)
    end_date = add_months(today, 12).replace(month=12, day=31)

    lease_id = _create_rent_lease(
        client, owner_a_headers, unit_id, tenant_id,
        start_date=start,
        end_date=end_date,
        due_day=1,
        accounting_start_date=accounting_start,
    )
    row = _overdue_row(client, owner_a_headers, lease_id)

    expected_count, expected_years, expected_month_keys = _expected_overdue_months(
        accounting_start, today
    )

    assert row["overdue_months"] == expected_count
    assert row["overdue_periods"][0]["month"] == expected_month_keys[0]
    assert row["overdue_periods"][-1]["month"] == expected_month_keys[-1]

    actual_years = {p["month"].split("-")[0] for p in row["overdue_periods"]}
    assert actual_years == expected_years

    # Explicitly validate the cross-year property we care about
    if expected_cross_year:
        assert len(expected_years) >= 2, (
            f"parametrize case expected cross-year but add_months gave "
            f"only one year: {expected_years}"
        )
    else:
        assert len(expected_years) == 1, (
            f"parametrize case expected same-year but add_months gave "
            f"cross-year: {expected_years}"
        )
    # month_range from app/services/dates.py produces same calendar boundaries
    for mkey in expected_month_keys:
        first, last = month_range(mkey)
        assert first.isoformat().startswith(mkey)
        assert last.isoformat().startswith(mkey)
        assert first <= last


def test_overdue_rents_future_accounting_start_no_overdue(
    client, owner_a_headers, unit_id, tenant_id
):
    # future accounting_start_date → 当前不产生 overdue
    today = date.today()
    lease_id = _create_rent_lease(
        client, owner_a_headers, unit_id, tenant_id,
        start_date=_shift_month(today, -3),
        end_date=date(today.year + 1, 12, 31),
        due_day=1,
        accounting_start_date=_shift_month(today, 1),
    )
    resp = client.get(f"{API}/reports/overdue-rents", headers=owner_a_headers)
    assert resp.status_code == 200
    assert all(r["lease_id"] != lease_id for r in resp.json())


def test_financial_summary_accounting_start_filters_expected(
    client, owner_a_headers, unit_id, tenant_id
):
    # accounting_start 晚于查询月 → 租约不计入 expected_rent_total（coalesce 口径）
    today = date.today()
    _create_rent_lease(
        client, owner_a_headers, unit_id, tenant_id,
        start_date=_shift_month(today, -2),
        end_date=date(today.year + 1, 12, 31),
        monthly_rent="65000.00",
        accounting_start_date=_shift_month(today, 1),
    )
    resp = client.get(
        f"{API}/reports/financial-summary?month={_month()}", headers=owner_a_headers
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["expected_rent_total"] == "0.00"
    assert data["occupied_units"] == 1


def test_monthly_report_accounting_start_excludes_lease(
    client, owner_a_headers, unit_id, tenant_id
):
    # monthly 与 financial-summary 同口径：accounting_start 之前不计入
    today = date.today()
    lease_id = _create_rent_lease(
        client, owner_a_headers, unit_id, tenant_id,
        start_date=_shift_month(today, -2),
        end_date=date(today.year + 1, 12, 31),
        monthly_rent="65000.00",
        accounting_start_date=_shift_month(today, 1),
    )
    resp = client.get(f"{API}/reports/monthly?month={_month()}", headers=owner_a_headers)
    assert resp.status_code == 200
    assert all(r["lease_id"] != lease_id for r in resp.json())


def test_financial_summary_period_matches_not_received_date(client, owner_a_headers, unit_id, tenant_id):
    """Regression: collected_rent/outstanding must attribute income to its RENT
    period (YYYY-MM in description), not the received_date. A late/backdated
    payment for a past month received during the current month must not inflate
    this month's collected nor drive outstanding_rent negative."""
    today = date.today()
    start = today.replace(day=1)
    lease_id = _create_rent_lease(
        client, owner_a_headers, unit_id, tenant_id,
        start_date=start,
        end_date=date(today.year + 1, 12, 31),
        due_day=1,
    )
    prev_month = _shift_month(today, -1).strftime("%Y-%m")
    # confirmed payment for LAST month, received THIS month (late/backdated)
    _post_income(client, owner_a_headers, lease_id, "65000.00", today,
                 status="confirmed", description=f"rent {prev_month}")
    resp = client.get(f"{API}/reports/financial-summary?month={_month()}", headers=owner_a_headers)
    assert resp.status_code == 200
    data = resp.json()
    # expected this month = 65000; no income for THIS month period -> collected 0
    assert data["expected_rent_total"] == "65000.00"
    assert data["collected_rent"] == "0.00"
    assert data["outstanding_rent"] == "65000.00"
    # cash received this month is real, but it belongs to last month's period
    assert data["total_income"] == "65000.00"


def test_monthly_report_period_matches_not_received_date(client, owner_a_headers, unit_id, tenant_id):
    """Regression for /monthly: per-lease collected matches the rent period."""
    today = date.today()
    start = today.replace(day=1)
    lease_id = _create_rent_lease(
        client, owner_a_headers, unit_id, tenant_id,
        start_date=start,
        end_date=date(today.year + 1, 12, 31),
        due_day=1,
    )
    prev_month = _shift_month(today, -1).strftime("%Y-%m")
    _post_income(client, owner_a_headers, lease_id, "65000.00", today,
                 status="confirmed", description=f"rent {prev_month}")
    resp = client.get(f"{API}/reports/monthly?month={_month()}", headers=owner_a_headers)
    assert resp.status_code == 200
    row = [r for r in resp.json() if r["lease_id"] == lease_id][0]
    assert row["expected"] == "65000.00"
    assert row["collected"] == "0.00"   # last-month payment must not count for this month
    assert row["outstanding"] == "65000.00"
