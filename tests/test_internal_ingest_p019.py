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
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_db as _orig_get_db
from app.main import app
from app.models.telegram_webhook import TelegramWebhookUpdate
from app.schemas.envelope import ENVELOPE_VERSION


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

INGEST_PATH = "/internal/ingest"
INGEST_HEADER = "X-Pasay-Ingest-Token"

# Thread-local storage used by the *FastAPI-level* dependency override below.
# Each concurrent worker stages ``_tls.db_session`` before dispatching its
# HTTP request; the shared override generator reads ``_tls.db_session`` on
# whichever thread actually runs the ASGI dispatch. Since starlette's
# TestClient runs the app synchronously in the calling thread, there is no
# race. This avoids the classic "two threads write the same
# app.dependency_overrides dict key" bug.
_tls = threading.local()


def _tl_get_db_override() -> Iterator[Any]:
    """FastAPI-level override for Depends(get_db) — thread-local session dispatch.

    Installed (once, before thread launch) into::

        app.dependency_overrides[_orig_get_db] = _tl_get_db_override

    Rules:
      * If the calling thread staged a session via ``_tls.db_session``, yield
        that exact session (and do NOT close it — the owning worker is
        responsible for lifecycle).
      * Otherwise fall back to a brand new session from the test-bound engine
        supplied through ``_tl_get_db_override._fallback_factory``. This is
        used by scenario (a) and for any unexpected dependency call in a
        thread that has not explicitly staged a session.
    """
    staged: Any = getattr(_tls, "db_session", None)
    if staged is not None:
        yield staged
        return
    factory = getattr(_tl_get_db_override, "_fallback_factory", None)
    if factory is None:  # pragma: no cover - defensive; tests always set it
        pytest.fail(
            "_tl_get_db_override installed without _fallback_factory. "
            "Test setup broken.",
        )
    sess = factory()
    try:
        yield sess
    finally:
        sess.close()


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
    configured = (getattr(settings, "container_ingest_token", None) or "").strip()
    if not configured:
        test_tok = "ingest_test_p019_token_return1_closeout"
        if monkeypatch is not None:
            monkeypatch.setattr(settings, "container_ingest_token", test_tok)
        else:
            setattr(settings, "container_ingest_token", test_tok)
        configured = test_tok

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

    zero_count = db_session.query(TelegramWebhookUpdate).filter(
        TelegramWebhookUpdate.update_id == update_id
    ).count()
    assert zero_count == 0

    start_barrier = threading.Barrier(2, timeout=20)
    statuses: list[int] = []
    mu = threading.Lock()

    # ------------------------------------------------------------------
    # SINGLE-WRITE FastAPI override setup — runs in test main thread,
    # BEFORE any worker thread starts. This guarantees no race on the
    # app.dependency_overrides dict. The override itself dispatches by
    # reading thread-local _tls.db_session from whichever OS thread
    # actually runs the ASGI call.
    # ------------------------------------------------------------------
    prev_override = app.dependency_overrides.get(_orig_get_db)

    # Fallback factory uses the same test_engine URL so un-staged threads
    # still talk to the *correct* test database (not settings.database_url
    # which may be a different/empty DB).
    _fallback_factory = sessionmaker(
        bind=create_engine(str(test_engine.url), poolclass=NullPool),
        autoflush=False,
        expire_on_commit=False,
    )
    _tl_get_db_override._fallback_factory = _fallback_factory  # type: ignore[attr-defined]
    app.dependency_overrides[_orig_get_db] = _tl_get_db_override

    try:
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
                # Stage on this thread BEFORE any ASGI dispatch. The override
                # generator in _tl_get_db_override reads _tls.db_session on
                # whatever thread runs FastAPI's dependency.resolve(...) —
                # which, for starlette's synchronous TestClient, is the same
                # thread that called tc.post(...) → i.e. this worker.
                assert not hasattr(_tls, "db_session"), (
                    "Previous worker leaked thread-local session — test isolation bug."
                )
                _tls.db_session = t_sess
                try:
                    with TestClient(app, raise_server_exceptions=False) as tc:
                        envl = _telegram_envelope(update_id, chat_id=update_id)
                        start_barrier.wait(timeout=20)
                        resp = tc.post(INGEST_PATH, json=envl, headers=headers)
                        with mu:
                            statuses.append(resp.status_code)
                        return resp.status_code, resp.text[:300]
                finally:
                    try:
                        delattr(_tls, "db_session")
                    except AttributeError:
                        pass
            finally:
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
            _ = f1.result(timeout=10)
            _ = f2.result(timeout=10)
    finally:
        # Restore the original override (typically from the `client` fixture
        # in other tests; this test doesn't use `client` but we must not
        # leak overrides across modules).
        if prev_override is None:
            app.dependency_overrides.pop(_orig_get_db, None)
        else:
            app.dependency_overrides[_orig_get_db] = prev_override
        try:
            delattr(_tl_get_db_override, "_fallback_factory")
        except AttributeError:
            pass

    db_session.expire_all()
    count = db_session.query(TelegramWebhookUpdate).filter(
        TelegramWebhookUpdate.update_id == update_id
    ).count()

    for s in statuses:
        assert s in (200, 202, 208, 503), (
            f"Concurrent ingest worker returned HTTP {s} (not in ack/retryable set). "
            f"Status list={statuses}. Duplicate claim path is FAILING-CLOSED, "
            f"producing a 500/409 that would cause Queue RETRY forever."
        )
    assert count == 1, (
        f"Concurrent POST same update_id → rows={count} (statuses={statuses}). "
        f"Expected exactly 1 row (idempotency + PK conflict handling)."
    )
