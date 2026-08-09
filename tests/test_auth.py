from app.models.user import User

API = "/api/v1"


def test_health_no_auth(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


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


def test_agent_cannot_create_property(client, agent_headers):
    resp = client.post(
        f"{API}/properties",
        json={"name": "Blocked", "address": "x", "city": "Pasay"},
        headers=agent_headers,
    )
    assert resp.status_code == 403
    assert resp.json() == {"detail": "Insufficient permissions"}
