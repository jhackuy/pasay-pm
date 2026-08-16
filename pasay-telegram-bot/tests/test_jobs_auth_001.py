"""JOB-SERVICE-AUTH-002: background proactive jobs authenticate as a real
SYSTEM principal, never as a fixed Owner fallback.

The jobs (v2_daily_digest / v2_next_check) read the deterministic endpoints
through a dedicated SYSTEM-keyed client (``PASSAY_JOB_API_KEY``). They never
bind the Owner's Telegram id, never send ``X-Telegram-User-Id``, and the
shared interactive client is never touched by a job. Without a SYSTEM key the
jobs are disabled (fail closed).
"""
from __future__ import annotations

import asyncio

import httpx

from pasay_bot.api_client import PasayApiClient
from pasay_bot.config import Settings
from pasay_bot.jobs import _send_digest, _send_next_check_reminders

from tests.conftest import FakeBackend


def _real_client(backend, key="sys-job-key"):
    return PasayApiClient(
        "http://test/api/v1", key, timeout=1.0,
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


def test_config_has_no_fixed_owner_telegram_id():
    settings = Settings()
    assert not hasattr(settings, "pasay_job_owner_telegram_id")
    # The SYSTEM job credential is opt-in; empty by default (fail closed).
    assert settings.pasay_job_api_key == ""


def test_jobs_disabled_without_system_credential():
    """Without PASSAY_JOB_API_KEY register_jobs registers nothing and never
    falls back to any other identity."""
    from pasay_bot.jobs import _build_job_api, register_jobs

    settings = Settings(pasay_job_api_key="")
    assert _build_job_api(settings) is None

    class _JobQueue:
        def __init__(self):
            self.entries = []

        def run_daily(self, cb, *, time, name):
            self.entries.append(("daily", name))

        def run_repeating(self, cb, *, interval, first, name):
            self.entries.append(("repeating", name))

    class _App:
        job_queue = _JobQueue()

    register_jobs(_App(), None, _EmptyStore(), settings, job_api=None)
    assert _App.job_queue.entries == []  # fail closed: no jobs, no fallback


def test_jobs_registered_with_system_credential():
    from pasay_bot.jobs import _build_job_api, register_jobs

    settings = Settings(pasay_job_api_key="sys-job-key", pasay_api_base="http://test/api/v1")
    job_api = _build_job_api(settings)
    assert job_api is not None
    try:

        class _JobQueue:
            def __init__(self):
                self.entries = []

            def run_daily(self, cb, *, time, name):
                self.entries.append(("daily", name))

            def run_repeating(self, cb, *, interval, first, name):
                self.entries.append(("repeating", name))

        class _App:
            job_queue = _JobQueue()

        register_jobs(_App(), None, _EmptyStore(), settings, job_api=job_api)
        names = [e[1] for e in _App.job_queue.entries]
        assert names == ["v2_daily_digest", "v2_next_check"]
    finally:
        asyncio.run(job_api.aclose())


def _requests_for(backend, method, path):
    return [
        (m, p, tg, auth)
        for (m, p, _), tg, auth in zip(
            backend.calls, backend.telegram_user_calls, backend.auth_calls
        )
        if m == method and p == path
    ]


def test_next_check_job_reads_as_system_never_binds_owner():
    backend = FakeBackend()
    backend.quick_tasks = []  # no due tasks -> no sends, but the read happens
    api = _real_client(backend)
    try:
        asyncio.run(_send_next_check_reminders(_HeadlessBot(), api, _EmptyStore()))
    finally:
        asyncio.run(api.aclose())

    calls = _requests_for(backend, "GET", "/operations/quick/tasks")
    assert calls, "job did not call /operations/quick/tasks"
    # SYSTEM job: no X-Telegram-User-Id header at all, only the SYSTEM key.
    assert calls[0][2] is None, calls[0]
    assert calls[0][3] == "Bearer sys-job-key", calls[0]
    # The shared client was never bound to any Telegram identity.
    assert api._telegram_user_id.get() is None


def test_digest_job_reads_as_system_never_binds_owner():
    backend = FakeBackend()
    backend.digest = {"pending": [], "in_progress": []}
    api = _real_client(backend)
    try:
        asyncio.run(_send_digest(_HeadlessBot(), api, _EmptyStore()))
    finally:
        asyncio.run(api.aclose())

    calls = _requests_for(backend, "GET", "/operations/digest")
    assert calls, "job did not call /operations/digest"
    assert calls[0][2] is None, calls[0]  # never X-Telegram-User-Id
    assert calls[0][3] == "Bearer sys-job-key", calls[0]
    assert api._telegram_user_id.get() is None


def test_job_401_is_swallowed_not_fatal():
    """A rejected/absent SYSTEM credential must degrade the job, never the bot."""
    backend = FakeBackend()
    backend.fail_status["/operations/quick/tasks"] = 401
    api = _real_client(backend)
    try:
        # PasayApiAuthError subclasses PasayApiError -> job logs and returns.
        asyncio.run(_send_next_check_reminders(_HeadlessBot(), api, _EmptyStore()))
        asyncio.run(_send_digest(_HeadlessBot(), api, _EmptyStore()))
    finally:
        asyncio.run(api.aclose())
    assert api._telegram_user_id.get() is None
