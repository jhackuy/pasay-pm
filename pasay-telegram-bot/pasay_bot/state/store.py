"""Local SQLite state: conversations (TTL) + idempotency keys.

Standard-library sqlite3 only (zero extra deps). This is bot-local state —
never PostgreSQL, never through the API.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

# CONVERGENCE-003 §1.6: the daily dedupe boundary is the PHILIPPINES
# operational date (Asia/Manila, UTC+8, no DST), never the UTC date — a UTC
# date flip must not cause two sends on one PH day.
PH_TZ = ZoneInfo("Asia/Manila")


def ph_local_date(now: datetime | None = None) -> str:
    """'YYYY-MM-DD' of the Philippines operational day."""
    now = now or datetime.now(PH_TZ)
    return now.astimezone(PH_TZ).date().isoformat()

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
CREATE TABLE IF NOT EXISTS user_defaults (
  user_id        TEXT PRIMARY KEY,
  payment_method TEXT NOT NULL DEFAULT 'Bank',
  updated_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rent_status_selectors (
  nonce        TEXT PRIMARY KEY,
  chat_id      TEXT NOT NULL,
  user_id      TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  expires_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS v2_context (
  chat_id      TEXT NOT NULL,
  user_id      TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  updated_at   TEXT NOT NULL,
  expires_at   TEXT NOT NULL,
  PRIMARY KEY (chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS known_groups (
  chat_id      TEXT PRIMARY KEY,
  title        TEXT NOT NULL DEFAULT '',
  first_seen   TEXT NOT NULL
);
-- CONVERGENCE-003: persistent same-day action marks (remind owner / rent
-- follow-up / next_check reminders). SQLite PRIMARY KEY makes the mark
-- atomic; the key embeds the PH local date so a restart can never re-fire.
CREATE TABLE IF NOT EXISTS daily_marks (
  key        TEXT PRIMARY KEY,
  created_at TEXT NOT NULL
);
-- WINDOWS-RUNTIME-REBOOT-RECOVERY-002 (PHASE C fix): delivery-truth record
-- for Remind-Owner. PRIMARY KEY (expense_id, date) makes the same-day gate
-- atomic AND stores the proven Telegram delivery facts (target, destination,
-- sent_at, message_id). A row is written ONLY after send_message returns a
-- confirmed message_id; a failed delivery writes no row, so the daily limit is
-- NOT consumed and a retry stays allowed. Survives process restart (SQLite).
CREATE TABLE IF NOT EXISTS reminder_deliveries (
  expense_id   TEXT NOT NULL,
  date         TEXT NOT NULL,
  target_user  TEXT NOT NULL DEFAULT '',
  destination  TEXT NOT NULL DEFAULT '',
  sent_at      TEXT NOT NULL,
  message_id   TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (expense_id, date)
);
-- PASAY-VNEXT-FOLLOWUP-FEEDBACK-005A: delivery-truth record for rent follow-up
-- assignment DM to the Secretary. This is NOT domain completion, but it is
-- the non-repeatable proof that the bot already delivered the notification.
-- Stored as Telegram message_id so we don't rely on message text.
CREATE TABLE IF NOT EXISTS followup_deliveries (
  task_id      TEXT PRIMARY KEY,
  unit_id      TEXT NOT NULL DEFAULT '',
  date         TEXT NOT NULL DEFAULT '',
  target_user  TEXT NOT NULL DEFAULT '',
  destination  TEXT NOT NULL DEFAULT '',
  sent_at      TEXT NOT NULL,
  message_id   TEXT NOT NULL DEFAULT ''
);
"""

DEFAULT_CONVERSATION_TTL = 900        # 15 minutes
DEFAULT_V2_CONTEXT_TTL = 3600         # 60 minutes (short-term conversation)
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

    # --- user defaults (e.g. last-used payment method) ---
    def get_user_default_method(self, user_id: Any) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT payment_method FROM user_defaults WHERE user_id=?",
                (str(user_id),),
            ).fetchone()
        if row is None:
            return "Bank"
        return row["payment_method"] or "Bank"

    def set_user_default_method(self, user_id: Any, method: str) -> None:
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO user_defaults (user_id, payment_method, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                  payment_method=excluded.payment_method, updated_at=excluded.updated_at
                """,
                (str(user_id), method, str(now)),
            )
            self._conn.commit()

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

    # --- PASAY-V2-FOUNDATION-001: short-term conversation context -----------
    # (chat_id, user_id) -> {task_ref, property_ref, intent, ...} so follow-up
    # messages ("coming tomorrow") attach to the active event without re-asking
    # which property. TTL is 60 minutes; context never holds secrets.
    def save_v2_context(
        self,
        chat_id: Any,
        user_id: Any,
        payload: Optional[dict] = None,
        ttl_seconds: int = DEFAULT_V2_CONTEXT_TTL,
    ) -> None:
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO v2_context
                  (chat_id, user_id, payload_json, updated_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET
                  payload_json=excluded.payload_json,
                  updated_at=excluded.updated_at,
                  expires_at=excluded.expires_at
                """,
                (
                    str(chat_id),
                    str(user_id),
                    json.dumps(payload or {}, ensure_ascii=False),
                    str(now),
                    str(now + ttl_seconds),
                ),
            )
            self._conn.commit()

    def get_v2_context(self, chat_id: Any, user_id: Any) -> Optional[dict]:
        now = int(time.time())
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM v2_context WHERE chat_id=? AND user_id=?",
                (str(chat_id), str(user_id)),
            ).fetchone()
            if row is None:
                return None
            if int(row["expires_at"]) < now:
                self._conn.execute(
                    "DELETE FROM v2_context WHERE chat_id=? AND user_id=?",
                    (str(chat_id), str(user_id)),
                )
                self._conn.commit()
                return None
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except json.JSONDecodeError:
                payload = {}
            return {
                "payload": payload,
                "updated_at": row["updated_at"],
                "expires_at": row["expires_at"],
            }

    def clear_v2_context(self, chat_id: Any, user_id: Any) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM v2_context WHERE chat_id=? AND user_id=?",
                (str(chat_id), str(user_id)),
            )
            self._conn.commit()

    # --- PASAY-V2-FOUNDATION-001: known group registry (daily digest) --------
    # --- CONVERGENCE-003: persistent same-day marks -------------------------
    def mark_daily(self, key: str) -> bool:
        """Atomically claim a same-day mark; True = first time today.

        The caller embeds the Philippines local date in ``key`` (e.g.
        ``remind_owner:12:2026-08-17``). The SQLite PRIMARY KEY makes the
        insert atomic — a restart or a second tap cannot re-fire.
        """
        now = int(time.time())
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO daily_marks (key, created_at) VALUES (?, ?)",
                    (key, str(now)),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def is_marked_daily(self, key: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM daily_marks WHERE key=?", (key,)
            ).fetchone()
        return row is not None

    def record_reminder_delivery(
        self, expense_id: Any, date: str, *, target_user: str = "",
        destination: str = "", message_id: str = "",
    ) -> bool:
        """Persist a CONFIRMED Remind-Owner delivery for ``(expense_id, date)``.

        True = first (and only) successful delivery today for this expense; a
        later attempt the same day returns False (daily gate, persisted).
        Only call this AFTER ``send_message`` returned a confirmed message.
        """
        now = datetime.now(PH_TZ).astimezone(PH_TZ).isoformat()
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO reminder_deliveries "
                    "(expense_id, date, target_user, destination, sent_at, message_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (str(expense_id), str(date), str(target_user or ""),
                     str(destination or ""), str(now), str(message_id or "")),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def get_reminder_delivery(self, expense_id: Any, date: str) -> Optional[dict]:
        """Return the persisted delivery record or None (not delivered today)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT expense_id, date, target_user, destination, sent_at, message_id "
                "FROM reminder_deliveries WHERE expense_id=? AND date=?",
                (str(expense_id), str(date)),
            ).fetchone()
        if row is None:
            return None
        return {
            "expense_id": row["expense_id"],
            "date": row["date"],
            "target_user": row["target_user"],
            "destination": row["destination"],
            "sent_at": row["sent_at"],
            "message_id": row["message_id"],
        }

    def record_followup_delivery(
        self,
        task_id: Any,
        *,
        unit_id: Any = "",
        date: str = "",
        target_user: str = "",
        destination: str = "",
        message_id: str = "",
    ) -> bool:
        """Persist a CONFIRMED rent follow-up DM delivery for a task id.

        True = first successful delivery for this task; later attempts return
        False (idempotent non-repeatability). Only call this AFTER
        ``send_message`` returned a confirmed message_id.
        """
        now = datetime.now(PH_TZ).astimezone(PH_TZ).isoformat()
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO followup_deliveries "
                    "(task_id, unit_id, date, target_user, destination, sent_at, message_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(task_id),
                        str(unit_id or ""),
                        str(date or ""),
                        str(target_user or ""),
                        str(destination or ""),
                        str(now),
                        str(message_id or ""),
                    ),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def get_followup_delivery(self, task_id: Any) -> Optional[dict]:
        """Return the persisted follow-up delivery record or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT task_id, unit_id, date, target_user, destination, sent_at, message_id "
                "FROM followup_deliveries WHERE task_id=?",
                (str(task_id),),
            ).fetchone()
        if row is None:
            return None
        return {
            "task_id": row["task_id"],
            "unit_id": row["unit_id"],
            "date": row["date"],
            "target_user": row["target_user"],
            "destination": row["destination"],
            "sent_at": row["sent_at"],
            "message_id": row["message_id"],
        }

    def remember_group(self, chat_id: Any, title: str = "") -> None:
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO known_groups (chat_id, title, first_seen)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title
                """,
                (str(chat_id), title, str(now)),
            )
            self._conn.commit()

    def list_known_groups(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT chat_id, title, first_seen FROM known_groups ORDER BY first_seen"
            ).fetchall()
            return [
                {"chat_id": row["chat_id"], "title": row["title"] or "",
                 "first_seen": row["first_seen"]}
                for row in rows
            ]

    # --- read-only rent status selectors (V1.3 Slice 2, Entry D) ------------
    # Each multi-match candidate card stores its candidate rows under a
    # per-card nonce so clicking a button only ever re-renders that card's own
    # candidates (a newer query in the same chat cannot hijack an older card).
    # The payload is the JSON-safe candidate list; internal ids are never
    # stored here (the rows are display-only fields).
    def save_rent_status_selector(
        self,
        nonce: Any,
        chat_id: Any,
        user_id: Any,
        payload: list,
        ttl_seconds: int = DEFAULT_CONVERSATION_TTL,
    ) -> None:
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO rent_status_selectors
                  (nonce, chat_id, user_id, payload_json, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(nonce) DO UPDATE SET
                  chat_id=excluded.chat_id, user_id=excluded.user_id,
                  payload_json=excluded.payload_json, created_at=excluded.created_at,
                  expires_at=excluded.expires_at
                """,
                (
                    str(nonce),
                    str(chat_id),
                    str(user_id),
                    json.dumps(payload, ensure_ascii=False),
                    str(now),
                    str(now + ttl_seconds),
                ),
            )
            self._conn.commit()

    def get_rent_status_selector(
        self, nonce: Any, chat_id: Any, user_id: Any
    ) -> Optional[list]:
        """Return the candidate rows only when the nonce exists, is owned by
        this chat+user, and is still inside its TTL; otherwise drop the row
        and return None (caller shows the friendly expired copy)."""
        now = int(time.time())
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM rent_status_selectors WHERE nonce=?",
                (str(nonce),),
            ).fetchone()
            if row is None:
                return None
            if (
                str(row["chat_id"]) != str(chat_id)
                or str(row["user_id"]) != str(user_id)
                or int(row["expires_at"]) < now
            ):
                self._conn.execute(
                    "DELETE FROM rent_status_selectors WHERE nonce=?",
                    (str(nonce),),
                )
                self._conn.commit()
                return None
            try:
                payload = json.loads(row["payload_json"] or "[]")
            except json.JSONDecodeError:
                payload = None
            return payload if isinstance(payload, list) else None

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
