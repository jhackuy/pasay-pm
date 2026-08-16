"""TELEGRAM-IPV4-TRANSPORT-001 acceptance tests for the PTB IPv4-forced transport.

Verifies, without touching the network:
- the httpx async transport built by _force_ipv4_transport() is bound to an IPv4
  local address (0.0.0.0), which is what forces httpcore/anyio to resolve and
  connect the remote api.telegram.org over IPv4 only (no hardcoded Telegram IP);
- _ipv4_httpx_request(timeout) and _TraceUpdatesRequest() both carry that
  transport (default request + the get_updates poller);
- the get_updates poll logging distinguishes a real empty response (NO_UPDATES)
  from a network/transport failure (ERROR + re-raise, never a fake "count=0").
"""
from __future__ import annotations

from pasay_bot.main import (
    _TraceUpdatesRequest,
    _force_ipv4_transport,
    _ipv4_httpx_request,
)


def _pool_local_address(request):
    """The httpcore AsyncConnectionPool's forced local binding address."""
    client = request._client
    transport = getattr(client, "_transport", None)
    assert transport is not None, "request client must have a transport"
    pool = getattr(transport, "_pool", None)
    assert pool is not None, "transport must have a connection pool"
    return getattr(pool, "_local_address", None)


def test_force_ipv4_transport_binds_ipv4_local_address():
    tr = _force_ipv4_transport()
    client = tr
    pool = client._pool
    assert getattr(pool, "_local_address", None) == "0.0.0.0", (
        "transport must force IPv4 by binding to the 0.0.0.0 local address"
    )


def test_ipv4_httpx_request_forces_ipv4():
    req = _ipv4_httpx_request(30)
    assert _pool_local_address(req) == "0.0.0.0"


def test_trace_updates_request_forces_ipv4():
    req = _TraceUpdatesRequest()
    assert _pool_local_address(req) == "0.0.0.0"


def test_post_logs_genuine_empty_as_no_updates(capsys):
    """A real Telegram empty response is the ONLY path logged as NO_UPDATES."""
    req = _TraceUpdatesRequest()

    async def _run():
        # Push an empty round-trip through the real post(); no network call is
        # made because the underlying mock monkeypatch below supplies the data.
        return await req.post("https://api.telegram.org/bot<REDACTED>/getUpdates")

    # Monkeypatch the base transport call so post() returns an empty list
    # directly, simulating Telegram answering with zero pending updates.
    async def _fake_do_request(*a, **k):
        return 200, b'{"ok": true, "result": []}'

    _orig = req._client.request
    import asyncio

    async def _fake_client_request(*a, **k):
        import json
        return type(
            "Resp",
            (),
            {"status_code": 200, "content": json.dumps({"ok": True, "result": []}).encode()},
        )()

    req._client.request = _fake_client_request
    try:
        asyncio.run(_run())
    finally:
        req._client.request = _orig

    out = capsys.readouterr().out
    assert "NO_UPDATES" in out, out
    assert "RETURN count=0" not in out, out
    assert "ERROR getUpdates" not in out, out


def test_post_reraise_network_failure_not_count0(capsys):
    """A network failure is re-raised as a NetworkError, never a fake count=0."""
    import asyncio

    from telegram.error import NetworkError

    req = _TraceUpdatesRequest()

    async def _raise_connect(*a, **k):
        raise RuntimeError("httpx.ConnectError: simulated IPv6 failure")

    _orig = req._client.request
    req._client.request = _raise_connect
    try:
        try:
            asyncio.run(req.post("https://api.telegram.org/bot<REDACTED>/getUpdates"))
            raised = False
        except NetworkError:
            # PTB wraps transport failures into NetworkError; the poller retries.
            raised = True
    finally:
        req._client.request = _orig

    assert raised, "network failure must propagate (not be swallowed as count=0)"
    out = capsys.readouterr().out
    assert "ERROR getUpdates" in out, out
    assert "RETURN count=0" not in out, out


def test_token_is_redacted_in_post_log(capsys):
    """The bot token embedded in the URL path must never be printed."""
    import asyncio

    req = _TraceUpdatesRequest()

    async def _ok(*a, **k):
        import json
        return type(
            "Resp",
            (),
            {"status_code": 200, "content": json.dumps({"ok": True, "result": []}).encode()},
        )()

    _orig = req._client.request
    req._client.request = _ok
    secret = "6666666666:AAfakeTokenNeverInLogsXXXX"
    try:
        asyncio.run(req.post(f"https://api.telegram.org/bot{secret}/getUpdates"))
    finally:
        req._client.request = _orig

    out = capsys.readouterr().out
    assert secret not in out, "bot token leaked into the diagnostic log"


def test_default_request_pool_is_ipv4_via_build_application(tmp_path):
    """build_application(bot=None) must wire IPv4-forced request + get_updates."""
    from pasay_bot.api_client import PasayApiClient
    from pasay_bot.config import Settings
    from pasay_bot.main import build_application
    from pasay_bot.state.store import StateStore

    settings = Settings(
        state_db=str(tmp_path / "state.db"),
        pasay_tg_bot_token="123:TEST",
        pasay_api_base="http://test/api/v1",
        pasay_admin_api_key="admin-key",
    )
    store = StateStore(settings.state_db)
    store.migrate()
    api = PasayApiClient(settings.pasay_api_base, "k", timeout=1.0)
    try:
        app = build_application(settings, api, store, admin_api_client=None)
        # The default request (used by ALL Telegram methods: getMe / sendMessage
        # / callbacks / setMyCommands ...) is forced onto IPv4.
        assert _pool_local_address(app.bot.request) == "0.0.0.0"
    finally:
        store.close()
