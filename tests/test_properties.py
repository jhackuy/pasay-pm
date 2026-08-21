API = "/api/v1"


def test_create_property(client, admin_headers, org_a):
    resp = client.post(
        f"{API}/properties",
        json={"name": "Seaside", "address": "12 Beach Rd", "city": "Pasay", "total_units": 3, "organization_id": org_a.id},
        headers=admin_headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Seaside"
    assert body["city"] == "Pasay"
    assert body["is_active"] is True


def test_manager_can_create_property(client, manager_headers, org_a):
    resp = client.post(
        f"{API}/properties",
        json={"name": "Manager Co.", "address": "x", "city": "Pasay", "total_units": 1, "organization_id": org_a.id},
        headers=manager_headers,
    )
    assert resp.status_code == 201


def test_list_and_get_property(client, admin_headers, property_id):
    resp = client.get(f"{API}/properties", headers=admin_headers)
    assert resp.status_code == 200
    assert [p["id"] for p in resp.json()] == [property_id]

    resp = client.get(f"{API}/properties/{property_id}", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["name"] == "Sunset Tower"


def test_update_property(client, admin_headers, property_id):
    resp = client.patch(
        f"{API}/properties/{property_id}", json={"city": "Manila"}, headers=admin_headers
    )
    assert resp.status_code == 200
    assert resp.json()["city"] == "Manila"


def test_delete_property_soft(client, admin_headers, property_id):
    resp = client.delete(f"{API}/properties/{property_id}", headers=admin_headers)
    assert resp.status_code == 200
    resp = client.get(f"{API}/properties/{property_id}", headers=admin_headers)
    assert resp.status_code == 404


def test_get_missing_property_404(client, admin_headers):
    resp = client.get(f"{API}/properties/99999", headers=admin_headers)
    assert resp.status_code == 404


def test_create_unit_and_read(client, admin_headers, property_id):
    resp = client.post(
        f"{API}/units",
        json={
            "property_id": property_id,
            "unit_number": "A-1",
            "monthly_rent": "15000.00",
            "status": "vacant",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201
    assert resp.json()["monthly_rent"] == "15000.00"

    resp = client.get(f"{API}/units/{resp.json()['id']}", headers=admin_headers)
    assert resp.status_code == 200


def test_create_unit_unknown_property_404(client, admin_headers):
    resp = client.post(
        f"{API}/units",
        json={"property_id": 99999, "unit_number": "X", "monthly_rent": "100.00"},
        headers=admin_headers,
    )
    assert resp.status_code == 404


def test_update_and_delete_unit(client, admin_headers, unit_id):
    resp = client.patch(
        f"{API}/units/{unit_id}", json={"status": "maintenance"}, headers=admin_headers
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "maintenance"

    resp = client.delete(f"{API}/units/{unit_id}", headers=admin_headers)
    assert resp.status_code == 200
    resp = client.get(f"{API}/units/{unit_id}", headers=admin_headers)
    assert resp.status_code == 404


def test_agent_can_read_properties(client, agent_headers, property_id):
    resp = client.get(f"{API}/properties", headers=agent_headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 1
