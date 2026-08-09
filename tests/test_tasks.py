API = "/api/v1"


def test_agent_can_create_task(client, agent_headers, unit_id):
    resp = client.post(
        f"{API}/tasks",
        json={"title": "Fix faucet", "unit_id": unit_id, "priority": "high"},
        headers=agent_headers,
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "open"


def test_agent_cannot_patch_task(client, agent_headers):
    resp = client.post(
        f"{API}/tasks", json={"title": "Paint wall"}, headers=agent_headers
    )
    task_id = resp.json()["id"]
    resp = client.patch(f"{API}/tasks/{task_id}", json={"status": "completed"}, headers=agent_headers)
    assert resp.status_code == 403


def test_admin_updates_and_deletes_task(client, admin_headers):
    task_id = client.post(
        f"{API}/tasks", json={"title": "Repaint door"}, headers=admin_headers
    ).json()["id"]

    resp = client.patch(
        f"{API}/tasks/{task_id}", json={"status": "in_progress"}, headers=admin_headers
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "in_progress"

    resp = client.delete(f"{API}/tasks/{task_id}", headers=admin_headers)
    assert resp.status_code == 200
    resp = client.get(f"{API}/tasks/{task_id}", headers=admin_headers)
    assert resp.status_code == 404
