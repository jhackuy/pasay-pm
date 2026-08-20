"""Cloudflare Container internal ingestion boundary.

Single internal endpoint used EXCLUSIVELY by the Cloudflare Worker queue
consumer via the native Container binding.

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
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.schemas.envelope import (
    EnvelopeKind,
    PasayQueueEnvelope,
    parse_envelope,
)
from app.services import telegram_webhook as wh_service

logger = logging.getLogger(__name__)

INGEST_TOKEN_HEADER = "X-Pasay-Ingest-Token"

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

    Uses PostgreSQL INSERT … ON CONFLICT DO NOTHING.  Caller treats False as
    "already consumed → HTTP 208 ALREADY_REPORTED" so the Queue acks.
    """
    from sqlalchemy import text

    try:
        result = db.execute(
            text(
                "INSERT INTO pasay_scheduled_job_ledger"
                " (event_id, job_name, occurred_at, payload)"
                " VALUES (:e, :j, CAST(:o AS TIMESTAMPTZ), CAST(:p AS JSONB))"
                " ON CONFLICT (event_id) DO NOTHING"
            ),
            {
                "e": event_id,
                "j": job_name,
                "o": occurred_at.replace("Z", "+00:00"),
                "p": __import__("json").dumps(payload) if payload is not None else None,
            },
        )
        db.commit()
        rowcount = getattr(result, "rowcount", 0) or 0
        return rowcount > 0
    except Exception:  # noqa: BLE001
        # Transient DB issue here surfaces as 5xx → Queue retries later.
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
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Single internal ingestion boundary for the Cloudflare Queue consumer.

    Never exposed to the public internet. Token-gated.
    Routes both telegram_update AND scheduled_job envelopes.
    """
    gate_resp = _gate_ingest_token(x_pasay_ingest_token)
    if gate_resp is not None:
        return gate_resp

    try:
        raw = await request.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("internal ingest body parse failed: %s", exc)
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
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": "envelope_malformed",
                "error_type": "EnvelopeValidationError",
                "detail": exc.errors(include_url=False),
            },
        )

    # ── Dispatch telegram_update → EXISTING service (ZERO duplication) ──
    if envelope.kind == EnvelopeKind.TELEGRAM_UPDATE:
        status, body = await wh_service.process_telegram_update_payload(
            db,
            envelope.payload,
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
            logger.error("scheduled ledger claim transient: %s", exc)
            return JSONResponse(
                status_code=503,
                content={
                    "ok": False,
                    "error": "ledger_claim_transient",
                    "error_type": type(exc).__name__,
                    "retryable": True,
                },
            )
        if not is_new:
            # Idempotent duplicate → 208 → Queue acks.
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
