"""StateStore — runtime-conditional backend.

RETURN1-FIX B+C: Cloudflare Container disk is EPHEMERAL; local SQLite loses
daily_marks / reminder_deliveries / followup_deliveries truths after sleep.

Two backends, same public interface, zero handler changes:

  * development/local (default or runtime_mode != "cloudflare-container")
    → SQLiteStateStore (original file-backed store)
  * PASAY_RUNTIME_MODE=cloudflare-container
    → PostgresStateStore (durable Neon/Postgres tables bs_*)

No new deps beyond psycopg2 which is already required by the backend.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

PH_TZ = ZoneInfo("Asia/Manila")


def ph_local_date(now: datetime | None = None) -> str:
    now = now or datetime.now(PH_TZ)
    return now.astimezone(PH_TZ).date().isoformat()


DEFAULT_CONVERSATION_TTL = 900
DEFAULT_V2_CONTEXT_TTL = 3600
DEFAULT_IDEMPOTENCY_TTL = 7 * 86400
DEFAULT_IN_FLIGHT_TTL = 120

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
CREATE TABLE IF NOT EXISTS daily_marks (
  key        TEXT PRIMARY KEY,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reminder_deliveries (
  expense_id   TEXT NOT NULL,
  date         TEXT NOT NULL,
  target_user  TEXT NOT NULL DEFAULT '',
  destination  TEXT NOT NULL DEFAULT '',
  sent_at      TEXT NOT NULL,
  message_id   TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (expense_id, date)
);
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


class _StateStoreBase:
    """Shared public interface for both backends.

    Each concrete subclass implements the exact same method signatures so
    no handler code needs to change when switching runtime modes.
    """

    # ---- override points --------------------------------------------------
    def migrate(self) -> None:
        raise NotImplementedError

    def recover_stale_in_flight(self, max_age_seconds: int = DEFAULT_IN_FLIGHT_TTL) -> int:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    # ---- user defaults ----------------------------------------------------
    def get_user_default_method(self, user_id: Any) -> str:
        raise NotImplementedError

    def set_user_default_method(self, user_id: Any, method: str) -> None:
        raise NotImplementedError

    # ---- conversations ----------------------------------------------------
    def save_conversation(self, chat_id, user_id, state, payload=None, nonce="", ttl_seconds=DEFAULT_CONVERSATION_TTL):
        raise NotImplementedError

    def get_conversation(self, chat_id, user_id) -> Optional[dict]:
        raise NotImplementedError

    def delete_conversation(self, chat_id, user_id) -> None:
        raise NotImplementedError

    # ---- v2 conversation context ------------------------------------------
    def save_v2_context(self, chat_id, user_id, payload=None, ttl_seconds=DEFAULT_V2_CONTEXT_TTL):
        raise NotImplementedError

    def get_v2_context(self, chat_id, user_id) -> Optional[dict]:
        raise NotImplementedError

    def clear_v2_context(self, chat_id, user_id) -> None:
        raise NotImplementedError

    # ---- daily marks ------------------------------------------------------
    def mark_daily(self, key: str) -> bool:
        raise NotImplementedError

    def is_marked_daily(self, key: str) -> bool:
        raise NotImplementedError

    # ---- reminder deliveries ----------------------------------------------
    def record_reminder_delivery(self, expense_id, date, *, target_user="", destination="", message_id="") -> bool:
        raise NotImplementedError

    def get_reminder_delivery(self, expense_id, date) -> Optional[dict]:
        raise NotImplementedError

    # ---- followup deliveries ----------------------------------------------
    def record_followup_delivery(self, task_id, *, unit_id="", date="", target_user="", destination="", message_id="") -> bool:
        raise NotImplementedError

    def get_followup_delivery(self, task_id) -> Optional[dict]:
        raise NotImplementedError

    # ---- known groups -----------------------------------------------------
    def remember_group(self, chat_id, title="") -> None:
        raise NotImplementedError

    def list_known_groups(self) -> list[dict]:
        raise NotImplementedError

    # ---- rent status selectors --------------------------------------------
    def save_rent_status_selector(self, nonce, chat_id, user_id, payload: list, ttl_seconds=DEFAULT_CONVERSATION_TTL):
        raise NotImplementedError

    def get_rent_status_selector(self, nonce, chat_id, user_id) -> Optional[list]:
        raise NotImplementedError

    # ---- idempotency keys -------------------------------------------------
    def get_idempotency(self, key: str) -> Optional[dict]:
        raise NotImplementedError

    def insert_idempotency_if_absent(self, key, kind, resource="", status="in_flight", ttl_seconds=DEFAULT_IDEMPOTENCY_TTL) -> bool:
        raise NotImplementedError

    def update_idempotency(self, key, status, resource=None, result=None) -> None:
        raise NotImplementedError


class SQLiteStateStore(_StateStoreBase):
    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        if db_path != ":memory:":
            parent = Path(db_path).parent
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except (OSError, PermissionError):
                pass
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass
        self.migrate()

    def migrate(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self.recover_stale_in_flight()
            self._conn.commit()

    def recover_stale_in_flight(self, max_age_seconds: int = DEFAULT_IN_FLIGHT_TTL) -> int:
        now = int(time.time())
        with self._lock:
            cur = self._conn.execute(
                "UPDATE idempotency_keys SET status='failed' "
                "WHERE status='in_flight' AND ? - CAST(created_at AS INTEGER) > ?",
                (now, max_age_seconds),
            )
            self._conn.commit()
            return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()

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
                "INSERT INTO user_defaults (user_id, payment_method, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
                "payment_method=excluded.payment_method, updated_at=excluded.updated_at",
                (str(user_id), method, str(now)),
            )
            self._conn.commit()

    def save_conversation(self, chat_id, user_id, state, payload=None, nonce="", ttl_seconds=DEFAULT_CONVERSATION_TTL):
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                "INSERT INTO conversations "
                "(chat_id, user_id, state, payload_json, nonce, updated_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(chat_id, user_id) DO UPDATE SET "
                "state=excluded.state, payload_json=excluded.payload_json, "
                "nonce=excluded.nonce, updated_at=excluded.updated_at, expires_at=excluded.expires_at",
                (
                    str(chat_id), str(user_id), state,
                    json.dumps(payload or {}, ensure_ascii=False),
                    nonce, str(now), str(now + ttl_seconds),
                ),
            )
            self._conn.commit()

    def get_conversation(self, chat_id, user_id) -> Optional[dict]:
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
                "state": row["state"], "payload": payload, "nonce": row["nonce"],
                "updated_at": row["updated_at"], "expires_at": row["expires_at"],
            }

    def delete_conversation(self, chat_id, user_id) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM conversations WHERE chat_id=? AND user_id=?",
                (str(chat_id), str(user_id)),
            )
            self._conn.commit()

    def save_v2_context(self, chat_id, user_id, payload=None, ttl_seconds=DEFAULT_V2_CONTEXT_TTL):
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                "INSERT INTO v2_context "
                "(chat_id, user_id, payload_json, updated_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(chat_id, user_id) DO UPDATE SET "
                "payload_json=excluded.payload_json, updated_at=excluded.updated_at, expires_at=excluded.expires_at",
                (str(chat_id), str(user_id),
                 json.dumps(payload or {}, ensure_ascii=False),
                 str(now), str(now + ttl_seconds)),
            )
            self._conn.commit()

    def get_v2_context(self, chat_id, user_id) -> Optional[dict]:
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
                "updated_at": row["updated_at"], "expires_at": row["expires_at"],
            }

    def clear_v2_context(self, chat_id, user_id) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM v2_context WHERE chat_id=? AND user_id=?",
                (str(chat_id), str(user_id)),
            )
            self._conn.commit()

    def mark_daily(self, key: str) -> bool:
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

    def record_reminder_delivery(self, expense_id, date, *, target_user="", destination="", message_id="") -> bool:
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

    def get_reminder_delivery(self, expense_id, date) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT expense_id, date, target_user, destination, sent_at, message_id "
                "FROM reminder_deliveries WHERE expense_id=? AND date=?",
                (str(expense_id), str(date)),
            ).fetchone()
        if row is None:
            return None
        return {
            "expense_id": row["expense_id"], "date": row["date"],
            "target_user": row["target_user"], "destination": row["destination"],
            "sent_at": row["sent_at"], "message_id": row["message_id"],
        }

    def record_followup_delivery(self, task_id, *, unit_id="", date="", target_user="", destination="", message_id="") -> bool:
        now = datetime.now(PH_TZ).astimezone(PH_TZ).isoformat()
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO followup_deliveries "
                    "(task_id, unit_id, date, target_user, destination, sent_at, message_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (str(task_id), str(unit_id or ""), str(date or ""),
                     str(target_user or ""), str(destination or ""),
                     str(now), str(message_id or "")),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def get_followup_delivery(self, task_id) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT task_id, unit_id, date, target_user, destination, sent_at, message_id "
                "FROM followup_deliveries WHERE task_id=?",
                (str(task_id),),
            ).fetchone()
        if row is None:
            return None
        return {
            "task_id": row["task_id"], "unit_id": row["unit_id"], "date": row["date"],
            "target_user": row["target_user"], "destination": row["destination"],
            "sent_at": row["sent_at"], "message_id": row["message_id"],
        }

    def remember_group(self, chat_id, title="") -> None:
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                "INSERT INTO known_groups (chat_id, title, first_seen) "
                "VALUES (?, ?, ?) ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title",
                (str(chat_id), title, str(now)),
            )
            self._conn.commit()

    def list_known_groups(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT chat_id, title, first_seen FROM known_groups ORDER BY first_seen"
            ).fetchall()
            return [
                {"chat_id": row["chat_id"], "title": row["title"] or "", "first_seen": row["first_seen"]}
                for row in rows
            ]

    def save_rent_status_selector(self, nonce, chat_id, user_id, payload: list, ttl_seconds=DEFAULT_CONVERSATION_TTL):
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                "INSERT INTO rent_status_selectors "
                "(nonce, chat_id, user_id, payload_json, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(nonce) DO UPDATE SET "
                "chat_id=excluded.chat_id, user_id=excluded.user_id, "
                "payload_json=excluded.payload_json, created_at=excluded.created_at, "
                "expires_at=excluded.expires_at",
                (str(nonce), str(chat_id), str(user_id),
                 json.dumps(payload, ensure_ascii=False),
                 str(now), str(now + ttl_seconds)),
            )
            self._conn.commit()

    def get_rent_status_selector(self, nonce, chat_id, user_id) -> Optional[list]:
        now = int(time.time())
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM rent_status_selectors WHERE nonce=?",
                (str(nonce),),
            ).fetchone()
            if row is None:
                return None
            if (str(row["chat_id"]) != str(chat_id) or str(row["user_id"]) != str(user_id)
                    or int(row["expires_at"]) < now):
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
                "key": row["key"], "kind": row["kind"],
                "resource": row["resource"], "status": row["status"],
                "result": result,
                "created_at": row["created_at"], "expires_at": row["expires_at"],
            }

    def insert_idempotency_if_absent(self, key, kind, resource="", status="in_flight", ttl_seconds=DEFAULT_IDEMPOTENCY_TTL) -> bool:
        now = int(time.time())
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO idempotency_keys "
                "(key, kind, resource, status, result_json, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, NULL, ?, ?)",
                (key, kind, resource, status, str(now), str(now + ttl_seconds)),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def update_idempotency(self, key, status, resource=None, result=None) -> None:
        now = int(time.time())
        with self._lock:
            row = self._conn.execute(
                "SELECT expires_at FROM idempotency_keys WHERE key=?", (key,)
            ).fetchone()
            expires = str(now + DEFAULT_IDEMPOTENCY_TTL) if row is None else row["expires_at"]
            self._conn.execute(
                "UPDATE idempotency_keys "
                "SET status=?, resource=COALESCE(NULLIF(?, ''), resource), result_json=?, expires_at=? "
                "WHERE key=?",
                (status, resource,
                 json.dumps(result) if result is not None else None,
                 expires, key),
            )
            self._conn.commit()


class PostgresStateStore(_StateStoreBase):
    """Durable Postgres-backed StateStore.

    Uses existing ``DATABASE_URL`` env (pooled Neon connection, already
    available inside the Container). Table names prefix ``bs_*`` to avoid
    collisions with business tables (migration ``bs_conversations`` etc.).
    """

    def __init__(self, _db_path: str = ""):
        import psycopg2
        self._lock = threading.RLock()
        self._dburl = (os.environ.get("DATABASE_URL")
                       or os.environ.get("POSTGRES_URL")
                       or "").strip()
        if not self._dburl:
            raise RuntimeError(
                "PostgresStateStore requires DATABASE_URL env (PASAY_RUNTIME_MODE=cloudflare-container)."
            )
        self._conn = psycopg2.connect(self._dburl)
        self._conn.autocommit = False
        self.migrate()

    def _cursor(self):
        # Best-effort auto-reconnect for transient connection loss.
        try:
            self._conn.rollback()
            cur = self._conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
        except Exception:
            import psycopg2
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = psycopg2.connect(self._dburl)
            self._conn.autocommit = False
        return self._conn.cursor()

    _EXPECTED_BS_TABLES: tuple[str, ...] = (
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

    def migrate(self) -> None:
        with self._lock:
            cur = self._cursor()
            missing: list[str] = []
            for tbl in self._EXPECTED_BS_TABLES:
                try:
                    cur.execute(
                        "SELECT 1 FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_name = %s LIMIT 1",
                        (tbl,),
                    )
                    if cur.fetchone() is None:
                        missing.append(tbl)
                except Exception as exc:
                    self._conn.rollback()
                    raise RuntimeError(
                        f"PostgresStateStore: cannot verify existence of bs_* tables "
                        f"(is DATABASE_URL role correct?): {type(exc).__name__}: {exc}"
                    ) from exc
            if missing:
                raise RuntimeError(
                    "PostgresStateStore: expected bs_* tables missing from public schema. "
                    "These tables are created by alembic migration "
                    "'ret1_postgres_bot_state_20260828' (runs as neondb_owner via STEP3). "
                    "pasay_runtime MUST NOT issue CREATE TABLE DDL. "
                    f"Missing tables: {sorted(missing)}"
                )
            self.recover_stale_in_flight()

    def recover_stale_in_flight(self, max_age_seconds: int = DEFAULT_IN_FLIGHT_TTL) -> int:
        now = int(time.time())
        with self._lock:
            cur = self._cursor()
            cur.execute(
                "UPDATE bs_idempotency_keys SET status='failed' "
                "WHERE status='in_flight' AND %s - CAST(created_at AS BIGINT) > %s",
                (now, max_age_seconds),
            )
            n = cur.rowcount or 0
            self._conn.commit()
            return n

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    def _one(self, cur) -> Optional[Any]:
        try:
            return cur.fetchone()
        except Exception:
            return None

    def get_user_default_method(self, user_id: Any) -> str:
        with self._lock:
            cur = self._cursor()
            cur.execute(
                "SELECT payment_method FROM bs_user_defaults WHERE user_id=%s",
                (str(user_id),),
            )
            row = self._one(cur)
        if row is None:
            return "Bank"
        return (row[0] or "Bank")

    def set_user_default_method(self, user_id: Any, method: str) -> None:
        now = str(int(time.time()))
        with self._lock:
            cur = self._cursor()
            cur.execute(
                "INSERT INTO bs_user_defaults (user_id, payment_method, updated_at) VALUES (%s, %s, %s) "
                "ON CONFLICT (user_id) DO UPDATE SET payment_method=EXCLUDED.payment_method, updated_at=EXCLUDED.updated_at",
                (str(user_id), method, now),
            )
            self._conn.commit()

    def save_conversation(self, chat_id, user_id, state, payload=None, nonce="", ttl_seconds=DEFAULT_CONVERSATION_TTL):
        now = int(time.time())
        with self._lock:
            cur = self._cursor()
            cur.execute(
                "INSERT INTO bs_conversations (chat_id, user_id, state, payload_json, nonce, updated_at, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (chat_id, user_id) DO UPDATE SET "
                "state=EXCLUDED.state, payload_json=EXCLUDED.payload_json, "
                "nonce=EXCLUDED.nonce, updated_at=EXCLUDED.updated_at, expires_at=EXCLUDED.expires_at",
                (str(chat_id), str(user_id), state,
                 json.dumps(payload or {}, ensure_ascii=False), nonce,
                 str(now), str(now + ttl_seconds)),
            )
            self._conn.commit()

    def get_conversation(self, chat_id, user_id) -> Optional[dict]:
        now = int(time.time())
        with self._lock:
            cur = self._cursor()
            cur.execute(
                "SELECT state, payload_json, nonce, updated_at, expires_at "
                "FROM bs_conversations WHERE chat_id=%s AND user_id=%s",
                (str(chat_id), str(user_id)),
            )
            row = self._one(cur)
            if row is None:
                return None
            state, payload_json, nonce, updated_at, expires_at = row
            if int(expires_at) < now:
                cur.execute(
                    "DELETE FROM bs_conversations WHERE chat_id=%s AND user_id=%s",
                    (str(chat_id), str(user_id)),
                )
                self._conn.commit()
                return None
            try:
                payload = json.loads(payload_json or "{}")
            except json.JSONDecodeError:
                payload = {}
            return {"state": state, "payload": payload, "nonce": nonce,
                    "updated_at": updated_at, "expires_at": expires_at}

    def delete_conversation(self, chat_id, user_id) -> None:
        with self._lock:
            cur = self._cursor()
            cur.execute(
                "DELETE FROM bs_conversations WHERE chat_id=%s AND user_id=%s",
                (str(chat_id), str(user_id)),
            )
            self._conn.commit()

    def save_v2_context(self, chat_id, user_id, payload=None, ttl_seconds=DEFAULT_V2_CONTEXT_TTL):
        now = int(time.time())
        with self._lock:
            cur = self._cursor()
            cur.execute(
                "INSERT INTO bs_v2_context (chat_id, user_id, payload_json, updated_at, expires_at) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (chat_id, user_id) DO UPDATE SET "
                "payload_json=EXCLUDED.payload_json, updated_at=EXCLUDED.updated_at, expires_at=EXCLUDED.expires_at",
                (str(chat_id), str(user_id),
                 json.dumps(payload or {}, ensure_ascii=False),
                 str(now), str(now + ttl_seconds)),
            )
            self._conn.commit()

    def get_v2_context(self, chat_id, user_id) -> Optional[dict]:
        now = int(time.time())
        with self._lock:
            cur = self._cursor()
            cur.execute(
                "SELECT payload_json, updated_at, expires_at FROM bs_v2_context "
                "WHERE chat_id=%s AND user_id=%s",
                (str(chat_id), str(user_id)),
            )
            row = self._one(cur)
            if row is None:
                return None
            payload_json, updated_at, expires_at = row
            if int(expires_at) < now:
                cur.execute(
                    "DELETE FROM bs_v2_context WHERE chat_id=%s AND user_id=%s",
                    (str(chat_id), str(user_id)),
                )
                self._conn.commit()
                return None
            try:
                payload = json.loads(payload_json or "{}")
            except json.JSONDecodeError:
                payload = {}
            return {"payload": payload, "updated_at": updated_at, "expires_at": expires_at}

    def clear_v2_context(self, chat_id, user_id) -> None:
        with self._lock:
            cur = self._cursor()
            cur.execute(
                "DELETE FROM bs_v2_context WHERE chat_id=%s AND user_id=%s",
                (str(chat_id), str(user_id)),
            )
            self._conn.commit()

    def mark_daily(self, key: str) -> bool:
        now = str(int(time.time()))
        with self._lock:
            cur = self._cursor()
            try:
                cur.execute(
                    "INSERT INTO bs_daily_marks (key, created_at) VALUES (%s, %s)",
                    (key, now),
                )
                self._conn.commit()
                return True
            except Exception as exc:
                cls = type(exc).__name__
                if "unique" in cls.lower() or "UniqueViolation" in cls or "Integrity" in cls:
                    self._conn.rollback()
                    return False
                self._conn.rollback()
                raise

    def is_marked_daily(self, key: str) -> bool:
        with self._lock:
            cur = self._cursor()
            cur.execute("SELECT 1 FROM bs_daily_marks WHERE key=%s", (key,))
            return self._one(cur) is not None

    def record_reminder_delivery(self, expense_id, date, *, target_user="", destination="", message_id="") -> bool:
        now = datetime.now(PH_TZ).astimezone(PH_TZ).isoformat()
        with self._lock:
            cur = self._cursor()
            try:
                cur.execute(
                    "INSERT INTO bs_reminder_deliveries "
                    "(expense_id, date, target_user, destination, sent_at, message_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (str(expense_id), str(date), str(target_user or ""),
                     str(destination or ""), str(now), str(message_id or "")),
                )
                self._conn.commit()
                return True
            except Exception as exc:
                cls = type(exc).__name__
                if "unique" in cls.lower() or "UniqueViolation" in cls or "Integrity" in cls:
                    self._conn.rollback()
                    return False
                self._conn.rollback()
                raise

    def get_reminder_delivery(self, expense_id, date) -> Optional[dict]:
        with self._lock:
            cur = self._cursor()
            cur.execute(
                "SELECT expense_id, date, target_user, destination, sent_at, message_id "
                "FROM bs_reminder_deliveries WHERE expense_id=%s AND date=%s",
                (str(expense_id), str(date)),
            )
            row = self._one(cur)
        if row is None:
            return None
        e_id, dt, tu, dest, sa, mid = row
        return {"expense_id": e_id, "date": dt, "target_user": tu,
                "destination": dest, "sent_at": sa, "message_id": mid}

    def record_followup_delivery(self, task_id, *, unit_id="", date="", target_user="", destination="", message_id="") -> bool:
        now = datetime.now(PH_TZ).astimezone(PH_TZ).isoformat()
        with self._lock:
            cur = self._cursor()
            try:
                cur.execute(
                    "INSERT INTO bs_followup_deliveries "
                    "(task_id, unit_id, date, target_user, destination, sent_at, message_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (str(task_id), str(unit_id or ""), str(date or ""),
                     str(target_user or ""), str(destination or ""),
                     str(now), str(message_id or "")),
                )
                self._conn.commit()
                return True
            except Exception as exc:
                cls = type(exc).__name__
                if "unique" in cls.lower() or "UniqueViolation" in cls or "Integrity" in cls:
                    self._conn.rollback()
                    return False
                self._conn.rollback()
                raise

    def get_followup_delivery(self, task_id) -> Optional[dict]:
        with self._lock:
            cur = self._cursor()
            cur.execute(
                "SELECT task_id, unit_id, date, target_user, destination, sent_at, message_id "
                "FROM bs_followup_deliveries WHERE task_id=%s",
                (str(task_id),),
            )
            row = self._one(cur)
        if row is None:
            return None
        tid, uid, dt, tu, dest, sa, mid = row
        return {"task_id": tid, "unit_id": uid, "date": dt, "target_user": tu,
                "destination": dest, "sent_at": sa, "message_id": mid}

    def remember_group(self, chat_id, title="") -> None:
        now = str(int(time.time()))
        with self._lock:
            cur = self._cursor()
            cur.execute(
                "INSERT INTO bs_known_groups (chat_id, title, first_seen) VALUES (%s, %s, %s) "
                "ON CONFLICT (chat_id) DO UPDATE SET title=EXCLUDED.title",
                (str(chat_id), title, now),
            )
            self._conn.commit()

    def list_known_groups(self) -> list[dict]:
        with self._lock:
            cur = self._cursor()
            cur.execute(
                "SELECT chat_id, title, first_seen FROM bs_known_groups ORDER BY first_seen"
            )
            rows = cur.fetchall()
            return [
                {"chat_id": c, "title": t or "", "first_seen": f}
                for (c, t, f) in rows
            ]

    def save_rent_status_selector(self, nonce, chat_id, user_id, payload: list, ttl_seconds=DEFAULT_CONVERSATION_TTL):
        now = int(time.time())
        with self._lock:
            cur = self._cursor()
            cur.execute(
                "INSERT INTO bs_rent_status_selectors "
                "(nonce, chat_id, user_id, payload_json, created_at, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (nonce) DO UPDATE SET "
                "chat_id=EXCLUDED.chat_id, user_id=EXCLUDED.user_id, "
                "payload_json=EXCLUDED.payload_json, created_at=EXCLUDED.created_at, "
                "expires_at=EXCLUDED.expires_at",
                (str(nonce), str(chat_id), str(user_id),
                 json.dumps(payload, ensure_ascii=False),
                 str(now), str(now + ttl_seconds)),
            )
            self._conn.commit()

    def get_rent_status_selector(self, nonce, chat_id, user_id) -> Optional[list]:
        now = int(time.time())
        with self._lock:
            cur = self._cursor()
            cur.execute(
                "SELECT chat_id, user_id, payload_json, expires_at FROM bs_rent_status_selectors WHERE nonce=%s",
                (str(nonce),),
            )
            row = self._one(cur)
            if row is None:
                return None
            rcid, ruid, payload_json, expires_at = row
            if (str(rcid) != str(chat_id) or str(ruid) != str(user_id)
                    or int(expires_at) < now):
                cur.execute(
                    "DELETE FROM bs_rent_status_selectors WHERE nonce=%s",
                    (str(nonce),),
                )
                self._conn.commit()
                return None
            try:
                payload = json.loads(payload_json or "[]")
            except json.JSONDecodeError:
                payload = None
            return payload if isinstance(payload, list) else None

    def get_idempotency(self, key: str) -> Optional[dict]:
        now = int(time.time())
        with self._lock:
            cur = self._cursor()
            cur.execute(
                "SELECT key, kind, resource, status, result_json, created_at, expires_at "
                "FROM bs_idempotency_keys WHERE key=%s",
                (key,),
            )
            row = self._one(cur)
            if row is None:
                return None
            k, kind, resource, status, result_json, created_at, expires_at = row
            if int(expires_at) < now:
                cur.execute("DELETE FROM bs_idempotency_keys WHERE key=%s", (key,))
                self._conn.commit()
                return None
            try:
                result = json.loads(result_json) if result_json else None
            except json.JSONDecodeError:
                result = None
            return {"key": k, "kind": kind, "resource": resource,
                    "status": status, "result": result,
                    "created_at": created_at, "expires_at": expires_at}

    def insert_idempotency_if_absent(self, key, kind, resource="", status="in_flight", ttl_seconds=DEFAULT_IDEMPOTENCY_TTL) -> bool:
        now = str(int(time.time()))
        expires = str(int(time.time()) + ttl_seconds)
        with self._lock:
            cur = self._cursor()
            try:
                cur.execute(
                    "INSERT INTO bs_idempotency_keys "
                    "(key, kind, resource, status, result_json, created_at, expires_at) "
                    "VALUES (%s, %s, %s, %s, NULL, %s, %s) "
                    "ON CONFLICT (key) DO NOTHING",
                    (key, kind, resource, status, now, expires),
                )
                self._conn.commit()
                return (cur.rowcount or 0) > 0
            except Exception:
                self._conn.rollback()
                return False

    def update_idempotency(self, key, status, resource=None, result=None) -> None:
        now = int(time.time())
        with self._lock:
            cur = self._cursor()
            cur.execute(
                "SELECT expires_at FROM bs_idempotency_keys WHERE key=%s",
                (key,),
            )
            row = self._one(cur)
            expires = str(now + DEFAULT_IDEMPOTENCY_TTL) if row is None else row[0]
            cur.execute(
                "UPDATE bs_idempotency_keys "
                "SET status=%s, resource=COALESCE(NULLIF(%s, ''), resource), "
                "result_json=%s, expires_at=%s WHERE key=%s",
                (status, resource or "",
                 json.dumps(result) if result is not None else None,
                 expires, key),
            )
            self._conn.commit()


class StateStore:
    """Public factory — returns SQLiteStateStore or PostgresStateStore.

    Selection rule:
      * runtime_mode == "cloudflare-container" → PostgresStateStore (durable Neon)
      * anything else (including default empty string) → SQLiteStateStore (dev/local)
    """

    def __new__(cls, db_path: str = ":memory:", *, runtime_mode: str | None = None):
        mode = (runtime_mode or "").strip()
        if mode == "cloudflare-container":
            inst = object.__new__(PostgresStateStore)
            inst.__init__(db_path)
            return inst
        inst = object.__new__(SQLiteStateStore)
        inst.__init__(db_path)
        return inst
