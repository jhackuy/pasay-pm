from decimal import Decimal

from app.services.commission_engine import compute_settlement

API = "/api/v1"


class _Rule:
    def __init__(self, rule_type, value):
        self.rule_type = rule_type
        self.value = value


def test_engine_percentage():
    result = compute_settlement(None, _Rule("percentage", Decimal("10")), Decimal("12000.00"))
    assert result == Decimal("1200.00")


def test_engine_flat():
    result = compute_settlement(None, _Rule("flat", Decimal("1500.00")), Decimal("12000.00"))
    assert result == Decimal("1500.00")


def test_engine_rounding_half_up():
    result = compute_settlement(None, _Rule("percentage", Decimal("10")), Decimal("12000.55"))
    assert result == Decimal("1200.06")


def _rule_payload(rule_type="percentage", value="10"):
    return {"name": "Rent commission", "rule_type": rule_type, "value": value, "agent_role": "出租"}


def test_create_rule_admin_ok(client, admin_headers):
    resp = client.post(f"{API}/commission/rules", json=_rule_payload(), headers=admin_headers)
    assert resp.status_code == 201
    assert resp.json()["rule_type"] == "percentage"


def test_create_rule_manager_forbidden(client, manager_headers):
    resp = client.post(f"{API}/commission/rules", json=_rule_payload(), headers=manager_headers)
    assert resp.status_code == 403


def test_settlement_computed_by_engine(client, admin_headers, agent, lease_id):
    rule_id = client.post(
        f"{API}/commission/rules", json=_rule_payload(), headers=admin_headers
    ).json()["id"]
    # 10% of lease monthly rent 12000.00
    resp = client.post(
        f"{API}/commission/settlements",
        json={"agent_id": agent[0].id, "lease_id": lease_id, "rule_id": rule_id, "notes": "Q1"},
        headers=admin_headers,
    )
    assert resp.status_code == 201
    assert resp.json()["computed_amount"] == "1200.00"
    assert resp.json()["status"] == "pending"


def test_client_cannot_force_computed_amount(client, admin_headers, agent, lease_id):
    rule_id = client.post(
        f"{API}/commission/rules", json=_rule_payload(value="5"), headers=admin_headers
    ).json()["id"]
    resp = client.post(
        f"{API}/commission/settlements",
        json={
            "agent_id": agent[0].id,
            "lease_id": lease_id,
            "rule_id": rule_id,
            "computed_amount": "99999.00",  # ignored: engine computes the real value
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201
    assert resp.json()["computed_amount"] == "600.00"


def test_settlement_inactive_rule_conflict(client, admin_headers, agent, lease_id):
    rule_id = client.post(
        f"{API}/commission/rules", json=_rule_payload(), headers=admin_headers
    ).json()["id"]
    client.patch(
        f"{API}/commission/rules/{rule_id}", json={"is_active": False}, headers=admin_headers
    )
    resp = client.post(
        f"{API}/commission/settlements",
        json={"agent_id": agent[0].id, "lease_id": lease_id, "rule_id": rule_id},
        headers=admin_headers,
    )
    assert resp.status_code == 409


def test_settlement_confirm_flow(client, admin_headers, agent, lease_id):
    rule_id = client.post(
        f"{API}/commission/rules", json=_rule_payload(), headers=admin_headers
    ).json()["id"]
    settlement_id = client.post(
        f"{API}/commission/settlements",
        json={"agent_id": agent[0].id, "lease_id": lease_id, "rule_id": rule_id},
        headers=admin_headers,
    ).json()["id"]

    resp = client.post(
        f"{API}/commission/settlements/{settlement_id}/confirm", headers=admin_headers
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "confirmed"

    resp = client.post(
        f"{API}/commission/settlements/{settlement_id}/confirm", headers=admin_headers
    )
    # Financial-safety V1.1: replay of settlement confirm returns the current
    # confirmed state instead of a conflict.
    assert resp.status_code == 200
    assert resp.json()["status"] == "confirmed"


def test_agent_sees_only_own_settlements(
    client, admin_headers, agent_headers, agent, manager, lease_id
):
    rule_id = client.post(
        f"{API}/commission/rules", json=_rule_payload(), headers=admin_headers
    ).json()["id"]
    own = client.post(
        f"{API}/commission/settlements",
        json={"agent_id": agent[0].id, "lease_id": lease_id, "rule_id": rule_id},
        headers=admin_headers,
    ).json()["id"]
    other = client.post(
        f"{API}/commission/settlements",
        json={"agent_id": manager[0].id, "lease_id": lease_id, "rule_id": rule_id},
        headers=admin_headers,
    ).json()["id"]

    resp = client.get(f"{API}/commission/settlements", headers=agent_headers)
    assert resp.status_code == 200
    ids = [s["id"] for s in resp.json()]
    assert own in ids
    assert other not in ids

    # and the agent sees the lease linked to their settlement
    leases = client.get(f"{API}/leases", headers=agent_headers).json()
    assert [l["id"] for l in leases] == [lease_id]
