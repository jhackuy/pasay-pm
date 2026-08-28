"""PASAY-M006 RETURN3-E Root Cause #4 regression test.

Verified fact (production SQL recovery):
    permission denied for schema public
    LINE 1: CREATE TABLE IF NOT EXISTS bs_conversations ...

Old PostgresStateStore.migrate() ran 9x CREATE TABLE IF NOT EXISTS bs_* DDL
as the unprivileged `pasay_runtime` Neon pooled role.  `pasay_runtime` must
NEVER hold `CREATE` on schema `public` (role separation: only
`neondb_owner` + alembic STEP3 migration are authoritative DDL issuers — see
ret1_postgres_bot_state_20260828 migration).  Old store.migrate() DDL path
produced SQLSTATE 42501 InsufficientPrivilege inside Cloudflare Container
StateStore constructor at every /start synthetic smoke → STEP9 retryable
forever → 90s deadline FAIL STOP (Run 33157913897).

Focused assertions here:
  1. PostgresStateStore constructor (migrate) never issues ANY DDL statement
     (CREATE/ALTER/DROP/GRANT/REVOKE) — only information_schema SELECTs +
     recover_stale_in_flight UPDATE on bs_idempotency_keys.
  2. If a regressed code path attempted CREATE TABLE the simulated pasay_runtime
     role would raise psycopg2.errors.InsufficientPrivilege (SQLSTATE 42501)
     — confirming role separation stays fail-closed.
  3. All StateStore operations exercised by bot handler cmd_start (and its
     downstream handler call sites: store.get_conversation, save_conversation,
     get_v2_context, save_v2_context, get_user_default_method,
     set_user_default_method, mark_daily, is_marked_daily, remember_group,
     list_known_groups, save_rent_status_selector, get_rent_status_selector,
     record_reminder_delivery, get_reminder_delivery, record_followup_delivery,
     get_followup_delivery, insert_idempotency_if_absent, get_idempotency,
     update_idempotency) succeed after migration-verification-only init.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
_BOT_ROOT = _REPO_ROOT / "pasay-telegram-bot"
if str(_BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(_BOT_ROOT))


class _FakePgError(Exception):
    """Duck-type stand-in for psycopg2.errors.InsufficientPrivilege (42501)."""
    pgcode = "42501"


_BS_TABLES_EXPECTED = (
    "bs_conversations",
    "bs_idempotency_keys",
    "bs_user_defaults",
    "bs_rent_status_selectors",
    "bs_v2_context",
    "bs_known_groups",
    "bs_daily_marks",
    "bs_reminder_deliveries",
    "bs_followup_deliveries",
)

_CREATED_RESPONSES = {name: (1,) for name in _BS_TABLES_EXPECTED}


def _install_fake_psycopg2_env(
    deny_create: bool = True,
    info_responses: dict[str, tuple] | None = None,
):
    """Replace the REAL psycopg2.connect with a fake implementation.

    Returns the global fake state dict. Caller must still wrap in patch.object.
    """
    import psycopg2 as _real_pg  # noqa: F401  (used for namespace)

    info_ok = dict(info_responses) if info_responses is not None else dict(_CREATED_RESPONSES)
    state: dict[str, Any] = {
        "deny_create": deny_create,
        "info_ok": info_ok,
        "executed_sql": [],
    }

    def _cursor_factory(conn):
        cur = MagicMock(name="FakeCur")
        cur._last_select_result = None
        cur.rowcount = 0

        def _execute(sql, params=None):
            s_stripped = str(sql).strip()
            state["executed_sql"].append(s_stripped)
            s_low = s_stripped.lower()
            if state["deny_create"] and s_low.startswith((
                "create ", "alter ", "drop ", "grant ", "revoke ",
            )):
                raise _FakePgError(
                    "permission denied for schema public — pasay_runtime holds NO "
                    "CREATE privilege (SQLSTATE 42501)"
                )
            if "information_schema.tables" in s_low:
                if params and len(params) == 1:
                    tbl = str(params[0])
                    cur._last_select_result = (1,) if tbl in state["info_ok"] else None
                    return
                cur._last_select_result = None
                return
            if s_low.startswith("update bs_idempotency_keys"):
                cur.rowcount = 0
                return
            if s_low.startswith("select "):
                cur._last_select_result = None
                return
            if s_low.startswith("insert "):
                cur.rowcount = 1
                return
            if s_low.startswith("delete "):
                cur.rowcount = 0
                return

        def _fetchone():
            return cur._last_select_result

        def _fetchall():
            return [] if cur._last_select_result is None else [cur._last_select_result]

        cur.execute.side_effect = _execute
        cur.fetchone.side_effect = _fetchone
        cur.fetchall.side_effect = _fetchall
        return cur

    def _connect(dsn=None, **kw):
        conn = MagicMock(name="FakePgConn")
        conn.autocommit = False
        conn.cursor = MagicMock(return_value=_cursor_factory(conn))
        conn.closed = 0
        conn.rollback = MagicMock()
        conn.commit = MagicMock()
        conn.close = MagicMock()
        return conn

    state["fake_connect"] = _connect
    return state


# ---------------------------------------------------------------------------
# F1 — PostgresStateStore.migrate() never issues DDL.
# ---------------------------------------------------------------------------
def test_M006_F1_postgres_store_migrate_issues_no_ddl_only_select_exists(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://pasay_runtime:***@pooler/db")
    import psycopg2 as _pg

    from pasay_bot.state.store import PostgresStateStore, StateStore

    st = _install_fake_psycopg2_env(deny_create=True)
    with patch.object(_pg, "connect", st["fake_connect"]):
        store = StateStore("unused", runtime_mode="cloudflare-container")

    assert isinstance(store, PostgresStateStore)
    executed = st["executed_sql"]
    any_create = any(
        s.lower().startswith(("create ", "alter ", "drop ", "grant ", "revoke "))
        for s in executed
    )
    assert not any_create, (
        f"M006 FAIL: PostgresStateStore issued DDL through pasay_runtime.\n"
        f"First 5 executed statements:\n  "
        + "\n  ".join(executed[:5])
    )
    info_probes = [s for s in executed if "information_schema.tables" in s.lower()]
    assert len(info_probes) == 9, (
        f"M006 FAIL: expected 9 bs_* table existence probes, got {len(info_probes)}"
    )


# ---------------------------------------------------------------------------
# F2 — regressed CREATE TABLE DDL raises SQLSTATE 42501 fail-closed.
# ---------------------------------------------------------------------------
def test_M006_F2_create_ddl_fails_with_sqlstate_42501_pasay_runtime(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://pasay_runtime:***@pooler/db")
    import psycopg2 as _pg

    from pasay_bot.state import store as store_mod

    class _RegressedPostgresStateStore(store_mod.PostgresStateStore):
        def migrate(self):
            cur = self._cursor()
            cur.execute(
                "CREATE TABLE IF NOT EXISTS bs_conversations "
                "(chat_id TEXT NOT NULL, user_id TEXT NOT NULL, "
                "PRIMARY KEY (chat_id, user_id))"
            )

    st = _install_fake_psycopg2_env(deny_create=True)
    with patch.object(_pg, "connect", st["fake_connect"]):
        with pytest.raises(_FakePgError) as excinfo:
            inst = object.__new__(_RegressedPostgresStateStore)
            inst.__init__("unused")
    joined = " ".join(str(a) for a in excinfo.value.args)
    assert "42501" in joined or "42501" in str(getattr(excinfo.value, "pgcode", "")), (
        "M006 FAIL: regressed CREATE TABLE DDL did not raise SQLSTATE 42501 "
        "(pasay_runtime role fail-closed check broken)."
    )


# ---------------------------------------------------------------------------
# F3 — All /start-relevant StateStore operations succeed after verify-only
#      init.
# ---------------------------------------------------------------------------
def test_M006_F3_start_handler_state_store_operations_succeed_no_ddl(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://pasay_runtime:***@pooler/db")
    import psycopg2 as _pg

    from pasay_bot.roles import Role, role_for_telegram_id
    from pasay_bot.state.store import PostgresStateStore, StateStore

    st = _install_fake_psycopg2_env(deny_create=True)
    with patch.object(_pg, "connect", st["fake_connect"]):
        store: PostgresStateStore = StateStore(  # type: ignore[assignment]
            "unused", runtime_mode="cloudflare-container"
        )

    assert role_for_telegram_id(5177241442) is Role.OWNER

    owner = 5177241442
    store.save_conversation(owner, owner, "greeting", payload={"l": "zh"}, nonce="n1", ttl_seconds=900)
    store.get_conversation(owner, owner)
    store.delete_conversation(owner, owner)
    store.save_v2_context(owner, owner, {"v": 1}, ttl_seconds=3600)
    store.get_v2_context(owner, owner)
    store.clear_v2_context(owner, owner)
    store.set_user_default_method(owner, "Bank")
    store.get_user_default_method(owner)
    assert store.mark_daily("home_page_seen_2026-08-28") is True
    store.is_marked_daily("home_page_seen_2026-08-28")
    store.remember_group(-987654321, title="Owners")
    store.list_known_groups()
    store.save_rent_status_selector("sel1", -1, owner, ["a", "b", "c"])
    store.get_rent_status_selector("sel1", -1, owner)
    store.record_reminder_delivery("e1", "2026-08-28", target_user=str(owner),
                                   destination="-100", message_id="99")
    store.get_reminder_delivery("e1", "2026-08-28")
    store.record_followup_delivery("tk1", unit_id="u1", date="2026-08-28",
                                   target_user=str(owner), destination="-100",
                                   message_id="101")
    store.get_followup_delivery("tk1")
    key = "ingest:u:5177241442:uid:8907979495:step9"
    store.insert_idempotency_if_absent(key, "webhook_ingest", resource="telegram_owner_start")
    store.insert_idempotency_if_absent(key, "webhook_ingest", resource="telegram_owner_start")
    store.get_idempotency(key)
    store.update_idempotency(key, "done", resource="telegram_owner_start", result={"ok": True})
    store.recover_stale_in_flight()

    executed = st["executed_sql"]
    any_create = any(
        s.lower().startswith(("create ", "alter ", "drop ", "grant ", "revoke "))
        for s in executed
    )
    assert not any_create, "M006 FAIL: DDL issued after start-handler operation chain."


# ---------------------------------------------------------------------------
# F4 — missing bs_* tables fail-closed with alembic-ownership RuntimeError.
# ---------------------------------------------------------------------------
def test_M006_F4_missing_bs_tables_failclosed_no_ddl_attempt(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://pasay_runtime:***@pooler/db")
    import psycopg2 as _pg

    from pasay_bot.state.store import StateStore

    info_gap = {n: (1,) for n in _BS_TABLES_EXPECTED}
    del info_gap["bs_conversations"]
    del info_gap["bs_v2_context"]
    st = _install_fake_psycopg2_env(deny_create=True, info_responses=info_gap)
    with patch.object(_pg, "connect", st["fake_connect"]):
        with pytest.raises(RuntimeError) as excinfo:
            StateStore("unused", runtime_mode="cloudflare-container")
    msg = str(excinfo.value)
    assert "MUST NOT issue CREATE TABLE DDL" in msg, (
        f"M006 FAIL: missing tables did not raise the alembic-ownership error. Got:\n{msg}"
    )
    assert "bs_conversations" in msg and "bs_v2_context" in msg

