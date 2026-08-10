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
