from datetime import date, timedelta

API = "/api/v1"


def _lease_payload(unit_id, tenant_id, status="active"):
    return {
        "unit_id": unit_id,
        "tenant_id": tenant_id,
        "start_date": "2026-01-01",
        "end_date": "2026-12-31",
        "monthly_rent": "12000.00",
        "deposit": "24000.00",
        "status": status,
    }


def test_create_lease_occupies_unit(client, admin_headers, unit_id, tenant_id):
    resp = client.post(
        f"{API}/leases", json=_lease_payload(unit_id, tenant_id), headers=admin_headers
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "active"

    unit = client.get(f"{API}/units/{unit_id}", headers=admin_headers).json()
    assert unit["status"] == "occupied"


def test_second_active_lease_conflict(client, admin_headers, unit_id, tenant_id, lease_id):
    resp = client.post(
        f"{API}/leases", json=_lease_payload(unit_id, tenant_id), headers=admin_headers
    )
    assert resp.status_code == 409
    assert resp.json() == {"detail": "Unit is already occupied"}


def test_terminate_lease_releases_unit(client, admin_headers, unit_id, tenant_id, lease_id):
    resp = client.patch(
        f"{API}/leases/{lease_id}", json={"status": "terminated"}, headers=admin_headers
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "terminated"

    unit = client.get(f"{API}/units/{unit_id}", headers=admin_headers).json()
    assert unit["status"] == "vacant"


def test_delete_active_lease_conflict(client, admin_headers, lease_id):
    resp = client.delete(f"{API}/leases/{lease_id}", headers=admin_headers)
    assert resp.status_code == 409


def test_patch_lease_back_to_active_conflict(
    client, admin_headers, unit_id, tenant_id, lease_id
):
    # create a second (inactive) lease, then try to activate it on an occupied unit
    resp = client.post(
        f"{API}/leases",
        json=_lease_payload(unit_id, tenant_id, status="terminated"),
        headers=admin_headers,
    )
    second = resp.json()["id"]
    resp = client.patch(
        f"{API}/leases/{second}", json={"status": "active"}, headers=admin_headers
    )
    assert resp.status_code == 409


def test_update_lease_rent(client, admin_headers, lease_id):
    resp = client.patch(
        f"{API}/leases/{lease_id}", json={"monthly_rent": "13000.00"}, headers=admin_headers
    )
    assert resp.status_code == 200
    assert resp.json()["monthly_rent"] == "13000.00"


def test_get_missing_lease_404(client, admin_headers):
    resp = client.get(f"{API}/leases/99999", headers=admin_headers)
    assert resp.status_code == 404


def test_lease_due_day(client, admin_headers, unit_id, tenant_id):
    payload = _lease_payload(unit_id, tenant_id)
    payload["due_day"] = 5
    resp = client.post(f"{API}/leases", json=payload, headers=admin_headers)
    assert resp.status_code == 201
    assert resp.json()["due_day"] == 5


def test_create_lease_without_accounting_start_date_returns_null(
    client, admin_headers, unit_id, tenant_id
):
    resp = client.post(
        f"{API}/leases", json=_lease_payload(unit_id, tenant_id), headers=admin_headers
    )
    assert resp.status_code == 201
    assert resp.json()["accounting_start_date"] is None


def test_lease_accounting_start_date_roundtrip(client, admin_headers, unit_id, tenant_id):
    payload = _lease_payload(unit_id, tenant_id)
    payload["accounting_start_date"] = "2026-03-01"
    resp = client.post(f"{API}/leases", json=payload, headers=admin_headers)
    assert resp.status_code == 201, resp.text
    assert resp.json()["accounting_start_date"] == "2026-03-01"
    lease_id = resp.json()["id"]

    resp = client.get(f"{API}/leases/{lease_id}", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["accounting_start_date"] == "2026-03-01"

    resp = client.patch(
        f"{API}/leases/{lease_id}",
        json={"accounting_start_date": "2026-05-01"},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["accounting_start_date"] == "2026-05-01"

    resp = client.patch(
        f"{API}/leases/{lease_id}",
        json={"accounting_start_date": None},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["accounting_start_date"] is None


def test_accounting_start_date_after_end_422(client, admin_headers, unit_id, tenant_id):
    payload = _lease_payload(unit_id, tenant_id)
    payload["accounting_start_date"] = "2027-01-01"  # end_date is 2026-12-31
    resp = client.post(f"{API}/leases", json=payload, headers=admin_headers)
    assert resp.status_code == 422


def test_accounting_start_date_before_start_422(client, admin_headers, unit_id, tenant_id):
    payload = _lease_payload(unit_id, tenant_id)
    payload["accounting_start_date"] = "2025-12-31"  # start_date is 2026-01-01
    resp = client.post(f"{API}/leases", json=payload, headers=admin_headers)
    assert resp.status_code == 422


def test_lease_update_accounting_start_date_out_of_range_422(
    client, admin_headers, lease_id
):
    resp = client.patch(
        f"{API}/leases/{lease_id}",
        json={"accounting_start_date": "2027-01-01"},
        headers=admin_headers,
    )
    assert resp.status_code == 422

    resp = client.patch(
        f"{API}/leases/{lease_id}",
        json={"accounting_start_date": "2025-12-31"},
        headers=admin_headers,
    )
    assert resp.status_code == 422


def test_lease_future_accounting_start_date_allowed(
    client, admin_headers, unit_id, tenant_id
):
    today = date.today()
    future = today + timedelta(days=10)
    payload = {
        "unit_id": unit_id,
        "tenant_id": tenant_id,
        "start_date": (today - timedelta(days=30)).isoformat(),
        "end_date": f"{today.year + 1}-12-31",
        "monthly_rent": "12000.00",
        "deposit": "0.00",
        "status": "active",
        "accounting_start_date": future.isoformat(),
    }
    resp = client.post(f"{API}/leases", json=payload, headers=admin_headers)
    assert resp.status_code == 201, resp.text
    assert resp.json()["accounting_start_date"] == future.isoformat()
