"""Issue #119 P0 LATENCY — Container-side telemetry for /internal/ingest.

The Worker now stamps every forwarded request with ``X-Pasay-Trace-Id``,
``X-Pasay-Trace-T0`` and ``X-Pasay-Trace-Source`` headers. The Container
MUST:

  1. Read those headers at the very first line of the handler.
  2. Emit a single structured ``pasay_ingest_latency`` log line per ingest
     with trace_id, source, kind, container_ingress_ms, dispatch_ms and
     http_status fields. This is the operator-side observability surface
     that lets us compute Worker→Container→PTB→Telegram hop-by-hop
     latency without a shared clock.
  3. Use the envelope.event_id as the authoritative trace_id even when
     the Worker forgot to stamp the header (backwards-compatible default
     for legacy callers / older Worker builds).

This test exercises the Container endpoint via FastAPI TestClient with
``process_telegram_update_payload`` monkeypatched out so the suite
needs no PostgreSQL — the goal is to prove the latency telemetry
contract, not the underlying dispatch. The end-to-end dispatch path is
already proven by test_internal_ingest_p019 + test_prod_arch_closeout_p0_031
against a real Postgres.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.schemas.envelope import ENVELOPE_VERSION


INGEST_TOKEN = "test-ingest-token-p119-latency-v1"
TEGRAM_SECRET = "test-webhook-secret-p119-latency-v1"


@pytest.fixture
def client_unit():
    """TestClient with the ingest token set; PTB dispatch is monkeypatched away."""
    old_ingest = settings.container_ingest_token
    old_tg = settings.telegram_webhook_secret
    settings.container_ingest_token = INGEST_TOKEN
    settings.telegram_webhook_secret = TEGRAM_SECRET
    try:
        with TestClient(app) as c:
            yield c
    finally:
        settings.container_ingest_token = old_ingest
        settings.telegram_webhook_secret = old_tg


@pytest.fixture
def captured_logs(caplog):
    """Capture log records emitted during the test."""
    caplog.set_level(logging.INFO, logger="app.api.routers.internal_ingest")
    return caplog


def _telegram_envelope(update_id: int = 9001, chat_id: int = 5177241442) -> dict[str, Any]:
    return {
        "version": ENVELOPE_VERSION,
        "kind": "telegram_update",
        "event_id": f"tg:{update_id}",
        "occurred_at": "2026-09-08T12:34:56+00:00",
        "payload": {
            "update_id": update_id,
            "message": {"chat": {"id": chat_id}, "text": "🏠 首页"},
        },
        "_telegram_meta": {"update_id": update_id, "chat_id": chat_id},
    }


def _scheduled_envelope(job_name: str = "pasay_heartbeat") -> dict[str, Any]:
    return {
        "version": ENVELOPE_VERSION,
        "kind": "scheduled_job",
        "event_id": f"sched:{job_name}:2026-09-08T12-30",
        "occurred_at": "2026-09-08T12:34:56+00:00",
        "payload": {"job_name": job_name, "scheduled_at": "2026-09-08T12:30:00+00:00"},
    }


def _find_latency_record(caplog) -> logging.LogRecord | None:
    """Find the structured ``pasay_ingest_latency`` record (works across
    ``old_string % args`` formatting used by Python logging).
    """
    for rec in caplog.records:
        if rec.name == "app.api.routers.internal_ingest" and rec.levelno >= logging.INFO:
            msg = rec.getMessage()
            if "pasay_ingest_latency" in msg:
                return rec
    return None


# ---------------------------------------------------------------------------
# 1) Worker direct forward (telegram_webhook_direct source) → Container
#    emits a pasay_ingest_latency line with all expected fields.
# ---------------------------------------------------------------------------


def test_telegram_update_direct_source_emits_latency_record(
    client_unit: TestClient, captured_logs, monkeypatch
):
    from app.api.routers import internal_ingest as ii

    sentinel_body = {
        "ok": True, "state": "done", "update_id": 9001,
        "dur_ms": 53, "attempts": 1, "cross_attempt": 1,
    }

    async def fake_process(db, raw_json, *, now=None, trace_id=None):
        return 200, sentinel_body

    monkeypatch.setattr(ii.wh_service, "process_telegram_update_payload", fake_process)

    envelope = _telegram_envelope(9001)
    resp = client_unit.post(
        "/internal/ingest",
        headers={
            "X-Pasay-Ingest-Token": INGEST_TOKEN,
            "X-Pasay-Trace-Id": "tg:9001",
            "X-Pasay-Trace-T0": "2026-09-08T12:34:56.789Z",
            "X-Pasay-Trace-Source": "telegram_webhook_direct",
        },
        json=envelope,
    )
    assert resp.status_code == 200
    assert resp.json() == sentinel_body

    rec = _find_latency_record(captured_logs)
    assert rec is not None, (
        "Container must emit a 'pasay_ingest_latency' structured log line for "
        "every direct-forward ingest (so operator grep can correlate with the "
        "Worker-side pasay_ingest_latency record)."
    )
    msg = rec.getMessage()
    # Trace propagation
    assert "trace_id=tg:9001" in msg, f"trace_id propagated: got {msg!r}"
    assert "source=telegram_webhook_direct" in msg, f"source tag: got {msg!r}"
    assert "kind=telegram_update" in msg, f"kind tag: got {msg!r}"
    # Per-hop latency fields (float with 2 decimal places)
    assert "container_ingress_ms=" in msg, f"container_ingress_ms: got {msg!r}"
    assert "dispatch_ms=" in msg, f"dispatch_ms: got {msg!r}"
    # HTTP status propagated
    assert "http_status=200" in msg, f"http_status: got {msg!r}"
    # Container state from the body
    assert "state=done" in msg, f"state=done: got {msg!r}"
    # update_id extracted from the envelope payload
    assert "update_id=9001" in msg, f"update_id=9001: got {msg!r}"


# ---------------------------------------------------------------------------
# 2) Queue consumer path (queue_consumer source) → same latency contract.
# ---------------------------------------------------------------------------

def test_telegram_update_queue_source_emits_latency_record(
    client_unit: TestClient, captured_logs, monkeypatch
):
    from app.api.routers import internal_ingest as ii

    async def fake_process(db, raw_json, *, now=None, trace_id=None):
        return 200, {"ok": True, "state": "done", "update_id": 9002}

    monkeypatch.setattr(ii.wh_service, "process_telegram_update_payload", fake_process)

    resp = client_unit.post(
        "/internal/ingest",
        headers={
            "X-Pasay-Ingest-Token": INGEST_TOKEN,
            "X-Pasay-Trace-Id": "tg:9002",
            "X-Pasay-Trace-T0": "2026-09-08T12:35:00.000Z",
            "X-Pasay-Trace-Source": "queue_consumer",
        },
        json=_telegram_envelope(9002),
    )
    assert resp.status_code == 200
    rec = _find_latency_record(captured_logs)
    assert rec is not None
    msg = rec.getMessage()
    assert "source=queue_consumer" in msg
    assert "trace_id=tg:9002" in msg


# ---------------------------------------------------------------------------
# 3) scheduled_job (pasay_heartbeat) → Container emits a scheduled_job
#    latency record (still no Queue path involved on the Worker side).
# ---------------------------------------------------------------------------

def test_scheduled_job_emits_latency_record(
    client_unit: TestClient, captured_logs, monkeypatch
):
    from app.api.routers import internal_ingest as ii

    # scheduled_job path doesn't call process_telegram_update_payload; the
    # claim step is the only DB touch (test seeder via monkeypatch).
    monkeypatch.setattr(ii, "_try_claim_scheduled_job", lambda *_a, **_kw: True)

    resp = client_unit.post(
        "/internal/ingest",
        headers={
            "X-Pasay-Ingest-Token": INGEST_TOKEN,
            "X-Pasay-Trace-Id": "sched:pasay_heartbeat:2026-09-08T12-30",
            "X-Pasay-Trace-T0": "2026-09-08T12:35:00.000Z",
            "X-Pasay-Trace-Source": "queue_consumer",
        },
        json=_scheduled_envelope("pasay_heartbeat"),
    )
    assert resp.status_code == 202

    rec = _find_latency_record(captured_logs)
    assert rec is not None
    msg = rec.getMessage()
    assert "kind=scheduled_job" in msg
    assert "state=accepted" in msg
    assert "http_status=202" in msg
    assert "job_name=pasay_heartbeat" in msg
    assert "source=queue_consumer" in msg


# ---------------------------------------------------------------------------
# 4) Legacy caller without trace headers → Container still emits a record
#    using envelope.event_id as the authoritative trace_id (back-compat).
# ---------------------------------------------------------------------------

def test_legacy_caller_without_trace_headers_uses_envelope_event_id(
    client_unit: TestClient, captured_logs, monkeypatch
):
    from app.api.routers import internal_ingest as ii

    async def fake_process(db, raw_json, *, now=None, trace_id=None):
        return 200, {"ok": True, "state": "done", "update_id": 9003}

    monkeypatch.setattr(ii.wh_service, "process_telegram_update_payload", fake_process)

    resp = client_unit.post(
        "/internal/ingest",
        headers={"X-Pasay-Ingest-Token": INGEST_TOKEN},
        # NO X-Pasay-Trace-* headers — legacy caller
        json=_telegram_envelope(9003),
    )
    assert resp.status_code == 200
    rec = _find_latency_record(captured_logs)
    assert rec is not None, "legacy caller MUST still emit a latency record"
    msg = rec.getMessage()
    # The Container falls back to envelope.event_id for trace_id so
    # operator grep keeps working across Worker version rollouts.
    assert "trace_id=tg:9003" in msg, f"envelope.event_id adopted as trace_id: got {msg!r}"
    # Source defaults to a stable legacy tag.
    assert "source=legacy_or_unknown" in msg, f"legacy source tag: got {msg!r}"


# ---------------------------------------------------------------------------
# 5) Handler exception path (process_telegram_update_payload returns 503)
#    still emits a latency record so a transient DB blip doesn't drop
#    the trace.
# ---------------------------------------------------------------------------

def test_telegram_update_503_still_emits_latency_record(
    client_unit: TestClient, captured_logs, monkeypatch
):
    from app.api.routers import internal_ingest as ii

    async def fake_process(db, raw_json, *, now=None, trace_id=None):
        return 503, {"ok": False, "state": "retryable", "error": "db_transient"}

    monkeypatch.setattr(ii.wh_service, "process_telegram_update_payload", fake_process)

    resp = client_unit.post(
        "/internal/ingest",
        headers={
            "X-Pasay-Ingest-Token": INGEST_TOKEN,
            "X-Pasay-Trace-Id": "tg:9004",
            "X-Pasay-Trace-T0": "2026-09-08T12:35:00.000Z",
            "X-Pasay-Trace-Source": "telegram_webhook_direct",
        },
        json=_telegram_envelope(9004),
    )
    # 503 from the underlying service propagates to the Queue consumer
    # contract so the upstream retry logic kicks in.
    assert resp.status_code == 503

    rec = _find_latency_record(captured_logs)
    assert rec is not None
    msg = rec.getMessage()
    assert "http_status=503" in msg
    assert "trace_id=tg:9004" in msg
    # The Container surfaces the same state tag the underlying service
    # returned so operators can grep retryable transitions.
    assert "state=retryable" in msg


# ---------------------------------------------------------------------------
# 6) dispatch_ms is a positive finite float (proves we measured the
#    underlying call duration rather than reading 0).
# ---------------------------------------------------------------------------

def test_dispatch_ms_is_finite_non_negative(
    client_unit: TestClient, captured_logs, monkeypatch
):
    import asyncio as _asyncio

    from app.api.routers import internal_ingest as ii

    async def fake_process_slow(db, raw_json, *, now=None, trace_id=None):
        # tiny await so dispatch_ms > 0 in the record
        await _asyncio.sleep(0.005)
        return 200, {"ok": True, "state": "done", "update_id": 9005}

    monkeypatch.setattr(ii.wh_service, "process_telegram_update_payload", fake_process_slow)

    client_unit.post(
        "/internal/ingest",
        headers={
            "X-Pasay-Ingest-Token": INGEST_TOKEN,
            "X-Pasay-Trace-Id": "tg:9005",
            "X-Pasay-Trace-Source": "telegram_webhook_direct",
        },
        json=_telegram_envelope(9005),
    )

    rec = _find_latency_record(captured_logs)
    assert rec is not None
    msg = rec.getMessage()
    # dispatch_ms field format from the Container is `dispatch_ms=%.2f`
    import re
    m = re.search(r"dispatch_ms=([0-9.]+)", msg)
    assert m, f"dispatch_ms present: {msg!r}"
    val = float(m.group(1))
    assert val >= 0, f"dispatch_ms must be non-negative (got {val})"
    # We slept 5ms inside the fake; the recorded dispatch_ms MUST be
    # at least roughly that — proves the timer was attached and not
    # a no-op zero placeholder.
    assert val >= 1.0, f"dispatch_ms must be >= 1ms after a 5ms sleep, got {val}"