"""PASay V1.1 financial-safety idempotency tests.

Runs against the REAL PostgreSQL test database (``pasay_pm_test``), no mocks:
- sequential x10 and concurrent x10 replays of every financial command
- timeout-after-commit replay (DB committed, HTTP response lost)
- stale-callback replay
- the system invariant: N identical commands (sequential or concurrent)
  produce the same final DB state as executing the command once.

Concurrency is executed through a real uvicorn ASGI server bound to the test
DB with per-request sessions, driven by a ThreadPoolExecutor.
"""
from __future__ import annotations

import concurrent.futures
import threading
import time

import httpx
import pytest
import uvicorn
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.database import get_db
from app.main import app
from app.models.lease import Lease, LeaseStatus
from app.models.property import Property, Unit, UnitStatus
from app.models.tenant import Tenant

API = "/api/v1"

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _income_payload(lease_id, key, amount="12000.00", status="pending"):
    return {
        "lease_id": lease_id,
        "amount": amount,
        "received_date": "2026-02-01",
        "payment_method": "cash",
        "status": status,
        "description": "rent Feb",
        "idempotency_key": key,
    }


def _expense_payload(status="pending"):
    return {
        "expense_date": "2026-02-05",
        "category": "repair",
        "amount": "5000.00",
        "payee": "Fix-It Co",
        "description": "AC repair",
        "status": status,
    }


def _audit_count(db, table_name, action, record_id=None):
    sql = (
        "SELECT count(*) FROM audit_logs WHERE table_name=:t AND action=:a"
        + (" AND record_id=:rid" if record_id is not None else "")
    )
    params = {"t": table_name, "a": action}
    if record_id is not None:
        params["rid"] = record_id
    return db.execute(text(sql), params).scalar()


@pytest.fixture()
def http_server(db_session, test_engine):
    """Start a real uvicorn server on the test DB with per-request sessions."""
    Session = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 20
        while not server.started:
            if time.time() > deadline:
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture()
def lease_id(db_session):
    """Lease data created directly in the test DB (never through the API), so
    the conftest ``client`` fixture never clobbers the per-request dependency
    override that ``http_server`` installs."""
    prop = Property(name="Sunset Tower", address="1 Roxas Blvd", city="Pasay", total_units=4)
    db_session.add(prop)
    db_session.flush()
    unit = Unit(
        property_id=prop.id,
        unit_number="101",
        floor="1",
        size_sqm="32.50",
        monthly_rent="12000.00",
        status=UnitStatus.vacant,
    )
    tenant = Tenant(full_name="Juan Dela Cruz", phone="+639170000000")
    db_session.add_all([unit, tenant])
    db_session.flush()
    lease = Lease(
        unit_id=unit.id,
        tenant_id=tenant.id,
        start_date="2026-01-01",
        end_date="2026-12-31",
        monthly_rent="12000.00",
        deposit="24000.00",
        status=LeaseStatus.active,
    )
    db_session.add(lease)
    db_session.commit()
    db_session.refresh(lease)
    return lease.id


def _concurrent(worker, n=10):
    """Run ``worker(i)`` on n threads and return results in order."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
        return [f.result() for f in [ex.submit(worker, i) for i in range(n)]]


def _new_client(base_url, api_key):
    return httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=30.0)


def _final_state(test_engine, key, income_id):
    """Business-state fingerprint for one idempotent create+confirm event."""
    Session = sessionmaker(bind=test_engine, expire_on_commit=False)
    db = Session()
    try:
        rows = db.execute(
            text("SELECT count(*) FROM incomes WHERE idempotency_key=:k"), {"k": key}
        ).scalar()
        status = db.execute(
            text("SELECT status FROM incomes WHERE idempotency_key=:k"), {"k": key}
        ).scalar()
        create_audits = _audit_count(db, "incomes", "create", income_id)
        confirm_audits = _audit_count(db, "incomes", "confirm", income_id)
        return (rows, status, create_audits, confirm_audits)
    finally:
        db.close()


# --------------------------------------------------------------------------
# income create idempotency
# --------------------------------------------------------------------------


def test_create_income_same_key_sequential_x10(
    http_server, admin_headers, lease_id, test_engine
):
    base = http_server
    key = f"seq-{time.time_ns()}"
    statuses, ids = [], []
    with _new_client(base, admin_headers["Authorization"].split()[-1]) as c:
        for _ in range(10):
            resp = c.post(f"{API}/incomes", json=_income_payload(lease_id, key))
            statuses.append(resp.status_code)
            assert resp.status_code in (200, 201), resp.text
            ids.append(resp.json()["id"])
    assert statuses[0] == 201
    assert all(s == 200 for s in statuses[1:])
    assert len(set(ids)) == 1

    Session = sessionmaker(bind=test_engine, expire_on_commit=False)
    db = Session()
    try:
        rows = db.execute(
            text("SELECT count(*) FROM incomes WHERE idempotency_key=:k"), {"k": key}
        ).scalar()
        create_audits = _audit_count(db, "incomes", "create", ids[0])
    finally:
        db.close()
    assert rows == 1
    assert create_audits == 1


def test_create_income_same_key_concurrent_x10(
    http_server, admin_headers, lease_id, test_engine
):
    key = f"con-{time.time_ns()}"
    statuses, ids = [], []

    def worker(i):
        with _new_client(http_server, admin_headers["Authorization"].split()[-1]) as c:
            resp = c.post(f"{API}/incomes", json=_income_payload(lease_id, key))
            return resp.status_code, resp.json().get("id")

    results = _concurrent(worker)
    for code, _id in results:
        assert code in (200, 201)
        ids.append(_id)
    assert len(set(ids)) == 1

    Session = sessionmaker(bind=test_engine, expire_on_commit=False)
    db = Session()
    try:
        rows = db.execute(
            text("SELECT count(*) FROM incomes WHERE idempotency_key=:k"), {"k": key}
        ).scalar()
        create_audits = _audit_count(db, "incomes", "create", ids[0])
    finally:
        db.close()
    assert rows == 1  # UNIQUE backstop: exactly one row landed
    assert create_audits == 1


# --------------------------------------------------------------------------
# income confirm / reverse
# --------------------------------------------------------------------------


def test_income_confirm_sequential_and_concurrent_x10(
    http_server, admin_headers, lease_id, test_engine
):
    with _new_client(http_server, admin_headers["Authorization"].split()[-1]) as c:
        # sequential x10 on one income
        inc1 = c.post(f"{API}/incomes", json=_income_payload(lease_id, f"cf-s-{time.time_ns()}")).json()
        for _ in range(10):
            resp = c.post(f"{API}/incomes/{inc1['id']}/confirm")
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == "confirmed"
        # concurrent x10 on a second income
        inc2 = c.post(f"{API}/incomes", json=_income_payload(lease_id, f"cf-c-{time.time_ns()}")).json()

        def worker(i):
            with _new_client(http_server, admin_headers["Authorization"].split()[-1]) as cc:
                return cc.post(f"{API}/incomes/{inc2['id']}/confirm")

        resps = _concurrent(worker)
        for r in resps:
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "confirmed"

    Session = sessionmaker(bind=test_engine, expire_on_commit=False)
    db = Session()
    try:
        for inc_id in (inc1["id"], inc2["id"]):
            status = db.execute(
                text("SELECT status FROM incomes WHERE id=:i"), {"i": inc_id}
            ).scalar()
            assert status == "confirmed"
            assert _audit_count(db, "incomes", "confirm", inc_id) == 1
    finally:
        db.close()


def test_income_reverse_sequential_and_concurrent_x10(
    http_server, admin_headers, lease_id, test_engine
):
    with _new_client(http_server, admin_headers["Authorization"].split()[-1]) as c:
        # create as confirmed (admin), then reverse sequential x10
        inc1 = c.post(
            f"{API}/incomes", json=_income_payload(lease_id, f"rv-s-{time.time_ns()}", status="confirmed")
        ).json()
        for _ in range(10):
            resp = c.post(f"{API}/incomes/{inc1['id']}/reverse")
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == "reversed"
        # concurrent x10 on a second income
        inc2 = c.post(
            f"{API}/incomes", json=_income_payload(lease_id, f"rv-c-{time.time_ns()}", status="confirmed")
        ).json()

        def worker(i):
            with _new_client(http_server, admin_headers["Authorization"].split()[-1]) as cc:
                return cc.post(f"{API}/incomes/{inc2['id']}/reverse")

        resps = _concurrent(worker)
        for r in resps:
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "reversed"

    Session = sessionmaker(bind=test_engine, expire_on_commit=False)
    db = Session()
    try:
        for inc_id in (inc1["id"], inc2["id"]):
            status = db.execute(
                text("SELECT status FROM incomes WHERE id=:i"), {"i": inc_id}
            ).scalar()
            assert status == "reversed"
            assert _audit_count(db, "incomes", "reverse", inc_id) == 1
    finally:
        db.close()


# --------------------------------------------------------------------------
# expense approve / reject / pay / reverse
# --------------------------------------------------------------------------


def test_expense_approve_reject_pay_reverse_sequential_and_concurrent_x10(
    http_server, admin_headers, manager_headers, test_engine
):
    admin_key = admin_headers["Authorization"].split()[-1]
    with _new_client(http_server, admin_key) as c:
        def _mk_expense():
            resp = c.post(f"{API}/expenses", json=_expense_payload(), headers=manager_headers)
            assert resp.status_code == 201, resp.text
            return resp.json()["id"]

        # --- approve: sequential x10 then concurrent x10 ---
        e1 = _mk_expense()
        for _ in range(10):
            r = c.post(f"{API}/expenses/{e1}/approve")
            assert r.status_code == 200 and r.json()["status"] == "approved"
        e2 = _mk_expense()

        def approve(i):
            with _new_client(http_server, admin_key) as cc:
                return cc.post(f"{API}/expenses/{e2}/approve")

        for r in _concurrent(approve):
            assert r.status_code == 200 and r.json()["status"] == "approved"

        # --- reject: concurrent x10 ---
        e3 = _mk_expense()

        def reject(i):
            with _new_client(http_server, admin_key) as cc:
                return cc.post(f"{API}/expenses/{e3}/reject")

        for r in _concurrent(reject):
            assert r.status_code == 200 and r.json()["status"] == "rejected"

        # --- pay: concurrent x10 (approve first) ---
        e4 = _mk_expense()
        assert c.post(f"{API}/expenses/{e4}/approve").json()["status"] == "approved"

        def pay(i):
            with _new_client(http_server, admin_key) as cc:
                return cc.post(f"{API}/expenses/{e4}/pay")

        for r in _concurrent(pay):
            assert r.status_code == 200 and r.json()["status"] == "paid"

        # --- reverse: concurrent x10 (approve+pay first) ---
        e5 = _mk_expense()
        c.post(f"{API}/expenses/{e5}/approve")
        c.post(f"{API}/expenses/{e5}/pay")

        def reverse_exp(i):
            with _new_client(http_server, admin_key) as cc:
                return cc.post(f"{API}/expenses/{e5}/reverse")

        for r in _concurrent(reverse_exp):
            assert r.status_code == 200 and r.json()["status"] == "reversed"

    Session = sessionmaker(bind=test_engine, expire_on_commit=False)
    db = Session()
    try:
        assert db.execute(text("SELECT status FROM expenses WHERE id=:i"), {"i": e1}).scalar() == "approved"
        assert db.execute(text("SELECT status FROM expenses WHERE id=:i"), {"i": e2}).scalar() == "approved"
        assert db.execute(text("SELECT status FROM expenses WHERE id=:i"), {"i": e3}).scalar() == "rejected"
        assert db.execute(text("SELECT status FROM expenses WHERE id=:i"), {"i": e4}).scalar() == "paid"
        assert db.execute(text("SELECT status FROM expenses WHERE id=:i"), {"i": e5}).scalar() == "reversed"
        # each transition audited exactly once per expense
        assert _audit_count(db, "expenses", "approve", e1) == 1
        assert _audit_count(db, "expenses", "approve", e2) == 1
        assert _audit_count(db, "expenses", "reject", e3) == 1
        assert _audit_count(db, "expenses", "pay", e4) == 1
        assert _audit_count(db, "expenses", "reverse", e5) == 1
    finally:
        db.close()


# --------------------------------------------------------------------------
# commission settlement confirm
# --------------------------------------------------------------------------


def _rule(client, headers):
    resp = client.post(
        f"{API}/commission/rules",
        json={"name": "Rent commission", "rule_type": "percentage", "value": "10", "agent_role": "出租"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_settlement_confirm_sequential_and_concurrent_x10(
    http_server, admin_headers, agent, lease_id, test_engine
):
    admin_key = admin_headers["Authorization"].split()[-1]
    with _new_client(http_server, admin_key) as c:
        rule_id = _rule(c, admin_headers)
        s1 = c.post(
            f"{API}/commission/settlements",
            json={"agent_id": agent[0].id, "lease_id": lease_id, "rule_id": rule_id},
            headers=admin_headers,
        ).json()["id"]
        for _ in range(10):
            r = c.post(f"{API}/commission/settlements/{s1}/confirm")
            assert r.status_code == 200 and r.json()["status"] == "confirmed"
        s2 = c.post(
            f"{API}/commission/settlements",
            json={"agent_id": agent[0].id, "lease_id": lease_id, "rule_id": rule_id},
            headers=admin_headers,
        ).json()["id"]

        def confirm(i):
            with _new_client(http_server, admin_key) as cc:
                return cc.post(f"{API}/commission/settlements/{s2}/confirm")

        for r in _concurrent(confirm):
            assert r.status_code == 200 and r.json()["status"] == "confirmed"

    Session = sessionmaker(bind=test_engine, expire_on_commit=False)
    db = Session()
    try:
        for sid in (s1, s2):
            status = db.execute(
                text("SELECT status FROM commission_settlements WHERE id=:i"), {"i": sid}
            ).scalar()
            assert status == "confirmed"
            assert _audit_count(db, "commission_settlements", "confirm", sid) == 1
    finally:
        db.close()


# --------------------------------------------------------------------------
# timeout-after-commit + stale callback replay
# --------------------------------------------------------------------------


def test_timeout_after_commit_replay(http_server, admin_headers, lease_id, test_engine):
    """DB committed but HTTP response lost: retry with the same key must
    return the existing row, never a second one."""
    key = f"timeout-{time.time_ns()}"
    with _new_client(http_server, admin_headers["Authorization"].split()[-1]) as c:
        first = c.post(f"{API}/incomes", json=_income_payload(lease_id, key))
        assert first.status_code == 201
        income_id = first.json()["id"]
        # simulate the client never receiving the response: retry same key
        retry = c.post(f"{API}/incomes", json=_income_payload(lease_id, key))
        assert retry.status_code == 200
        assert retry.json()["id"] == income_id

        # timeout-after-commit on confirm as well
        conf1 = c.post(f"{API}/incomes/{income_id}/confirm")
        assert conf1.status_code == 200
        conf2 = c.post(f"{API}/incomes/{income_id}/confirm")
        assert conf2.status_code == 200 and conf2.json()["status"] == "confirmed"

    Session = sessionmaker(bind=test_engine, expire_on_commit=False)
    db = Session()
    try:
        assert db.execute(
            text("SELECT count(*) FROM incomes WHERE idempotency_key=:k"), {"k": key}
        ).scalar() == 1
        assert _audit_count(db, "incomes", "create", income_id) == 1
        assert _audit_count(db, "incomes", "confirm", income_id) == 1
    finally:
        db.close()


def test_stale_callback_replay(http_server, admin_headers, lease_id, test_engine):
    """A stale card replayed after the event fully landed must not re-create
    anything and must return the current state."""
    key = f"stale-{time.time_ns()}"
    with _new_client(http_server, admin_headers["Authorization"].split()[-1]) as c:
        created = c.post(f"{API}/incomes", json=_income_payload(lease_id, key))
        assert created.status_code == 201
        income_id = created.json()["id"]
        assert c.post(f"{API}/incomes/{income_id}/confirm").json()["status"] == "confirmed"

        # stale replay of the whole flow: create (same key) + confirm
        replay = c.post(f"{API}/incomes", json=_income_payload(lease_id, key))
        assert replay.status_code == 200
        assert replay.json()["id"] == income_id
        assert replay.json()["status"] == "confirmed"
        replay_confirm = c.post(f"{API}/incomes/{income_id}/confirm")
        assert replay_confirm.status_code == 200
        assert replay_confirm.json()["status"] == "confirmed"

    Session = sessionmaker(bind=test_engine, expire_on_commit=False)
    db = Session()
    try:
        assert db.execute(
            text("SELECT count(*) FROM incomes WHERE idempotency_key=:k"), {"k": key}
        ).scalar() == 1
        assert _audit_count(db, "incomes", "create", income_id) == 1
        assert _audit_count(db, "incomes", "confirm", income_id) == 1
    finally:
        db.close()


# --------------------------------------------------------------------------
# system invariant: N identical commands == executing once
# --------------------------------------------------------------------------


def test_invariant_n_identical_commands_equal_one(
    http_server, admin_headers, lease_id, test_engine
):
    """N identical commands (sequential or concurrent) must leave the same
    final business state as executing the command exactly once."""
    with _new_client(http_server, admin_headers["Authorization"].split()[-1]) as c:
        # once
        key1 = f"once-{time.time_ns()}"
        inc_once = c.post(f"{API}/incomes", json=_income_payload(lease_id, key1)).json()
        c.post(f"{API}/incomes/{inc_once['id']}/confirm")
        state_once = _final_state(test_engine, key1, inc_once["id"])

        # sequential x10
        key2 = f"seq-{time.time_ns()}"
        inc_seq = c.post(f"{API}/incomes", json=_income_payload(lease_id, key2)).json()
        for _ in range(10):
            c.post(f"{API}/incomes", json=_income_payload(lease_id, key2))
            c.post(f"{API}/incomes/{inc_seq['id']}/confirm")
        state_seq = _final_state(test_engine, key2, inc_seq["id"])

        # concurrent x10
        key3 = f"conc-{time.time_ns()}"

        def full_flow(i):
            with _new_client(http_server, admin_headers["Authorization"].split()[-1]) as cc:
                created = cc.post(f"{API}/incomes", json=_income_payload(lease_id, key3)).json()
                cc.post(f"{API}/incomes/{created['id']}/confirm")
                return created["id"]

        ids = _concurrent(full_flow)
        state_concurrent = _final_state(test_engine, key3, ids[0])

    assert len(set(ids)) == 1
    assert state_once == (1, "confirmed", 1, 1)
    assert state_seq == state_once
    assert state_concurrent == state_once
