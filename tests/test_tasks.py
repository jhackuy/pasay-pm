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


def test_recurring_task_create_sets_next_due_date(client, admin_headers):
    resp = client.post(
        f"{API}/tasks",
        json={
            "title": "Quarterly HVAC",
            "recurring": True,
            "interval_months": 3,
            "due_date": "2026-08-15",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["recurring"] is True
    assert data["interval_months"] == 3
    assert data["next_due_date"] == "2026-11-15"


def test_complete_recurring_task_creates_next(client, admin_headers):
    resp = client.post(
        f"{API}/tasks",
        json={
            "title": "Quarterly HVAC",
            "recurring": True,
            "interval_months": 3,
            "due_date": "2026-08-15",
        },
        headers=admin_headers,
    )
    task_id = resp.json()["id"]

    resp = client.post(f"{API}/tasks/{task_id}/complete", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["completed"]["status"] == "completed"
    assert data["completed"]["completed_at"] is not None
    assert data["completed"]["last_completed_at"] is not None
    next_task = data["next"]
    assert next_task is not None
    assert next_task["status"] == "scheduled"
    assert next_task["recurring"] is True
    assert next_task["interval_months"] == 3
    assert next_task["due_date"] == "2026-11-15"
    assert next_task["next_due_date"] == "2027-02-15"

    # completing the same task twice conflicts
    resp = client.post(f"{API}/tasks/{task_id}/complete", headers=admin_headers)
    assert resp.status_code == 409


def test_complete_non_recurring_task_no_next(client, admin_headers):
    task_id = client.post(
        f"{API}/tasks", json={"title": "One-off"}, headers=admin_headers
    ).json()["id"]
    resp = client.post(f"{API}/tasks/{task_id}/complete", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["completed"]["status"] == "completed"
    assert data["next"] is None


def test_agent_cannot_complete_task(client, agent_headers):
    task_id = client.post(
        f"{API}/tasks", json={"title": "Agent task"}, headers=agent_headers
    ).json()["id"]
    resp = client.post(f"{API}/tasks/{task_id}/complete", headers=agent_headers)
    assert resp.status_code == 403


def test_task_assigned_to_unknown_user_404(client, admin_headers):
    resp = client.post(
        f"{API}/tasks",
        json={"title": "Unassigned", "assigned_to": 999999},
        headers=admin_headers,
    )
    assert resp.status_code == 404
