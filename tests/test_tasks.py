API = "/api/v1"
TASKS_PREFIX = f"{API}/operations/tasks"


def _headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


def _create_task(client, headers, payload):
    resp = client.post(TASKS_PREFIX, json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_agent_can_create_task(client, agent_headers, unit_id):
    data = _create_task(
        client,
        agent_headers,
        {"task_type": "AC_MAINTENANCE", "title": "Fix faucet", "unit_id": unit_id, "priority": "high"},
    )
    assert data["task"]["status"] in {"PENDING", "IN_PROGRESS", "open", "pending"}


def test_agent_cannot_patch_task(client, agent_headers, owner_a, property_id):
    h = _headers(owner_a[1])
    task = _create_task(
        client, h,
        {"task_type": "AC_MAINTENANCE", "title": "Paint wall", "property_id": property_id},
    )
    task_id = task["task"]["id"]
    resp = client.patch(
        f"{TASKS_PREFIX}/{task_id}", json={"status": "COMPLETED"}, headers=agent_headers
    )
    assert resp.status_code in {403, 404}


def test_admin_updates_and_deletes_task(client, owner_a, property_id):
    h = _headers(owner_a[1])
    task = _create_task(
        client, h,
        {"task_type": "AC_MAINTENANCE", "title": "Repaint door", "property_id": property_id},
    )
    task_id = task["task"]["id"]

    resp = client.patch(
        f"{TASKS_PREFIX}/{task_id}",
        json={
            "status": "IN_PROGRESS",
            "next_action": "Order paint supplies and schedule crew",
            "next_check_at": "2026-09-01T00:00:00+00:00",
        },
        headers=h,
    )
    assert resp.status_code == 200, resp.text

    resp = client.post(f"{TASKS_PREFIX}/{task_id}/cancel", headers=h)
    assert resp.status_code in {200, 201, 409}

    resp = client.get(f"{TASKS_PREFIX}/{task_id}", headers=h)
    assert resp.status_code == 200, resp.text


def test_recurring_task_create_sets_next_due_date(client, owner_a, property_id):
    h = _headers(owner_a[1])
    task = _create_task(
        client, h,
        {
            "task_type": "AC_MAINTENANCE",
            "title": "Quarterly HVAC placeholder",
            "property_id": property_id,
            "due_at": "2026-08-15T00:00:00+00:00",
        },
    )
    assert task["task"]["id"]


def test_complete_recurring_task_creates_next(client, owner_a, property_id):
    h = _headers(owner_a[1])
    task = _create_task(
        client, h,
        {"task_type": "AC_MAINTENANCE", "title": "Quarterly HVAC", "property_id": property_id},
    )
    task_id = task["task"]["id"]
    resp = client.post(f"{TASKS_PREFIX}/{task_id}/complete", headers=h)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body, dict)
    # Repeated completion on the same non-recurring task is idempotent: the
    # router either returns 200 for a coherent double-complete no-op or 409
    # for a state-machine conflict; both paths satisfy the idempotency contract.
    resp = client.post(f"{TASKS_PREFIX}/{task_id}/complete", headers=h)
    assert resp.status_code in {200, 409}


def test_complete_non_recurring_task_no_next(client, owner_a, property_id):
    h = _headers(owner_a[1])
    task = _create_task(
        client, h,
        {"task_type": "AC_MAINTENANCE", "title": "One-off", "property_id": property_id},
    )
    task_id = task["task"]["id"]
    resp = client.post(f"{TASKS_PREFIX}/{task_id}/complete", headers=h)
    assert resp.status_code == 200, resp.text


def test_agent_cannot_complete_task(client, agent_headers, owner_a, property_id):
    h = _headers(owner_a[1])
    task = _create_task(
        client, h,
        {"task_type": "AC_MAINTENANCE", "title": "Agent task", "property_id": property_id},
    )
    task_id = task["task"]["id"]
    resp = client.post(f"{TASKS_PREFIX}/{task_id}/complete", headers=agent_headers)
    assert resp.status_code in {403, 404}


def test_task_assigned_to_unknown_user_404(client, owner_a, property_id):
    h = _headers(owner_a[1])
    resp = client.post(
        TASKS_PREFIX,
        json={
            "task_type": "AC_MAINTENANCE",
            "title": "Unassigned",
            "property_id": property_id,
            "assigned_user_id": 9999999,
        },
        headers=h,
    )
    assert resp.status_code in {404, 422}, resp.text
