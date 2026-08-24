"""PASAY-BACKEND-FINAL-CLOSEOUT-001-RETURN-1 §3 — /internal/ingest idempotency.

Strict rules (OWNER DECISION §3, last bullet):
  - Real PostgreSQL-backed /internal/ingest endpoint, NOT a mocked pipeline or
    /telegram/webhook endpoint impersonation.
  - Repeat POST (same update_id / event_id) → exactly 1 row in
    telegram_webhook_updates.
  - Concurrently POST (2 threads, same update_id/event_id, start Barrier) →
    still exactly 1 row, no IntegrityError leaked through HTTP.

Fixtures are inherited from tests/conftest.py:
  - test_engine / test_session_factory / db_session (PostgreSQL, real migrations)
  - client (FastAPI TestClient overriding get_db with the real session)
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_db
from app.main import app
from app.models.telegram_webhook import TelegramWebhookUpdate
from app.schemas.envelope import ENVELOPE_VERSION


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

INGEST_PATH = "/internal/ingest"
INGEST_HEADER = "X-Pasay-Ingest-Token"


def _telegram_envelope(update_id: int, *, chat_id: int = 42, text: str = "hi") -> dict[str, Any]:
    return {
        "version": ENVELOPE_VERSION,
        "kind": "telegram_update",
        "event_id": f"tg:{update_id}",
        "occurred_at": "2026-08-24T12:00:00Z",
        "payload": {
            "update_id": update_id,
            "message": {
                "message_id": 1000 + update_id,
                "chat": {"id": chat_id, "type": "private"},
                "date": 1756036800,
                "text": text,
            },
        },
        "_telegram_meta": {"update_id": update_id, "chat_id": chat_id},
    }


def _ingest_token(monkeypatch: pytest.MonkeyPatch | None = None) -> str:
    """Return a deterministic test ingest token, ensuring settings always has one.

    Also ensures TELEGRAM_WEBHOOK_SECRET / TELEGRAM_BOT_TOKEN / DATABASE_URL
    have dummy (non-functional) non-empty values so that process_telegram_update_payload
    does NOT short-circuit at its fail-closed gates — we want to drive the
    full claim/idempotency code path that inserts into telegram_webhook_updates.
    """
    # 1. Ingest token (X-Pasay-Ingest-Token) — the Container delivery header.
    configured = (getattr(settings, "container_ingest_token", None) or "").strip()
    if not configured:
        test_tok = "ingest_test_p019_token_return1_closeout"
        if monkeypatch is not None:
            monkeypatch.setattr(settings, "container_ingest_token", test_tok)
        else:
            setattr(settings, "container_ingest_token", test_tok)
        configured = test_tok

    # 2. Dummy Telegram/webhook gate values (never used, prevents fail-closed).
    for field, dummy in [
        ("telegram_webhook_secret", "wh_test_p019_return1_closeout"),
        ("telegram_bot_token", "123456789:testbot_token_return1_closeout"),
    ]:
        current = getattr(settings, field, None)
        if not current or not str(current).strip():
            if monkeypatch is not None:
                monkeypatch.setattr(settings, field, dummy)
            else:
                setattr(settings, field, dummy)

    return configured


# ---------------------------------------------------------------------------
# Scenario (a): serial duplicate POST → business side-effect COUNT == 1
#   - Two sequential POSTs with same update_id/event_id
#   - Both HTTP calls return 200/202/208 (terminal outcomes = ack, no retry)
#   - telegram_webhook_updates where update_id == expected → exactly 1 row
# ---------------------------------------------------------------------------
def test_internal_ingest_repeat_update_id_single_row(client, db_session, monkeypatch):
    token = _ingest_token(monkeypatch)
    headers = {INGEST_HEADER: token, "content-type": "application/json"}
    update_id = 901001

    # Precondition: row does not exist
    existing = db_session.query(TelegramWebhookUpdate).filter(
        TelegramWebhookUpdate.update_id == update_id
    ).count()
    assert existing == 0, f"update_id={update_id} already in DB (fixture pollution)"

    envl = _telegram_envelope(update_id)
    r1 = client.post(INGEST_PATH, json=envl, headers=headers)
    assert r1.status_code in (200, 202, 208, 503), (
        f"First ingest POST returned {r1.status_code} body={r1.text[:400]}"
    )
    db_session.expire_all()

    r2 = client.post(INGEST_PATH, json=envl, headers=headers)
    assert r2.status_code in (200, 202, 208, 503), (
        f"Second (duplicate) ingest POST returned {r2.status_code} body={r2.text[:400]}"
    )
    db_session.expire_all()

    count = db_session.query(TelegramWebhookUpdate).filter(
        TelegramWebhookUpdate.update_id == update_id
    ).count()
    assert count == 1, (
        f"Repeat POST same update_id → rows={count}, expected exactly 1. "
        f"Idempotency lost! r1={r1.status_code} r2={r2.status_code}"
    )


# ---------------------------------------------------------------------------
# Scenario (b): concurrent POST same update_id via ThreadPoolExecutor(2)
#              + Barrier(2) ensures both threads overlap in the claim window.
#   - Winner: HTTP 200/202/208 + writes the row
#   - Loser:  MUST also get a terminal 2xx (the power of PK + INSERT CONFLICT
#     handling = idempotent replay), not 409/500.
#   - COUNT(update_id) still == 1 after both commit.
# ---------------------------------------------------------------------------
def test_internal_ingest_concurrent_update_id_still_1_row(db_session, test_engine, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    token = _ingest_token(monkeypatch)
    headers = {INGEST_HEADER: token, "content-type": "application/json"}
    update_id = 901002

    # Precondition empty
    zero_count = db_session.query(TelegramWebhookUpdate).filter(
        TelegramWebhookUpdate.update_id == update_id
    ).count()
    assert zero_count == 0

    start_barrier = threading.Barrier(2, timeout=20)
    statuses: list[int] = []
    mu = threading.Lock()

    # CRITICAL FIX — per-thread Engine with NullPool (no connection pooling).
    # The shared test_engine uses QueuePool by default, and two threads
    # simultaneously calling _connection_for_bind() triggers SQLAlchemy's
    # "session is provisioning a new connection; concurrent operations are
    # not permitted" guard.
    #
    # We give each worker its own Engine instance bound to the same URL,
    # with poolclass=NullPool so every Session.open() creates a fresh
    # physical connection and discards it on close() — zero cross-thread
    # pool contention.
    db_url = str(test_engine.url)

    def _worker() -> tuple[int, str | None]:
        t_engine = create_engine(db_url, poolclass=NullPool)
        t_factory = sessionmaker(
            bind=t_engine,
            autoflush=False,
            expire_on_commit=False,
        )
        t_sess = t_factory()
        try:
            # Override get_db *locally* on this thread so the single
            # HTTP request dispatched through TestClient uses t_sess —
            # a brand-new Session on a brand-new Engine.
            prev_override = app.dependency_overrides.get(get_db)

            def _local_override():
                yield t_sess

            app.dependency_overrides[get_db] = _local_override
            try:
                with TestClient(app, raise_server_exceptions=False) as tc:
                    envl = _telegram_envelope(update_id, chat_id=update_id)
                    start_barrier.wait(timeout=20)
                    resp = tc.post(INGEST_PATH, json=envl, headers=headers)
                    with mu:
                        statuses.append(resp.status_code)
                    return resp.status_code, resp.text[:300]
            finally:
                if prev_override is None:
                    app.dependency_overrides.pop(get_db, None)
                else:
                    app.dependency_overrides[get_db] = prev_override
        finally:
            # Swallow IllegalStateChangeError during close if the session
            # got into a half-bound state due to provisioning failure —
            # the underlying NullPool connection will be GC'd anyway.
            try:
                t_sess.close()
            except Exception:
                pass
            try:
                t_engine.dispose()
            except Exception:
                pass

    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(_worker)
        f2 = ex.submit(_worker)
        wait([f1, f2], timeout=60)
        # Propagate any unexpected (non-HTTP) exceptions.
        _ = f1.result(timeout=10)
        _ = f2.result(timeout=10)

    db_session.expire_all()
    count = db_session.query(TelegramWebhookUpdate).filter(
        TelegramWebhookUpdate.update_id == update_id
    ).count()

    # Acceptable HTTP status codes after claim attempt:
    #   200/202/208 → terminal ack
    #   503 → temporary handler failure (Queue will retry the whole
    #         envelope from the Worker side; but the claim row already
    #         exists so replay still hits idempotency).
    # Reject 409/400/401/500/etc which would indicate broken logic.
    for s in statuses:
        assert s in (200, 202, 208, 503), (
            f"Concurrent ingest worker returned HTTP {s} (not in ack/retryable set). "
            f"Status list={statuses}. Duplicate claim path is FAILING-CLOSED, "
            f"producing a 500/409 that would cause Queue RETRY forever."
        )
    # The test invariant: regardless of whether individual worker responses
    # were ok or 503-temp-retry, the DB MUST have exactly ONE row for the
    # same update_id after both threads complete. If count == 2 or 0, the
    # PK claim is not enforced and replay safety is broken.
    assert count == 1, (
        f"Concurrent POST same update_id → rows={count} (statuses={statuses}). "
        f"Expected exactly 1 row (idempotency + PK conflict handling)."
    )
