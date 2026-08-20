"""Targeted tests for PASAY-WEBHOOK-ARCH-P0-001 (Issue #18 Telegram webhook).

Scenarios covered (contract §9):
  T1. 正常消息 (POST valid message Update → 200 OK, state=done, DB row written)
  T2. 幂等重放 (same update_id POSTed twice → 2nd returns 200 with replay=true,
      process_update is NOT called a 2nd time)
  T3. 异常隔离·malformed body (invalid JSON / no update_id → 400/422, no row
      inserted, process NOT called)
  T4. 异常隔离·handler permanent exception (process_update raises BadRequest
      → row marked failed, 200 OK returned, process NOT retried)
  T5. 异常隔离·handler temporary error exhausts retries (process_update raises
      NetworkError every time → in-process backoff retries N times then marks
      failed; 200 OK returned)
  T6. 重启后继续 (a row left in ``claimed`` with created_at past the staleness
      cutoff allows a fresh POST to reclaim it)
  T7. Secret header gating (missing/wrong secret → 403; no secret configured →
      401; correct secret → accepted)
  T8. Health endpoint exposes ``telegram_webhook`` sub-object (after a processed
      update the stats counters reflect it)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

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


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------

@pytest.fixture()
def webhook_client(db_session):
    """A TestClient that overrides get_db with the test session + isolates the
    process-level PTB boot singleton so tests never accidentally share state."""
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    # Reset module-level singletons per test so ``get_ptb_application`` behaves
    # deterministically and one test's boot failure does not poison the next.
    wh_service._PTB_APP_READY = False
    wh_service._PTB_APP = None
    wh_service._PTB_APP_INIT_ERR = None

    # Temporarily pin the backend secret so router gating works in tests.
    with patch.object(settings, "telegram_webhook_secret", _TEST_SECRET):
        with patch.object(settings, "telegram_webhook_max_attempts", 3):
            with TestClient(app) as c:
                yield c
    app.dependency_overrides.clear()


def _stub_ptb_app(process_update_fn):
    """Return a mock ``get_ptb_application`` coroutine that resolves to a PTB
    app whose ``process_update`` is the provided (possibly-raising) async fn."""

    async def _boot():
        app_mock = MagicMock(name="PTB_Application")
        app_mock.process_update = AsyncMock(side_effect=process_update_fn)
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
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == TelegramWebhookState.failed.value
    assert body["retryable"] is False
    assert body["error_type"] == "BadRequest"

    # transition_update uses bulk UPDATE (synchronize_session=False); force
    # the fixture session to discard its cached ORM objects and re-read from DB.
    db_session.expire_all()
    row = db_session.get(TelegramWebhookUpdate, 100004)
    assert row.state == TelegramWebhookState.failed.value
    assert row.last_error_type == "BadRequest"
    assert row.attempt_count == 1  # permanent failure: no extra attempts


# ---------------------------------------------------------------------------
# T5 Temporary error exhausts in-process retry budget → marks failed.
# ---------------------------------------------------------------------------

def test_t5_temp_error_retries_then_failed(webhook_client, db_session):
    from telegram.error import NetworkError

    calls = []

    async def _raise_temp(u):
        calls.append(1)
        raise NetworkError("httpx.ConnectError telegram.org unreachable")

    # Budget 3 → exactly 3 calls, then marks failed.
    boot = _stub_ptb_app(_raise_temp)
    # Patch settings locally to guarantee budget=3.
    with patch.object(settings, "telegram_webhook_max_attempts", 3):
        with patch.object(wh_service, "get_ptb_application", side_effect=boot):
            # asyncio.sleep inside the retry loop would be slow; mock sleep to 0.
            with patch("app.services.telegram_webhook.asyncio.sleep", new_callable=AsyncMock):
                resp = webhook_client.post(
                    _WH_URL,
                    json=_make_update_payload(update_id=100005),
                    headers={_SECRET_HEADER: _TEST_SECRET},
                )
    assert resp.status_code == 200
    body = resp.json()
    assert body["error_type"] == "NetworkError"
    assert body["state"] == "failed"
    assert body["retryable"] is False
    assert len(calls) == 3, f"expected budget-exhaustion 3 calls, got {len(calls)}"


# ---------------------------------------------------------------------------
# T6 Restart recovery: a stale ``claimed`` row is re-claimable by the next POST.
# ---------------------------------------------------------------------------

def test_t6_restart_stale_claimed_row_reclaimed(webhook_client, db_session):
    # Pre-plant a row in ``claimed`` state with created_at older than stale cutoff.
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

    calls = []

    async def _ok(u):
        calls.append(1)
        return None

    boot = _stub_ptb_app(_ok)
    with patch.object(wh_service, "get_ptb_application", side_effect=boot):
        resp = webhook_client.post(
            _WH_URL,
            json=_make_update_payload(update_id=100006),
            headers={_SECRET_HEADER: _TEST_SECRET},
        )
    assert resp.status_code == 200
    assert resp.json()["state"] == "done"
    assert len(calls) == 1, "stale claim must allow re-dispatch after restart"
    row = db_session.get(TelegramWebhookUpdate, 100006)
    assert row.state == TelegramWebhookState.done.value
    assert row.attempt_count == 2  # prior stale + fresh re-claim


# ---------------------------------------------------------------------------
# T7 Secret gating.
# ---------------------------------------------------------------------------

def test_t7_secret_unconfigured_returns_401(db_session):
    # Separate client WITHOUT the secret override in webhook_client fixture.
    def override_get_db():
        yield db_session
    app.dependency_overrides[get_db] = override_get_db
    # Reset the boot singleton again to be safe.
    wh_service._PTB_APP_READY = False
    wh_service._PTB_APP = None
    wh_service._PTB_APP_INIT_ERR = None
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
