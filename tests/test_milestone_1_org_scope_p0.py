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
    # Paginated response: strict emptiness (fail-closed cross-org isolation)
    assert isinstance(items, dict), items
    assert items["total"] == 0, items
    assert len(items["items"]) == 0, items
    assert items["limit"] in (50, 1, 500), items
    assert items["offset"] >= 0, items
    assert not any(item.get("full_name") == "Tenant A" for item in items["items"])


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
    # Paginated response: strict emptiness (fail-closed cross-org isolation)
    assert isinstance(items, dict), items
    assert items["total"] == 0, items
    assert len(items["items"]) == 0, items
    assert items["limit"] in (50, 1, 500), items
    assert items["offset"] >= 0, items


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
    # Paginated response: strict emptiness (fail-closed cross-org isolation)
    assert isinstance(items, dict), items
    assert items["total"] == 0, items
    assert len(items["items"]) == 0, items
    assert items["limit"] in (50, 1, 500), items
    assert items["offset"] >= 0, items


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
    # Paginated response: strict emptiness (fail-closed cross-org isolation)
    assert isinstance(items, dict), items
    assert items["total"] == 0, items
    assert len(items["items"]) == 0, items
    assert items["limit"] in (50, 1, 500), items
    assert items["offset"] >= 0, items


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
    # Strict emptiness: items is a plain list from body["items"]
    assert isinstance(items, list), items
    assert len(items) == 0, items
    assert body["total"] == 0


def test_repair_t2_get_cross_org_404(client, owner_a, owner_b, org_a, org_b):
    _setup_org_b_property_and_unit(client, owner_b, org_b)
    repair_id_a = _create_org_a_repair(client, owner_a, org_a)
    owner_b_headers = _bearer(owner_b[1])

    resp_get_b = client.get(f"{API}/repairs/{repair_id_a}", headers=owner_b_headers)
    assert resp_get_b.status_code == 404


# ---------------------------------------------------------------------------
# FIX2 Blocker regressions (Owner 3 Blocker explicit HTTP/migration contracts)
# ---------------------------------------------------------------------------


def test_fix2_blocker2_income_patch_lease_id_none_http_409(
    client, owner_a, org_a
):
    """FIX2 Blocker 2: IncomeUpdate.lease_id = None (PATCH /incomes/{id})
    MUST return HTTP 409 Conflict (CrossOrgReference through the shared
    scope_exception_to_http translator), NOT an unhandled 500.

    (IncomeCreate.lease_id is already required int per Pydantic; the real
    place a caller can sneak in a None is via IncomeUpdate.lease_id:
    int | None = None on the PATCH endpoint.)
    """
    owner_a_headers = _bearer(owner_a[1])
    # Create a perfectly valid pending Income (with a real lease) first.
    lease_id_a = _create_org_a_tenant_and_lease(client, owner_a, org_a)
    resp_create = client.post(
        f"{API}/incomes",
        json={
            "lease_id": lease_id_a,
            "amount": "12000.00",
            "received_date": "2026-03-01",
            "payment_method": "cash",
            "status": "pending",
            "description": "valid income",
        },
        headers=owner_a_headers,
    )
    assert resp_create.status_code == 201, resp_create.text
    income_id = resp_create.json()["id"]

    # Then PATCH with lease_id=None explicitly (keep amount unchanged so
    # other fields don't trigger their own 422). The router should convert
    # the CrossOrgReference raised by _check_lease() to HTTP 409, never
    # let it bubble up as 500.
    resp_patch = client.patch(
        f"{API}/incomes/{income_id}",
        json={
            "lease_id": None,
            "amount": "12000.00",
            "received_date": "2026-03-01",
            "payment_method": "cash",
        },
        headers=owner_a_headers,
    )
    assert resp_patch.status_code == 409, (
        f"Expected HTTP 409 Conflict for PATCH lease_id=None, "
        f"got {resp_patch.status_code}. Body: {resp_patch.text[:500]!r}"
    )
    body = resp_patch.json()
    detail = str(body.get("detail", ""))
    assert (
        "canonical lease ownership" in detail or "lease_id is required" in detail
    ), body


def test_fix2_blocker2_income_cross_org_idempotency_replay_http_404(
    client, owner_a, owner_b, org_a, org_b
):
    """FIX2 Blocker 2: Org A creates an Income with idempotency key=K (using
    OrgA lease). OrgB then replays the SAME key=K using OrgB's OWN lease —
    must return HTTP 404 (fail-closed: idempotency hit must verify that the
    FOUND Income belongs to the CALLER's org; otherwise it looks like a miss,
    i.e. 404, not returning OrgA's record nor re-creating under same key).

    The previous M1-FIX1 code already guarded this; we keep the hard contract
    explicit here so any future regression that reopens the cross-org replay
    door (e.g. removing _assert_income_co_org before idempotency return) is
    caught in CI.
    """
    owner_a_headers = _bearer(owner_a[1])
    owner_b_headers = _bearer(owner_b[1])

    shared_key = "idem-key-fix2-001"

    # --- OrgA setup: create 1 Income with shared_key using OrgA lease ---
    lease_id_a = _create_org_a_tenant_and_lease(client, owner_a, org_a)
    resp_a_create = client.post(
        f"{API}/incomes",
        json={
            "lease_id": lease_id_a,
            "amount": "12000.00",
            "received_date": "2026-03-01",
            "payment_method": "cash",
            "status": "confirmed",
            "idempotency_key": shared_key,
            "description": "OrgA creates with shared key",
        },
        headers=owner_a_headers,
    )
    assert resp_a_create.status_code == 201, resp_a_create.text
    income_a_id = resp_a_create.json()["id"]
    # Sanity: OrgA replaying the same key returns the SAME record (HTTP 200)
    resp_a_replay = client.post(
        f"{API}/incomes",
        json={
            "lease_id": lease_id_a,
            "amount": "12000.00",
            "received_date": "2026-03-01",
            "payment_method": "cash",
            "status": "confirmed",
            "idempotency_key": shared_key,
        },
        headers=owner_a_headers,
    )
    assert resp_a_replay.status_code == 200, resp_a_replay.text
    assert resp_a_replay.json()["id"] == income_a_id

    # --- OrgB setup: create its OWN, perfectly valid lease in OrgB ---
    _property_id_b, unit_id_b = _setup_org_b_property_and_unit(
        client, owner_b, org_b
    )
    tenant_id_b_resp = client.post(
        f"{API}/tenants",
        json={
            "organization_id": org_b.id,
            "full_name": "Tenant B",
            "phone": "+639170000002",
        },
        headers=owner_b_headers,
    )
    assert tenant_id_b_resp.status_code == 201, tenant_id_b_resp.text
    tenant_id_b = tenant_id_b_resp.json()["id"]
    lease_b_resp = client.post(
        f"{API}/leases",
        json={
            "unit_id": unit_id_b,
            "tenant_id": tenant_id_b,
            "start_date": "2026-02-01",
            "end_date": "2027-01-31",
            "monthly_rent": "15000.00",
            "deposit": "30000.00",
            "status": "active",
        },
        headers=owner_b_headers,
    )
    assert lease_b_resp.status_code == 201, lease_b_resp.text
    lease_id_b = lease_b_resp.json()["id"]

    # --- ACTUAL TEST: OrgB replays shared_key with OrgB's lease_id. ---
    # OrgB's request is perfectly formed (valid OrgB lease), but the key K
    # already hit for OrgA's Income → fail-closed: MUST NOT return OrgA's
    # record as if it was OrgB's → HTTP 404.
    resp_b_replay = client.post(
        f"{API}/incomes",
        json={
            "lease_id": lease_id_b,
            "amount": "15000.00",
            "received_date": "2026-03-02",
            "payment_method": "bank_transfer",
            "status": "pending",
            "idempotency_key": shared_key,
            "description": "OrgB replay of shared key",
        },
        headers=owner_b_headers,
    )
    assert resp_b_replay.status_code == 404, (
        f"Expected HTTP 404 fail-closed on cross-org idempotency replay, "
        f"got {resp_b_replay.status_code}. Body: {resp_b_replay.text[:600]!r}"
    )
    # Belt-and-suspenders: OrgB still cannot read OrgA's record (paranoia).
    resp_peek = client.get(f"{API}/incomes/{income_a_id}", headers=owner_b_headers)
    assert resp_peek.status_code == 404
