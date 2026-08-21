API = "/api/v1"


def test_create_tenant(client, admin_headers, org_a):
    resp = client.post(
        f"{API}/tenants",
        json={"full_name": "Maria Santos", "phone": "+639171111111", "email": "maria@example.com", "organization_id": org_a.id},
        headers=admin_headers,
    )
    assert resp.status_code == 201
    assert resp.json()["full_name"] == "Maria Santos"


def test_list_and_get_tenant(client, admin_headers, tenant_id):
    resp = client.get(f"{API}/tenants", headers=admin_headers)
    assert resp.status_code == 200
    assert [t["id"] for t in resp.json()] == [tenant_id]

    resp = client.get(f"{API}/tenants/{tenant_id}", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["full_name"] == "Juan Dela Cruz"


def test_update_tenant(client, admin_headers, tenant_id):
    resp = client.patch(
        f"{API}/tenants/{tenant_id}", json={"phone": "+639172222222"}, headers=admin_headers
    )
    assert resp.status_code == 200
    assert resp.json()["phone"] == "+639172222222"


def test_delete_tenant(client, admin_headers, tenant_id):
    resp = client.delete(f"{API}/tenants/{tenant_id}", headers=admin_headers)
    assert resp.status_code == 200
    resp = client.get(f"{API}/tenants/{tenant_id}", headers=admin_headers)
    assert resp.status_code == 404
