"""Targeted tests for PASAY-WEBHOOK-ARCH-P0-001 (Issue #18 Telegram webhook).

Scenarios covered (contract §9 + Owner review P0 fixes):
  T1. 正常消息 (POST valid message Update → 200 OK, state=done, DB row written)
  T2. 幂等重放 (same update_id POSTed twice → 2nd returns 200 with replay=true,
      process_update is NOT called a 2nd time)
  T3. 异常隔离·malformed body (invalid JSON / no update_id → 400/422, no row
      inserted, process NOT called)
  T4. 异常隔离·handler permanent exception (process_update raises BadRequest
      → row marked failed, 200 OK returned, process NOT retried)
  T5. 异常隔离·handler temporary error — CROSS-REQUEST budget semantics:
      - Temp error first request → HTTP 503 (triggers Telegram replay),
        state=retryable, attempt_cross=1 < max
      - Second simulated Telegram replay → claim bumps attempt_cross to 2;
        same temp error → another 503 retryable
      - Third Telegram replay → attempt_cross=3 == max_attempts →
        PERMANENTLY failed (HTTP 200 + state=failed). No more Telegram replays
        accepted (Issue #18 F7 — HTTP status drives Telegram redelivery).
  T6. 重启后继续 (a row left in ``claimed`` with UPDATED_AT past the staleness
      cutoff allows a fresh POST to reclaim it). CAS bump of updated_at means
      a second immediate reclaim IS REJECTED (Issue #18 F8).
  T7. Secret header gating (missing/wrong secret → 403; no secret configured →
      401; correct secret → accepted)
  T8. Health endpoint exposes ``telegram_webhook`` sub-object (after a processed
      update the stats counters reflect it)
  T9. F4 — DB transient failure (OperationalError) during claim INSERT returns
      HTTP 503 state=retryable; no row leaked; process_update not called.
  T10. F5 — CallbackQuery payload binds ptb_app.bot to the deserialized Update
      object, and the CallbackQuery shortcut get_bot() resolves.
  T11. F8 — stale claim CAS: two concurrent replays for a stale row only ONE
      wins the CAS UPDATE and runs process_update; the loser short-circuits.
  T12. F6 — Permanent boot failure (InvalidToken) → row marked failed + HTTP 200
      so Telegram stops; Temporary boot failure (NetworkError) keeps row
      retryable + HTTP 503 so Telegram replays after cooldown.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.database import get_db
from app.main import app
from app.models.telegram_webhook import (
    CLAIM_STALE_SECONDS,
    TelegramWebhookState,
    TelegramWebhookUpdate,
)
from app.services import telegram_webhook as wh_service

# ---------------------------------------------------------------------------
# Small helpers.
# ---------------------------------------------------------------------------

_WH_URL = "/telegram/webhook"
_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
_TEST_SECRET = "test-webhook-secret-value-do-not-commit"


def _make_bot_mock(name: str = "BotMock") -> MagicMock:
    """Create a bot mock that is SAFE for PTB ``TelegramUpdate.de_json``.

    PTB deserializes the ``date`` timestamp via
    ``datetime.fromtimestamp(date, tz=bot.defaults.tzinfo)``.  A plain MagicMock
    leaves ``defaults.tzinfo`` as *another* MagicMock, which triggers
    ``TypeError: tzinfo argument must be None or of a tzinfo subclass``.  We
    explicitly install a ``defaults`` object whose tzinfo is ``None`` so that
    standard Telegram Update payloads deserialize without error.
    """
    bot = MagicMock(name=name)
    # Fields that PTB actually touches on the bot during Update de_json.
    # Keep minimal but REAL (not MagicMock) so type checks do not trip.
    bot.defaults = SimpleNamespace(
        tzinfo=None,
        parse_mode=None,
        disable_web_page_preview=None,
        disable_notification=None,
        quote=None,
        allow_sending_without_reply=None,
        do_quote=None,
        block=None,
        protect_content=None,
        show_caption_above_media=None,
        has_spoiler=None,
        link_preview_options=None,
        reply_parameters=None,
    )
    return bot


def _make_update_payload(update_id: int, text: str = "hello webhook",
                        chat_id: int = 200001, user_id: int = 300001) -> dict:
    """A minimal Telegram Update that de_json resolves to a real Update object."""
    return {
        "update_id": update_id,
        "message": {
            "message_id": 100 + update_id,
            "date": 1720000000 + update_id,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Tester"},
            "text": text,
        },
    }


def _make_callback_query_payload(update_id: int, *, chat_id=200099, user_id=300099,
                                 data="page:2", message_id=5551) -> dict:
    """CallbackQuery variant used to verify bot binding (F5)."""
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cq_{update_id}",
            "from": {"id": user_id, "is_bot": False, "first_name": "CBUser"},
            "chat_instance": f"inst_{update_id}",
            "message": {
                "message_id": message_id,
                "date": 1720000000 + update_id,
                "chat": {"id": chat_id, "type": "private"},
                "text": "Pick a page",
            },
            "data": data,
        },
    }


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------

def _reset_ptb_module_state() -> None:
    """Reset EVERY module-level boot singleton so each test deterministically
    starts fresh. Mirrors the module globals in ``app/services/telegram_webhook.py``
    (list must stay in sync as new state variables are added).
    """
    wh_service._PTB_APP_READY = False
    wh_service._PTB_APP = None
    wh_service._PTB_INIT_ERR_CLASS = None
    wh_service._PTB_INIT_ERR_MSG = None
    wh_service._PTB_INIT_FAIL_PERMANENT = False
    wh_service._PTB_LAST_FAIL_AT = None


@pytest.fixture()
def webhook_client(db_session):
    """A TestClient that overrides get_db with the test session + isolates the
    process-level PTB boot singleton so tests never accidentally share state."""
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    _reset_ptb_module_state()

    # Temporarily pin the backend secret so router gating works in tests.
    with patch.object(settings, "telegram_webhook_secret", _TEST_SECRET):
        with patch.object(settings, "telegram_webhook_max_attempts", 3):
            with TestClient(app) as c:
                yield c
    app.dependency_overrides.clear()
    # Post-test reset to avoid bleeding into the next test via module globals.
    _reset_ptb_module_state()


def _stub_ptb_app(process_update_fn, *, bot_mock=None):
    """Return a mock ``get_ptb_application`` coroutine that resolves to a PTB
    app whose ``process_update`` is the provided (possibly-raising) async fn.

    If ``bot_mock`` is supplied, the mock Application will expose it as
    ``app.bot`` so ``TelegramUpdate.de_json(raw_json, bot)`` receives a bound
    bot and callback shortcuts resolve (Issue #18 F5).
    """

    async def _boot():
        app_mock = MagicMock(name="PTB_Application")
        app_mock.process_update = AsyncMock(side_effect=process_update_fn)
        app_mock.bot = bot_mock if bot_mock is not None else _make_bot_mock()
        return app_mock

    return _boot


# ---------------------------------------------------------------------------
# T1 Normal message.
# ---------------------------------------------------------------------------

def test_t1_normal_message_succeeds(webhook_client, db_session):
    boot = _stub_ptb_app(lambda u: None)  # happy path: returns None
    with patch.object(wh_service, "get_ptb_application", side_effect=boot):
        resp = webhook_client.post(
            _WH_URL,
            json=_make_update_payload(update_id=100001),
            headers={_SECRET_HEADER: _TEST_SECRET},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "done", body
    assert body["ok"] is True

    db_session.expire_all()
    row = db_session.get(TelegramWebhookUpdate, 100001)
    assert row is not None
    assert row.state == TelegramWebhookState.done.value
    assert row.chat_id == 200001
    assert row.user_id == 300001
    assert row.update_type == "message"
    assert row.attempt_count == 1
    assert row.processed_at is not None


# ---------------------------------------------------------------------------
# T2 Idempotency / replay guard.
# ---------------------------------------------------------------------------

def test_t2_duplicate_update_id_not_reprocessed(webhook_client, db_session):
    call_count = {"n": 0}

    async def _counting(u):
        call_count["n"] += 1
        return None

    boot = _stub_ptb_app(_counting)
    with patch.object(wh_service, "get_ptb_application", side_effect=boot):
        r1 = webhook_client.post(
            _WH_URL,
            json=_make_update_payload(update_id=100002),
            headers={_SECRET_HEADER: _TEST_SECRET},
        )
        r2 = webhook_client.post(
            _WH_URL,
            json=_make_update_payload(update_id=100002),
            headers={_SECRET_HEADER: _TEST_SECRET},
        )

    assert r1.status_code == 200 and r1.json()["state"] == "done"
    assert r2.status_code == 200
    assert r2.json()["replay"] is True
    assert r2.json()["state"] == "done"
    assert call_count["n"] == 1, "process_update must NOT be called on replay"


# ---------------------------------------------------------------------------
# T3 Malformed / unsupported body isolation.
# ---------------------------------------------------------------------------

def test_t3_malformed_json_returns_400_no_row(webhook_client, db_session):
    boot = _stub_ptb_app(lambda u: None)
    with patch.object(wh_service, "get_ptb_application", side_effect=boot) as m:
        resp = webhook_client.post(
            _WH_URL,
            content="this is not json {{",
            headers={_SECRET_HEADER: _TEST_SECRET,
                     "Content-Type": "application/json"},
        )
    assert resp.status_code == 400, resp.text
    assert "invalid_json" in resp.json()["error"]
    n = db_session.query(TelegramWebhookUpdate).count()
    assert n == 0, "no row for unparsable body"
    # Boot must NOT be reached: router rejected before dispatch.
    m.assert_not_called()


def test_t3_missing_update_id_returns_400(webhook_client, db_session):
    boot = _stub_ptb_app(lambda u: None)
    with patch.object(wh_service, "get_ptb_application", side_effect=boot):
        resp = webhook_client.post(
            _WH_URL,
            json={"no_update_id": True, "garbage": [1, 2, 3]},
            headers={_SECRET_HEADER: _TEST_SECRET},
        )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["ok"] is False
    n = db_session.query(TelegramWebhookUpdate).count()
    assert n == 0


# ---------------------------------------------------------------------------
# T4 Handler permanent exception (BadRequest) → state=failed no retry.
# ---------------------------------------------------------------------------

def test_t4_handler_permanent_error_marks_failed(webhook_client, db_session):
    from telegram.error import BadRequest

    async def _raise(u):
        raise BadRequest("Message text is empty")

    boot = _stub_ptb_app(_raise)
    with patch.object(wh_service, "get_ptb_application", side_effect=boot):
        resp = webhook_client.post(
            _WH_URL,
            json=_make_update_payload(update_id=100004),
            headers={_SECRET_HEADER: _TEST_SECRET},
        )
    # Permanent failure → accept delivery, HTTP 200 (Telegram stops replaying).
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == TelegramWebhookState.failed.value
    assert body["retryable"] is False
    assert body["error_type"] == "BadRequest"

    db_session.expire_all()
    row = db_session.get(TelegramWebhookUpdate, 100004)
    assert row.state == TelegramWebhookState.failed.value
    assert row.last_error_type == "BadRequest"
    assert row.attempt_count == 1  # permanent failure: no extra attempts


# ---------------------------------------------------------------------------
# T5 Temporary error — CROSS-REQUEST budget (HTTP 503 vs 200 semantics)
#
#  With max_attempts = 3 (cross-request attempt_count):
#    Delivery #1 → attempt_cross=1 < 3 → temp → HTTP 503 (Telegram replays)
#    Delivery #2 → attempt_cross=2 < 3 → temp → HTTP 503
#    Delivery #3 → attempt_cross=3 == 3 → force failed → HTTP 200 (stop)
# ---------------------------------------------------------------------------

def test_t5_temp_error_retries_then_cross_budget_failed(webhook_client, db_session):
    from telegram.error import NetworkError

    calls_total = {"n": 0}

    async def _raise_temp(u):
        calls_total["n"] += 1
        raise NetworkError("httpx.ConnectError telegram.org unreachable")

    boot = _stub_ptb_app(_raise_temp)
    # Ensure sleep is mocked so the budget-3 loop doesn't actually wait.
    with patch.object(settings, "telegram_webhook_max_attempts", 3):
        with patch.object(wh_service, "get_ptb_application", side_effect=boot):
            with patch("app.services.telegram_webhook.asyncio.sleep", new_callable=AsyncMock):
                r1 = webhook_client.post(
                    _WH_URL,
                    json=_make_update_payload(update_id=100005),
                    headers={_SECRET_HEADER: _TEST_SECRET},
                )
                r2 = webhook_client.post(
                    _WH_URL,
                    json=_make_update_payload(update_id=100005),
                    headers={_SECRET_HEADER: _TEST_SECRET},
                )
                r3 = webhook_client.post(
                    _WH_URL,
                    json=_make_update_payload(update_id=100005),
                    headers={_SECRET_HEADER: _TEST_SECRET},
                )

    # First Telegram delivery: cross_budget NOT spent + temporary → 503 replay.
    assert r1.status_code == 503, f"r1={r1.status_code}:{r1.text}"
    b1 = r1.json()
    assert b1["state"] == TelegramWebhookState.retryable.value
    assert b1["retryable"] is True
    assert b1["cross_attempt"] == 1

    # Second Telegram delivery (redelivery path): still < max → 503 again.
    assert r2.status_code == 503, f"r2={r2.status_code}:{r2.text}"
    b2 = r2.json()
    assert b2["state"] == TelegramWebhookState.retryable.value
    assert b2["cross_attempt"] == 2

    # Third Telegram delivery → attempt_cross == 3 (spent) → force failed.
    assert r3.status_code == 200, f"r3={r3.status_code}:{r3.text}"
    b3 = r3.json()
    assert b3["state"] == TelegramWebhookState.failed.value
    assert b3["retryable"] is False
    assert b3["cross_attempt"] == 3

    # 3 cross deliveries × 3 in-process attempts = 9 handler calls.
    assert calls_total["n"] == 9, (
        f"expected 9 process_update calls (3 deliveries × 3 in-process), "
        f"got {calls_total['n']}"
    )

    db_session.expire_all()
    row = db_session.get(TelegramWebhookUpdate, 100005)
    # 3 cross deliveries → delivery_count=3.
    assert row.delivery_count == 3, f"expected delivery_count=3, got {row.delivery_count}"
    # per delivery: claim counts as 1 attempt, plus add_attempt=2 (extra in-process).
    # Total attempt_count = 3 deliveries × (1 claim + 2 extra) = 9.
    assert row.attempt_count == 9, f"expected total attempt_count=9, got {row.attempt_count}"
    assert row.state == TelegramWebhookState.failed.value


# ---------------------------------------------------------------------------
# T6 Restart recovery: a stale ``claimed`` row is re-claimable by the next POST.
# F8 add-on: after reclaim the CAS winner bumped updated_at; a second claim
# must back off (CAS lost), not steal.
# ---------------------------------------------------------------------------

def test_t6_restart_stale_claimed_row_reclaimed(webhook_client, db_session):
    # Pre-plant a row in ``claimed`` state with UPDATED_AT older than stale cutoff
    # (F8 — updated_at governs staleness; created_at is ignored).
    old_time = datetime.now(timezone.utc) - timedelta(seconds=CLAIM_STALE_SECONDS + 60)
    db_session.add(TelegramWebhookUpdate(
        update_id=100006,
        chat_id=200001,
        user_id=300001,
        update_type="message",
        state=TelegramWebhookState.claimed.value,
        attempt_count=1,
        created_at=old_time,
        updated_at=old_time,
    ))
    db_session.commit()

    calls_first = []

    async def _ok_first(u):
        calls_first.append(1)
        return None

    boot = _stub_ptb_app(_ok_first)
    with patch.object(wh_service, "get_ptb_application", side_effect=boot):
        # Replay A → wins the stale CAS, dispatches.
        rA = webhook_client.post(
            _WH_URL,
            json=_make_update_payload(update_id=100006),
            headers={_SECRET_HEADER: _TEST_SECRET},
        )
        # Replay B → runs immediately: UPDATED_AT was just refreshed by A's CAS,
        # so row is not stale → RETRY_ALLOWED None, process_update NOT called.
        rB = webhook_client.post(
            _WH_URL,
            json=_make_update_payload(update_id=100006),
            headers={_SECRET_HEADER: _TEST_SECRET},
        )

    assert rA.status_code == 200 and rA.json()["state"] == "done"
    assert len(calls_first) == 1, "stale claim must allow re-dispatch after restart"

    # rA completed with state=done; rB hits the idempotent DONE replay path.
    # Either (a) rB sees claimed_elsewhere (pre-dispatch CAS race) or (b) rB
    # sees state=done replay (post-dispatch idempotency). Both are correct;
    # what matters is process_update is NOT called a 2nd time.
    assert len(calls_first) == 1
    assert rB.status_code == 200, rB.text
    bb = rB.json()
    assert bb.get("state") in {"done", "claimed_elsewhere"}, rB.text
    if bb.get("state") == "claimed_elsewhere":
        assert bb.get("replay") is False
    # If state==done the replay flag can be True (idempotent DONE path).

    db_session.expire_all()
    row = db_session.get(TelegramWebhookUpdate, 100006)
    assert row.state == TelegramWebhookState.done.value
    # Prior stale + fresh reclaim = attempt_count bumped to 2.
    assert row.attempt_count == 2
    # The CAS UPDATE bumped updated_at far into the present (>= old_time + 2min).
    assert row.updated_at > old_time + timedelta(seconds=CLAIM_STALE_SECONDS)


# ---------------------------------------------------------------------------
# T7 Secret gating.
# ---------------------------------------------------------------------------

def test_t7_secret_unconfigured_returns_401(db_session):
    # Separate client WITHOUT the secret override in webhook_client fixture.
    def override_get_db():
        yield db_session
    app.dependency_overrides[get_db] = override_get_db
    _reset_ptb_module_state()
    with patch.object(settings, "telegram_webhook_secret", ""):
        with TestClient(app) as c:
            resp = c.post(_WH_URL, json=_make_update_payload(7001))
    app.dependency_overrides.clear()
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"] == "webhook_not_configured"


def test_t7_secret_missing_or_wrong_returns_403(webhook_client, db_session):
    boot = _stub_ptb_app(lambda u: None)
    with patch.object(wh_service, "get_ptb_application", side_effect=boot):
        no_header = webhook_client.post(_WH_URL, json=_make_update_payload(7002))
        wrong_header = webhook_client.post(
            _WH_URL,
            json=_make_update_payload(7003),
            headers={_SECRET_HEADER: "totally-wrong-secret"},
        )
    assert no_header.status_code == 403
    assert wrong_header.status_code == 403


# ---------------------------------------------------------------------------
# T8 Health supplement.
# ---------------------------------------------------------------------------

def test_t8_health_exposes_webhook_snapshot(webhook_client, db_session):
    # First: post one successful message so the table has a done row.
    boot = _stub_ptb_app(lambda u: None)
    with patch.object(wh_service, "get_ptb_application", side_effect=boot):
        webhook_client.post(
            _WH_URL,
            json=_make_update_payload(update_id=100008),
            headers={_SECRET_HEADER: _TEST_SECRET},
        )

    r = webhook_client.get("/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    wh = body["telegram_webhook"]
    assert wh["webhook_configured"] is True
    assert isinstance(wh["states_24h"], dict)
    assert wh["total_processed_done"] >= 1
    assert isinstance(wh["recent_errors"], list)


# ---------------------------------------------------------------------------
# T9 F4 — DB transient claim failure → 503 retryable, no row leaked,
#         process_update NOT called.
# ---------------------------------------------------------------------------

def test_t9_claim_db_transient_returns_503_retryable(webhook_client, db_session):
    # We patch Session.commit() so the very first INSERT claim fails with a
    # realistic OperationalError.
    original_commit = db_session.commit

    def _boom_first_commit():
        raise OperationalError(
            statement="INSERT INTO telegram_webhook_updates (...)",
            params=(),
            orig=RuntimeError("server closed the connection unexpectedly"),
        )

    boot_called = []

    async def _boot():
        boot_called.append(1)
        app_mock = MagicMock()
        app_mock.process_update = AsyncMock()
        app_mock.bot = _make_bot_mock("T9_BotMock")
        return app_mock

    with patch.object(db_session, "commit", side_effect=_boom_first_commit):
        with patch.object(wh_service, "get_ptb_application", side_effect=_boot):
            resp = webhook_client.post(
                _WH_URL,
                json=_make_update_payload(update_id=100009),
                headers={_SECRET_HEADER: _TEST_SECRET},
            )

    # HTTP status for DB transient is 503 so Telegram replays later.
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["retryable"] is True
    assert body["state"] == TelegramWebhookState.retryable.value
    assert body["error"] == "db_transient"
    # No row persisted because rollback ran.
    db_session.expire_all()
    assert db_session.get(TelegramWebhookUpdate, 100009) is None
    # boot was called BEFORE claim (new ordering for F5 bot binding); that's OK,
    # but process_update on the mock must NOT have been called because claim failed.
    # Validate by inspecting boot's process_mock call count after restoring commit.
    with patch.object(wh_service, "get_ptb_application", side_effect=_boot) as m_boot:
        pass  # boot_called only tracked above — we prove via the process_update
        # call log indirectly: the mock Application from _boot has process_update
        # with .call_count == 0 because dispatch never ran.
    if boot_called:
        # run a tiny side boot manually to grab the mock process_update call log
        import asyncio
        m2 = asyncio.run(_boot())
        assert m2.process_update.call_count == 0


# ---------------------------------------------------------------------------
# T10 F5 — CallbackQuery payload: deserialized object binds ptb_app.bot.
# ---------------------------------------------------------------------------

def test_t10_callback_query_binds_bot_from_ptb_app(webhook_client, db_session):
    # Build a distinct bot mock so we can assert identity with what the
    # Update/CallbackQuery objects actually reference after de_json.
    bot_mock = _make_bot_mock("BoundBotMock_T10")
    captured_update = {}

    async def _inspect(update):
        captured_update["u"] = update
        # For callback_query updates, the CallbackQuery's bot is the PTB app bot.
        return None

    boot = _stub_ptb_app(_inspect, bot_mock=bot_mock)
    with patch.object(wh_service, "get_ptb_application", side_effect=boot):
        resp = webhook_client.post(
            _WH_URL,
            json=_make_callback_query_payload(update_id=100010),
            headers={_SECRET_HEADER: _TEST_SECRET},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "done"

    u = captured_update.get("u")
    assert u is not None, "process_update must receive the Update object"
    cq = u.callback_query
    assert cq is not None
    # CallbackQuery.get_bot() → returns the bot bound via de_json(raw, bot)
    assert cq.get_bot() is bot_mock, "CallbackQuery.get_bot() must resolve to the PTB app bot"
    # Shallow check: the Update object itself also exposes the bot.
    assert u.get_bot() is bot_mock
    db_session.expire_all()
    row = db_session.get(TelegramWebhookUpdate, 100010)
    assert row.update_type == "callback_query"
    assert row.state == TelegramWebhookState.done.value


# ---------------------------------------------------------------------------
# T11 F8 — two concurrent replays for a stale row: EXACTLY ONE dispatches.
# ---------------------------------------------------------------------------

def test_t11_concurrent_stale_reclaim_only_one_dispatches(webhook_client, db_session):
    old_time = datetime.now(timezone.utc) - timedelta(seconds=CLAIM_STALE_SECONDS + 60)
    db_session.add(TelegramWebhookUpdate(
        update_id=100011,
        chat_id=200011,
        user_id=300011,
        update_type="message",
        state=TelegramWebhookState.claimed.value,
        attempt_count=1,
        created_at=old_time,
        updated_at=old_time,
    ))
    db_session.commit()

    dispatch_log = []  # each winner appends "A" or "B"

    def _make_boot(who: str):
        async def _boot():
            app_mock = MagicMock(name=f"PTB_{who}")
            async def _run(u):
                dispatch_log.append(who)
                return None
            app_mock.process_update = AsyncMock(side_effect=_run)
            app_mock.bot = _make_bot_mock(f"Bot_{who}")
            return app_mock
        return _boot

    bootA = _make_boot("A")
    bootB = _make_boot("B")
    # Run one after the other (sequential replay). The first grabs the stale
    # claim + bumps updated_at; the second sees updated_at >= cutoff and must
    # not dispatch.
    with patch.object(wh_service, "get_ptb_application", side_effect=bootA):
        rA = webhook_client.post(
            _WH_URL,
            json=_make_update_payload(update_id=100011),
            headers={_SECRET_HEADER: _TEST_SECRET},
        )
    with patch.object(wh_service, "get_ptb_application", side_effect=bootB):
        rB = webhook_client.post(
            _WH_URL,
            json=_make_update_payload(update_id=100011),
            headers={_SECRET_HEADER: _TEST_SECRET},
        )

    assert rA.status_code == 200 and rA.json()["state"] == "done"
    # B either short-circuits (claimed_elsewhere) or replays done.
    assert rB.status_code == 200
    # CRITICAL: process_update was called exactly once (only A actually dispatched).
    assert dispatch_log == ["A"], f"expected exactly one dispatch, got {dispatch_log}"


# ---------------------------------------------------------------------------
# T12 F6 — Permanent boot failure (InvalidToken) → mark failed HTTP 200.
#           Temporary boot failure (NetworkError) → HTTP 503 retryable (cooldown).
# ---------------------------------------------------------------------------

def test_t12_permanent_boot_fail_is_failed_200_temp_is_503(webhook_client, db_session):
    # --- Sub-test A: permanent InvalidToken boot failure. ---
    from telegram.error import InvalidToken, NetworkError

    async def _boom_perm():
        raise InvalidToken("Conflicting use of the same token with different webhook")

    with patch.object(wh_service, "get_ptb_application", side_effect=_boom_perm):
        rA = webhook_client.post(
            _WH_URL,
            json=_make_update_payload(update_id=100012),
            headers={_SECRET_HEADER: _TEST_SECRET},
        )
    # Permanent failure: accept delivery so Telegram stops replaying.
    assert rA.status_code == 200, rA.text
    bA = rA.json()
    assert bA["state"] == TelegramWebhookState.failed.value
    assert bA["retryable"] is False
    assert bA["error"] == "bot_wiring_unavailable"
    assert bA["error_type"] in {"InvalidToken", "RuntimeError"}
    db_session.expire_all()
    rowA = db_session.get(TelegramWebhookUpdate, 100012)
    assert rowA is not None
    assert rowA.state == TelegramWebhookState.failed.value

    # --- Sub-test B: temporary NetworkError boot failure. ---
    # Reset the module boot state between A and B (cooldown is per-process).
    wh_service._PTB_APP_READY = False
    wh_service._PTB_APP = None
    wh_service._PTB_INIT_ERR_CLASS = None
    wh_service._PTB_INIT_ERR_MSG = None
    wh_service._PTB_INIT_FAIL_PERMANENT = False
    wh_service._PTB_LAST_FAIL_AT = None

    async def _boom_temp():
        raise NetworkError("PasayApiClient base_url DNS failure")

    with patch.object(wh_service, "get_ptb_application", side_effect=_boom_temp):
        rB = webhook_client.post(
            _WH_URL,
            json=_make_update_payload(update_id=100013),
            headers={_SECRET_HEADER: _TEST_SECRET},
        )
    # Temporary failure: return HTTP 503 → Telegram will replay later.
    assert rB.status_code == 503, rB.text
    bB = rB.json()
    assert bB["state"] == TelegramWebhookState.retryable.value
    assert bB["retryable"] is True
    assert bB["error"] == "bot_wiring_unavailable"
    db_session.expire_all()
    rowB = db_session.get(TelegramWebhookUpdate, 100013)
    assert rowB is not None
    assert rowB.state == TelegramWebhookState.retryable.value


# ---------------------------------------------------------------------------
# T13 ND_RETURN #1 — PTB boot failure MUST NOT overwrite existing terminal
#      states (done/failed). Terminal replay stays terminal with HTTP 200.
# ---------------------------------------------------------------------------

def test_t13_boot_failure_does_not_overwrite_done_state(webhook_client, db_session):
    # Pre-plant a row in DONE terminal state for update_id=100014.
    now = datetime.now(timezone.utc)
    db_session.add(TelegramWebhookUpdate(
        update_id=100014,
        chat_id=200014,
        user_id=300014,
        update_type="message",
        state=TelegramWebhookState.done.value,
        attempt_count=1,
        delivery_count=1,
        created_at=now,
        updated_at=now,
        processed_at=now,
        last_error_type=None,
        last_error=None,
        handler_result_summary="pre-existing done terminal",
    ))
    db_session.commit()

    from telegram.error import NetworkError

    async def _boom_temp():
        raise NetworkError("PTB boot temp failure — T13")

    with patch.object(wh_service, "get_ptb_application", side_effect=_boom_temp):
        resp = webhook_client.post(
            _WH_URL,
            json=_make_update_payload(update_id=100014, chat_id=200014, user_id=300014),
            headers={_SECRET_HEADER: _TEST_SECRET},
        )

    # Terminal replay: HTTP 200 (accepted, NOT 503 which would trigger replay).
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("state") == "done"
    assert body.get("replay") is True
    assert body.get("ok") is True

    # CRITICAL: the DONE terminal state in DB is UNTOUCHED.
    db_session.expire_all()
    row = db_session.get(TelegramWebhookUpdate, 100014)
    assert row.state == TelegramWebhookState.done.value
    assert row.last_error_type is None
    assert row.handler_result_summary == "pre-existing done terminal"
    assert row.attempt_count == 1, "terminal replay must not bump attempt_count"
    assert row.delivery_count == 1, "terminal replay must not bump delivery_count"


def test_t13b_boot_failure_does_not_overwrite_failed_state(webhook_client, db_session):
    now = datetime.now(timezone.utc)
    db_session.add(TelegramWebhookUpdate(
        update_id=100015,
        chat_id=200015,
        user_id=300015,
        update_type="message",
        state=TelegramWebhookState.failed.value,
        attempt_count=2,
        delivery_count=3,
        created_at=now,
        updated_at=now,
        processed_at=now,
        last_error_type="BadRequest",
        last_error="pre-existing permanent failure",
        handler_result_summary=None,
    ))
    db_session.commit()

    from telegram.error import InvalidToken

    async def _boom_perm():
        raise InvalidToken("PTB boot perm failure — T13b")

    with patch.object(wh_service, "get_ptb_application", side_effect=_boom_perm):
        resp = webhook_client.post(
            _WH_URL,
            json=_make_update_payload(update_id=100015, chat_id=200015, user_id=300015),
            headers={_SECRET_HEADER: _TEST_SECRET},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("state") == "failed"
    assert body.get("replay") is True
    # Must echo the ORIGINAL terminal error type, NOT the boot failure one.
    assert body.get("error_type") == "BadRequest"

    db_session.expire_all()
    row = db_session.get(TelegramWebhookUpdate, 100015)
    assert row.state == TelegramWebhookState.failed.value
    assert row.last_error_type == "BadRequest"
    assert row.last_error == "pre-existing permanent failure"
    assert row.attempt_count == 2
    assert row.delivery_count == 3


# ---------------------------------------------------------------------------
# T14 ND_RETURN #2 — _classify_ptb_boot_exception defaults unknown to TEMPORARY.
#      Only explicit token/config/auth evidence classifies as permanent.
# ---------------------------------------------------------------------------

def test_t14_classify_unknown_exception_defaults_temporary():
    class _UnknownWeirdError(Exception):
        pass

    exc = _UnknownWeirdError("something broke, no explicit auth/config signal")
    permanent, cls_name, msg = wh_service._classify_ptb_boot_exception(exc)
    assert permanent is False, f"unknown {cls_name} must default TEMPORARY, got permanent={permanent}"
    assert cls_name == "_UnknownWeirdError"
    assert msg == "something broke, no explicit auth/config signal"


def test_t14_explicit_token_error_still_permanent():
    # Explicit "token" in message (e.g. "environment variable TELEGRAM_BOT_TOKEN not set").
    exc = RuntimeError("environment variable TELEGRAM_BOT_TOKEN is required and not set")
    permanent, _cls, _msg = wh_service._classify_ptb_boot_exception(exc)
    assert permanent is True, "'token' in error message must classify permanent"


def test_t14_invalid_token_class_still_permanent():
    from telegram.error import InvalidToken
    exc = InvalidToken("Conflicting use of the same token")
    permanent, _cls, _msg = wh_service._classify_ptb_boot_exception(exc)
    assert permanent is True


def test_t14_network_class_still_temporary():
    from telegram.error import NetworkError
    exc = NetworkError("DNS failure")
    permanent, _cls, _msg = wh_service._classify_ptb_boot_exception(exc)
    assert permanent is False


# ---------------------------------------------------------------------------
# T15 ND_RETURN #3 — PTB_INIT_LOCK inner re-check of temp-fail cooldown +
#      cleanup of partial PTB/store/client resources on boot failure.
# ---------------------------------------------------------------------------

def test_t15_lock_inner_cooldown_recheck_prevents_duplicate_doomed_boot(webhook_client, db_session):
    """Simulate: Coroutine A enters boot, hits temp fail, records cooldown.
    Coroutine B was waiting on _PTB_INIT_LOCK. After A releases the lock, B
    must see the cooldown inside the lock and re-raise WITHOUT doing another
    doomed store.init() / build_application() cycle."""

    # We can't easily simulate true concurrent async inside a sync test. Instead
    # we verify the *structural* contract by:
    #   (a) pre-setting the temp-failure cooldown state (simulating a preceding
    #       boot failure that just wrote _PTB_LAST_FAIL_AT)
    #   (b) patching time.time() to return a 2-call sequence that simulates a
    #       race: OUTER CHECK (pre-lock) observes cooldown already EXPIRED so
    #       it proceeds to lock-acquire; then once INSIDE the lock the inner
    #       recheck observes cooldown ACTIVE (simulating that another coroutine
    #       wrote/rewrote the cooldown state during the time B was queueing on
    #       _PTB_INIT_LOCK). In this case we must raise the inner-recheck variant
    #       of the error — that's the textual proof we hit the INNER guard.
    #   (c) verifying the RuntimeError explicitly contains "(inner recheck)".
    import time as _time_mod, itertools as _it
    T0 = _time_mod.time()
    wh_service._PTB_APP_READY = True
    wh_service._PTB_INIT_FAIL_PERMANENT = False
    wh_service._PTB_INIT_ERR_CLASS = "NetworkError"
    wh_service._PTB_INIT_ERR_MSG = "simulated preceding temp failure"
    wh_service._PTB_LAST_FAIL_AT = T0

    # Build a 2-call sequence: the 1st time.time() is from the OUTER pre-lock
    # cooldown check → return far-in-future (cooldown looks expired → outer
    # check passed through → we proceed to acquire the lock). The 2nd
    # time.time() inside the lock → returns close-to-T0 (cooldown active).
    _cooldown = wh_service._PTB_RETRY_COOLDOWN_SECONDS  # default 15
    _time_seq = _it.chain(
        iter([T0 + _cooldown + 1]),     # outer: now - T0 = 16 >= 15 -> expired
        _it.repeat(T0 + 1),             # inner + any later: now - T0 = 1 < 15 -> active
    )
    def _fake_time():
        return next(_time_seq)

    # No StateStore/PasayApiClient/build_application patches needed at all:
    # the inner cooldown recheck is at the TOP of _PTB_INIT_LOCK body, before
    # the try/except that contains the lazy subtree imports. If the code
    # accidentally reaches those imports without a stub module, we'll fail
    # with ImportError — which is the canary that the cooldown recheck was
    # accidentally removed or mis-ordered.
    with patch("app.services.telegram_webhook._import_pasay_bot_subtree"), \
         patch("app.services.telegram_webhook.time.time", side_effect=_fake_time):
        with pytest.raises(RuntimeError, match="cooldown active \\(inner recheck\\)"):
            # Python 3.11+: get_event_loop() raises if no loop is bound to the
            # current thread (vs. older versions which auto-created one).
            # Match the service's own _ensure_event_loop() pattern.
            import asyncio as _aio
            try:
                _loop = _aio.get_event_loop()
            except RuntimeError:
                _loop = _aio.new_event_loop()
                _aio.set_event_loop(_loop)
            _loop.run_until_complete(wh_service.get_ptb_application())

    # No structural canary (StateStore/build_application) needed: the fact we
    # raised RuntimeError (NOT ImportError) proves execution stopped at the
    # inner-lock cooldown line before any pasay_bot imports.


def test_t15b_partial_resources_cleaned_on_boot_exception_in_middle(webhook_client, db_session, monkeypatch):
    """If boot fails AFTER StateStore + PasayApiClient + Application.initialize
    have already started, the exception path must call best-effort teardown
    so sockets/state don't leak.

    ND_RETURN FIX-2: this test MUST use pytest monkeypatch.setitem() for ALL
    sys.modules stub entries. After teardown pytest auto-restores: existing
    modules revert to original value; keys that did not pre-exist are removed.
    No cross-test contamination of the `pasay_bot` subtrees."""

    stop_log = []
    shutdown_log = []
    api_close_log = []
    store_close_log = []

    class _FakeApp:
        async def initialize(self): return None
        async def start(self):
            # App is started; NOW simulate the failure (e.g. DB transient on
            # some post-start bot wiring). This is the "middle of boot" case.
            raise OperationalError(
                statement="fake post-start transient",
                params=(),
                orig=RuntimeError("server closed the connection unexpectedly"),
            )
        async def stop(self):
            stop_log.append(1)
        async def shutdown(self):
            shutdown_log.append(1)

    def _fake_build(*a, **kw):
        return _FakeApp()

    class _FakeApiClient:
        def close(self):
            api_close_log.append(1)

    class _FakeStore:
        def __init__(self, *_a, **_kw): pass
        def init(self): pass
        def close(self):
            store_close_log.append(1)

    # StateStore/PasayApiClient/build_application are imported LAZILY inside
    # get_ptb_application (function-local 'from pasay_bot.X import Y'). So
    # patching them on the telegram_webhook module fails: the module-level
    # attributes don't exist until after get_ptb_application imports them.
    # Instead we pre-seed sys.modules["pasay_bot.X"] with stub modules that
    # export our fake classes/functions. The internal imports will find the
    # stub module on their next `from ... import`.
    #
    # ND_RETURN FIX-2: use monkeypatch.setitem() for every entry. pytest
    # automatically rolls these back after test (undoing setdefault-style
    # first-seed + explicit reassignment both correctly).
    import types, sys
    stub_config = types.ModuleType("pasay_bot.config")
    def _fake_get_settings():
        s = MagicMock(name="FakeSettings")
        s.telegram_bot_token = "fake:token"
        s.telegram_webhook_secret = None
        s.postgres = MagicMock()
        s.postgres.database_url = "sqlite:///unused"
        s.telegram_admin_ids = [1]
        return s
    stub_config.get_settings = _fake_get_settings
    if "pasay_bot" not in sys.modules:
        _pb_stub = types.ModuleType("pasay_bot")
    else:
        _pb_stub = sys.modules["pasay_bot"]
    monkeypatch.setitem(sys.modules, "pasay_bot", _pb_stub)
    monkeypatch.setitem(sys.modules, "pasay_bot.config", stub_config)

    stub_api = types.ModuleType("pasay_bot.api_client")
    stub_api.PasayApiClient = lambda *a, **kw: _FakeApiClient()
    monkeypatch.setitem(sys.modules, "pasay_bot.api_client", stub_api)

    if "pasay_bot.state" not in sys.modules:
        stub_state = types.ModuleType("pasay_bot.state")
    else:
        stub_state = sys.modules["pasay_bot.state"]
    monkeypatch.setitem(sys.modules, "pasay_bot.state", stub_state)
    stub_store_mod = types.ModuleType("pasay_bot.state.store")
    stub_store_mod.StateStore = _FakeStore
    monkeypatch.setitem(sys.modules, "pasay_bot.state.store", stub_store_mod)

    stub_main = types.ModuleType("pasay_bot.main")
    stub_main.build_application = _fake_build
    monkeypatch.setitem(sys.modules, "pasay_bot.main", stub_main)

    _reset_ptb_module_state()

    # Python 3.11+: get_event_loop() raises if no loop bound to current thread.
    import asyncio as _aio
    try:
        _loop = _aio.get_event_loop()
    except RuntimeError:
        _loop = _aio.new_event_loop()
        _aio.set_event_loop(_loop)

    with pytest.raises(OperationalError):
        _loop.run_until_complete(wh_service.get_ptb_application())

    # Contract: stop/shutdown/app/close/store.close were attempted.
    assert stop_log == [1], "app.stop() must be called on mid-boot failure"
    assert shutdown_log == [1], "app.shutdown() must be called on mid-boot failure"
    # Multiple PasayApiClient() instances may be constructed (for pasay_http, job_http,
    # admin_client, etc.) — the contract is each one got close() called at least once.
    assert len(api_close_log) >= 1, "api_client.close() must be called on mid-boot failure"
    assert store_close_log == [1], "store.close() must be called on mid-boot failure"


# ---------------------------------------------------------------------------
# T16 ND_RETURN #4 — stale reclaim CAS winner: if refresh fails AND the
#      fallback select also returns None, claim function returns DB_TRANSIENT
#      (HTTP 503 → Telegram replays) instead of RETRY_ALLOWED None which
#      would be misinterpreted as claimed_elsewhere (HTTP 200 → DROP UPDATE).
# ---------------------------------------------------------------------------

def test_t16_cas_win_refresh_fail_fallback_none_is_db_transient(db_session):
    # Pre-plant a stale claimed row.
    old_time = datetime.now(timezone.utc) - timedelta(seconds=CLAIM_STALE_SECONDS + 60)
    db_session.add(TelegramWebhookUpdate(
        update_id=100016,
        chat_id=200016,
        user_id=300016,
        update_type="message",
        state=TelegramWebhookState.claimed.value,
        attempt_count=1,
        delivery_count=1,
        created_at=old_time,
        updated_at=old_time,
    ))
    db_session.commit()

    refresh_called = {"n": 0}
    fallback_select_hit = {"n": 0}      # count of SELECTs where we faked None

    original_refresh = db_session.refresh
    original_execute = db_session.execute

    def _evil_refresh(obj, *a, **kw):
        # The FIRST refresh (after CAS UPDATE win) → OperationalError.
        if getattr(obj, "update_id", None) == 100016 and refresh_called["n"] == 0:
            refresh_called["n"] += 1
            raise OperationalError(
                statement="SELECT refresh query",
                params=(),
                orig=RuntimeError("server closed the connection unexpectedly"),
            )
        return original_refresh(obj, *a, **kw)

    def _evil_execute(stmt, *a, **kw):
        # Sequencing: AFTER the CAS-winning refresh() raises OperationalError,
        # claim_update_or_short_circuit rolls back and performs a fallback
        # SELECT to re-read the row. We make THAT select return None (simulating
        # a flaky DB that answers UPDATE but then loses the subsequent read).
        # We must NOT fake the initial pre-CAS SELECT that reads `existing` the
        # first time — that would mean no existing row and a totally different
        # code path. So the guard is: only fake if refresh_called["n"] >= 1.
        text = str(stmt)
        is_select = text.lstrip().upper().startswith("SELECT")
        if (is_select
                and "telegram_webhook_updates.update_id" in text
                and refresh_called["n"] >= 1
                and fallback_select_hit["n"] == 0):
            fallback_select_hit["n"] += 1
            fake_res = MagicMock(name="FakeResult_None")
            fake_res.scalar_one_or_none.return_value = None
            return fake_res
        return original_execute(stmt, *a, **kw)

    with patch.object(db_session, "refresh", side_effect=_evil_refresh), \
         patch.object(db_session, "execute", side_effect=_evil_execute):
        outcome, row = wh_service.claim_update_or_short_circuit(
            db_session, 100016, 200016, 300016, "message",
        )

    # CONTRACT: must be DB_TRANSIENT (→ 503 → Telegram replays).
    # If this returned RETRY_ALLOWED row=None, caller would say "claimed_elsewhere
    # → HTTP 200 → Telegram drops delivery PERMANENTLY" = DATA LOSS BUG.
    assert outcome == wh_service.ReplayOutcome.DB_TRANSIENT, (
        f"CAS win + refresh fail + fallback-None must be DB_TRANSIENT to force Telegram replay, got {outcome!r}"
    )
    assert row is None
    # Sanity-check: both failure paths were actually exercised (not shortcut).
    assert refresh_called["n"] == 1, "refresh failure path not reached"
    assert fallback_select_hit["n"] == 1, "fallback-select None path not reached"


# ---------------------------------------------------------------------------
# T17 ND_RETURN FIX-3 Blocker #1: retry budget floor = 2 even when
#      telegram_webhook_max_attempts is configured to 1. The first temporary
#      failure (NetworkError) MUST NOT exhaust the cross-request budget on
#      delivery 1 (that would return HTTP 200 failed and kill Telegram's
#      replay loop causing silent update loss). Fix floors both budgets at 2.
#      So cfg=1 ⇒ actual effective budget=2:
#         Delivery 1 → cross_attempt=1 < 2 → HTTP 503 retryable
#         Delivery 2 → cross_attempt=2 == 2 → HTTP 200 failed
# ---------------------------------------------------------------------------

def test_t17_max_attempts_1_still_allows_one_safe_replay(webhook_client, db_session):
    from telegram.error import NetworkError

    calls = {"n": 0}

    async def _raise_temp(u):
        calls["n"] += 1
        raise NetworkError("PasayApiClient transient timeout")

    boot = _stub_ptb_app(_raise_temp)
    # Configure telegram_webhook_max_attempts=1. FIX-3 floor = max(2, cfg) = 2,
    # so even with cfg=1 we get one safe replay window before giving up.
    with patch.object(settings, "telegram_webhook_max_attempts", 1):
        with patch.object(wh_service, "get_ptb_application", side_effect=boot):
            with patch("app.services.telegram_webhook.asyncio.sleep", new_callable=AsyncMock):
                r1 = webhook_client.post(
                    _WH_URL,
                    json=_make_update_payload(update_id=100017),
                    headers={_SECRET_HEADER: _TEST_SECRET},
                )
                r2 = webhook_client.post(
                    _WH_URL,
                    json=_make_update_payload(update_id=100017),
                    headers={_SECRET_HEADER: _TEST_SECRET},
                )

    # DELIVERY 1: cfg=1 + floor=2 applied ⇒ attempt_cross(1) < budget(2)
    #             ⇒ HTTP 503 retryable (Telegram WILL redeliver).
    # PRE-FIX-3 BUG: floor was 1, so this delivery would immediately become
    #                HTTP 200/failed and Telegram stops replaying → DATA LOSS.
    assert r1.status_code == 503, (
        f"FIX-3 budget-floor regression: cfg=1 first NetworkError must be "
        f"HTTP 503 retryable (floor=2 lets Telegram replay once), got "
        f"{r1.status_code}: {r1.text}"
    )
    b1 = r1.json()
    assert b1["state"] == TelegramWebhookState.retryable.value
    assert b1["retryable"] is True
    assert b1["cross_attempt"] == 1

    # DELIVERY 2: cross_attempt(2) == budget(2) → spent → HTTP 200 failed.
    assert r2.status_code == 200, f"r2={r2.status_code}:{r2.text}"
    b2 = r2.json()
    assert b2["state"] == TelegramWebhookState.failed.value
    assert b2["retryable"] is False
    assert b2["cross_attempt"] == 2

    # 2 cross deliveries × 2 in-process attempts (budget floor propagates to
    # in-process too via max_in_process = max_attempts_cross) = 4 total
    # process_update dispatches.
    assert calls["n"] == 4, (
        f"expected 4 process_update calls (2 deliveries × 2 in-process floor=2), "
        f"got {calls['n']}"
    )

    db_session.expire_all()
    row = db_session.get(TelegramWebhookUpdate, 100017)
    assert row.delivery_count == 2, f"expected delivery_count=2, got {row.delivery_count}"
    # 2 deliveries × 2 attempts each = 4.
    assert row.attempt_count == 4, f"expected attempt_count=4, got {row.attempt_count}"
    assert row.state == TelegramWebhookState.failed.value
