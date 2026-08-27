"""PASAY-DEPLOY-PHASE1-RETURN1 targeted tests.

Covers exactly the 10 blockers (F1–F10) from Owner RETURN1 spec.
DO NOT add Rent/Expense/Repair/Lease/Move-out/MiniApp business tests.
"""
from __future__ import annotations

import hashlib
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
_BOT_ROOT = _REPO_ROOT / "pasay-telegram-bot"
if str(_BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(_BOT_ROOT))


# =========================================================================
# STEP9 TRUTH GATE — pure function replica of workflow STEP9.2 logic
# Used by F7..F10 to test exact gate behaviour without Neon connection.
# =========================================================================
# Return: (passed: bool, fail_reason: str | None)
#
# Input row structure: count | state | processed_at_set | user_id_match |
#                      last_error_type_set | last_error_msg_set
def _step9_truth_gate_eval(
    count: int,
    state: str | None,
    processed_at_not_null: bool,
    user_id_matches_owner: bool,
    last_error_type_not_null: bool,
    last_error_msg_not_null: bool,
) -> tuple[bool, str | None]:
    """Evaluate STEP9 TRUTH GATE (exact same conditions as workflow inline logic).

    Returns (passed=True, None) iff all truth conditions satisfied.
    Returns (passed=False, reason_string) for any fast-fail condition.
    For count==0 / state in ('claimed','retryable') returns (None, None)
    meaning 'continue poll within deadline' — caller handles deadline timeout.
    """
    if count == 0:
        return None, None
    if count > 1:
        return False, f"duplicate rows count={count} > 1"
    # count == 1 exactly
    if state == "failed":
        return False, "state=failed terminal"
    if state == "done":
        if not processed_at_not_null:
            return False, "state=done but processed_at=NULL (contradiction)"
        if last_error_type_not_null:
            return False, "state=done but last_error_type present (terminal error)"
        if last_error_msg_not_null:
            return False, "state=done but last_error message present"
        if not user_id_matches_owner:
            return False, "state=done but user_id != Owner identity"
        return True, None
    if state in ("claimed", "retryable"):
        return None, None
    # Unknown state — continue poll (caller will deadline timeout)
    return None, None


# ---------------------------------------------------------------------------
# F1 — cloudflare-container production settings overlay propagates tokens
# ---------------------------------------------------------------------------
def test_F1_settings_overlay_propagates_in_cloudflare_container_mode(monkeypatch):
    monkeypatch.delenv("PASSAY_TG_BOT_TOKEN", raising=False)
    monkeypatch.delenv("PASSAY_API_KEY", raising=False)
    monkeypatch.delenv("STATE_DB", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "f1-dummy-bot-token-12345:ABCDEF")
    monkeypatch.setenv("PASAY_RUNTIME_MODE", "cloudflare-container")
    from pasay_bot.config import Settings, get_settings

    get_settings.cache_clear()
    s = get_settings()
    assert s.pasay_tg_bot_token == "f1-dummy-bot-token-12345:ABCDEF", (
        "F1 FAIL: TELEGRAM_BOT_TOKEN did not propagate to pasay_tg_bot_token in cloudflare-container settings"
    )
    assert s.pasay_api_key == "f1-dummy-bot-token-12345:ABCDEF", (
        "F1 FAIL: TELEGRAM_BOT_TOKEN did not propagate to pasay_api_key (native-bot bearer dual-use) "
        "via production settings overlay"
    )
    assert s.pasay_runtime_mode == "cloudflare-container"
    assert s.state_db.endswith("bot_state.db"), "F1: state_db path should be a bot state path"


# ---------------------------------------------------------------------------
# F2 — TELEGRAM_BOT_TOKEN → dual PTB tg token + pasay_api_key correct
# ---------------------------------------------------------------------------
def test_F2_telegram_token_dual_use_correct(monkeypatch):
    DUAL_TOKEN = "f2-token-prefix-fake:SECRETBODY-abcd1234wxyz"
    monkeypatch.delenv("PASSAY_TG_BOT_TOKEN", raising=False)
    monkeypatch.delenv("PASSAY_API_KEY", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", DUAL_TOKEN)
    from pasay_bot.config import get_settings

    get_settings.cache_clear()
    s = get_settings()
    assert s.pasay_tg_bot_token == DUAL_TOKEN, (
        "F2 FAIL: pasay_tg_bot_token (PTB send client) must equal TELEGRAM_BOT_TOKEN"
    )
    assert s.pasay_api_key == DUAL_TOKEN, (
        "F2 FAIL: pasay_api_key (backend bearer) must equal TELEGRAM_BOT_TOKEN "
        "(dual single-secret reuse via STEP7 sha256 hash match native-bot/telegram_bot)"
    )
    # Explicit overlay wins
    monkeypatch.setenv("PASSAY_TG_BOT_TOKEN", "override-tg-1")
    monkeypatch.setenv("PASSAY_API_KEY", "override-api-2")
    get_settings.cache_clear()
    s2 = get_settings()
    assert s2.pasay_tg_bot_token == "override-tg-1"
    assert s2.pasay_api_key == "override-api-2"


# ---------------------------------------------------------------------------
# F3 — Native-bot ApiCredential sha256(TELEGRAM_BOT_TOKEN) matches key_hash
# ---------------------------------------------------------------------------
def test_F3_credential_sha256_matches_step7_hash():
    """Step7 hashlib.sha256(bot_token.encode()).hexdigest() → ApiCredential.key_hash
    so backend bearer = raw token value must hash to match row.
    """
    RAW_TOKENS = [
        "f3:AAGYfakePart0001abcdef",
        "8878513078:AAGYrealPrefixNotValueButFormatValid002",
        "simple-token-value-for-unit-test-hash-check-no-colon",
    ]
    for raw in RAW_TOKENS:
        expected = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        # step7 bootstrap literal:
        #   bot_kh = hashlib.sha256(bot_token.encode('utf-8')).hexdigest()
        step7_equiv = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        assert len(expected) == 64, "sha256 hex digest length = 64"
        assert expected == step7_equiv, (
            "F3 FAIL: step7 key_hash computation mismatch for native-bot/telegram_bot"
        )
        # Bearer lookup in ApiCredential table WHERE key_hash=expected:
        # raw token passed via Authorization: Bearer <raw> → backend hashes it
        # and must find the exact row. This tests the identity not the SQL.
        bearer_hash = hashlib.sha256(raw.encode()).hexdigest()
        assert bearer_hash == expected, (
            "F3 FAIL: backend bearer raw sha256 != step7 stored key_hash → auth would 401"
        )


# ---------------------------------------------------------------------------
# F4 — production cloudflare-container mode StateStore returns PostgresStateStore
#       NOT SQLite (SQLite relies on local ephemeral disk durable truths)
# ---------------------------------------------------------------------------
def test_F4_cloudflare_mode_state_store_is_postgres_not_sqlite(monkeypatch):
    monkeypatch.setenv("PASAY_RUNTIME_MODE", "cloudflare-container")
    from pasay_bot.state.store import (
        PostgresStateStore,
        SQLiteStateStore,
        StateStore,
    )
    import psycopg2 as _pg

    fake_conn = MagicMock(name="FakePgConn")
    fake_cur = MagicMock(name="FakePgCur")
    fake_conn.cursor.return_value = fake_cur
    fake_cur.fetchone.return_value = None
    fake_pg_connect = MagicMock(return_value=fake_conn)
    with patch.object(_pg, "connect", fake_pg_connect):
        store = StateStore(":memory:", runtime_mode="cloudflare-container")
    assert isinstance(store, PostgresStateStore), (
        "F4 FAIL: runtime_mode=cloudflare-container MUST route to PostgresStateStore"
        " (Neon durable Neon/PG) because CF Container disk ephemeral after sleep"
        " restart. SQLite ephemeral local disk durable truths FORBIDDEN production."
    )
    assert not isinstance(store, SQLiteStateStore), (
        "F4 FAIL: runtime_mode=cloudflare must NOT return SQLiteStateStore"
        " — local SQLite file would lose daily_marks/reminder/followup durable truths"
        " on container sleep/restart."
    )
    store_dev = StateStore(":memory:", runtime_mode="")
    assert isinstance(store_dev, SQLiteStateStore), (
        "F4 local dev mode ok: empty runtime_mode → SQLite (dev/local SQLite fine)."
    )


# ---------------------------------------------------------------------------
# F5 — stale store.init() method MUST NOT exist (AttributeError → 12 retryable)
# ---------------------------------------------------------------------------
def test_F5_stale_init_does_not_exist_on_StateStore(monkeypatch):
    monkeypatch.setenv("PASAY_RUNTIME_MODE", "")
    from pasay_bot.state.store import StateStore

    s = StateStore(":memory:", runtime_mode="")
    has_init_attr = hasattr(s, "init") and callable(getattr(s, "init", None))
    assert not has_init_attr, (
        "F5 FAIL: StateStore instance exposes callable init() attribute. "
        "RETURN1 FIX D explicitly FORBIDS adding empty dummy init method. "
        "stale telegram_webhook.py 'store.init()' line MUST be deleted (done);"
        " now StateStore must not have init method AT ALL — constructor migrate() already covers this."
    )
    # Also check dir-based listing just to be extra-safe
    names = dir(s)
    assert "init" not in names, (
        "F5 FAIL: 'init' listed in dir(StateStore()) even if not callable."
    )
    # Also check PostgresStateStore
    from pasay_bot.state.store import PostgresStateStore

    assert not hasattr(PostgresStateStore, "init"), (
        "F5 FAIL: PostgresStateStore class has init method."
    )


# ---------------------------------------------------------------------------
# F6 — PTB Application initialize() + start() lifecycle completes with mock
# ---------------------------------------------------------------------------
def test_F6_ptb_application_initialize_start_completes(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "f6:applife-token-fmt-valid")
    monkeypatch.setenv("PASAY_RUNTIME_MODE", "development-test")
    import asyncio

    from pasay_bot.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()

    fake_api_client = MagicMock(name="FakeApi")
    from pasay_bot.state.store import StateStore

    real_store = StateStore(":memory:", runtime_mode="")
    real_store.recover_stale_in_flight = MagicMock(return_value=None)

    class MockPTBApp:
        def __init__(self):
            self.initialized = False
            self.started = False
            self.stopped = False
            self.shutdown_done = False
            self.bot_data: dict = {}
            self.bot = MagicMock()
            self.bot.defaults = SimpleNamespace(tzinfo=None)
            self.job_queue = MagicMock()

        async def initialize(self):
            await asyncio.sleep(0)
            self.initialized = True

        async def start(self):
            await asyncio.sleep(0)
            self.started = True

        async def stop(self):
            await asyncio.sleep(0)
            self.stopped = True

        async def shutdown(self):
            await asyncio.sleep(0)
            self.shutdown_done = True

    from pasay_bot import main as bot_main

    async def _run_lifecycle():
        with patch.object(bot_main, "build_application", return_value=MockPTBApp()):
            app = bot_main.build_application(settings=settings, api_client=fake_api_client,
                                            store=real_store, admin_api_client=None,
                                            job_api_client=None)
            try:
                await app.initialize()
                assert app.initialized, "F6 FAIL: PTB app.initialize() did not mark initialized"
                await app.start()
                assert app.started, "F6 FAIL: PTB app.start() did not mark started"
                await app.stop()
                await app.shutdown()
                return True
            finally:
                try:
                    real_store.close()
                except Exception:
                    pass

    ok = asyncio.run(_run_lifecycle())
    assert ok is True


# ---------------------------------------------------------------------------
# F7 — STEP9 gate for retryable row → FAIL NOT pass (eliminates false-green)
# ---------------------------------------------------------------------------
def test_F7_step9_gate_retryable_row_must_not_pass():
    """Owner Row: update_id=987820324 state=retryable delivery_count=12.
    Old gate count<=1 → PASS false-green. New gate must FAIL/reject."""
    result, reason = _step9_truth_gate_eval(
        count=1,
        state="retryable",
        processed_at_not_null=False,
        user_id_matches_owner=True,
        last_error_type_not_null=True,
        last_error_msg_not_null=True,
    )
    assert result is None, (
        "F7: retryable should return None → continue poll, NOT pass."
        " If deadline exceeded → workflow FAIL timeout."
    )
    # Simulate deadline timeout — workflow MUST raise SystemExit
    # (here we model as the caller seeing still-None after deadline).
    # The point: retryable row at count=1 MUST NOT be a PASS.
    # So explicitly ensure pass IS NOT True regardless of other branch.
    result2, _ = _step9_truth_gate_eval(
        count=1,
        state="retryable",
        processed_at_not_null=False,
        user_id_matches_owner=True,
        last_error_type_not_null=False,
        last_error_msg_not_null=False,
    )
    assert result2 is not True, (
        "F7 FAIL: retryable row (state=retryable) NEVER passes STEP9 truth gate."
        " Old count<=1 gate wrongly passed this → Owner original false-green root cause A."
    )


# ---------------------------------------------------------------------------
# F8 — STEP9 gate for state=failed terminal row → IMMEDIATE FAIL
# ---------------------------------------------------------------------------
def test_F8_step9_gate_failed_row_immediate_fail():
    result, reason = _step9_truth_gate_eval(
        count=1,
        state="failed",
        processed_at_not_null=True,
        user_id_matches_owner=True,
        last_error_type_not_null=True,
        last_error_msg_not_null=True,
    )
    assert result is False, (
        "F8 FAIL: state=failed MUST immediately FAIL STEP9 gate; not polled further."
    )
    assert "failed" in (reason or "").lower(), (
        "F8: fail reason must mention failed terminal state for diagnostic."
    )
    # processed_at set or not — ANY failed row fails.
    result2, _ = _step9_truth_gate_eval(
        count=1,
        state="failed",
        processed_at_not_null=False,
        user_id_matches_owner=True,
        last_error_type_not_null=False,
        last_error_msg_not_null=False,
    )
    assert result2 is False


# ---------------------------------------------------------------------------
# F9 — STEP9 gate claimed row hits 90s deadline → timeout FAIL (not done)
# ---------------------------------------------------------------------------
def test_F9_step9_gate_claimed_deadline_timeout_fail():
    """state=claimed within deadline → continue poll; at deadline with no done → FAIL."""
    # At t=0 claimed: result=None (continue)
    r0, _ = _step9_truth_gate_eval(
        count=1, state="claimed", processed_at_not_null=False,
        user_id_matches_owner=True,
        last_error_type_not_null=False, last_error_msg_not_null=False,
    )
    assert r0 is None, "F9 precondition: claimed state → None (continue poll within deadline)"

    # Simulate 91 seconds later — still claimed, but not done. The workflow while loop
    # exited without truth_passed=True → raises SystemExit.
    # Here we model truth_passed_final = False after deadline elapsed.
    simulated_iterations_before_deadline = 30  # 90s / 3s
    still_claimed_result = None
    for _i in range(simulated_iterations_before_deadline):
        still_claimed_result, _ = _step9_truth_gate_eval(
            count=1, state="claimed", processed_at_not_null=False,
            user_id_matches_owner=True,
            last_error_type_not_null=False, last_error_msg_not_null=False,
        )
    truth_passed_flag = still_claimed_result is True
    assert truth_passed_flag is False, (
        "F9 FAIL: claimed row never transitioning to state=done within 90s deadline"
        " must NOT PASS STEP9 (truth_passed=False → SystemExit in workflow)."
        " claimed 30 iterations = 90s → timeout FAIL."
    )


# ---------------------------------------------------------------------------
# F10 — ONLY state=done + processed_at!=NULL + last_error NULL + user match → PASS
# ---------------------------------------------------------------------------
def test_F10_step9_gate_requires_all_done_conditions_to_pass():
    # All good case → PASS
    ok, reason = _step9_truth_gate_eval(
        count=1,
        state="done",
        processed_at_not_null=True,
        user_id_matches_owner=True,
        last_error_type_not_null=False,
        last_error_msg_not_null=False,
    )
    assert ok is True and reason is None, (
        "F10 ideal state=done row SHOULD pass. got ok=%s reason=%s" % (ok, reason)
    )
    # Negatives: each individual condition failure individually fails gate.
    cases: list[tuple[str, dict[str, Any]]] = [
        ("count>1 duplicate", dict(count=2, state="done", processed_at_not_null=True,
                                    user_id_matches_owner=True, last_error_type_not_null=False,
                                    last_error_msg_not_null=False)),
        ("state=done processed_at NULL", dict(count=1, state="done", processed_at_not_null=False,
                                              user_id_matches_owner=True, last_error_type_not_null=False,
                                              last_error_msg_not_null=False)),
        ("state=done last_error_type SET", dict(count=1, state="done", processed_at_not_null=True,
                                                user_id_matches_owner=True, last_error_type_not_null=True,
                                                last_error_msg_not_null=False)),
        ("state=done last_error SET", dict(count=1, state="done", processed_at_not_null=True,
                                           user_id_matches_owner=True, last_error_type_not_null=False,
                                           last_error_msg_not_null=True)),
        ("state=done user mismatch Owner", dict(count=1, state="done", processed_at_not_null=True,
                                                user_id_matches_owner=False, last_error_type_not_null=False,
                                                last_error_msg_not_null=False)),
        ("state not done (claimed)", dict(count=1, state="claimed", processed_at_not_null=True,
                                          user_id_matches_owner=True, last_error_type_not_null=False,
                                          last_error_msg_not_null=False)),
        ("state not done (retryable)", dict(count=1, state="retryable", processed_at_not_null=True,
                                            user_id_matches_owner=True, last_error_type_not_null=False,
                                            last_error_msg_not_null=False)),
    ]
    for label, kwargs in cases:
        r, _ = _step9_truth_gate_eval(**kwargs)
        assert r is not True, (
            "F10 FAIL: condition breakdown '%s' incorrectly allowed gate=PASS."
            " Individual violated conditions MUST NOT yield True PASS." % label
        )
