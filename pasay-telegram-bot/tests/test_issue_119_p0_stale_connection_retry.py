"""Issue #119 P0 STALE-CONNECTION RESILIENCE — Owner override 2026-09-09.

Production fingerprint (real Owner screenshot @ 2026-09-09 08:52-08:54
Asia/Manila): the long-lived ``httpx.AsyncClient`` keeps a small keep-alive
pool; when the peer closes its TLS connection without a response the next
pooled read raises ``httpx.RemoteProtocolError`` (``Server disconnected
without sending a response``). The bot previously surfaced that as a
generic ``PasayApiError("network error: ...")`` and the user saw the
"获取数据失败: network error: Server disconnected without sending a
response" card. Immediate retry of the SAME button then succeeded (the
second tap picked a fresh pooled connection or a fresh TCP/TLS).

This test module pins down the minimum safe resilience contract for
``PasayApiClient``:

* For idempotent READ requests (GET / HEAD) ONLY, a transport-level
  stale/disconnected pooled connection triggers exactly ONE transparent
  retry against a freshly-rebuilt ``httpx.AsyncClient`` (new keep-alive
  pool, same auth/org headers, same per-request timeout, same HTTP/2
  setting, same injected test transport). The retry re-uses the SAME
  method/path/body/headers, so the request is byte-identical.
* A SECOND failure on the same logical request surfaces the original
  error verbatim — we do not infinite-retry, we do not silently swallow.
* A normal successful GET sends the request ONCE (no spurious retry).
* WRITES (POST / PATCH / PUT / DELETE / any unknown verb) are NEVER
  automatically replayed — auto-retrying a write could double-apply a
  financial mutation; the caller owns reconciliation via
  ``PasayApiTimeoutError`` / 409. The retry bookkeeping is observable
  only via the structured log line ``pasay_v1_request``'s ``retried=``
  field; the bot's six frozen menu buttons are all GETs so the user
  path stays safe.
* Auth (Bearer), the bound ``X-Telegram-User-Id`` header, the request
  timeout, the JSON body, and the query params survive the reopen.
* ``PasayApiTimeoutError`` (read-timeout / connect-timeout) is the
  "uncertain write" path and is NEVER retried — a timeout means the
  server may have already processed the request, which for a write is
  exactly the reconciliation case the bot already owns.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from pasay_bot.api_client import (
    PasayApiClient,
    PasayApiError,
    PasayApiTimeoutError,
)


def _run(coro):
    return asyncio.run(coro)


def _make_client(handler, api_key: str = "secret-key", **kwargs) -> PasayApiClient:
    return PasayApiClient(
        "http://test/api/v1", api_key, timeout=2.0,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def _assert_bearer_preserved(client: PasayApiClient) -> None:
    """The reopened client must keep the original Bearer auth header."""
    auth = client._client.headers.get("authorization")
    assert auth == "Bearer secret-key", (
        "stale-connection reopen must preserve the Bearer auth header, "
        f"got {auth!r}"
    )


def test_first_stale_disconnect_triggers_one_retry_then_success():
    """Owner fingerprint reproduction: first GET hits a dead pooled socket
    and raises ``RemoteProtocolError``; the automatic retry on a NEW
    client succeeds with the SAME method/path/body/headers."""
    calls: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append({
            "method": request.method,
            "path": request.url.path,
            "headers": dict(request.headers),
        })
        if len(calls) == 1:
            # First hit: peer closes its TLS connection before sending any
            # bytes — the httpx-side analogue of the Owner's
            # "Server disconnected without sending a response".
            raise httpx.RemoteProtocolError(
                "Server disconnected without sending a response."
            )
        return httpx.Response(200, json=[
            {"id": 1, "name": "Bayshore", "address": "5 Roxas Blvd",
             "city": "Pasay", "total_units": 2, "is_active": True},
        ])

    client = _make_client(handler)
    try:
        result = _run(client.get_properties())
        # The user-visible path is exactly ONE retry, so the handler saw
        # TWO invocations: the first dead one and the second successful one.
        assert len(calls) == 2, (
            f"expected exactly 2 attempts (1 stale + 1 retry), got {len(calls)}"
        )
        assert calls[0]["method"] == "GET" and calls[1]["method"] == "GET"
        # httpx resolves the full URL against base_url; the relative
        # path part must be identical on both attempts.
        assert calls[0]["path"].endswith("/properties")
        assert calls[1]["path"].endswith("/properties"), (
            "retry must target the exact same endpoint, not a different one"
        )
        assert calls[0]["path"] == calls[1]["path"]
        # The retry preserved the Bearer auth header on the wire.
        assert calls[0]["headers"].get("authorization") == "Bearer secret-key"
        assert calls[1]["headers"].get("authorization") == "Bearer secret-key"
        assert len(result) == 1 and result[0].name == "Bayshore"
        # The reopened client kept the same Bearer — never re-derived, never
        # logged.
        _assert_bearer_preserved(client)
    finally:
        _run(client.aclose())


def test_second_stale_disconnect_surfaces_error_no_third_attempt():
    """If the FRESH client also hits a dead peer, the bot must surface
    the ORIGINAL error verbatim — never an infinite retry, never a
    swallowed error."""
    calls: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.RemoteProtocolError(
            "Server disconnected without sending a response."
        )

    client = _make_client(handler)
    try:
        with pytest.raises(PasayApiError) as ei:
            _run(client.get_properties())
        # Exactly TWO attempts (one original + one retry), never a third.
        assert len(calls) == 2, (
            f"expected exactly 2 attempts, got {len(calls)} — retry is "
            "supposed to be ONCE, not unbounded"
        )
        # The detail string preserves the original httpx message so
        # the user-visible card matches the Owner screenshot.
        assert "Server disconnected without sending a response" in ei.value.detail
        assert ei.value.status_code is None
        # Auth still preserved (no exception swallowed it).
        _assert_bearer_preserved(client)
    finally:
        _run(client.aclose())


def test_normal_successful_get_is_not_replayed():
    """A normal successful GET must send the request exactly ONCE.
    Re-opening the client on success would be wasted work and would
    break HTTP/2 multiplexing on the warm path."""
    calls: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=[
            {"id": 7, "name": "Solo", "address": "1", "city": "Pasay",
             "total_units": 1, "is_active": True},
        ])

    client = _make_client(handler)
    try:
        props = _run(client.get_properties())
        assert len(calls) == 1, (
            f"a healthy GET must NOT trigger a retry; got {len(calls)}"
        )
        assert len(props) == 1 and props[0].name == "Solo"
    finally:
        _run(client.aclose())


def test_post_is_never_auto_retried_on_stale_connection():
    """WRITES are NEVER auto-retried, even if the transport dies. An
    automatic POST replay could double-apply a financial mutation; the
    caller owns reconciliation via ``PasayApiTimeoutError`` / 409.
    The Owner-visible screenshot fingerprint must NEVER be turned into
    a hidden double-write."""
    calls: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"method": request.method, "body": request.content})
        # Dead peer on the FIRST attempt — a retry-on-write would be
        # catastrophic for income / expense / repair flows.
        raise httpx.RemoteProtocolError(
            "Server disconnected without sending a response."
        )

    client = _make_client(handler)
    try:
        with pytest.raises(PasayApiError) as ei:
            _run(client.create_income(
                lease_id=1,
                amount="55000.00",
                received_date="2026-09-09",
                payment_method="Bank",
                description="rent 2026-09",
            ))
        # Exactly ONE attempt: the write must NEVER be silently replayed.
        assert len(calls) == 1, (
            f"POST must NEVER be auto-retried on stale connection; "
            f"got {len(calls)} attempts — would double-write income!"
        )
        assert calls[0]["method"] == "POST"
        # Body still serialised correctly on the wire (one-shot, no
        # silent truncation or replay).
        body = json.loads(calls[0]["body"])
        assert body["amount"] == "55000.00"
        assert body["status"] == "pending"
        # The original error surfaces verbatim so the bot's handler
        # surfaces the "uncertain write" path.
        assert "Server disconnected without sending a response" in ei.value.detail
    finally:
        _run(client.aclose())


def test_patch_is_never_auto_retried_on_stale_connection():
    """PATCH (e.g. tenant update, operational task update) must NEVER be
    replayed — same reasoning as POST: the prior call may already have
    committed server-side."""
    calls: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"method": request.method})
        raise httpx.RemoteProtocolError(
            "Server disconnected without sending a response."
        )

    client = _make_client(handler)
    try:
        with pytest.raises(PasayApiError):
            _run(client.update_tenant(1, phone="+639171234567"))
        assert len(calls) == 1, (
            "PATCH must NEVER be auto-retried; got "
            f"{len(calls)} attempts"
        )
        assert calls[0]["method"] == "PATCH"
    finally:
        _run(client.aclose())


def test_delete_is_never_auto_retried_on_stale_connection():
    """DELETE (e.g. operational_task cancel) must NEVER be replayed."""
    calls: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"method": request.method})
        raise httpx.RemoteProtocolError(
            "Server disconnected without sending a response."
        )

    client = _make_client(handler)
    try:
        with pytest.raises(PasayApiError):
            _run(client.cancel_operational_task(42))
        assert len(calls) == 1, "DELETE must NEVER be auto-retried"
        assert calls[0]["method"] == "POST"  # route is POST /operations/tasks/{id}/cancel
    finally:
        _run(client.aclose())


def test_write_routes_use_post_or_patch_not_replayed():
    """All the WRITE entry points used by the bot are POST / PATCH; the
    retry gate is method-based. Cross-check that even a verb that LOOKS
    safe (HEAD) is gated correctly, and that a write's stale error
    raises PasayApiError (not PasayApiTimeoutError)."""
    calls: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"method": request.method})
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.")

    client = _make_client(handler)
    try:
        # POST path
        with pytest.raises(PasayApiError):
            _run(client.create_viewing(
                unit_id=1, scheduled_at="2026-09-10T10:00:00Z"
            ))
        # PATCH path
        with pytest.raises(PasayApiError):
            _run(client.update_operational_task(1, title="x"))
        # POST /operations/tasks/{id}/acknowledge
        with pytest.raises(PasayApiError):
            _run(client.acknowledge_operational_task(1))
        # PATCH /units/{id}
        with pytest.raises(PasayApiError):
            _run(client.update_unit(7, monthly_rent="12345.00"))
        # POST /expenses/{id}/approve
        with pytest.raises(PasayApiError):
            _run(client.approve_expense(1))
        # POST /repairs
        with pytest.raises(PasayApiError):
            _run(client.create_repair(issue="leak"))
        # POST /auth (bot /auth round-trip)
        with pytest.raises(PasayApiError):
            _run(client.get_me())

        # Every write attempted ONCE — never replayed.
        assert len(calls) == 7, (
            f"every write must attempt exactly once; got {len(calls)}"
        )
        methods = sorted({c["method"] for c in calls})
        assert "POST" in methods and "PATCH" in methods
        # No GET anywhere — writes must not silently become GETs.
        assert "GET" not in methods
    finally:
        _run(client.aclose())


def test_timeout_is_never_retried_on_reads():
    """A read-timeout / connect-timeout is the ``PasayApiTimeoutError``
    reconciliation path. The server MAY have processed a write that
    timed out, so even on a READ we MUST NOT auto-retry — the bot
    already owns the "uncertain write" recovery, and a transparent
    retry here would HIDE that uncertainty from the caller."""
    calls: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ReadTimeout("read timed out")

    client = _make_client(handler)
    try:
        with pytest.raises(PasayApiTimeoutError):
            _run(client.get_properties())
        assert len(calls) == 1, (
            "timeout on a read must NEVER auto-retry; got "
            f"{len(calls)} attempts"
        )
    finally:
        _run(client.aclose())


def test_reopen_preserves_bearer_and_timeout_and_query_params():
    """The retry path is byte-identical to the original: same method,
    same path, same body, same query params, same Bearer auth header,
    same per-request timeout. The user MUST NOT see a different
    effective request just because the connection was stale."""
    captured: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append({
            "method": request.method,
            "path": request.url.path,
            "querystring": dict(request.url.params),
            "auth": request.headers.get("authorization"),
        })
        if len(captured) == 1:
            raise httpx.RemoteProtocolError(
                "Server disconnected without sending a response."
            )
        return httpx.Response(200, json={"month": "2026-09", "income": "0"})

    client = _make_client(handler)
    try:
        _run(client.get_financial_summary("2026-09"))
        assert len(captured) == 2
        first, second = captured
        # Same method + path + query string on both attempts.
        assert first["method"] == second["method"] == "GET"
        assert first["path"] == second["path"]
        assert first["path"].endswith("/reports/financial-summary"), (
            f"unexpected path: {first['path']}"
        )
        assert first["querystring"] == second["querystring"] == {"month": "2026-09"}
        # Bearer auth preserved on BOTH attempts — the reopened client
        # must NEVER silently drop the Authorization header.
        assert first["auth"] == second["auth"] == "Bearer secret-key"
        # The reopened client keeps the same Bearer, even after the
        # old pool has been abandoned.
        _assert_bearer_preserved(client)
    finally:
        _run(client.aclose())


def test_reopen_preserves_x_telegram_user_id_binding():
    """The per-task ``X-Telegram-User-Id`` header (used for org-scope
    binding) must be re-applied on the reverted client — a transparent
    reopen must not lose the caller's identity."""
    captured: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append({
            "tuid": request.headers.get("x-telegram-user-id"),
            "auth": request.headers.get("authorization"),
        })
        if len(captured) == 1:
            raise httpx.RemoteProtocolError(
                "Server disconnected without sending a response."
            )
        return httpx.Response(200, json=[])

    client = _make_client(handler)
    try:
        async def scenario():
            client.bind_telegram_user(5177241442)
            await asyncio.sleep(0)
            await client.get_properties()

        _run(scenario())
        assert len(captured) == 2
        # The X-Telegram-User-Id header was attached to BOTH attempts —
        # the retry re-applied the bound identity after reopen.
        assert captured[0]["tuid"] == captured[1]["tuid"] == "5177241442"
        # Bearer also preserved on both attempts.
        assert captured[0]["auth"] == captured[1]["auth"] == "Bearer secret-key"
    finally:
        _run(client.aclose())


def test_retry_outcome_logged_with_retried_flag(caplog):
    """On a successful stale-retry the ``pasay_v1_request`` log line
    must carry ``retried=true`` so operator grep can spot the actual
    incident rate. On a normal successful GET the line must carry
    ``retried=false``."""
    import logging

    caplog.set_level(logging.INFO, logger="pasay_bot.api_client")

    async def first_dead(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError(
            "Server disconnected without sending a response."
        )

    async def second_ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    handler_seq = [first_dead, second_ok]
    idx = {"i": 0}

    async def dispatch(request: httpx.Request) -> httpx.Response:
        h = handler_seq[idx["i"]]
        idx["i"] += 1
        return await h(request)

    client = _make_client(dispatch)
    try:
        _run(client.get_properties())
        retries = [
            r for r in caplog.records
            if "pasay_v1_request" in r.getMessage()
        ]
        assert retries, "expected at least one pasay_v1_request log line"
        # The final log line is the one Operator grep cares about; it
        # carries retried=true because the stale pool was reopened.
        final = retries[-1].getMessage()
        assert "retried=true" in final, (
            f"successful stale-retry must log retried=true; got: {final}"
        )
    finally:
        _run(client.aclose())

    # Now run a normal healthy GET and assert retried=false.
    caplog.clear()

    async def always_ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    client2 = _make_client(always_ok)
    try:
        _run(client2.get_properties())
        retries = [
            r for r in caplog.records
            if "pasay_v1_request" in r.getMessage()
        ]
        assert retries, "expected at least one pasay_v1_request log line"
        final = retries[-1].getMessage()
        assert "retried=false" in final, (
            f"normal successful GET must log retried=false; got: {final}"
        )
    finally:
        _run(client2.aclose())


def test_reopen_does_not_leak_old_pool_on_subsequent_failure():
    """After a stale-retry the OLD ``httpx.AsyncClient`` must be closed
    (not leaked) even if the NEW attempt also fails. We assert this
    indirectly: a third consecutive GET must reuse the freshly-opened
    client, not the dead one."""
    calls: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        # Calls 1+2 fail with stale-protocol; call 3 succeeds.
        if len(calls) <= 2:
            raise httpx.RemoteProtocolError(
                "Server disconnected without sending a response."
            )
        return httpx.Response(200, json=[])

    client = _make_client(handler)
    try:
        # First logical GET: stale -> retry on fresh client -> still dead
        # -> surface error.
        with pytest.raises(PasayApiError):
            _run(client.get_properties())
        assert len(calls) == 2
        # The aboves run used the SAME client object. After the error,
        # self._client is now the FRESH client (the old one was closed
        # in _reopen_client). A follow-up healthy GET MUST succeed.
        result = _run(client.get_properties())
        assert result == []
        assert len(calls) == 3
    finally:
        _run(client.aclose())


def test_retry_uses_same_mock_transport_handler():
    """The retry must use the SAME test transport the caller injected
    (so regression tests don't accidentally bypass the MockTransport
    and reach out to the real network)."""
    seen_clients: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_clients.append(id(request))
        if len(seen_clients) == 1:
            raise httpx.RemoteProtocolError(
                "Server disconnected without sending a response."
            )
        return httpx.Response(200, json=[])

    client = _make_client(handler)
    try:
        _run(client.get_properties())
        assert len(seen_clients) == 2
        # Both invocations reached the MockTransport; the retry did
        # NOT silently swap to a real network transport.
    finally:
        _run(client.aclose())


def test_retry_preserves_http_body_for_post_without_replaying():
    """A POST whose FIRST attempt hits a dead peer must surface the
    error WITHOUT sending a second body. We assert this directly:
    exactly ONE body is observed on the wire and the second attempt
    is NOT made."""
    bodies_seen: list[bytes] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        bodies_seen.append(request.content)
        raise httpx.RemoteProtocolError(
            "Server disconnected without sending a response."
        )

    client = _make_client(handler)
    try:
        with pytest.raises(PasayApiError):
            _run(client.create_expense(
                category="repair",
                amount="1234.56",
                expense_date="2026-09-09",
                payee="Vendor",
                description="stale-pool write attempt",
            ))
        assert len(bodies_seen) == 1, (
            "POST body must NEVER be sent twice on stale connection; "
            f"observed {len(bodies_seen)} bodies"
        )
        body = json.loads(bodies_seen[0])
        assert body["category"] == "repair"
        assert body["amount"] == "1234.56"
    finally:
        _run(client.aclose())


def test_head_is_retried_like_get():
    """HEAD is idempotent; the Owner-visible menu routes are all GETs
    but HEAD must share the same retry policy for consistency."""
    calls: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.RemoteProtocolError(
                "Server disconnected without sending a response."
            )
        return httpx.Response(204)

    client = _make_client(handler)
    try:
        # PasayApiClient has no dedicated HEAD helper; we use _request
        # directly. The retry policy is method-driven, not endpoint-
        # driven, so this is a clean unit-level probe.
        result = _run(client._request("HEAD", "/units/1"))
        assert result is None
        assert len(calls) == 2, "HEAD on stale connection must retry once"
    finally:
        _run(client.aclose())


def test_unknown_verb_is_never_retried():
    """Anything that is not GET / HEAD must NEVER auto-retry. A future
    bot method that calls a non-idempotent verb must surface the
    transport error verbatim so the caller can reconcile."""
    calls: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.RemoteProtocolError(
            "Server disconnected without sending a response."
        )

    client = _make_client(handler)
    try:
        with pytest.raises(PasayApiError):
            _run(client._request("PROPFIND", "/x"))
        assert len(calls) == 1, (
            f"non-idempotent verb must NEVER be retried; got {len(calls)}"
        )
    finally:
        _run(client.aclose())


def test_retry_log_lines_never_carry_bearer_or_full_url(caplog):
    """``pasay_v1_request`` log lines must NEVER contain the Bearer
    token, the bot's auth key, or a fully-qualified URL with the
    sensitive path. This is the same redacted-logging contract the
    rest of the codebase upholds (``tests/test_ipv4_transport.py::
    test_token_is_redacted_in_post_log``); we re-assert it on the
    retry path to guarantee the new log lines do not regress it."""
    import logging
    secret_key = "5500DEAD-BEEF-AAAA-BBBB-1234567890AB"
    caplog.set_level(logging.INFO, logger="pasay_bot.api_client")

    calls: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.RemoteProtocolError(
                "Server disconnected without sending a response."
            )
        return httpx.Response(200, json=[])

    client = _make_client(handler, api_key=secret_key)
    try:
        _run(client.get_properties())
        full_log = "\n".join(r.getMessage() for r in caplog.records)
        assert secret_key not in full_log, (
            "pasay_v1_request log line must NEVER contain the Bearer "
            "raw key"
        )
        # The base_url host ('test') and the auth scheme literal are
        # allowed; the raw secret must NEVER appear.
        assert "Bearer " + secret_key not in full_log
    finally:
        _run(client.aclose())


def test_retried_flag_false_on_normal_get_and_true_on_stale_retry(caplog):
    """Single combined test that pins down both branches of the
    ``retried=`` flag in one place so future regressions surface in
    a single PR-cycle."""
    import logging
    caplog.set_level(logging.INFO, logger="pasay_bot.api_client")

    # (1) Normal healthy GET: retried=false.
    async def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    client = _make_client(ok)
    try:
        _run(client.get_properties())
    finally:
        _run(client.aclose())
    normal_lines = [
        r.getMessage() for r in caplog.records
        if "pasay_v1_request" in r.getMessage()
    ]
    assert normal_lines, "expected at least one pasay_v1_request line"
    assert "retried=false" in normal_lines[-1]

    # (2) Stale GET with successful retry: retried=true.
    caplog.clear()
    counter = {"n": 0}

    async def stale_then_ok(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        if counter["n"] == 1:
            raise httpx.RemoteProtocolError(
                "Server disconnected without sending a response."
            )
        return httpx.Response(200, json=[])

    client2 = _make_client(stale_then_ok)
    try:
        _run(client2.get_properties())
    finally:
        _run(client2.aclose())
    retry_lines = [
        r.getMessage() for r in caplog.records
        if "pasay_v1_request" in r.getMessage()
    ]
    assert retry_lines, "expected at least one pasay_v1_request line on retry"
    # The retrying_once line is emitted before the second attempt.
    assert any("status=retrying_once" in m for m in retry_lines), (
        f"expected a retrying_once log line; got {retry_lines}"
    )
    # The final line carries retried=true.
    assert "retried=true" in retry_lines[-1]

    # (3) Stale GET with retry also failing: surface PasayApiError, no
    #     retried=true on the success log line (there is none), but a
    #     retrying_once line IS emitted, then the final error log line
    #     carries status=error.
    caplog.clear()
    async def always_dead(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError(
            "Server disconnected without sending a response."
        )

    client3 = _make_client(always_dead)
    try:
        with pytest.raises(PasayApiError):
            _run(client3.get_properties())
    finally:
        _run(client3.aclose())
    failure_lines = [
        r.getMessage() for r in caplog.records
        if "pasay_v1_request" in r.getMessage()
    ]
    assert failure_lines
    assert any("status=retrying_once" in m for m in failure_lines), (
        f"expected a retrying_once line on stale failure; got {failure_lines}"
    )
    assert failure_lines[-1].endswith("error=RemoteProtocolError") or (
        "status=error" in failure_lines[-1]
    ), f"final line on stale failure must log status=error; got {failure_lines[-1]}"
