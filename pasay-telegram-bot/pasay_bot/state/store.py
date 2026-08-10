"""Local SQLite state: conversations (TTL) + idempotency keys.

Standard-library sqlite3 only (zero extra deps). This is bot-local state —
never PostgreSQL, never through the API.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  chat_id   TEXT NOT NULL,
  user_id   TEXT NOT NULL,
  state     TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  nonce     TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  PRIMARY KEY (chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS idempotency_keys (
  key         TEXT PRIMARY KEY,
  kind        TEXT NOT NULL,
  resource    TEXT NOT NULL DEFAULT '',
  status      TEXT NOT NULL,
  result_json TEXT,
  created_at  TEXT NOT NULL,
  expires_at  TEXT NOT NULL
);
"""

DEFAULT_CONVERSATION_TTL = 900        # 15 minutes
DEFAULT_IDEMPOTENCY_TTL = 7 * 86400   # 7 days (longer than card TTL)
DEFAULT_IN_FLIGHT_TTL = 120           # in_flight stale window (crash recovery)


class StateStore:
    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self.migrate()

    def migrate(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            # Startup recovery (F3): leftover in_flight rows from a crashed
            # process must not lock a card for the full 7-day TTL. Stale rows
            # become failed, so the next click is allowed to retry.
            self.recover_stale_in_flight()
            self._conn.commit()

    def recover_stale_in_flight(self, max_age_seconds: int = DEFAULT_IN_FLIGHT_TTL) -> int:
        """Mark in_flight idempotency keys older than ``max_age_seconds`` as
        failed so retries are possible after a crash/restart."""
        now = int(time.time())
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE idempotency_keys
                SET status='failed'
                WHERE status='in_flight' AND ? - CAST(created_at AS INTEGER) > ?
                """,
                (now, max_age_seconds),
            )
            self._conn.commit()
            return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- conversations ---
    def save_conversation(
        self,
        chat_id: Any,
        user_id: Any,
        state: str,
        payload: Optional[dict] = None,
        nonce: str = "",
        ttl_seconds: int = DEFAULT_CONVERSATION_TTL,
    ) -> None:
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO conversations
                  (chat_id, user_id, state, payload_json, nonce, updated_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET
                  state=excluded.state, payload_json=excluded.payload_json,
                  nonce=excluded.nonce, updated_at=excluded.updated_at,
                  expires_at=excluded.expires_at
                """,
                (
                    str(chat_id),
                    str(user_id),
                    state,
                    json.dumps(payload or {}, ensure_ascii=False),
                    nonce,
                    str(now),
                    str(now + ttl_seconds),
                ),
            )
            self._conn.commit()

    def get_conversation(self, chat_id: Any, user_id: Any) -> Optional[dict]:
        now = int(time.time())
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM conversations WHERE chat_id=? AND user_id=?",
                (str(chat_id), str(user_id)),
            ).fetchone()
            if row is None:
                return None
            if int(row["expires_at"]) < now:
                self._conn.execute(
                    "DELETE FROM conversations WHERE chat_id=? AND user_id=?",
                    (str(chat_id), str(user_id)),
                )
                self._conn.commit()
                return None
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except json.JSONDecodeError:
                payload = {}
            return {
                "state": row["state"],
                "payload": payload,
                "nonce": row["nonce"],
                "updated_at": row["updated_at"],
                "expires_at": row["expires_at"],
            }

    def delete_conversation(self, chat_id: Any, user_id: Any) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM conversations WHERE chat_id=? AND user_id=?",
                (str(chat_id), str(user_id)),
            )
            self._conn.commit()

    # --- idempotency keys ---
    def get_idempotency(self, key: str) -> Optional[dict]:
        now = int(time.time())
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM idempotency_keys WHERE key=?", (key,)
            ).fetchone()
            if row is None:
                return None
            if int(row["expires_at"]) < now:
                self._conn.execute(
                    "DELETE FROM idempotency_keys WHERE key=?", (key,)
                )
                self._conn.commit()
                return None
            try:
                result = json.loads(row["result_json"]) if row["result_json"] else None
            except json.JSONDecodeError:
                result = None
            return {
                "key": row["key"],
                "kind": row["kind"],
                "resource": row["resource"],
                "status": row["status"],
                "result": result,
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
            }

    def insert_idempotency_if_absent(
        self,
        key: str,
        kind: str,
        resource: str = "",
        status: str = "in_flight",
        ttl_seconds: int = DEFAULT_IDEMPOTENCY_TTL,
    ) -> bool:
        """Atomically insert if missing. Returns True when newly inserted."""
        now = int(time.time())
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO idempotency_keys
                  (key, kind, resource, status, result_json, created_at, expires_at)
                VALUES (?, ?, ?, ?, NULL, ?, ?)
                """,
                (key, kind, resource, status, str(now), str(now + ttl_seconds)),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def update_idempotency(
        self,
        key: str,
        status: str,
        resource: Optional[str] = None,
        result: Optional[Any] = None,
    ) -> None:
        now = int(time.time())
        with self._lock:
            row = self._conn.execute(
                "SELECT expires_at FROM idempotency_keys WHERE key=?", (key,)
            ).fetchone()
            if row is None:
                expires = str(now + DEFAULT_IDEMPOTENCY_TTL)
            else:
                expires = row["expires_at"]
            self._conn.execute(
                """
                UPDATE idempotency_keys
                SET status=?, resource=COALESCE(NULLIF(?, ''), resource), result_json=?, expires_at=?
                WHERE key=?
                """,
                (status, resource, json.dumps(result) if result is not None else None,
                 expires, key),
            )
            self._conn.commit()
