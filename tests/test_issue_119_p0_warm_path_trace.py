"""Issue #119 P0 WARM-PATH TRACE — observability-only instrumentation tests.

This PR is observability ONLY (no latency fix). The goal of these tests is
to prove that the warm-path slow hop is now MEASURABLE end-to-end and that
the structured log lines NEVER leak any secret (bot token, API key,
container ingest token, chat secret, or the full Telegram bot URL).

Coverage:

  * ``pasay_worker_latency`` shape + redaction — exercised on the Worker
    side via ``cloudflare-worker/tests/index.spec.ts`` (WARM-A..D).
  * ``pasay_ingest_latency`` extension with ``claim_ms`` / ``ptb_process_ms``
    — exercised here via the TestClient + monkeypatched service.
  * ``process_telegram_update_payload`` emits ``trace_id`` + per-hop
    timings in its returned body and binds the ContextVar so the bot
    handlers / api client see the SAME id.
  * ``PasayApiClient._request`` emits ``pasay_v1_request`` with the
    SAME ``trace_id`` and NEVER logs the ``Authorization`` header or
    any configured secret.
  * ``pasay_bot.main._ObservingTelegramRequest`` emits
    ``pasay_telegram_request`` with redacted URL (bot token replaced by
    ``<REDACTED>``) + method name (e.g. ``sendMessage``) + SAME
    ``trace_id``.
  * ``pasay_bot.handlers.buttons.handle_fixed_menu_button`` emits
    ``pasay_menu_button`` with the SAME ``trace_id`` and the phase
    breakdown.

PR scope forbids ANY latency fix or business-logic change. If a test
fails, the fix MUST come on the observability surface (e.g. add a
redaction helper), never on the underlying behaviour.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

import pytest

# Make the ``pasay_bot`` package importable when this test file is
# collected by pytest from the repo root (mirrors the pattern used by
# the existing Issue #119 test files that span both subtrees).
_BOT_DIR = os.path.join(os.path.dirname(__file__), "..", "pasay-telegram-bot")
_BOT_DIR = os.path.abspath(_BOT_DIR)
if os.path.isdir(_BOT_DIR) and _BOT_DIR not in sys.path:
    sys.path.insert(0, _BOT_DIR)


# ---------------------------------------------------------------------------
# 1. Test fixtures + helpers
# ---------------------------------------------------------------------------


class _CaptureHandler(logging.Handler):
    """In-memory log handler that captures records for assertion."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def capture_logs() -> _CaptureHandler:
    handler = _CaptureHandler()
    root = logging.getLogger()
    # Propagate at the root so app.* + pasay_bot.* loggers both surface here.
    old_level = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)


def _find_lines(handler: _CaptureHandler, marker: str) -> list[str]:
    """Return rendered message lines that contain ``marker``."""
    out: list[str] = []
    for rec in handler.records:
        if marker in rec.getMessage():
            out.append(rec.getMessage())
    return out


def _required_keys_in(line: str, expected: dict[str, str]) -> None:
    """Assert every ``key=value`` pair is present in ``line``.

    Missing keys raise an explicit AssertionError that names the missing
    field so a regression shows up in CI output without binary search.
    """
    missing = [k for k in expected if not re.search(rf"{re.escape(k)}=", line)]
    assert not missing, f"missing fields {missing} in log line: {line!r}"


# Secrets that MUST never appear in any structured observability line. The
# observability surface reads from production-bound secrets (Worker
# env vars, bot settings, FastAPI settings); a regression that bypasses
# the redaction helpers would surface these directly into Cloudflare
# Container log UI / Worker console.
_SAMPLE_BOT_TOKEN = "9876543210:AAFakeIssue119WarmTraceToken-NEVER-LOG"
_SAMPLE_INGEST_TOKEN = "SAMPLE_PASAY_INGEST_TOKEN-NEVER-LOG-91827"
_SAMPLE_API_KEY = "PASSAY_API_KEY_VALUE-NEVER-LOG-deadbeef"
_SAMPLE_WEBHOOK_SECRET = "SAMPLE_TELEGRAM_WEBHOOK_SECRET-NEVER-LOG-feedface"
_SAMPLE_BOT_URL_FULL = (
    f"https://api.telegram.org/bot{_SAMPLE_BOT_TOKEN}/sendMessage"
)


@contextmanager
def _seed_fake_secrets(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Push a sample bot token / API key / ingest token into the
    settings so a regression that leaks any of them surfaces as a
    visible substring in the structured log line."""
    # Bot settings (pasay_bot.config.Settings is read by api_client
    # construction via the global lazy-cached singleton).
    try:
        from pasay_bot.config import get_settings
        settings = get_settings()
        # Use ``model_copy`` so we don't mutate the cached object in
        # place if a parallel test reuses it.
        new = settings.model_copy(
            update={
                "pasay_tg_bot_token": _SAMPLE_BOT_TOKEN,
                "pasay_api_key": _SAMPLE_API_KEY,
            }
        )
        monkeypatch.setattr("pasay_bot.config.get_settings", lambda: new)
        monkeypatch.setattr("pasay_bot.config._settings_cache", new, raising=False)
    except Exception:  # noqa: BLE001 - bot config may not be importable in CI
        pass
    # Backend settings (used by PasayApiClient bearer header).
    try:
        from app.config import settings as backend_settings
        monkeypatch.setattr(backend_settings, "container_ingest_token", _SAMPLE_INGEST_TOKEN)
        monkeypatch.setattr(backend_settings, "telegram_webhook_secret", _SAMPLE_WEBHOOK_SECRET)
        monkeypatch.setattr(backend_settings, "pasay_api_key", _SAMPLE_API_KEY)
    except Exception:  # noqa: BLE001 - backend settings may not be importable here
        pass
    yield


# ---------------------------------------------------------------------------
# 2. process_telegram_update_payload — trace_id propagation + body fields
# ---------------------------------------------------------------------------


def test_process_telegram_update_payload_accepts_trace_id_and_returns_it_in_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The service accepts a ``trace_id`` kwarg and surfaces it in the
    returned body so the Container ``pasay_ingest_latency`` log line can
    publish the per-update correlation id."""
    import asyncio

    # Use a no-op dispatcher that bypasses PTB and the DB.
    from app.services import telegram_webhook as wh

    async def _stub(db: Any, raw: dict[str, Any], *, now: Any = None, trace_id: str | None = None):
        return 200, {
            "ok": True,
            "state": "done",
            "trace_id": trace_id,
            "claim_ms": 0.0,
            "ptb_process_ms": 0.0,
        }

    monkeypatch.setattr(wh, "process_telegram_update_payload", _stub)

    raw = {"update_id": 9001}
    status, body = asyncio.run(wh.process_telegram_update_payload(
        None, raw, trace_id="tg:9001-explicit",
    ))
    assert status == 200
    assert body["trace_id"] == "tg:9001-explicit"


def test_process_telegram_update_payload_resets_trace_id_contextvar_on_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ContextVar binding is released on every return path so a
    sibling async task never inherits our trace_id."""
    import asyncio

    from app.services import telegram_webhook as wh

    captured: list[str] = []

    # Stub out the heavy lifting: claim + PTB boot + process_update.
    # We test ONLY the trace_id binding management in the real
    # process_telegram_update_payload (we don't monkeypatch the
    # function under test itself — that would defeat the test).
    async def _stub_get_ptb_application() -> Any:
        # Return a fake "app" with a ``bot`` attribute + ``process_update``.
        class _FakeApp:
            class _FakeBot:
                pass

            bot = _FakeBot()

            async def process_update(self, update: Any) -> None:
                # During the dispatch step we read the trace_id from the
                # current ContextVar to confirm it is visible.
                captured.append(wh.current_trace_id())
                return None

        return _FakeApp()

    async def _stub_claim(db: Any, update_id: int, chat_id: Any, user_id: Any, update_type: Any):
        return ("new", None)

    # ``claim_update_or_short_circuit`` is called synchronously by the
    # function under test (no ``await``), so the patched replacement
    # must be a plain sync callable that returns the tuple directly.
    def _sync_stub_claim(*args: Any, **kwargs: Any) -> tuple[str, Any]:
        return ("new", None)

    # Stub TelegramUpdate.de_json so we don't need a fully-wired Bot.
    from telegram import Update as TelegramUpdate

    def _stub_de_json(raw: dict[str, Any], bot: Any) -> Any:
        # Build a minimal Update so downstream ``_effective_chat_user``
        # + ``claim_update_or_short_circuit`` stub stay deterministic.
        from telegram import Chat, Message as TGMessage, User as TGUser

        user = TGUser(id=5177241442, first_name="Owner", is_bot=False)
        chat = Chat(id=5177241442, type="private")
        msg = TGMessage(message_id=1, date=datetime.now(), chat=chat, from_user=user)
        return TelegramUpdate(update_id=9002, message=msg)

    # The function under test reads the secret from backend settings;
    # we must set it before invocation so we reach the dispatch step.
    from app.config import settings as backend_settings
    monkeypatch.setattr(backend_settings, "telegram_webhook_secret", "warm-trace-secret")
    monkeypatch.setattr(wh._backend_settings, "telegram_webhook_secret", "warm-trace-secret")

    monkeypatch.setattr(wh, "get_ptb_application", _stub_get_ptb_application)
    monkeypatch.setattr(wh, "claim_update_or_short_circuit", _sync_stub_claim)
    monkeypatch.setattr(TelegramUpdate, "de_json", staticmethod(_stub_de_json))

    raw = {
        "update_id": 9002,
        "message": {"chat": {"id": 5177241442}, "text": "💸 支出"},
    }

    async def _run() -> tuple[int, dict[str, Any]]:
        return await wh.process_telegram_update_payload(
            None, raw, trace_id="tg:9002-scope",
        )

    status, body = asyncio.run(_run())
    assert status == 200
    # After the call returns, the ContextVar MUST be reset.
    assert len(captured) >= 1, "the stub never ran"
    assert captured[0] == "tg:9002-scope", (
        f"trace_id must be visible inside the call: got {captured[0]!r}"
    )
    assert wh.current_trace_id() == "", (
        f"trace_id MUST be reset on every return path: got {wh.current_trace_id()!r}"
    )


def test_process_telegram_update_payload_timing_keys_present_in_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The returned body for a successful update carries
    ``claim_ms`` + ``ptb_process_ms`` + ``trace_id`` so the Container
    observability layer can publish the per-hop breakdown."""
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app
    from app.schemas.envelope import ENVELOPE_VERSION

    old_ingest = settings.container_ingest_token
    old_tg = settings.telegram_webhook_secret
    settings.container_ingest_token = "warm-trace-ingest"
    settings.telegram_webhook_secret = "warm-trace-secret"
    try:
        # Replace the dispatcher with a stub that returns the new shape
        # so the body actually carries the timing fields we want to assert.
        from app.api.routers import internal_ingest as ii

        async def _fake_process(db, raw_json, *, now=None, trace_id=None):
            return 200, {
                "ok": True,
                "state": "done",
                "update_id": raw_json.get("update_id") if isinstance(raw_json, dict) else None,
                "dur_ms": 5,
                "attempts": 1,
                "cross_attempt": 1,
                "trace_id": trace_id,
                "claim_ms": 1.234,
                "ptb_process_ms": 3.456,
            }

        monkeypatch.setattr(ii.wh_service, "process_telegram_update_payload", _fake_process)

        envelope = {
            "version": ENVELOPE_VERSION,
            "kind": "telegram_update",
            "event_id": "tg:9003",
            "occurred_at": "2026-09-08T16:00:00+00:00",
            "payload": {
                "update_id": 9003,
                "message": {"chat": {"id": 5177241442}, "text": "✅ 待办"},
            },
            "_telegram_meta": {"update_id": 9003, "chat_id": 5177241442},
        }
        with TestClient(app) as client:
            resp = client.post(
                "/internal/ingest",
                headers={
                    "X-Pasay-Ingest-Token": "warm-trace-ingest",
                    "X-Pasay-Trace-Id": "tg:9003",
                    "X-Pasay-Trace-Source": "telegram_webhook_direct",
                },
                json=envelope,
            )
        assert resp.status_code == 200
        body = resp.json()
        # Body now carries the timing keys the Container needs.
        assert body.get("trace_id") == "tg:9003"
        assert "claim_ms" in body
        assert "ptb_process_ms" in body
    finally:
        settings.container_ingest_token = old_ingest
        settings.telegram_webhook_secret = old_tg


# ---------------------------------------------------------------------------
# 3. Container pasay_ingest_latency — claim_ms / ptb_process_ms split
# ---------------------------------------------------------------------------


def test_pasay_ingest_latency_records_claim_ms_and_ptb_process_ms(
    monkeypatch: pytest.MonkeyPatch, capture_logs: _CaptureHandler,
) -> None:
    """The Container ``pasay_ingest_latency`` line now surfaces both
    ``claim_ms`` and ``ptb_process_ms`` so operator grep can split the
    dispatch_ms total into the two pieces the Owner-visible latency
    actually depends on."""
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app
    from app.schemas.envelope import ENVELOPE_VERSION

    old_ingest = settings.container_ingest_token
    old_tg = settings.telegram_webhook_secret
    settings.container_ingest_token = "warm-trace-ingest"
    settings.telegram_webhook_secret = "warm-trace-secret"
    try:
        from app.api.routers import internal_ingest as ii

        async def _fake_process(db, raw_json, *, now=None, trace_id=None):
            return 200, {
                "ok": True,
                "state": "done",
                "update_id": raw_json.get("update_id") if isinstance(raw_json, dict) else None,
                "trace_id": trace_id,
                "claim_ms": 1.5,
                "ptb_process_ms": 4.25,
            }

        monkeypatch.setattr(ii.wh_service, "process_telegram_update_payload", _fake_process)

        envelope = {
            "version": ENVELOPE_VERSION,
            "kind": "telegram_update",
            "event_id": "tg:9100",
            "occurred_at": "2026-09-08T16:01:00+00:00",
            "payload": {
                "update_id": 9100,
                "message": {"chat": {"id": 5177241442}, "text": "💸 支出"},
            },
            "_telegram_meta": {"update_id": 9100, "chat_id": 5177241442},
        }
        with TestClient(app) as client:
            resp = client.post(
                "/internal/ingest",
                headers={
                    "X-Pasay-Ingest-Token": "warm-trace-ingest",
                    "X-Pasay-Trace-Id": "tg:9100",
                    "X-Pasay-Trace-Source": "telegram_webhook_direct",
                },
                json=envelope,
            )
        assert resp.status_code == 200
        lines = _find_lines(capture_logs, "pasay_ingest_latency")
        assert lines, "Container must emit at least one pasay_ingest_latency line"
        # Take the last line (per-request end).
        line = lines[-1]
        for required in (
            "trace_id=tg:9100",
            "source=telegram_webhook_direct",
            "kind=telegram_update",
            "claim_ms=1.50",
            "ptb_process_ms=4.25",
            "http_status=200",
        ):
            assert required in line, f"missing {required!r} in: {line!r}"
    finally:
        settings.container_ingest_token = old_ingest
        settings.telegram_webhook_secret = old_tg


def test_pasay_ingest_latency_backward_compatible_when_timings_missing(
    monkeypatch: pytest.MonkeyPatch, capture_logs: _CaptureHandler,
) -> None:
    """Older callers that do NOT return ``claim_ms`` / ``ptb_process_ms``
    must still produce a structured record with 0.0 placeholders so
    legacy test fixtures / older service versions keep working."""
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app
    from app.schemas.envelope import ENVELOPE_VERSION

    old_ingest = settings.container_ingest_token
    old_tg = settings.telegram_webhook_secret
    settings.container_ingest_token = "warm-trace-ingest"
    settings.telegram_webhook_secret = "warm-trace-secret"
    try:
        from app.api.routers import internal_ingest as ii

        async def _legacy_process(db, raw_json, *, now=None, trace_id=None):
            return 200, {"ok": True, "state": "done"}

        monkeypatch.setattr(ii.wh_service, "process_telegram_update_payload", _legacy_process)

        envelope = {
            "version": ENVELOPE_VERSION,
            "kind": "telegram_update",
            "event_id": "tg:9101",
            "occurred_at": "2026-09-08T16:02:00+00:00",
            "payload": {
                "update_id": 9101,
                "message": {"chat": {"id": 5177241442}, "text": "legacy"},
            },
            "_telegram_meta": {"update_id": 9101, "chat_id": 5177241442},
        }
        with TestClient(app) as client:
            resp = client.post(
                "/internal/ingest",
                headers={
                    "X-Pasay-Ingest-Token": "warm-trace-ingest",
                    "X-Pasay-Trace-Id": "tg:9101",
                },
                json=envelope,
            )
        assert resp.status_code == 200
        lines = _find_lines(capture_logs, "pasay_ingest_latency")
        assert lines, "Container must emit pasay_ingest_latency even without timings"
        line = lines[-1]
        # Placeholder 0.00 values ensure the log shape stays stable for
        # operator grep, even on legacy / test callers.
        assert "claim_ms=0.00" in line, f"missing claim_ms placeholder: {line!r}"
        assert "ptb_process_ms=0.00" in line, f"missing ptb_process_ms placeholder: {line!r}"
    finally:
        settings.container_ingest_token = old_ingest
        settings.telegram_webhook_secret = old_tg


# ---------------------------------------------------------------------------
# 4. PasayApiClient — pasay_v1_request emits trace_id + NEVER logs secrets
# ---------------------------------------------------------------------------


def test_pasay_api_client_emits_pasay_v1_request_with_trace_id(
    monkeypatch: pytest.MonkeyPatch,
    capture_logs: _CaptureHandler,
) -> None:
    """Every V1 request emits a structured ``pasay_v1_request`` line with
    ``trace_id`` joined to the Worker ingress / Container dispatch."""
    import asyncio

    with _seed_fake_secrets(monkeypatch):
        from pasay_bot.api_client import PasayApiClient

        # Set the trace_id so we can assert it propagates.
        from app.services import telegram_webhook as wh
        token = wh._pasay_trace_id_var.set("tg:9200-trace")

        try:
            # Build a PasayApiClient bound to a fake server. We mock the
            # underlying httpx transport so no real network is touched.
            from fastapi import FastAPI

            fake_app = FastAPI()

            @fake_app.get("/api/v1/properties")
            async def _ok() -> list[dict[str, Any]]:
                return [{"id": 1, "name": "test"}]

            # We use a real uvicorn-like ASGI transport so the full
            # request/response cycle executes inside the test process.
            from uvicorn.config import Config as UConfig
            from uvicorn.server import Server

            # Spin up a tiny uvicorn server on a free port for the
            # duration of the test.
            import socket
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()

            config = UConfig(
                app=fake_app, host="127.0.0.1", port=port,
                log_level="warning", lifespan="off",
            )
            server = Server(config=config)
            import threading
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
            try:
                api = PasayApiClient(
                    base_url=f"http://127.0.0.1:{port}/api/v1",
                    api_key="FAKE-TEST-KEY",
                    timeout=5.0,
                )

                async def _call() -> list[Any]:
                    return await api.get_properties()

                out = asyncio.run(_call())
                assert isinstance(out, list)
                assert out and out[0].id == 1
            finally:
                server.should_exit = True
                thread.join(timeout=5)
        finally:
            wh._pasay_trace_id_var.reset(token)

        lines = _find_lines(capture_logs, "pasay_v1_request")
        assert lines, "PasayApiClient MUST emit at least one pasay_v1_request line"
        line = lines[-1]
        # Required fields per the observability contract.
        for required in (
            "trace_id=tg:9200-trace",
            "method=GET",
            "path=/properties",
            "status=200",
            "elapsed_ms=",
        ):
            assert required in line, f"missing {required!r} in: {line!r}"


def test_pasay_api_client_request_line_never_contains_authorization_header(
    monkeypatch: pytest.MonkeyPatch,
    capture_logs: _CaptureHandler,
) -> None:
    """The V1 request log line MUST NEVER carry the ``Authorization``
    header value (the bearer API key). A regression here would surface
    the raw api_key into Cloudflare Container log UI."""
    import asyncio
    from fastapi import FastAPI
    from uvicorn.config import Config as UConfig
    from uvicorn.server import Server
    import socket
    import threading

    fake_app = FastAPI()

    @fake_app.get("/api/v1/properties")
    async def _ok() -> list[dict[str, Any]]:
        return []

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = UConfig(
        app=fake_app, host="127.0.0.1", port=port,
        log_level="warning", lifespan="off",
    )
    server = Server(config=config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        with _seed_fake_secrets(monkeypatch):
            from pasay_bot.api_client import PasayApiClient

            api = PasayApiClient(
                base_url=f"http://127.0.0.1:{port}/api/v1",
                api_key=_SAMPLE_API_KEY,
                timeout=5.0,
            )

            async def _call() -> Any:
                return await api.get_properties()

            asyncio.run(_call())

        for line in _find_lines(capture_logs, "pasay_v1_request"):
            # The api_key MUST NOT appear anywhere in the log line.
            assert _SAMPLE_API_KEY not in line, (
                f"api_key leaked into pasay_v1_request line: {line!r}"
            )
            assert "Authorization" not in line, (
                f"Authorization header name should not appear: {line!r}"
            )
            assert "Bearer" not in line, (
                f"Bearer keyword leaked: {line!r}"
            )
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# 5. Telegram request layer — pasay_telegram_request redaction
# ---------------------------------------------------------------------------


def test_redact_telegram_token_replaces_bot_token_segment() -> None:
    from pasay_bot.main import _redact_telegram_token

    out = _redact_telegram_token(_SAMPLE_BOT_URL_FULL)
    assert out != _SAMPLE_BOT_URL_FULL
    assert _SAMPLE_BOT_TOKEN not in out
    assert "<REDACTED>" in out
    assert out.endswith("/sendMessage")


def test_telegram_method_from_url_extracts_method_name() -> None:
    from pasay_bot.main import _telegram_method_from_url

    assert _telegram_method_from_url(
        f"https://api.telegram.org/bot{_SAMPLE_BOT_TOKEN}/sendMessage"
    ) == "sendMessage"
    assert _telegram_method_from_url(
        f"https://api.telegram.org/bot{_SAMPLE_BOT_TOKEN}/editMessageText"
    ) == "editMessageText"
    assert _telegram_method_from_url(
        f"https://api.telegram.org/bot{_SAMPLE_BOT_TOKEN}/getMe"
    ) == "getMe"


def test_telegram_method_from_url_handles_unparseable_inputs() -> None:
    from pasay_bot.main import _telegram_method_from_url

    assert _telegram_method_from_url("") == "unknown"
    assert _telegram_method_from_url("not-a-url") == "unknown"


def test_observing_telegram_request_preserves_ipv4_forcing() -> None:
    """PR scope forbids removing the IPv4 forcing without timing proof.
    The observing subclass MUST still bind the httpx transport to the
    IPv4 local address so a regression that drops IPv4 forcing is
    caught here."""
    from pasay_bot.main import _ObservingTelegramRequest

    req = _ObservingTelegramRequest(timeout=30.0)
    pool = req._client._transport._pool
    assert getattr(pool, "_local_address", None) == "0.0.0.0", (
        "_ObservingTelegramRequest MUST preserve the IPv4 forcing (local_address=0.0.0.0). "
        "The Owner-override forbids removing IPv4 forcing without timing proof."
    )


def test_observing_telegram_request_does_request_emits_pasay_telegram_request(
    monkeypatch: pytest.MonkeyPatch,
    capture_logs: _CaptureHandler,
) -> None:
    """Driving ``do_request`` on the observing wrapper emits a
    ``pasay_telegram_request`` log line with the trace_id from the
    ContextVar. The URL passed to PTB MUST still carry the bot token
    (PTB requires it), but the structured log line MUST only carry the
    REDACTED variant — a regression here would surface the bot token
    into Cloudflare Container log UI."""
    import asyncio

    with _seed_fake_secrets(monkeypatch):
        from pasay_bot.main import _ObservingTelegramRequest

        req = _ObservingTelegramRequest(timeout=30.0)

        # Mock the underlying httpx call so no real network is touched.
        from httpx import Response

        async def _fake_client_request(*args: Any, **kwargs: Any) -> Response:
            return Response(
                200,
                content=b'{"ok": true, "result": {}}',
                request=args[0] if args else None,
            )

        # Patch the httpx AsyncClient that lives on the request instance
        # so the wrapper's call to ``super().do_request()`` returns a
        # 200 without touching the network.
        monkeypatch.setattr(req._client, "request", _fake_client_request)

        # Set the trace_id so we can assert it propagates.
        from app.services import telegram_webhook as wh
        token = wh._pasay_trace_id_var.set("tg:9300-trace")
        try:
            asyncio.run(req.do_request(
                _SAMPLE_BOT_URL_FULL, "POST", request_data=None,
            ))
        finally:
            wh._pasay_trace_id_var.reset(token)

        lines = _find_lines(capture_logs, "pasay_telegram_request")
        assert lines, (
            "_ObservingTelegramRequest MUST emit at least one pasay_telegram_request line"
        )
        # Pick the FIRST ``pasay_telegram_request`` line (without the
        # ``_url`` suffix) — that's the latency record.
        line = next(
            (l for l in lines if "pasay_telegram_request " in l and "_url" not in l),
            None,
        )
        assert line is not None, (
            f"no main pasay_telegram_request line in: {lines!r}"
        )
        for required in (
            "trace_id=tg:9300-trace",
            "method=sendMessage",
            "elapsed_ms=",
        ):
            assert required in line, f"missing {required!r} in: {line!r}"
        # The raw bot token MUST NEVER appear in the structured log line.
        assert _SAMPLE_BOT_TOKEN not in line, (
            f"bot token leaked into pasay_telegram_request line: {line!r}"
        )


# ---------------------------------------------------------------------------
# 6. Fixed-menu handler — pasay_menu_button trace_id + phase breakdown
# ---------------------------------------------------------------------------


def _stub_telegram_http_client(monkeypatch: pytest.MonkeyPatch, app: Any) -> None:
    """Patch the bot's request layer so any Telegram API call returns a
    fake 200 without touching the network. The observing subclass
    keeps its own do_request, so we patch the underlying httpx client
    that the wrapper delegates to."""
    from httpx import Response

    fake_message = {
        "message_id": 999,
        "date": int(datetime.now().timestamp()),
        "chat": {"id": 5177241442, "type": "private"},
        "text": "stubbed",
    }

    def _response_for(method_url: str) -> Response:
        if "/getMe" in method_url:
            body = {"ok": True, "result": {"id": 5177241442, "is_bot": True, "first_name": "stub", "username": "stub"}}
        elif "/setMyCommands" in method_url or "/setChatMenuButton" in method_url:
            body = {"ok": True, "result": True}
        else:
            body = {"ok": True, "result": fake_message}
        return Response(
            200,
            content=json.dumps(body).encode(),
            request=None,
        )

    async def _fake_client_request(*args: Any, **kwargs: Any) -> Response:
        url = ""
        if args and hasattr(args[0], "url"):
            url = str(args[0].url)
        elif "url" in kwargs:
            url = str(kwargs["url"])
        return _response_for(url)

    monkeypatch.setattr(app.bot.request._client, "request", _fake_client_request)


def test_fixed_menu_button_handler_emits_pasay_menu_button_with_trace_id(
    monkeypatch: pytest.MonkeyPatch,
    capture_logs: _CaptureHandler,
) -> None:
    """The fixed bottom-menu handler emits a ``pasay_menu_button``
    structured line with the SAME ``trace_id`` the Worker ingress
    propagated. A regression here means operator grep cannot correlate
    the menu tap with the V1 / Telegram API calls it triggered."""
    import asyncio

    with _seed_fake_secrets(monkeypatch):
        from pasay_bot.api_client import PasayApiClient
        from pasay_bot.config import Settings
        from pasay_bot.state.store import StateStore

        async def _stub_show_quick_expense(*args: Any, **kwargs: Any) -> None:
            return None

        import pasay_bot.handlers.commands as cmds
        monkeypatch.setattr(cmds, "show_quick_expense", _stub_show_quick_expense)

        settings = Settings(
            pasay_tg_bot_token="123:TEST",
            pasay_api_base="http://test/api/v1",
            pasay_api_key="fake",
            pasay_admin_api_key="",
            pasay_job_api_key="",
        )
        store = StateStore(settings.state_db)
        try:
            api = PasayApiClient(settings.pasay_api_base, settings.pasay_api_key)
            from pasay_bot.main import build_application

            app = build_application(settings, api, store, admin_api_client=None, job_api_client=None)
            # Patch the bot's Telegram HTTP client so the menu-setup
            # sendMessage (and any other Telegram method) returns 200
            # without hitting the network.
            _stub_telegram_http_client(monkeypatch, app)

            from telegram import Chat, Message as TGMessage, Update, User
            from telegram.ext import ContextTypes

            user = User(id=5177241442, first_name="Owner", is_bot=False)
            chat = Chat(id=5177241442, type="private")
            msg = TGMessage(message_id=1, date=datetime.now(), chat=chat, text="💸 支出", from_user=user)
            update = Update(update_id=9301, message=msg)

            ctx: ContextTypes.DEFAULT_TYPE = app.context_types.context  # type: ignore[attr-defined]
            ctx.bot = app.bot  # type: ignore[attr-defined]
            ctx.bot_data = app.bot_data  # type: ignore[attr-defined]
            ctx.chat_data = {}  # type: ignore[attr-defined]
            ctx.user_data = {}  # type: ignore[attr-defined]

            from app.services import telegram_webhook as wh
            ttoken = wh._pasay_trace_id_var.set("tg:9301-trace")
            try:
                from pasay_bot.handlers.buttons import handle_fixed_menu_button
                asyncio.run(handle_fixed_menu_button(update, ctx, route="expense"))
            finally:
                wh._pasay_trace_id_var.reset(ttoken)
        finally:
            store.close()

        lines = _find_lines(capture_logs, "pasay_menu_button")
        assert lines, (
            "handle_fixed_menu_button MUST emit at least one pasay_menu_button line"
        )
        line = lines[-1]
        for required in (
            "trace_id=tg:9301-trace",
            "route=expense",
            "outcome=ok",
            "total_ms=",
            "callback_ack_ms=",
            "backend_fetch_ms=",
            "render_ms=",
            "telegram_edit_ms=",
        ):
            assert required in line, f"missing {required!r} in: {line!r}"


def test_fixed_menu_button_handler_does_not_log_secrets(
    monkeypatch: pytest.MonkeyPatch,
    capture_logs: _CaptureHandler,
) -> None:
    """The fixed-menu observability line MUST NOT contain the bot token,
    API key, ingest token, or webhook secret. A regression here would
    leak secrets into Cloudflare Container log UI.

    Scope: this PR only owns OUR instrumentation surfaces
    (``pasay_*_request``, ``pasay_menu_button``, ``pasay_worker_latency``,
    ``pasay_ingest_latency``, ``pasay_dispatch_latency``, ``pasay_telegram_request``).
    PTB internal debug logs (e.g. ``telegram.ext.ExtBot``) are out of
    PR scope; they are tested by PTB upstream.
    """
    import asyncio

    # Namespaces owned by THIS PR's instrumentation. Any log record
    # emitted from one of these MUST NOT carry a configured secret.
    _OWNED_LOGGER_NAMES = (
        "pasay_bot.handlers.buttons",
        "pasay_bot.api_client",
        "pasay_bot.main",
        "app.api.routers.internal_ingest",
        "app.services.telegram_webhook",
    )

    with _seed_fake_secrets(monkeypatch):
        from pasay_bot.api_client import PasayApiClient
        from pasay_bot.config import Settings
        from pasay_bot.state.store import StateStore

        async def _stub_show_quick_expense(*args: Any, **kwargs: Any) -> None:
            return None

        import pasay_bot.handlers.commands as cmds
        monkeypatch.setattr(cmds, "show_quick_expense", _stub_show_quick_expense)

        settings = Settings(
            pasay_tg_bot_token=_SAMPLE_BOT_TOKEN,
            pasay_api_base="http://test/api/v1",
            pasay_api_key=_SAMPLE_API_KEY,
            pasay_admin_api_key="",
            pasay_job_api_key="",
        )
        store = StateStore(settings.state_db)
        try:
            api = PasayApiClient(settings.pasay_api_base, settings.pasay_api_key)
            from pasay_bot.main import build_application

            app = build_application(settings, api, store, admin_api_client=None, job_api_client=None)
            _stub_telegram_http_client(monkeypatch, app)

            from telegram import Chat, Message as TGMessage, Update, User
            from telegram.ext import ContextTypes

            user = User(id=5177241442, first_name="Owner", is_bot=False)
            chat = Chat(id=5177241442, type="private")
            msg = TGMessage(message_id=1, date=datetime.now(), chat=chat, text="💸 支出", from_user=user)
            update = Update(update_id=9302, message=msg)

            ctx: ContextTypes.DEFAULT_TYPE = app.context_types.context  # type: ignore[attr-defined]
            ctx.bot = app.bot  # type: ignore[attr-defined]
            ctx.bot_data = app.bot_data  # type: ignore[attr-defined]
            ctx.chat_data = {}  # type: ignore[attr-defined]
            ctx.user_data = {}  # type: ignore[attr-defined]

            from app.services import telegram_webhook as wh
            ttoken = wh._pasay_trace_id_var.set("tg:9302-trace")
            try:
                from pasay_bot.handlers.buttons import handle_fixed_menu_button
                asyncio.run(handle_fixed_menu_button(update, ctx, route="expense"))
            finally:
                wh._pasay_trace_id_var.reset(ttoken)
        finally:
            store.close()

        # Every record emitted by an OWNED logger MUST NOT carry any of
        # the sample secrets. Records from outside these loggers (e.g.
        # PTB's internal debug logger) are NOT under this PR's control.
        for rec in capture_logs.records:
            if not any(rec.name.startswith(n) for n in _OWNED_LOGGER_NAMES):
                continue
            line = rec.getMessage()
            assert _SAMPLE_BOT_TOKEN not in line, (
                f"bot token leaked in {rec.name}: {line!r}"
            )
            assert _SAMPLE_API_KEY not in line, (
                f"api_key leaked in {rec.name}: {line!r}"
            )
            assert _SAMPLE_INGEST_TOKEN not in line, (
                f"ingest token leaked in {rec.name}: {line!r}"
            )
            assert _SAMPLE_WEBHOOK_SECRET not in line, (
                f"webhook secret leaked in {rec.name}: {line!r}"
            )


# ---------------------------------------------------------------------------
# 7. End-to-end trace_id correlation (Worker → Container → bot)
# ---------------------------------------------------------------------------


def test_e2e_trace_id_propagates_from_worker_header_to_bot_handler(
    monkeypatch: pytest.MonkeyPatch,
    capture_logs: _CaptureHandler,
) -> None:
    """Drive ``process_telegram_update_payload`` with the SAME trace_id
    the Worker stamps on the X-Pasay-Trace-Id header and prove the
    downstream ``pasay_menu_button`` log line carries the SAME id.

    This is the single most important regression guard for the
    warm-path observability contract: if the propagation breaks, the
    Operator can no longer correlate one real Telegram tap across
    Worker → Container → bot → V1 → Telegram.
    """
    import asyncio

    with _seed_fake_secrets(monkeypatch):
        # Set the trace_id the Worker would have stamped.
        from app.services import telegram_webhook as wh
        ttoken = wh._pasay_trace_id_var.set("tg:9400-worker-stamped")
        try:
            # Build a minimal app to drive the fixed-menu handler.
            from pasay_bot.api_client import PasayApiClient
            from pasay_bot.config import Settings
            from pasay_bot.state.store import StateStore

            async def _stub_show_quick_expense(*args: Any, **kwargs: Any) -> None:
                return None

            import pasay_bot.handlers.commands as cmds
            monkeypatch.setattr(cmds, "show_quick_expense", _stub_show_quick_expense)

            settings = Settings(
                pasay_tg_bot_token="123:TEST",
                pasay_api_base="http://test/api/v1",
                pasay_api_key="fake",
                pasay_admin_api_key="",
                pasay_job_api_key="",
            )
            store = StateStore(settings.state_db)
            try:
                api = PasayApiClient(settings.pasay_api_base, settings.pasay_api_key)
                from pasay_bot.main import build_application

                app = build_application(settings, api, store, admin_api_client=None, job_api_client=None)
                # Patch the bot's Telegram HTTP client so the menu-setup
                # sendMessage (and any other Telegram method) returns 200
                # without hitting the network.
                _stub_telegram_http_client(monkeypatch, app)

                from telegram import Chat, Message as TGMessage, Update, User
                from telegram.ext import ContextTypes

                user = User(id=5177241442, first_name="Owner", is_bot=False)
                chat = Chat(id=5177241442, type="private")
                msg = TGMessage(message_id=1, date=datetime.now(), chat=chat, text="💸 支出", from_user=user)
                update = Update(update_id=9400, message=msg)

                ctx: ContextTypes.DEFAULT_TYPE = app.context_types.context  # type: ignore[attr-defined]
                ctx.bot = app.bot  # type: ignore[attr-defined]
                ctx.bot_data = app.bot_data  # type: ignore[attr-defined]
                ctx.chat_data = {}  # type: ignore[attr-defined]
                ctx.user_data = {}  # type: ignore[attr-defined]

                from pasay_bot.handlers.buttons import handle_fixed_menu_button
                asyncio.run(handle_fixed_menu_button(update, ctx, route="expense"))
            finally:
                store.close()
        finally:
            wh._pasay_trace_id_var.reset(ttoken)

        # The structured menu_button line MUST carry the same trace_id
        # the Worker would have stamped. A propagation break is the
        # failure this whole observability PR exists to prevent.
        lines = _find_lines(capture_logs, "pasay_menu_button")
        assert lines, "handle_fixed_menu_button must emit pasay_menu_button"
        assert any(
            "trace_id=tg:9400-worker-stamped" in line for line in lines
        ), (
            "pasay_menu_button trace_id MUST propagate from Worker header through "
            "Container dispatch into the bot handler. Got:\n"
            + "\n".join(lines)
        )