"""Idempotency guard: in_flight/done/failed transitions + double-click replay."""
import time

from pasay_bot.state.idempotency import IdempotencyGuard
from pasay_bot.state.store import StateStore


def test_acquire_new_then_in_flight():
    store = StateStore(":memory:")
    guard = IdempotencyGuard(store)
    assert guard.acquire("ik:cnf:abc") == "new"
    assert store.get_idempotency("ik:cnf:abc")["status"] == "in_flight"
    store.close()


def test_double_click_blocked():
    store = StateStore(":memory:")
    guard = IdempotencyGuard(store)
    assert guard.acquire("ik:cnf:abc") == "new"
    assert guard.acquire("ik:cnf:abc") == "in_flight"
    store.close()


def test_settle_done_replays_stored_result():
    store = StateStore(":memory:")
    guard = IdempotencyGuard(store)
    key = "ik:cnf:abc"
    assert guard.acquire(key) == "new"
    guard.settle(key, {"id": 1, "status": "confirmed"}, resource="1")
    assert guard.acquire(key) == "done"
    assert guard.result(key) == {"id": 1, "status": "confirmed"}
    store.close()


def test_fail_allows_retry():
    store = StateStore(":memory:")
    guard = IdempotencyGuard(store)
    key = "ik:cnf:abc"
    assert guard.acquire(key) == "new"
    guard.fail(key)
    assert store.get_idempotency(key)["status"] == "failed"
    assert guard.acquire(key) == "retry"
    store.close()


def test_failed_then_done_after_retry():
    store = StateStore(":memory:")
    guard = IdempotencyGuard(store)
    key = "ik:cnf:abc"
    guard.acquire(key)
    guard.fail(key)
    assert guard.acquire(key) == "retry"
    guard.settle(key, {"id": 2, "status": "confirmed"}, resource="2")
    assert guard.acquire(key) == "done"
    assert guard.result(key)["id"] == 2
    store.close()


def test_expired_key_pruned():
    store = StateStore(":memory:")
    guard = IdempotencyGuard(store)
    key = "ik:cnf:expired"
    store.insert_idempotency_if_absent(key, "income", "", "in_flight", ttl_seconds=-1)
    assert guard.acquire(key) == "new"  # expired -> treated as fresh
    store.close()


def test_kind_and_resource_recorded():
    store = StateStore(":memory:")
    guard = IdempotencyGuard(store)
    key = "ik:rv:5:abcd"
    guard.acquire(key, kind="income", resource="5")
    row = store.get_idempotency(key)
    assert row["kind"] == "income"
    assert row["resource"] == "5"
    store.close()


# --- F3: stale in_flight aging + startup recovery ---

def test_stale_in_flight_allows_retry():
    store = StateStore(":memory:")
    guard = IdempotencyGuard(store)
    key = "ik:cnf:stale"
    store.insert_idempotency_if_absent(key, "income", "", "in_flight", ttl_seconds=300)
    store._conn.execute(
        "UPDATE idempotency_keys SET created_at=? WHERE key=?",
        (str(int(time.time()) - 200), key),
    )
    store._conn.commit()
    assert guard.acquire(key) == "retry"  # not blocked forever
    store.close()


def test_fresh_in_flight_still_blocks():
    store = StateStore(":memory:")
    guard = IdempotencyGuard(store)
    key = "ik:cnf:fresh"
    assert guard.acquire(key) == "new"
    assert guard.acquire(key) == "in_flight"
    store.close()


def test_recover_stale_in_flight_on_startup(tmp_path):
    db = str(tmp_path / "recover.db")
    store = StateStore(db)
    store.insert_idempotency_if_absent("ik:cnf:stale", "income", "", "in_flight", ttl_seconds=300)
    store._conn.execute("UPDATE idempotency_keys SET created_at=?", (str(int(time.time()) - 200),))
    store._conn.commit()
    store.close()

    store2 = StateStore(db)  # startup recovery marks stale in_flight -> failed
    row = store2.get_idempotency("ik:cnf:stale")
    assert row is not None and row["status"] == "failed"
    store2.close()


# --- F9: corrupt result_json must not crash reads ---

def test_corrupt_result_json_returns_none_result():
    store = StateStore(":memory:")
    key = "ik:cnf:corrupt"
    store.insert_idempotency_if_absent(key, "income", "", "done")
    store._conn.execute(
        "UPDATE idempotency_keys SET result_json=? WHERE key=?", ("{not json", key)
    )
    store._conn.commit()
    row = store.get_idempotency(key)
    assert row is not None and row["status"] == "done" and row["result"] is None
    store.close()
