"""BOT-BACKEND-AUTH-001: background proactive jobs must authenticate.

The backend resolves these deterministic reads against a HUMAN subject via the
``X-Telegram-User-Id`` header; an unbound call is rejected 401. jobs.py now
binds the configured verified owner telegram id before the read and clears it
afterwards so no identity leaks into another async task.
"""
from __future__ import annotations

import asyncio

import httpx

from pasay_bot.api_client import PasayApiClient
from pasay_bot.config import Settings
from pasay_bot.jobs import _bind_owner_for_read, _clear_owner_for_read

from tests.conftest import FakeBackend


def _real_client(backend):
    return PasayApiClient(
        "http://test/api/v1", "k", timeout=1.0,
        transport=httpx.MockTransport(backend.handler),
    )


class _HeadlessBot:
    """Minimal bot; jobs only send when there is a known group (kept empty), so
    send_message never runs."""

    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text=None, **kw):
        self.sent.append(chat_id)
        return type("M", (), {"message_id": len(self.sent)})()


class _EmptyStore:
    def list_known_groups(self):
        return []


def test_config_default_is_verified_owner():
    assert Settings().pasay_job_owner_telegram_id == 5177241442
    assert Settings().pasay_job_owner_telegram_id > 0


def test_bind_owner_true_when_configured_false_when_disabled():
    class Shim:
        def __init__(self):
            self.cur = None

        def bind_telegram_user(self, uid):
            if not isinstance(uid, int) or uid <= 0:
                raise ValueError("bad id")
            self.cur = uid

        def clear_telegram_user(self):
            self.cur = None

    api = Shim()
    assert _bind_owner_for_read(api, Settings(pasay_job_owner_telegram_id=5177241442)) is True
    assert api.cur == 5177241442
    _clear_owner_for_read(api)
    assert api.cur is None

    api2 = Shim()
    assert _bind_owner_for_read(api2, Settings(pasay_job_owner_telegram_id=0)) is False
    assert api2.cur is None


def _requests_for(backend, method, path):
    return [
        (m, p, tg)
        for (m, p, _), tg in zip(backend.calls, backend.telegram_user_calls)
        if m == method and p == path
    ]


def test_next_check_job_sends_bound_header_then_clears():
    from pasay_bot import jobs

    backend = FakeBackend()
    backend.quick_tasks = []  # no due tasks -> no sends, but the read still happens
    api = _real_client(backend)
    try:
        asyncio.run(
            jobs._send_next_check_reminders(
                _HeadlessBot(), api, _EmptyStore(),
                Settings(pasay_job_owner_telegram_id=5177241442),
            )
        )
    finally:
        asyncio.run(api.aclose())

    calls = _requests_for(backend, "GET", "/operations/quick/tasks")
    assert calls, "job did not call /operations/quick/tasks"
    assert calls[0][2] == "5177241442", calls[0]
    # After the run the shared client is unbound again.
    assert api._telegram_user_id.get() is None


def test_digest_job_sends_bound_header_then_clears():
    from pasay_bot import jobs

    backend = FakeBackend()
    backend.digest = {"pending": [], "in_progress": []}
    api = _real_client(backend)
    try:
        asyncio.run(
            jobs._send_digest(
                _HeadlessBot(), api, _EmptyStore(),
                Settings(pasay_job_owner_telegram_id=5177241442),
            )
        )
    finally:
        asyncio.run(api.aclose())

    calls = _requests_for(backend, "GET", "/operations/digest")
    assert calls, "job did not call /operations/digest"
    assert calls[0][2] == "5177241442", calls[0]
    assert api._telegram_user_id.get() is None
