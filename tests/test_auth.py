from app.models.user import User

API = "/api/v1"


def test_health_no_auth(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    # Core contract: the backend declares itself healthy.
    assert body["status"] == "ok"
    # V1.3 PASAY-WEBHOOK-ARCH-P0-001: /health now additionally surfaces a
    # ``telegram_webhook`` diagnostic sub-object so operators can see the
    # webhook subsystem's state without prying into logs.
    assert "telegram_webhook" in body
    diag = body["telegram_webhook"]
    assert set(diag.keys()) >= {
        "webhook_configured",
        "telegram_bot_token_configured",
        "window_seconds",
        "states_24h",
        "recent_errors",
        "total_processed_done",
    }


def test_auth_valid_key_returns_client_info(client, admin, admin_headers):
    resp = client.post(f"{API}/auth", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["username"] == "admin"
    assert body["role"] == "admin"
    assert body["id"] == admin[0].id


def test_auth_invalid_key_rejected(client):
    resp = client.post(f"{API}/auth", headers={"Authorization": "Bearer wrong-key"})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Invalid API key"}


def test_missing_header_rejected(client):
    resp = client.get(f"{API}/properties")
    assert resp.status_code == 401


def test_inactive_user_rejected(client, admin, admin_headers, db_session):
    user = db_session.query(User).filter(User.id == admin[0].id).first()
    user.is_active = False
    db_session.commit()
    resp = client.post(f"{API}/auth", headers=admin_headers)
    assert resp.status_code == 401


def test_agent_cannot_create_property(client, agent_headers, db_session):
    from app.models.membership import Organization
    org_b = Organization(name="Org-B-Isolated", display_name="No agent membership")
    db_session.add(org_b)
    db_session.flush()
    resp = client.post(
        f"{API}/properties",
        json={"name": "Blocked", "address": "x", "city": "Pasay",
              "total_units": 1, "organization_id": org_b.id},
        headers=agent_headers,
    )
    assert resp.status_code == 403
    _d = resp.json().get("detail", "")
    assert "permission" in _d.lower() or "membership" in _d.lower()
