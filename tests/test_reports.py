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
