API = "/api/v1"


def _create_property(client, headers):
    return client.post(
        f"{API}/properties",
        json={"name": "Audited", "address": "1 St", "city": "Pasay"},
        headers=headers,
    )


def test_create_is_audited(client, admin_headers):
    resp = _create_property(client, admin_headers)
    assert resp.status_code == 201

    logs = client.get(f"{API}/audit-logs", headers=admin_headers).json()
    assert len(logs) == 1
    assert logs[0]["table_name"] == "properties"
    assert logs[0]["record_id"] == resp.json()["id"]
    assert logs[0]["action"] == "create"


def test_update_is_audited_with_changes(client, admin_headers, property_id):
    client.patch(f"{API}/properties/{property_id}", json={"city": "Taguig"}, headers=admin_headers)
    logs = client.get(f"{API}/audit-logs", headers=admin_headers).json()
    update_log = next(l for l in logs if l["action"] == "update")
    assert update_log["changed_fields"] == {"city": ["Pasay", "Taguig"]}


def test_confirm_action_is_audited(client, admin_headers, lease_id):
    income_id = client.post(
        f"{API}/incomes",
        json={
            "lease_id": lease_id,
            "amount": "1000.00",
            "received_date": "2026-03-01",
            "status": "pending",
        },
        headers=admin_headers,
    ).json()["id"]
    client.post(f"{API}/incomes/{income_id}/confirm", headers=admin_headers)

    logs = client.get(
        f"{API}/audit-logs?table_name=incomes&record_id={income_id}", headers=admin_headers
    ).json()
    assert [l["action"] for l in logs] == ["confirm", "create"]


def test_audit_logs_admin_only(client, manager_headers):
    resp = client.get(f"{API}/audit-logs", headers=manager_headers)
    assert resp.status_code == 403


def test_audit_logs_filter(client, admin_headers):
    _create_property(client, admin_headers)
    client.post(
        f"{API}/incomes",
        json={"amount": "500.00", "received_date": "2026-04-01", "status": "pending"},
        headers=admin_headers,
    )
    logs = client.get(f"{API}/audit-logs?table_name=properties", headers=admin_headers).json()
    assert len(logs) == 1
    assert all(l["table_name"] == "properties" for l in logs)
