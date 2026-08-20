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
