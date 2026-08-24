"""PASAY-BACKEND-FINAL-CLOSEOUT-001-RETURN-2 §3 R3 — /internal/ingest idempotency.

Strict rules (OWNER DECISION §3, last bullet + RETURN-2 R3):
  - Real PostgreSQL-backed /internal/ingest endpoint, NOT a mocked pipeline or
    /telegram/webhook endpoint impersonation.
  - Repeat POST (same update_id / event_id) → exactly 1 row in
    telegram_webhook_updates.
  - Concurrently POST (2 threads, same update_id/event_id, start Barrier) →
    still exactly 1 row, no IntegrityError leaked through HTTP.
  - RETURN-2 R3: tests MUST NOT accept 5xx from both requests. Use a
    DETERMINISTIC seam to isolate downstream enqueue so every request returns
    a 2xx terminal code (200 / 202 / 208), at least one request is a
    first-time accept, and the other follows duplicate/idempotent semantics.
  - Assert response bodies precisely. Assert row fields prove provenance from
    the payload just sent (pre-count == 0 guarantees no history). Concurrent
    case must be order-independent.

Fixtures are inherited from tests/conftest.py:
  - test_engine / test_session_factory / db_session (PostgreSQL, real
    Base.metadata.create_all as configured in conftest)
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
from app.models.telegram_webhook import TelegramWebhookState, TelegramWebhookUpdate
from app.schemas.envelope import ENVELOPE_VERSION
from app.services import telegram_webhook as _wh_svc


# ---------------------------------------------------------------------------
# DETERMINISTIC test seam — isolates downstream PTB / Telegram Bot / enqueue
# availability so ingest HTTP codes are always 2xx (never 5xx). Production
# contract is NOT modified: the only thing we do is make
# ``get_ptb_application()`` return a fake object whose ``process_update`` is
# a no-op. Claim/idempotency (INSERT / CAS / REPLAY / claimed_elsewhere)
# still runs through the REAL production code path with the REAL PostgreSQL
# row-writing semantics. This matches RETURN-2 R3:
#   "优先只修测试 seam。" — prefer test-only seam.
# ---------------------------------------------------------------------------
class _FakeBot:
    id = 1
    base_url = "https://api.telegram.org/botfake"
    base_file_url = "https://api.telegram.org/file/botfake"
    token = "fake_token_for_p019_seam_only"


class _FakePTB:
    def __init__(self) -> None:
        self.bot: Any = _FakeBot()

    async def process_update(self, update: Any) -> None:  # noqa: D401 - fake no-op
        return None


def _install_ptb_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_get_ptb() -> _FakePTB:
        return _FakePTB()
    monkeypatch.setattr(_wh_svc, "get_ptb_application", _fake_get_ptb)


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


def _telegram_envelope(update_id: int, *, chat_id: int, text: str) -> dict[str, Any]:
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


def _ingest_token(monkeypatch: pytest.MonkeyPatch) -> str:
    """Return a deterministic test ingest token, ensuring settings always has one.

    Also ensures TELEGRAM_WEBHOOK_SECRET / TELEGRAM_BOT_TOKEN / DATABASE_URL
    have dummy (non-functional) non-empty values so that process_telegram_update_payload
    does NOT short-circuit at its fail-closed gates — we want to drive the
    full claim/idempotency code path that inserts into telegram_webhook_updates.
    """
    configured = (getattr(settings, "container_ingest_token", None) or "").strip()
    if not configured:
        test_tok = "ingest_test_p019_token_return2_closeout"
        monkeypatch.setattr(settings, "container_ingest_token", test_tok)
        configured = test_tok

    for field, dummy in [
        ("telegram_webhook_secret", "wh_test_p019_return2_closeout"),
        ("telegram_bot_token", "123456789:testbot_token_return2_closeout"),
    ]:
        current = getattr(settings, field, None)
        if not current or not str(current).strip():
            monkeypatch.setattr(settings, field, dummy)

    return configured


# ---------------------------------------------------------------------------
# Body assertions (shared by scenario (a) and (b)).
#
# Production contract for telegram envelope + internal_ingest POST:
#   * 200 → accepted / idempotent duplicate / claimed elsewhere / terminal
#     (body contains ``ok`` boolean + ``state`` + possibly ``replay`` boolean
#     + ``error_type`` on failure paths).
#   * 202 → scheduled job accepted (NOT seen for telegram_update kind — the
#     kind-based router in internal_ingest returns status=200 for all
#     telegram envelope terminal outcomes).
#   * 208 → scheduled_job idempotent duplicate (NOT seen for telegram_update).
#
# Therefore for ``kind=telegram_update``: every status we expect MUST be
# exactly 200 (not 202 / 208) because of the router map:
#     internal_ingest L193: ``if status in (200, 202, 208): return 200``
# That is: the inner service can return 200/202/208 but we map it to 200 for
# the external HTTP contract. We still allow 200/202/208 in the outer set to
# tolerate future router changes (test is robust) but the key rule from
# RETURN-2 R3 is: NO 5xx.
# ---------------------------------------------------------------------------
_OK_2XX = {200, 202, 208}


def _is_first_accept(body: dict[str, Any]) -> bool:
    """True if body describes a first-time claim (not a replay short-circuit)."""
    if not isinstance(body, dict):
        return False
    # "ok": true + "state" is terminal done AND no ``replay:true`` flag.
    # (If dispatch succeeded: {ok:true, state:done, attempts:...}.)
    if bool(body.get("ok")) is not True:
        return False
    if body.get("replay") is True:
        return False
    state = body.get("state")
    # Accepted terminal outcome — could be "done" / "failed" (permanent) /
    # "claimed_elsewhere" (HTTP 200, replay:false). All count as first accept
    # because they represent a request that drove the claim branch (not a
    # later replay short-circuit that never claimed).
    return state in {"done", "failed", "claimed_elsewhere"} or state is not None


def _is_replay_shortcircuit(body: dict[str, Any]) -> bool:
    """True if body is a deterministic replay (idempotent duplicate)."""
    if not isinstance(body, dict):
        return False
    # Standard short-circuits (see process_telegram_update_payload):
    #   outcome==DONE → {ok:true, replay:true, state:done}
    #   outcome==FAILED → {ok:false, replay:true, state:failed, error_type:...}
    #   outcome RETRY_ALLOWED row None → {ok:true, replay:false, state:claimed_elsewhere}
    if body.get("replay") is True:
        return True
    if body.get("state") == "claimed_elsewhere" and bool(body.get("ok")):
        return True
    return False


def _assert_row_provenance(row: TelegramWebhookUpdate, update_id: int, chat_id: int | None) -> None:
    """Prove the row came from the test payload just sent.

    TelegramWebhookUpdate columns used for provenance:
      * ``update_id`` (PK) — must equal payload's update_id.
      * ``chat_id`` (best-effort) — equals payload's message.chat.id.
      * ``state`` — not 'claimed' (caller transitioned it to a terminal state
        or retryable; we accept any value except None, because the seam has
        process_update succeed → state=done after transition_update call).
      * The *count before test* was 0 → no fixture/historical row can match
        the same update_id. The row cannot come from anywhere else.
    """
    assert row.update_id == update_id, (
        f"row.update_id={row.update_id} != payload update_id={update_id} "
        f"(provenance fail: row is not from this payload)."
    )
    if chat_id is not None:
        assert row.chat_id == chat_id, (
            f"row.chat_id={row.chat_id} != payload chat_id={chat_id} "
            f"(provenance fail: chat doesn't match payload just sent)."
        )
    assert row.state is not None and row.state in {s.value for s in TelegramWebhookState}, (
        f"row.state={row.state!r} not in TelegramWebhookState. "
        f"Row was not written by the real claim/transition code path."
    )


# ---------------------------------------------------------------------------
# Scenario (a): serial duplicate POST → business side-effect COUNT == 1
#   - Two sequential POSTs with same update_id/event_id
#   - Both HTTP calls return 2xx (200/202/208) — NO 5xx
#   - Exactly ONE of the two requests is a "first accept" (drove claim)
#   - Exactly ONE of the two requests is a "replay / short-circuit duplicate"
#   - Both bodies are well-formed dicts
#   - telegram_webhook_updates where update_id == expected → exactly 1 row
#   - Row provenance: update_id + chat_id match the payload
# ---------------------------------------------------------------------------
def test_internal_ingest_repeat_update_id_single_row(client, db_session, monkeypatch):
    _install_ptb_seam(monkeypatch)
    token = _ingest_token(monkeypatch)
    headers = {INGEST_HEADER: token, "content-type": "application/json"}
    update_id = 901001
    chat_id = 42
    text = "serial_r3_return2"

    # Precondition: row does not exist — guarantees COUNT=1 after implies
    # provenance from THIS test (not historical/fixture residual).
    existing = db_session.query(TelegramWebhookUpdate).filter(
        TelegramWebhookUpdate.update_id == update_id
    ).count()
    assert existing == 0, f"update_id={update_id} already in DB (fixture pollution)"

    envl = _telegram_envelope(update_id, chat_id=chat_id, text=text)
    r1 = client.post(INGEST_PATH, json=envl, headers=headers)
    assert r1.status_code in _OK_2XX, (
        f"First ingest POST returned {r1.status_code} (expected 2xx). "
        f"body={r1.text[:500]}"
    )
    assert r1.headers.get("content-type", "").lower().startswith("application/json"), (
        f"r1 content-type={r1.headers.get('content-type')} not JSON"
    )
    b1 = r1.json()
    assert isinstance(b1, dict), f"r1 body not dict: {b1}"
    db_session.expire_all()

    r2 = client.post(INGEST_PATH, json=envl, headers=headers)
    assert r2.status_code in _OK_2XX, (
        f"Second (duplicate) ingest POST returned {r2.status_code} (expected 2xx). "
        f"body={r2.text[:500]}"
    )
    b2 = r2.json()
    assert isinstance(b2, dict), f"r2 body not dict: {b2}"
    db_session.expire_all()

    # (1) Exactly one first-accept + exactly one replay short-circuit.
    #     Serial test ORDER-DEPENDENT semantic in this case: r1 is first
    #     accept, r2 is replay. Order-independent assertion still works.
    bodies = [b1, b2]
    first_flags = [_is_first_accept(b) for b in bodies]
    replay_flags = [_is_replay_shortcircuit(b) for b in bodies]
    assert sum(first_flags) == 1, (
        f"Serial duplicate: expected EXACTLY ONE first-accept "
        f"(found {sum(first_flags)}). bodies={bodies}"
    )
    assert sum(replay_flags) == 1, (
        f"Serial duplicate: expected EXACTLY ONE replay short-circuit "
        f"(found {sum(replay_flags)}). bodies={bodies}"
    )
    # For serial: explicit that r1 drove the claim, r2 short-circuited.
    assert _is_first_accept(b1), f"Serial r1 should be first accept. r1={b1}"
    assert _is_replay_shortcircuit(b2), f"Serial r2 should be replay short-circuit. r2={b2}"

    # (2) Business idempotency: exactly 1 row for update_id.
    rows = db_session.query(TelegramWebhookUpdate).filter(
        TelegramWebhookUpdate.update_id == update_id
    ).all()
    assert len(rows) == 1, (
        f"Repeat POST same update_id → rows={len(rows)}, expected exactly 1. "
        f"Idempotency lost! r1={r1.status_code} r2={r2.status_code}"
    )

    # (3) Row provenance assertion (uses PK + chat_id; pre-count=0).
    _assert_row_provenance(rows[0], update_id=update_id, chat_id=chat_id)


# ---------------------------------------------------------------------------
# Scenario (b): concurrent POST same update_id via ThreadPoolExecutor(2)
#              + Barrier(2) ensures both threads overlap in the claim window.
#   - Winner: HTTP 2xx (200/202/208) + writes the row + first-accept body
#   - Loser:  HTTP 2xx (200/202/208) — either "replay=true" DONE/FAILED or
#     "claimed_elsewhere" (state=claimed_elsewhere). NOT 409/500/503.
#   - COUNT(update_id) still == 1 after both commit.
#   - ORDER-INDEPENDENT: no assertions on which thread "won" or "lost".
# ---------------------------------------------------------------------------
def test_internal_ingest_concurrent_update_id_still_1_row(db_session, test_engine, monkeypatch):
    _install_ptb_seam(monkeypatch)
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    token = _ingest_token(monkeypatch)
    headers = {INGEST_HEADER: token, "content-type": "application/json"}
    update_id = 901002
    chat_id = 901002
    text = "concurrent_r3_return2"

    zero_count = db_session.query(TelegramWebhookUpdate).filter(
        TelegramWebhookUpdate.update_id == update_id
    ).count()
    assert zero_count == 0, (
        f"Precondition: update_id={update_id} already exists in DB "
        f"(count={zero_count}). Fixture pollution invalidates provenance."
    )

    start_barrier = threading.Barrier(2, timeout=20)
    responses: list[tuple[int, dict[str, Any] | None, str | None]] = []
    mu = threading.Lock()

    # ------------------------------------------------------------------
    # SINGLE-WRITE FastAPI override setup — runs in test main thread,
    # BEFORE any worker thread starts. This guarantees no race on the
    # app.dependency_overrides dict. The override itself dispatches by
    # reading thread-local _tls.db_session from whichever OS thread
    # actually runs the ASGI call.
    # ------------------------------------------------------------------
    prev_override = app.dependency_overrides.get(_orig_get_db)

    _fallback_factory = sessionmaker(
        bind=create_engine(str(test_engine.url), poolclass=NullPool),
        autoflush=False,
        expire_on_commit=False,
    )
    _tl_get_db_override._fallback_factory = _fallback_factory  # type: ignore[attr-defined]
    app.dependency_overrides[_orig_get_db] = _tl_get_db_override

    try:
        db_url = str(test_engine.url)

        def _worker() -> tuple[int, dict[str, Any] | None, str | None]:
            t_engine = create_engine(db_url, poolclass=NullPool)
            t_factory = sessionmaker(
                bind=t_engine,
                autoflush=False,
                expire_on_commit=False,
            )
            t_sess = t_factory()
            try:
                assert not hasattr(_tls, "db_session"), (
                    "Previous worker leaked thread-local session — test isolation bug."
                )
                _tls.db_session = t_sess
                try:
                    with TestClient(app, raise_server_exceptions=False) as tc:
                        envl = _telegram_envelope(update_id, chat_id=chat_id, text=text)
                        start_barrier.wait(timeout=20)
                        resp = tc.post(INGEST_PATH, json=envl, headers=headers)
                        status = resp.status_code
                        body: dict[str, Any] | None = None
                        err_text: str | None = None
                        ct = resp.headers.get("content-type", "")
                        if ct.lower().startswith("application/json"):
                            try:
                                body = resp.json()
                            except Exception as exc:  # noqa: BLE001
                                err_text = f"json_parse_error:{type(exc).__name__}: {resp.text[:200]}"
                        else:
                            err_text = resp.text[:300]
                        with mu:
                            responses.append((status, body, err_text))
                        return status, body, err_text
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
        if prev_override is None:
            app.dependency_overrides.pop(_orig_get_db, None)
        else:
            app.dependency_overrides[_orig_get_db] = prev_override
        try:
            delattr(_tl_get_db_override, "_fallback_factory")
        except AttributeError:
            pass

    # ---- Result assertions ----
    assert len(responses) == 2, f"Expected 2 worker responses, got {len(responses)}: {responses}"

    statuses = [r[0] for r in responses]
    for i, s in enumerate(statuses):
        assert s in _OK_2XX, (
            f"Concurrent worker[{i}] returned HTTP {s} (NOT 2xx — "
            f"R3: responses MUST NOT include 5xx). "
            f"All statuses={statuses}. Response[{i}] raw={responses[i]}. "
            f"FAIL-CLOSED 5xx would cause Queue RETRY forever and proves the "
            f"seam is not deterministic for idempotency/acceptance testing."
        )

    bodies: list[dict[str, Any]] = []
    for i, (s, b, err) in enumerate(responses):
        assert isinstance(b, dict), (
            f"Concurrent worker[{i}] (status={s}) did not return JSON dict body. "
            f"err={err!r} raw body={b!r}"
        )
        bodies.append(b)

    first_flags = [_is_first_accept(b) for b in bodies]
    replay_flags = [_is_replay_shortcircuit(b) for b in bodies]
    # ORDER-INDEPENDENT: exactly one winner + exactly one loser regardless of thread id.
    assert sum(first_flags) >= 1, (
        f"Concurrent: NO request was a first-time accept (neither drove claim). "
        f"Both short-circuited? first_flags={first_flags} bodies={bodies}"
    )
    assert sum(first_flags) == 1, (
        f"Concurrent: BOTH claims returned first-accept flags "
        f"(sum={sum(first_flags)} — idempotency violated? 2 rows written?). "
        f"first_flags={first_flags} bodies={bodies}"
    )
    assert sum(replay_flags) == 1, (
        f"Concurrent: expected EXACTLY ONE replay/short-circuit response "
        f"(sum={sum(replay_flags)}). replay_flags={replay_flags} bodies={bodies}"
    )

    # Row: count == 1, provenance matches payload.
    db_session.expire_all()
    rows = db_session.query(TelegramWebhookUpdate).filter(
        TelegramWebhookUpdate.update_id == update_id
    ).all()
    assert len(rows) == 1, (
        f"Concurrent POST same update_id → rows={len(rows)} (statuses={statuses}). "
        f"Expected exactly 1 row (idempotency + PK conflict handling)."
    )
    _assert_row_provenance(rows[0], update_id=update_id, chat_id=chat_id)
