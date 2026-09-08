"""Cloudflare Container internal ingestion boundary.

Single internal endpoint used EXCLUSIVELY by the Cloudflare Worker
through the native Container binding (both the interactive direct
forward and the queue consumer paths).

- Public internet MUST NOT reach this path (Cloudflare Container never
  exposes this publicly; the Worker only hits it via the binding).
- One route dispatches BOTH telegram_update AND scheduled_job envelopes.
- Telegram envelope payloads are routed *directly* into the existing
  ``process_telegram_update_payload`` service — ZERO logic duplicated.
- Idempotency for scheduled jobs uses the same Postgres boundary.

CONTRACT (mirrors cloudflare-worker/src/index.ts deliver_envelope_to_container):

  HTTP 200 → container accepted / idempotent duplicate  →  Queue  ack
  HTTP 202 → accepted async                             →  Queue  ack
  HTTP 208 → idempotent duplicate (already processed)   →  Queue  ack
  HTTP 400 → envelope permanently malformed             →  Queue  terminal (drop)
  HTTP 401 → ingest token missing / mismatch            →  Queue  retry (operator fix)
  HTTP 5xx → container runtime transient                →  Queue  retry

Issue #119 P0 LATENCY telemetry:

The Worker stamps ``X-Pasay-Trace-Id`` (= envelope.event_id) and
``X-Pasay-Trace-T0`` (= envelope.occurred_at) on every forwarded
request, plus ``X-Pasay-Trace-Source`` distinguishing the
``telegram_webhook_direct`` (interactive fast path) from the
``queue_consumer`` path (scheduled / retry). The Container logs a
single structured line per ingest with the trace id and the
container_ingress_ms / dispatch_ms numbers so operator grep can
compute Worker→Container→PTB→Telegram hop-by-hop latency without
any shared clock (both sides record offsets relative to T0).
"""
from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import ScheduledJobLedger
from app.schemas.envelope import (
    EnvelopeKind,
    PasayQueueEnvelope,
    parse_envelope,
)
from app.services import telegram_webhook as wh_service

logger = logging.getLogger(__name__)

INGEST_TOKEN_HEADER = "X-Pasay-Ingest-Token"
# Issue #119 P0 latency: trace propagation headers from the Worker.
TRACE_ID_HEADER = "X-Pasay-Trace-Id"
TRACE_T0_HEADER = "X-Pasay-Trace-T0"
TRACE_SOURCE_HEADER = "X-Pasay-Trace-Source"
# Container-side tag values for the Worker ``X-Pasay-Trace-Source`` field.
TRACE_SOURCE_DIRECT = "telegram_webhook_direct"
TRACE_SOURCE_QUEUE = "queue_consumer"
# Backwards-compatible default for any legacy caller that did not stamp
# the source header (e.g. older Worker builds, tests).
TRACE_SOURCE_LEGACY = "legacy_or_unknown"

router = APIRouter(prefix="/internal", tags=["internal"])

# ---------------------------------------------------------------------------
# Idempotency for scheduled jobs.
#
# Table ``pasay_scheduled_job_ledger`` is created by Alembic migration
# ``a1b2c3d4e5f6_scheduled_job_ledger`` (PASAY-TASK-011 FIX1).  The runtime
# path MUST NOT lazily CREATE TABLE — the migration chain is the single
# schema authority (Scope E: Alembic single-head contract + ND_RETURN
# blocker #4).
# ---------------------------------------------------------------------------


def _try_claim_scheduled_job(
    db: Session,
    event_id: str,
    job_name: str,
    occurred_at: str,
    payload: dict[str, Any] | None,
) -> bool:
    """Return True if we own this event_id (first time), False if duplicate.

    PostgreSQL INSERT … ON CONFLICT DO NOTHING on the single-column PK
    ``event_id``.  Production path only (PASAY-TASK-011 FIX8: SQLite
    dialect branches were the wrong CI direction and have been removed
    from the production ingestion boundary).

    CRITICAL — Ledger ownership (FIX12 final closeout):
    The ``pasay_scheduled_job_ledger`` table has exactly TWO authoritative
    sources of truth, and this function MUST reference neither of them
    via inline ``sa.Table(...)`` re-declaration:
      (1) Alembic revision ``a1b2c3d4e5f6_scheduled_job_ledger`` — DDL authority.
      (2) ORM model ``app.models.scheduled_job.ScheduledJobLedger`` — Python
          side column contract authority, imported above.
    Here we reference ``ScheduledJobLedger.__table__`` for the SQLAlchemy
    Core table, which propagates any future column contract change to
    the INSERT/ON CONFLICT clause *automatically*.  Re-declaring columns
    inline inside this handler would create a silent-drift anti-pattern
    banned by Scope E (Alembic single-head + runtime ownership contract).
    """
    import json as _json

    from sqlalchemy.dialects import postgresql

    try:
        ledger = ScheduledJobLedger.__table__
        oa_str = occurred_at.replace("Z", "+00:00")
        payload_json = _json.dumps(payload) if payload is not None else None

        ins = (
            postgresql.insert(ledger)
            .values(
                event_id=event_id,
                job_name=job_name,
                occurred_at=oa_str,
                payload=payload_json,
            )
            .on_conflict_do_nothing(index_elements=["event_id"])
        )
        result = db.execute(ins)
        db.commit()
        rowcount = getattr(result, "rowcount", 0) or 0
        return rowcount > 0
    except Exception:  # noqa: BLE001
        db.rollback()
        raise


def _gate_ingest_token(header_value: str | None) -> JSONResponse | None:
    """Return None if token is valid; otherwise return a ready-to-send 401.

    We intentionally bypass FastAPI's default HTTPException (which wraps the
    payload under "detail") so the Queue consumer sees the stable contract
    {"ok": false, "error": ...} directly.
    """
    configured = (getattr(settings, "container_ingest_token", None) or "").strip()
    if not configured:
        logger.error("CONTAINER_INGEST_TOKEN not configured — rejecting internal ingest (fail closed)")
        return JSONResponse(
            status_code=401,
            content={"ok": False, "error": "ingest_not_configured"},
        )
    if not header_value or header_value != configured:
        logger.warning("internal ingest token mismatch")
        return JSONResponse(
            status_code=401,
            content={"ok": False, "error": "forbidden"},
        )
    return None


@router.post("/ingest")
async def internal_ingest(
    request: Request,
    x_pasay_ingest_token: str | None = Header(default=None, alias=INGEST_TOKEN_HEADER),
    x_pasay_trace_id: str | None = Header(default=None, alias=TRACE_ID_HEADER),
    x_pasay_trace_t0: str | None = Header(default=None, alias=TRACE_T0_HEADER),
    x_pasay_trace_source: str | None = Header(default=None, alias=TRACE_SOURCE_HEADER),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Single internal ingestion boundary for the Cloudflare Worker
    (interactive direct forward + queue consumer share this single path).

    Never exposed to the public internet. Token-gated.
    Routes both telegram_update AND scheduled_job envelopes.
    """
    gate_resp = _gate_ingest_token(x_pasay_ingest_token)
    if gate_resp is not None:
        return gate_resp

    # Issue #119 P0 latency: capture wall-clock + trace context at the
    # very first line of the handler so the dispatch_ms we log later is
    # measured against this same monotonic reading. Trace fields default
    # to legacy tags so older Worker builds (and tests) still emit a
    # consistent log shape — operator grep keeps working.
    t_container_arrival = time.monotonic()
    trace_source = x_pasay_trace_source or TRACE_SOURCE_LEGACY
    # The trace_id from the Worker is envelope.event_id for both direct
    # forward (tg:{update_id}) and queue consumer (sched:{job_name}:...).
    # We adopt it as the trace_id so the structured log line below ties
    # the Container dispatch back to the original Worker ingress event.
    trace_id = x_pasay_trace_id or ""

    try:
        raw = await request.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "internal ingest body parse failed trace_id=%s source=%s %s: %s",
            trace_id, trace_source, type(exc).__name__, exc,
        )
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "invalid_json", "error_type": type(exc).__name__},
        )

    if not isinstance(raw, dict):
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "envelope_malformed", "detail": "not an object"},
        )

    try:
        envelope: PasayQueueEnvelope = parse_envelope(raw)
    except ValidationError as exc:
        # Permanently malformed → 400 so Queue consumer marks "terminal"
        # (does not retry forever). See Scope C Queue retry/ack rules.
        logger.warning(
            "internal ingest envelope parse failed trace_id=%s source=%s detail=%r",
            trace_id, trace_source, exc.errors(include_url=False),
        )
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": "envelope_malformed",
                "error_type": "EnvelopeValidationError",
                "detail": exc.errors(include_url=False),
            },
        )

    # Adopt the envelope's canonical event_id as the authoritative trace
    # id if the Worker didn't propagate one — both direct forward and
    # queue consumer emit the same envelope.event_id shape.
    if not trace_id:
        trace_id = envelope.event_id

    # ── Dispatch telegram_update → EXISTING service (ZERO duplication) ──
    if envelope.kind == EnvelopeKind.TELEGRAM_UPDATE:
        t_before_dispatch = time.monotonic()
        status, body = await wh_service.process_telegram_update_payload(
            db,
            envelope.payload,
            trace_id=trace_id,
        )
        dispatch_ms = (time.monotonic() - t_before_dispatch) * 1000.0
        container_ingress_ms = (time.monotonic() - t_container_arrival) * 1000.0
        # Issue #119 P0 WARM-PATH TRACE: the underlying service now reports
        # claim_ms / ptb_process_ms in the body (telegram_webhook.process
        # _telegram_update_payload added per-hop timing in #119 warm-path
        # observability). We surface them so operator grep can split the
        # ``dispatch_ms`` total into the two pieces the Owner-visible
        # latency actually depends on. Older service versions / test
        # monkeypatches that do not yet return the keys fall back to 0.0.
        body_dict = body if isinstance(body, dict) else {}
        claim_ms = float(body_dict.get("claim_ms") or 0.0)
        ptb_process_ms = float(body_dict.get("ptb_process_ms") or 0.0)
        # Issue #119 P0 LATENCY telemetry: structured single-line record
        # keyed by trace_id so operator grep can compute Worker→Container
        # → PTB→Telegram hop-by-hop latency without a shared clock.
        logger.info(
            "pasay_ingest_latency trace_id=%s source=%s kind=telegram_update "
            "container_ingress_ms=%.2f dispatch_ms=%.2f claim_ms=%.2f "
            "ptb_process_ms=%.2f http_status=%s state=%s update_id=%s",
            trace_id,
            trace_source,
            container_ingress_ms,
            dispatch_ms,
            claim_ms,
            ptb_process_ms,
            status,
            body_dict.get("state"),
            envelope.payload.get("update_id") if isinstance(envelope.payload, dict) else None,
        )
        # Map existing service HTTP codes onto the Queue ack/retry/terminal
        # contract.  The service already returns:
        #   200 → terminal (done / failed / replay short-circuit / claimed elsewhere)
        #   400 → malformed update (permanent)
        #   401 → webhook secret not configured (but Worker already gated this)
        #   503 → retryable (DB transient / PTB temp down)
        if status in (200, 202, 208):
            return JSONResponse(status_code=200, content=body)
        if status == 400:
            # Permanently malformed Telegram Update — same envelope rules.
            return JSONResponse(status_code=400, content=body)
        # 503 or anything else → transient container → Queue retries.
        return JSONResponse(status_code=503, content=body)

    # ── Dispatch scheduled_job → minimal unified ingestion ──
    if envelope.kind == EnvelopeKind.SCHEDULED_JOB:
        payload = envelope.payload
        try:
            is_new = _try_claim_scheduled_job(
                db,
                event_id=envelope.event_id,
                job_name=payload.job_name,
                occurred_at=envelope.occurred_at,
                payload=payload.model_dump(mode="json") if payload.params is not None else None,
            )
        except Exception as exc:  # noqa: BLE001
            # DB transient → 503 → Queue retries.
            logger.error(
                "scheduled ledger claim transient trace_id=%s source=%s %s: %s",
                trace_id, trace_source, type(exc).__name__, exc,
            )
            return JSONResponse(
                status_code=503,
                content={
                    "ok": False,
                    "error": "ledger_claim_transient",
                    "error_type": type(exc).__name__,
                    "retryable": True,
                },
            )
        container_ingress_ms = (time.monotonic() - t_container_arrival) * 1000.0
        # Telemetry: scheduled_job dispatch has no PTB handler so dispatch_ms
        # is effectively the claim step (always <5ms in healthy Postgres).
        if not is_new:
            # Idempotent duplicate → 208 → Queue acks.
            logger.info(
                "pasay_ingest_latency trace_id=%s source=%s kind=scheduled_job "
                "container_ingress_ms=%.2f dispatch_ms=%.2f http_status=208 state=idempotent_duplicate "
                "job_name=%s",
                trace_id, trace_source, container_ingress_ms, container_ingress_ms,
                payload.job_name,
            )
            return JSONResponse(
                status_code=208,
                content={
                    "ok": True,
                    "state": "idempotent_duplicate",
                    "event_id": envelope.event_id,
                    "job_name": payload.job_name,
                },
            )
        # First-time claim: today we just log. Future reminder/digest/task
        # wake-up jobs route their dispatch here. Scope F: "本任务只建立基础
        # 通道，不新增运营功能" → NO business jobs are implemented.
        logger.info(
            "pasay_ingest_latency trace_id=%s source=%s kind=scheduled_job "
            "container_ingress_ms=%.2f dispatch_ms=%.2f http_status=202 state=accepted "
            "job_name=%s scheduled_at=%s",
            trace_id, trace_source, container_ingress_ms, container_ingress_ms,
            payload.job_name, payload.scheduled_at,
        )
        logger.info(
            "scheduled job ingested event_id=%s job_name=%s scheduled_at=%s",
            envelope.event_id, payload.job_name, payload.scheduled_at,
        )
        return JSONResponse(
            status_code=202,
            content={
                "ok": True,
                "state": "accepted",
                "event_id": envelope.event_id,
                "job_name": payload.job_name,
            },
        )

    # Unreachable (parse discriminator gates kind above); fail safe.
    return JSONResponse(
        status_code=400,
        content={"ok": False, "error": "envelope_unknown_kind"},
    )
