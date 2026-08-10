from datetime import date, timedelta

API = "/api/v1"


def _month():
    return date.today().strftime("%Y-%m")


def test_financial_summary(client, admin_headers, lease_id, unit_id):
    today = date.today()
    resp = client.post(
        f"{API}/incomes",
        json={
            "lease_id": lease_id,
            "amount": "12000.00",
            "received_date": today.isoformat(),
            "payment_method": "cash",
            "status": "confirmed",
            "description": "rent",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text

    resp = client.post(
        f"{API}/expenses",
        json={
            "expense_date": today.isoformat(),
            "category": "repair",
            "amount": "2000.00",
            "payee": "Fix-It Co",
            "status": "approved",
            "unit_id": unit_id,
        },
        headers=admin_headers,
    )
    expense_id = resp.json()["id"]
    client.post(f"{API}/expenses/{expense_id}/pay", headers=admin_headers)

    resp = client.get(
        f"{API}/reports/financial-summary?month={_month()}", headers=admin_headers
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["month"] == _month()
    assert data["expected_rent_total"] == "12000.00"
    assert data["collected_rent"] == "12000.00"
    assert data["outstanding_rent"] == "0.00"
    assert data["total_income"] == "12000.00"
    assert data["total_expense"] == "2000.00"
    assert data["net_income"] == "10000.00"
    assert data["units_count"] == 1
    assert data["occupied_units"] == 1
    assert data["vacant_units"] == 0


def test_financial_summary_unit_filter(client, admin_headers, unit_id, tenant_id):
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
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text

    resp = client.get(
        f"{API}/reports/financial-summary?month={_month()}&unit_id=999999",
        headers=admin_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["expected_rent_total"] == "0.00"
    assert data["units_count"] == 0

    resp = client.get(
        f"{API}/reports/financial-summary?month={_month()}&unit_id={unit_id}",
        headers=admin_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["expected_rent_total"] == "12000.00"
    assert data["units_count"] == 1
    assert data["occupied_units"] == 1


def test_financial_summary_agent_forbidden(client, agent_headers):
    resp = client.get(f"{API}/reports/financial-summary", headers=agent_headers)
    assert resp.status_code == 403


def test_overdue_rents_report(client, admin_headers, unit_id, tenant_id):
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
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    new_lease_id = resp.json()["id"]

    resp = client.get(f"{API}/reports/overdue-rents", headers=admin_headers)
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
    client, admin_headers, unit_id, tenant_id, start_date, end_date,
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
    resp = client.post(f"{API}/leases", json=payload, headers=admin_headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _overdue_row(client, admin_headers, lease_id):
    resp = client.get(f"{API}/reports/overdue-rents", headers=admin_headers)
    assert resp.status_code == 200
    rows = [r for r in resp.json() if r["lease_id"] == lease_id]
    assert len(rows) == 1
    return rows[0]


def _post_income(client, admin_headers, lease_id, amount, received_date,
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
    resp = client.post(f"{API}/incomes", json=payload, headers=admin_headers)
    assert resp.status_code == 201, resp.text


def test_overdue_rents_current_month(client, admin_headers, unit_id, tenant_id):
    today = date.today()
    start = today.replace(day=1)
    lease_id = _create_rent_lease(
        client, admin_headers, unit_id, tenant_id,
        start_date=start,
        end_date=date(start.year + 1, 12, 31),
        due_day=1,
    )
    row = _overdue_row(client, admin_headers, lease_id)
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


def test_overdue_rents_consecutive_months(client, admin_headers, unit_id, tenant_id):
    today = date.today()
    start = _shift_month(today, -2)
    lease_id = _create_rent_lease(
        client, admin_headers, unit_id, tenant_id,
        start_date=start,
        end_date=date(start.year + 1, 12, 31),
        due_day=1,
    )
    row = _overdue_row(client, admin_headers, lease_id)
    assert row["overdue_months"] == 3
    assert row["total_outstanding"] == "195000.00"
    assert row["overdue_periods"] == [
        {"month": _month_key(_shift_month(today, -2)), "amount": "65000.00"},
        {"month": _month_key(_shift_month(today, -1)), "amount": "65000.00"},
        {"month": _month_key(today), "amount": "65000.00"},
    ]
    assert row["oldest_due_date"] == start.isoformat()


def test_overdue_rents_one_month_paid(client, admin_headers, unit_id, tenant_id):
    today = date.today()
    start = _shift_month(today, -2)
    lease_id = _create_rent_lease(
        client, admin_headers, unit_id, tenant_id,
        start_date=start,
        end_date=date(start.year + 1, 12, 31),
        due_day=1,
    )
    paid_month = _shift_month(today, -1)
    _post_income(
        client, admin_headers, lease_id, "65000.00", today,
        description=f"rent {_month_key(paid_month)}",
    )
    row = _overdue_row(client, admin_headers, lease_id)
    assert row["overdue_months"] == 2
    assert row["total_outstanding"] == "130000.00"
    assert [p["month"] for p in row["overdue_periods"]] == [
        _month_key(start),
        _month_key(today),
    ]


def test_overdue_rents_received_month_fallback(client, admin_headers, unit_id, tenant_id):
    today = date.today()
    start = _shift_month(today, -2)
    lease_id = _create_rent_lease(
        client, admin_headers, unit_id, tenant_id,
        start_date=start,
        end_date=date(start.year + 1, 12, 31),
        due_day=1,
    )
    paid_month = _shift_month(today, -1)
    _post_income(client, admin_headers, lease_id, "65000.00", paid_month)
    row = _overdue_row(client, admin_headers, lease_id)
    assert row["overdue_months"] == 2
    assert [p["month"] for p in row["overdue_periods"]] == [
        _month_key(start),
        _month_key(today),
    ]


def test_overdue_rents_pending_not_counted(client, admin_headers, unit_id, tenant_id):
    today = date.today()
    start = today.replace(day=1)
    lease_id = _create_rent_lease(
        client, admin_headers, unit_id, tenant_id,
        start_date=start,
        end_date=date(start.year + 1, 12, 31),
        due_day=1,
    )
    _post_income(
        client, admin_headers, lease_id, "65000.00", today,
        status="pending", description=f"rent {_month_key(start)}",
    )
    row = _overdue_row(client, admin_headers, lease_id)
    assert row["overdue_months"] == 1
    assert row["total_outstanding"] == "65000.00"


def test_overdue_rents_advance_future_payment(client, admin_headers, unit_id, tenant_id):
    today = date.today()
    start = _shift_month(today, -2)
    lease_id = _create_rent_lease(
        client, admin_headers, unit_id, tenant_id,
        start_date=start,
        end_date=date(start.year + 1, 12, 31),
        due_day=1,
    )
    future_month = _shift_month(today, 1)
    _post_income(
        client, admin_headers, lease_id, "65000.00", today,
        description=f"rent {_month_key(future_month)}",
    )
    row = _overdue_row(client, admin_headers, lease_id)
    assert row["overdue_months"] == 3
    assert row["total_outstanding"] == "195000.00"
    assert _month_key(future_month) not in [p["month"] for p in row["overdue_periods"]]


def test_overdue_rents_lease_started_midterm(client, admin_headers, unit_id, tenant_id):
    today = date.today()
    start = today - timedelta(days=15)
    lease_id = _create_rent_lease(
        client, admin_headers, unit_id, tenant_id,
        start_date=start,
        end_date=date(start.year + 1, 12, 31),
    )
    row = _overdue_row(client, admin_headers, lease_id)
    assert row["overdue_months"] == 1
    assert row["total_outstanding"] == "65000.00"
    assert row["overdue_periods"] == [{"month": _month_key(start), "amount": "65000.00"}]
    assert row["oldest_due_date"] == start.isoformat()


def test_overdue_rents_lease_ended(client, admin_headers, unit_id, tenant_id):
    today = date.today()
    start = _shift_month(today, -5)
    end = _shift_month(today, -2) - timedelta(days=1)
    lease_id = _create_rent_lease(
        client, admin_headers, unit_id, tenant_id,
        start_date=start,
        end_date=end,
        due_day=1,
    )
    row = _overdue_row(client, admin_headers, lease_id)
    assert row["overdue_months"] == 3
    assert row["total_outstanding"] == "195000.00"
    assert [p["month"] for p in row["overdue_periods"]] == [
        _month_key(_shift_month(today, -5)),
        _month_key(_shift_month(today, -4)),
        _month_key(_shift_month(today, -3)),
    ]


def test_monthly_report(client, admin_headers, lease_id):
    resp = client.get(f"{API}/reports/monthly?month={_month()}", headers=admin_headers)
    assert resp.status_code == 200
    rows = [r for r in resp.json() if r["lease_id"] == lease_id]
    assert len(rows) == 1
    row = rows[0]
    assert row["unit"] == "101"
    assert row["tenant"] == "Juan Dela Cruz"
    assert row["expected"] == "12000.00"
    assert row["collected"] == "0.00"
    assert row["outstanding"] == "12000.00"


def test_commission_report(client, admin_headers, manager_headers, agent, lease_id):
    rule = client.post(
        f"{API}/commission/rules",
        json={
            "name": "Rent 5%",
            "rule_type": "percentage",
            "value": "5.00",
            "agent_role": "出租",
        },
        headers=admin_headers,
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
        f"{API}/reports/commission?month={_month()}", headers=admin_headers
    )
    assert resp.status_code == 200
    rows = [r for r in resp.json() if r["agent_id"] == agent[0].id]
    assert len(rows) == 1
    row = rows[0]
    assert row["agent"] == "agent"
    assert row["rule"] == "Rent 5%"
    assert row["computed_total"] == "600.00"
    assert row["settlements"] == 1


def test_tasks_report(client, admin_headers, unit_id):
    today = date.today()
    client.post(
        f"{API}/tasks",
        json={
            "title": "Fix A",
            "unit_id": unit_id,
            "due_date": (today - timedelta(days=3)).isoformat(),
        },
        headers=admin_headers,
    )
    resp = client.post(
        f"{API}/tasks",
        json={
            "title": "Scheduled A",
            "status": "scheduled",
            "due_date": (today + timedelta(days=10)).isoformat(),
            "recurring": True,
            "interval_months": 1,
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["next_due_date"] is not None

    resp = client.get(
        f"{API}/reports/tasks?status=pending&overdue=true", headers=admin_headers
    )
    assert resp.status_code == 200
    titles = [r["title"] for r in resp.json()]
    assert "Fix A" in titles
    assert "Scheduled A" not in titles

    resp = client.get(f"{API}/reports/tasks?status=scheduled", headers=admin_headers)
    assert resp.status_code == 200
    assert [r["title"] for r in resp.json()] == ["Scheduled A"]


def test_expenses_report(client, admin_headers, unit_id):
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
            headers=admin_headers,
        )
        expense_id = resp.json()["id"]
        client.post(f"{API}/expenses/{expense_id}/pay", headers=admin_headers)

    resp = client.get(
        f"{API}/reports/expenses?month={_month()}", headers=admin_headers
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
        f"{API}/reports/expenses?month={_month()}&category=repair", headers=admin_headers
    )
    assert resp.status_code == 200
    assert resp.json()["total_amount"] == "3000.00"


def test_overdue_rents_no_accounting_start_regression(
    client, admin_headers, unit_id, tenant_id
):
    # 旧租约不传 accounting_start_date → 行为完全不变（回归）
    today = date.today()
    start = _shift_month(today, -2)
    lease_id = _create_rent_lease(
        client, admin_headers, unit_id, tenant_id,
        start_date=start,
        end_date=date(start.year + 1, 12, 31),
        due_day=1,
    )
    row = _overdue_row(client, admin_headers, lease_id)
    assert row["overdue_months"] == 3
    assert row["total_outstanding"] == "195000.00"
    assert [p["month"] for p in row["overdue_periods"]] == [
        _month_key(_shift_month(today, -2)),
        _month_key(_shift_month(today, -1)),
        _month_key(today),
    ]


def test_overdue_rents_accounting_start_excludes_history(
    client, admin_headers, unit_id, tenant_id
):
    # 租约历史已半年、accounting_start_date=本月 → 历史月份不报欠租
    today = date.today()
    start = _shift_month(today, -6)
    lease_id = _create_rent_lease(
        client, admin_headers, unit_id, tenant_id,
        start_date=start,
        end_date=date(start.year + 1, 12, 31),
        due_day=1,
        accounting_start_date=today.replace(day=1),
    )
    row = _overdue_row(client, admin_headers, lease_id)
    assert row["overdue_months"] == 1
    assert row["total_outstanding"] == "65000.00"
    assert row["overdue_periods"] == [{"month": _month_key(today), "amount": "65000.00"}]


def test_overdue_rents_accounting_start_unpaid_after(
    client, admin_headers, unit_id, tenant_id
):
    # accounting_start_date 之后未付月份 → 正常 overdue
    today = date.today()
    start = _shift_month(today, -3)
    accounting_start = _shift_month(today, -2)
    lease_id = _create_rent_lease(
        client, admin_headers, unit_id, tenant_id,
        start_date=start,
        end_date=date(start.year + 1, 12, 31),
        due_day=1,
        accounting_start_date=accounting_start,
    )
    row = _overdue_row(client, admin_headers, lease_id)
    assert row["overdue_months"] == 3
    assert row["total_outstanding"] == "195000.00"
    assert [p["month"] for p in row["overdue_periods"]] == [
        _month_key(_shift_month(today, -2)),
        _month_key(_shift_month(today, -1)),
        _month_key(today),
    ]


def test_overdue_rents_income_before_accounting_start_ignored(
    client, admin_headers, unit_id, tenant_id
):
    # accounting_start_date 之前的 confirmed income 不影响之后的月份
    today = date.today()
    start = _shift_month(today, -3)
    accounting_start = _shift_month(today, -2)
    lease_id = _create_rent_lease(
        client, admin_headers, unit_id, tenant_id,
        start_date=start,
        end_date=date(start.year + 1, 12, 31),
        due_day=1,
        accounting_start_date=accounting_start,
    )
    _post_income(
        client, admin_headers, lease_id, "65000.00", start,
        description=f"rent {_month_key(start)}",
    )
    row = _overdue_row(client, admin_headers, lease_id)
    assert row["overdue_months"] == 3
    assert row["total_outstanding"] == "195000.00"


def test_overdue_rents_income_after_accounting_start_covers_period(
    client, admin_headers, unit_id, tenant_id
):
    # accounting_start_date 之后的 confirmed income 正确覆盖对应 period
    today = date.today()
    start = _shift_month(today, -3)
    accounting_start = _shift_month(today, -2)
    lease_id = _create_rent_lease(
        client, admin_headers, unit_id, tenant_id,
        start_date=start,
        end_date=date(start.year + 1, 12, 31),
        due_day=1,
        accounting_start_date=accounting_start,
    )
    paid_month = _shift_month(today, -1)
    _post_income(
        client, admin_headers, lease_id, "65000.00", today,
        description=f"rent {_month_key(paid_month)}",
    )
    row = _overdue_row(client, admin_headers, lease_id)
    assert row["overdue_months"] == 2
    assert [p["month"] for p in row["overdue_periods"]] == [
        _month_key(_shift_month(today, -2)),
        _month_key(today),
    ]


def test_overdue_rents_accounting_start_cross_year(
    client, admin_headers, unit_id, tenant_id
):
    # 跨年租约：accounting 起点在上一年，应收周期跨两个年份
    today = date.today()
    start = _shift_month(today, -14)
    accounting_start = _shift_month(today, -8)
    lease_id = _create_rent_lease(
        client, admin_headers, unit_id, tenant_id,
        start_date=start,
        end_date=date(today.year + 1, 12, 31),
        due_day=1,
        accounting_start_date=accounting_start,
    )
    row = _overdue_row(client, admin_headers, lease_id)
    assert row["overdue_months"] == 9
    assert row["overdue_periods"][0]["month"] == _month_key(accounting_start)
    assert row["overdue_periods"][-1]["month"] == _month_key(today)
    years = {p["month"].split("-")[0] for p in row["overdue_periods"]}
    assert years == {str(today.year - 1), str(today.year)}


def test_overdue_rents_future_accounting_start_no_overdue(
    client, admin_headers, unit_id, tenant_id
):
    # future accounting_start_date → 当前不产生 overdue
    today = date.today()
    lease_id = _create_rent_lease(
        client, admin_headers, unit_id, tenant_id,
        start_date=_shift_month(today, -3),
        end_date=date(today.year + 1, 12, 31),
        due_day=1,
        accounting_start_date=_shift_month(today, 1),
    )
    resp = client.get(f"{API}/reports/overdue-rents", headers=admin_headers)
    assert resp.status_code == 200
    assert all(r["lease_id"] != lease_id for r in resp.json())


def test_financial_summary_accounting_start_filters_expected(
    client, admin_headers, unit_id, tenant_id
):
    # accounting_start 晚于查询月 → 租约不计入 expected_rent_total（coalesce 口径）
    today = date.today()
    _create_rent_lease(
        client, admin_headers, unit_id, tenant_id,
        start_date=_shift_month(today, -2),
        end_date=date(today.year + 1, 12, 31),
        monthly_rent="65000.00",
        accounting_start_date=_shift_month(today, 1),
    )
    resp = client.get(
        f"{API}/reports/financial-summary?month={_month()}", headers=admin_headers
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["expected_rent_total"] == "0.00"
    assert data["occupied_units"] == 1


def test_monthly_report_accounting_start_excludes_lease(
    client, admin_headers, unit_id, tenant_id
):
    # monthly 与 financial-summary 同口径：accounting_start 之前不计入
    today = date.today()
    lease_id = _create_rent_lease(
        client, admin_headers, unit_id, tenant_id,
        start_date=_shift_month(today, -2),
        end_date=date(today.year + 1, 12, 31),
        monthly_rent="65000.00",
        accounting_start_date=_shift_month(today, 1),
    )
    resp = client.get(f"{API}/reports/monthly?month={_month()}", headers=admin_headers)
    assert resp.status_code == 200
    assert all(r["lease_id"] != lease_id for r in resp.json())
