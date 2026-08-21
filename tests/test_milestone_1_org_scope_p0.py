"""PASAY-MILESTONE-001 Targeted Regression Tests.

覆盖: Income / Expense / Lease / Repair / Tenant 5 个域的跨组织访问 fail-closed 验证。

Pattern (每个域做 T1 + T2)：
- T1 list_isolation: owner_a 创建 OrgA 的 1 条记录；owner_b GET /api/v1/<domain> list
  -> 返回空列表，看不见 OrgA 数据（数据泄露路径封闭证据）
- T2 get_cross_org_404: owner_a 创建 OrgA 记录 -> id=X；owner_b GET /api/v1/<domain>/X
  -> 必须 HTTP 404（fail-closed 404，not 403 — 因为存在性不泄露）
- T3 (only for create payloads): owner_a 尝试用 owner_b 下的 org_id 创建记录
  -> 必须 403/409（CrossOrgReference）
"""
from __future__ import annotations

import pytest

API = "/api/v1"


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _setup_org_a_property_and_unit(client, owner_a, org_a):
    """owner_a 在 OrgA 创建 property + unit，返回 (property_id, unit_id)。"""
    owner_a_headers = _bearer(owner_a[1])
    resp = client.post(
        f"{API}/properties",
        json={
            "organization_id": org_a.id,
            "name": "Sunset Tower A",
            "address": "1 Roxas Blvd",
            "city": "Pasay",
            "total_units": 4,
        },
        headers=owner_a_headers,
    )
    assert resp.status_code == 201, resp.text
    property_id_a = resp.json()["id"]

    resp = client.post(
        f"{API}/units",
        json={
            "property_id": property_id_a,
            "unit_number": "101",
            "floor": "1",
            "size_sqm": "32.50",
            "monthly_rent": "12000.00",
            "status": "vacant",
        },
        headers=owner_a_headers,
    )
    assert resp.status_code == 201, resp.text
    unit_id_a = resp.json()["id"]
    return property_id_a, unit_id_a


def _setup_org_b_property_and_unit(client, owner_b, org_b):
    """owner_b 在 OrgB 创建 property2 + unit2，返回 (property_id, unit_id)。"""
    owner_b_headers = _bearer(owner_b[1])
    resp = client.post(
        f"{API}/properties",
        json={
            "organization_id": org_b.id,
            "name": "Sunrise Tower B",
            "address": "2 Ayala Ave",
            "city": "Makati",
            "total_units": 2,
        },
        headers=owner_b_headers,
    )
    assert resp.status_code == 201, resp.text
    property_id_b = resp.json()["id"]

    resp = client.post(
        f"{API}/units",
        json={
            "property_id": property_id_b,
            "unit_number": "201",
            "floor": "2",
            "size_sqm": "45.00",
            "monthly_rent": "18000.00",
            "status": "vacant",
        },
        headers=owner_b_headers,
    )
    assert resp.status_code == 201, resp.text
    unit_id_b = resp.json()["id"]
    return property_id_b, unit_id_b


# ---------------------------------------------------------------------------
# Tenant: T1 (list_isolation), T2 (get_cross_org_404), T3 (cross-org create)
# ---------------------------------------------------------------------------


def test_tenant_t1_list_isolation(client, owner_a, owner_b, org_a, org_b):
    _setup_org_a_property_and_unit(client, owner_a, org_a)
    _setup_org_b_property_and_unit(client, owner_b, org_b)
    owner_a_headers = _bearer(owner_a[1])
    owner_b_headers = _bearer(owner_b[1])

    resp = client.post(
        f"{API}/tenants",
        json={
            "organization_id": org_a.id,
            "full_name": "Tenant A",
        },
        headers=owner_a_headers,
    )
    assert resp.status_code == 201, resp.text

    resp_list_b = client.get(f"{API}/tenants", headers=owner_b_headers)
    assert resp_list_b.status_code == 200, resp_list_b.text
    items = resp_list_b.json()
    assert len(items) == 0
    assert not any(item.get("full_name") == "Tenant A" for item in items)


def test_tenant_t2_get_cross_org_404(client, owner_a, owner_b, org_a, org_b):
    _setup_org_a_property_and_unit(client, owner_a, org_a)
    _setup_org_b_property_and_unit(client, owner_b, org_b)
    owner_a_headers = _bearer(owner_a[1])
    owner_b_headers = _bearer(owner_b[1])

    resp = client.post(
        f"{API}/tenants",
        json={
            "organization_id": org_a.id,
            "full_name": "Tenant A",
        },
        headers=owner_a_headers,
    )
    assert resp.status_code == 201, resp.text
    id_t_a = resp.json()["id"]

    resp_get_b = client.get(f"{API}/tenants/{id_t_a}", headers=owner_b_headers)
    assert resp_get_b.status_code == 404


def test_tenant_t3_create_cross_org_reference_blocked(
    client, owner_a, owner_b, org_a, org_b
):
    """Bonus T3: owner_a 尝试用 org_b.id 创建 Tenant -> 403 (ScopeBlocked)."""
    _setup_org_a_property_and_unit(client, owner_a, org_a)
    _setup_org_b_property_and_unit(client, owner_b, org_b)
    owner_a_headers = _bearer(owner_a[1])

    resp = client.post(
        f"{API}/tenants",
        json={
            "organization_id": org_b.id,
            "full_name": "Tenant Cross Org",
        },
        headers=owner_a_headers,
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Lease: T1 (list_isolation), T2 (get_cross_org_404)
# ---------------------------------------------------------------------------


def _create_org_a_tenant_and_lease(client, owner_a, org_a):
    owner_a_headers = _bearer(owner_a[1])
    _property_id_a, unit_id_a = _setup_org_a_property_and_unit(client, owner_a, org_a)

    resp = client.post(
        f"{API}/tenants",
        json={
            "organization_id": org_a.id,
            "full_name": "Tenant A",
        },
        headers=owner_a_headers,
    )
    assert resp.status_code == 201, resp.text
    tenant_id_a = resp.json()["id"]

    resp = client.post(
        f"{API}/leases",
        json={
            "unit_id": unit_id_a,
            "tenant_id": tenant_id_a,
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
            "monthly_rent": "12000.00",
            "deposit": "24000.00",
            "status": "active",
        },
        headers=owner_a_headers,
    )
    assert resp.status_code == 201, resp.text
    lease_id_a = resp.json()["id"]
    return lease_id_a


def test_lease_t1_list_isolation(client, owner_a, owner_b, org_a, org_b):
    _setup_org_b_property_and_unit(client, owner_b, org_b)
    _create_org_a_tenant_and_lease(client, owner_a, org_a)
    owner_b_headers = _bearer(owner_b[1])

    resp_list_b = client.get(f"{API}/leases", headers=owner_b_headers)
    assert resp_list_b.status_code == 200, resp_list_b.text
    items = resp_list_b.json()
    assert len(items) == 0


def test_lease_t2_get_cross_org_404(client, owner_a, owner_b, org_a, org_b):
    _setup_org_b_property_and_unit(client, owner_b, org_b)
    lease_id_a = _create_org_a_tenant_and_lease(client, owner_a, org_a)
    owner_b_headers = _bearer(owner_b[1])

    resp_get_b = client.get(f"{API}/leases/{lease_id_a}", headers=owner_b_headers)
    assert resp_get_b.status_code == 404


# ---------------------------------------------------------------------------
# Income: T1 (list_isolation), T2 (get_cross_org_404)
# ---------------------------------------------------------------------------


def _create_org_a_confirmed_income(client, owner_a, org_a):
    owner_a_headers = _bearer(owner_a[1])
    lease_id_a = _create_org_a_tenant_and_lease(client, owner_a, org_a)

    resp = client.post(
        f"{API}/incomes",
        json={
            "lease_id": lease_id_a,
            "amount": "12000.00",
            "received_date": "2026-02-01",
            "payment_method": "cash",
            "status": "confirmed",
            "description": "rent Feb 2026",
        },
        headers=owner_a_headers,
    )
    assert resp.status_code == 201, resp.text
    income_id_a = resp.json()["id"]
    return income_id_a


def test_income_t1_list_isolation(client, owner_a, owner_b, org_a, org_b):
    _setup_org_b_property_and_unit(client, owner_b, org_b)
    _create_org_a_confirmed_income(client, owner_a, org_a)
    owner_b_headers = _bearer(owner_b[1])

    resp_list_b = client.get(f"{API}/incomes", headers=owner_b_headers)
    assert resp_list_b.status_code == 200, resp_list_b.text
    items = resp_list_b.json()
    assert len(items) == 0


def test_income_t2_get_cross_org_404(client, owner_a, owner_b, org_a, org_b):
    _setup_org_b_property_and_unit(client, owner_b, org_b)
    income_id_a = _create_org_a_confirmed_income(client, owner_a, org_a)
    owner_b_headers = _bearer(owner_b[1])

    resp_get_b = client.get(f"{API}/incomes/{income_id_a}", headers=owner_b_headers)
    assert resp_get_b.status_code == 404


# ---------------------------------------------------------------------------
# Expense: T1 (list_isolation), T2 (get_cross_org_404)
# ---------------------------------------------------------------------------


def _create_org_a_pending_expense(client, owner_a, org_a):
    owner_a_headers = _bearer(owner_a[1])
    _property_id_a, unit_id_a = _setup_org_a_property_and_unit(client, owner_a, org_a)

    resp = client.post(
        f"{API}/expenses",
        json={
            "expense_date": "2026-02-05",
            "category": "repair",
            "amount": "5000.00",
            "payee": "Fix-It Co",
            "description": "AC repair",
            "unit_id": unit_id_a,
            "status": "pending",
        },
        headers=owner_a_headers,
    )
    assert resp.status_code == 201, resp.text
    expense_id_a = resp.json()["id"]
    return expense_id_a


def test_expense_t1_list_isolation(client, owner_a, owner_b, org_a, org_b):
    _setup_org_b_property_and_unit(client, owner_b, org_b)
    _create_org_a_pending_expense(client, owner_a, org_a)
    owner_b_headers = _bearer(owner_b[1])

    resp_list_b = client.get(f"{API}/expenses", headers=owner_b_headers)
    assert resp_list_b.status_code == 200, resp_list_b.text
    items = resp_list_b.json()
    assert len(items) == 0


def test_expense_t2_get_cross_org_404(client, owner_a, owner_b, org_a, org_b):
    _setup_org_b_property_and_unit(client, owner_b, org_b)
    expense_id_a = _create_org_a_pending_expense(client, owner_a, org_a)
    owner_b_headers = _bearer(owner_b[1])

    resp_get_b = client.get(f"{API}/expenses/{expense_id_a}", headers=owner_b_headers)
    assert resp_get_b.status_code == 404


# ---------------------------------------------------------------------------
# Repair: T1 (list_isolation), T2 (get_cross_org_404)
# ---------------------------------------------------------------------------


def _create_org_a_repair(client, owner_a, org_a):
    owner_a_headers = _bearer(owner_a[1])
    _property_id_a, unit_id_a = _setup_org_a_property_and_unit(client, owner_a, org_a)

    resp = client.post(
        f"{API}/repairs",
        json={
            "issue": "漏水",
            "unit_id": unit_id_a,
            "created_source": "manual",
        },
        headers=owner_a_headers,
    )
    assert resp.status_code == 201, resp.text
    repair_id_a = resp.json()["id"]
    return repair_id_a


def test_repair_t1_list_isolation(client, owner_a, owner_b, org_a, org_b):
    _setup_org_b_property_and_unit(client, owner_b, org_b)
    _create_org_a_repair(client, owner_a, org_a)
    owner_b_headers = _bearer(owner_b[1])

    resp_list_b = client.get(f"{API}/repairs", headers=owner_b_headers)
    assert resp_list_b.status_code == 200, resp_list_b.text
    body = resp_list_b.json()
    items = body["items"]
    assert len(items) == 0
    assert body["total"] == 0


def test_repair_t2_get_cross_org_404(client, owner_a, owner_b, org_a, org_b):
    _setup_org_b_property_and_unit(client, owner_b, org_b)
    repair_id_a = _create_org_a_repair(client, owner_a, org_a)
    owner_b_headers = _bearer(owner_b[1])

    resp_get_b = client.get(f"{API}/repairs/{repair_id_a}", headers=owner_b_headers)
    assert resp_get_b.status_code == 404
