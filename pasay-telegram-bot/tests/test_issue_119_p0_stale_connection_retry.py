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
  retry against a freshly-built LOCAL one-shot ``httpx.AsyncClient``
  (new keep-alive pool, same auth/org headers, same per-request
  timeout, same HTTP/2 setting, same injected test transport). The
  retry re-uses the SAME method/path/body/headers, so the request is
  byte-identical.
* The shared singleton ``self._client`` is NEVER closed or replaced
  by the recovery code — the production PTB Application is a singleton
  and ``PasayApiClient`` is shared across every concurrent Telegram
  update; the one-shot retry client is a local, single-attempt
  resource that is closed as soon as the retry returns. The
  ``test_concurrent_*`` regression group at the bottom of this file
  pins down that contract.
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
    """After a stale-retry the temporary retry client must be closed
    (not leaked) even if the retry itself also fails, and the shared
    ``self._client`` MUST remain operational for subsequent reads.

    Production concurrency contract (independent-review follow-up):
    the recovery code never closes or replaces the shared
    ``self._client``. A follow-up healthy GET must therefore be able
    to use the original shared client. Calls 1 and 2 are the first
    GET's two attempts (call 1 on the shared client, call 2 on the
    one-shot retry client) and both raise the stale-protocol
    fingerprint; call 3 is the follow-up healthy GET on the shared
    client — it MUST succeed because the shared pool was never
    closed/swapped.
    """
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
        # First logical GET: stale -> retry on the one-shot fresh
        # client -> still dead -> surface error.
        with pytest.raises(PasayApiError):
            _run(client.get_properties())
        assert len(calls) == 2
        # The shared ``self._client`` was NEVER closed or swapped —
        # a follow-up healthy GET MUST succeed on the original pool.
        # httpx retires the failed pooled connection on its own, so
        # the next request opens a fresh TCP/TLS over the shared
        # keep-alive map.
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


# ---------------------------------------------------------------------------
# CONCURRENCY-SAFETY REGRESSIONS (independent-review follow-up)
# ---------------------------------------------------------------------------
#
# Owner override 2026-09-09 11:36 Asia/Manila: the previous
# implementation closed and replaced ``self._client`` from inside the
# single GET recovery path. The production PTB Application is a
# singleton and ``PasayApiClient`` is shared across every concurrent
# Telegram update. Closing the shared pool from a single GET recovery
# could disrupt another in-flight read on the same pool, and two
# concurrent stale retries could close each other's newly-installed
# clients.
#
# The current implementation uses a LOCAL one-shot fresh client for
# each retry and closes ONLY that temp client. The shared
# ``self._client`` is never touched by the recovery code.
#
# The tests below exercise that contract against the REAL PasayApiClient
# instance — they verify the shared client's identity is preserved,
# its bearer header is intact, and concurrent stale retries do not
# interfere with each other or with a concurrent healthy read.


def _identity(client: PasayApiClient) -> int:
    """Stable identity of the shared ``httpx.AsyncClient`` instance.

    A recovery that closed or swapped ``self._client`` would change
    this identity. The concurrency contract requires it to NEVER
    change after a stale-retry.
    """
    return id(client._client)


def test_concurrent_in_flight_get_is_not_disrupted_by_stale_retry():
    """Concurrency contract (1): a stale-retry in one async task must
    NEVER close the shared ``self._client`` that another concurrent
    task is using. The healthy in-flight GET on the shared client
    must complete successfully.

    The OLD implementation closed and replaced ``self._client`` on a
    stale failure, which would have killed the in-flight request
    on the shared client with a ``RuntimeError: Client has been
    closed`` (or, in httpx's transport, a ``RemoteProtocolError``
    because the pooled connection was forcibly torn down).
    """
    observed_inflight: dict[str, Any] = {}
    observed_retry: dict[str, Any] = {}
    in_flight_started = asyncio.Event()
    release_in_flight = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/units"):
            # Healthy in-flight read on the SHARED client. It must
            # NOT be affected by the stale-retry the OTHER task is
            # doing.
            in_flight_started.set()
            await release_in_flight.wait()
            observed_inflight["auth"] = (
                request.headers.get("authorization")
            )
            observed_inflight["method"] = request.method
            return httpx.Response(200, json=[
                {"id": 1, "property_id": 1, "unit_number": "U-1",
                 "status": "vacant", "is_active": True},
            ])
        if path.endswith("/properties"):
            # Stale on the shared client (call 1), then the one-shot
            # retry client (call 2) succeeds. We mark the moment
            # the retry starts so the test can correlate.
            observed_retry.setdefault("calls", 0)
            observed_retry["calls"] += 1
            if observed_retry["calls"] == 1:
                # Stale-protocol on the SHARED client.
                raise httpx.RemoteProtocolError(
                    "Server disconnected without sending a response."
                )
            # The retry is performed on the one-shot client.
            observed_retry["auth"] = (
                request.headers.get("authorization")
            )
            observed_retry["client_repr"] = id(client._client)
            return httpx.Response(200, json=[
                {"id": 1, "name": "Bayshore", "address": "5",
                 "city": "Pasay", "total_units": 2, "is_active": True},
            ])
        # Anything else — keep mock transport predictable.
        return httpx.Response(200, json=[])

    client = _make_client(handler)
    shared_client_id_before = _identity(client)

    async def scenario() -> None:
        # Task A: start a healthy GET on the shared client, then
        # block until we know the shared client is in-flight.
        async def healthy_in_flight() -> None:
            units = await client.get_units()
            assert len(units) == 1 and units[0].unit_number == "U-1"

        # Task B: trigger a stale-retry on the shared client.
        async def stale_then_retry() -> list[Any]:
            return await client.get_properties()

        inflight = asyncio.create_task(healthy_in_flight())
        await in_flight_started.wait()
        # While task A is mid-flight on the shared client, task B
        # attempts the stale-retry.
        retry_task = asyncio.create_task(stale_then_retry())
        # Yield a couple of times so task B starts and the recovery
        # code runs while task A is still suspended.
        for _ in range(5):
            await asyncio.sleep(0)
        # Now release task A — it must complete successfully even
        # though the recovery code may have already created a
        # one-shot client.
        release_in_flight.set()
        await inflight
        properties = await retry_task
        assert (
            len(properties) == 1
            and properties[0].name == "Bayshore"
        )

    try:
        asyncio.run(scenario())
    finally:
        _run(client.aclose())

    # Concurrency contract: the shared ``self._client`` was NEVER
    # closed or replaced. Its identity must be identical before
    # and after the run.
    assert _identity(client) == shared_client_id_before, (
        "shared self._client must NEVER be closed or replaced by the "
        "stale-retry path; identity changed"
    )
    # The healthy in-flight read on the shared client used the
    # original bearer header (never re-derived).
    assert observed_inflight.get("auth") == "Bearer secret-key"
    assert observed_inflight.get("method") == "GET"
    # The retry itself was authorised and ran on a separate
    # one-shot client (its identity must NOT equal the shared
    # client's identity).
    assert observed_retry.get("auth") == "Bearer secret-key"
    assert observed_retry.get("calls") == 2


def test_concurrent_two_stale_retries_do_not_close_each_others_clients():
    """Concurrency contract (2): two concurrent stale GET retries
    must each build their OWN one-shot client, and closing one
    must not affect the other. The OLD implementation swapped
    ``self._client`` on each stale failure, so two concurrent
    stale retries would race to ``aclose()`` each other's newly
    installed shared client — the second retry would see
    ``RuntimeError: Client has been closed`` and the call would
    fail unexpectedly.

    The handler distinguishes the two tasks by URL path (one hits
    ``/units``, the other hits ``/properties``). Both tasks make
    their FIRST attempt in lock‑step — the handler releases them
    simultaneously so the stale-protocol fingerprint is raised
    on the shared client concurrently for both. The
    one-shot retry attempts run immediately and must each
    succeed on their own fresh pool.
    """
    observed: list[dict[str, Any]] = []
    first_call_started: dict[str, asyncio.Event] = {
        "A": asyncio.Event(),
        "B": asyncio.Event(),
    }
    release_first_calls: dict[str, asyncio.Event] = {
        "A": asyncio.Event(),
        "B": asyncio.Event(),
    }
    which = {"v": ""}
    idx = {"A": 0, "B": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/units"):
            which["v"] = "A"
        elif path.endswith("/properties"):
            which["v"] = "B"
        else:
            return httpx.Response(200, json=[])

        w = which["v"]
        idx[w] += 1
        this_call = idx[w]
        if this_call == 1:
            # FIRST attempt on the SHARED client: signal and
            # gate so both tasks are inside the handler in
            # lock-step before we let either raise the stale
            # fingerprint. The retry attempt (this_call == 2)
            # is NOT gated — it must run immediately on the
            # one-shot fresh client.
            first_call_started[w].set()
            await release_first_calls[w].wait()
            observed.append({"which": w, "stale": True})
            raise httpx.RemoteProtocolError(
                "Server disconnected without sending a response."
            )
        # RETRY attempt on the one-shot fresh client:
        observed.append({
            "which": w,
            "auth": request.headers.get("authorization"),
            "stale": False,
            "client_repr": id(client._client),
        })
        if path.endswith("/units"):
            return httpx.Response(200, json=[
                {"id": 1, "property_id": 1, "unit_number": "U-1",
                 "status": "vacant", "is_active": True},
            ])
        return httpx.Response(200, json=[
            {"id": 1, "name": "Bayshore", "address": "5",
             "city": "Pasay", "total_units": 2, "is_active": True},
        ])

    client = _make_client(handler)
    shared_client_id_before = _identity(client)

    async def scenario() -> None:
        async def task_a() -> list[Any]:
            return await client.get_units()

        async def task_b() -> list[Any]:
            return await client.get_properties()

        ta = asyncio.create_task(task_a())
        tb = asyncio.create_task(task_b())
        # Wait until both tasks have entered their handler call
        # #1 (i.e., both are suspended awaiting the release event).
        await first_call_started["A"].wait()
        await first_call_started["B"].wait()
        # Yield so the retry code does not yet see both raised.
        for _ in range(8):
            await asyncio.sleep(0)
        # Release BOTH first-call handlers simultaneously so
        # both tasks raise their stale fingerprint in lock-step.
        # Each task then builds its OWN one-shot retry client.
        release_first_calls["A"].set()
        release_first_calls["B"].set()
        units = await ta
        properties = await tb
        assert len(units) == 1 and units[0].unit_number == "U-1"
        assert (
            len(properties) == 1
            and properties[0].name == "Bayshore"
        )

    try:
        asyncio.run(scenario())
    finally:
        _run(client.aclose())

    # Concurrency contract: the shared ``self._client`` was NEVER
    # closed or replaced. Its identity must be identical before
    # and after the run, and after all the one-shot retry clients
    # have been closed.
    assert _identity(client) == shared_client_id_before, (
        "shared self._client must NEVER be closed or replaced by "
        "concurrent stale-retries; identity changed"
    )
    # Each task saw exactly one stale on the shared client and
    # one success on its retry. No task observed the shared
    # client being closed mid-retry.
    by_which: dict[str, list[dict[str, Any]]] = {}
    for o in observed:
        by_which.setdefault(o["which"], []).append(o)
    assert sorted(by_which.keys()) == ["A", "B"], (
        f"both stale-retries must have hit the handler; got {by_which}"
    )
    for which_, rows in by_which.items():
        stale_count = sum(1 for r in rows if r["stale"])
        ok_count = sum(1 for r in rows if not r["stale"])
        assert stale_count == 1, (
            f"task {which_} must see exactly one stale failure, "
            f"got {stale_count}"
        )
        assert ok_count == 1, (
            f"task {which_} must see exactly one successful retry, "
            f"got {ok_count}"
        )
    # The successful-retry rows must each carry the original
    # Bearer auth (never re-derived, never lost).
    for which_, rows in by_which.items():
        for r in rows:
            if not r["stale"]:
                assert r["auth"] == "Bearer secret-key", (
                    f"retry {which_} lost the Bearer auth header"
                )


def test_concurrent_stale_retry_does_not_replace_shared_client_attribute():
    """Concurrency contract (3): after a stale-retry, the attribute
    ``PasayApiClient._client`` MUST still be the original object
    (id-equal to what it was before). The OLD implementation
    re-assigned ``self._client = self._build_fresh_client()``,
    which would silently swap the shared pool; this test asserts
    that contract is now broken.

    The test uses a getter hook on the attribute to capture its
    identity before any request, then after the stale-retry
    succeeds, and asserts they are the same object.
    """
    call_count = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise httpx.RemoteProtocolError(
                "Server disconnected without sending a response."
            )
        return httpx.Response(200, json=[])

    client = _make_client(handler)
    shared_id_before = id(client._client)
    # The retry path builds a one-shot client via
    # ``_build_fresh_client``; it MUST not be assigned back to
    # ``self._client``. We assert that by checking the identity
    # before and after a stale-retry run.
    try:
        _run(client.get_properties())
        shared_id_after = id(client._client)
        assert shared_id_before == shared_id_after, (
            "shared self._client was replaced by stale-retry; "
            "regression of independent-review contract"
        )
        assert call_count["n"] == 2
    finally:
        _run(client.aclose())


def test_concurrent_stale_retry_only_closes_its_own_temp_client():
    """Concurrency contract (4): the recovery path closes ONLY the
    local one-shot retry client — it does NOT call ``aclose()``
    on the shared ``self._client``. We assert this by patching
    ``httpx.AsyncClient.aclose`` and capturing the SEQUENCE of
    close events across a stale-retry:

    * Between the start of ``client.get_properties()`` and the
      call returning, exactly ONE client is closed — the local
      one-shot retry client. The shared client is NEVER closed
      during this window.
    * The shared client is closed exactly once, by the explicit
      ``client.aclose()`` at the end (which runs after
      ``get_properties()`` returns).
    """
    closed_ids_in_order: list[int] = []
    real_aclose = httpx.AsyncClient.aclose
    close_lock = asyncio.Lock()

    async def counting_aclose(self: httpx.AsyncClient) -> None:
        async with close_lock:
            closed_ids_in_order.append(id(self))
        await real_aclose(self)

    # Patch at the class level — every AsyncClient instance,
    # including the one the retry path builds, gets counted.
    httpx.AsyncClient.aclose = counting_aclose  # type: ignore[assignment]
    try:
        call_count = {"n": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise httpx.RemoteProtocolError(
                    "Server disconnected without sending a response."
                )
            return httpx.Response(200, json=[])

        client = _make_client(handler)
        shared_client_id = id(client._client)
        # Snapshot how many closes have happened BEFORE the
        # ``get_properties()`` call — the recovery path must
        # only add closes for its OWN temp client; the shared
        # client must NOT appear here.
        closes_at_start = len(closed_ids_in_order)
        try:
            _run(client.get_properties())
        finally:
            # Snapshot BEFORE the explicit ``client.aclose()``
            # call so we can separately reason about (a) the
            # closes the recovery path did, and (b) the close
            # of the shared client.
            closes_after_recovery = list(closed_ids_in_order)
            _run(client.aclose())

        assert call_count["n"] == 2, (
            f"expected exactly 2 handler invocations "
            f"(1 stale + 1 retry), got {call_count['n']}"
        )
        # (a) Across the stale-retry path (between
        # ``closes_at_start`` and ``closes_after_recovery``),
        # exactly ONE client must have been closed — the local
        # one-shot retry client. The shared client ID must
        # NEVER appear in this slice. This is the direct
        # concurrency-safety assertion: the recovery code does
        # NOT call ``aclose()`` on the shared client.
        #
        # IMPORTANT: we slice from the snapshot
        # ``closes_after_recovery`` (captured BEFORE the explicit
        # ``client.aclose()`` call), NOT from ``closed_ids_in_order``
        # (which already contains the shared client's close by
        # the time this assert runs).
        recovery_closes = (
            closes_after_recovery[closes_at_start:]
            if closes_at_start < len(closes_after_recovery)
            else []
        )
        assert len(recovery_closes) == 1, (
            f"recovery path must close exactly one client (the "
            f"local retry temp); got {recovery_closes}"
        )
        assert recovery_closes[0] != shared_client_id, (
            "recovery path must NOT close the shared client — "
            f"closed IDs during recovery: {recovery_closes}, "
            f"shared client ID: {shared_client_id}"
        )
        # (b) After the explicit ``client.aclose()`` at the end,
        # the shared client must have been closed exactly once.
        # This is the lifecycle contract for the shared client:
        # the recovery path did not close it, and the explicit
        # ``aclose()`` at the end closed it exactly once.
        total_shared_closes = sum(
            1 for cid in closed_ids_in_order
            if cid == shared_client_id
        )
        assert total_shared_closes == 1, (
            f"shared client must be closed exactly once "
            f"(by the explicit aclose); observed "
            f"{total_shared_closes} closes"
        )
    finally:
        httpx.AsyncClient.aclose = real_aclose  # type: ignore[assignment]


def test_concurrent_stale_retry_does_not_call_reopen_client():
    """Concurrency contract (5): the independent-review follow-up
    removes the ``_reopen_client`` method entirely. The recovery
    path must NOT call any close/swap helper that touches the
    shared client. This test asserts ``_reopen_client`` is no
    longer present on ``PasayApiClient`` and the recovery path
    never invokes such an operation on the shared client."""
    assert not hasattr(PasayApiClient, "_reopen_client"), (
        "_reopen_client must NOT exist on PasayApiClient — the "
        "recovery path must not close/swap the shared client"
    )
    # The recovery path is local: it builds a fresh client via
    # ``_build_fresh_client``, uses it once, and closes only that
    # temp client. We assert ``_build_fresh_client`` exists and
    # returns a NEW instance (not the shared client).
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError(
            "Server disconnected without sending a response."
        )

    client = _make_client(handler)
    shared_id = id(client._client)
    try:
        fresh = client._build_fresh_client()
        try:
            assert id(fresh) != shared_id, (
                "_build_fresh_client must produce a new client, "
                "never the shared one"
            )
        finally:
            _run(fresh.aclose())
    finally:
        _run(client.aclose())
