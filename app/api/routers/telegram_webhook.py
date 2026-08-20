"""Telegram webhook HTTP endpoint (public, un-authed except header).

FastAPI request parsing never raises a 500 to the caller; malformed bodies
return 422/400 and our idempotency table is only touched after the Telegram
Update object is confirmed well-formed.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.services import telegram_webhook as wh_service

logger = logging.getLogger(__name__)

# The official Telegram header. Note the ``Api`` capitalization is what Telegram
# actually sends (not ``API``). It is a shared secret set by the operator both
# here AND via the setWebhook call.
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"

router = APIRouter(prefix="/telegram/webhook", tags=["telegram"])


@router.post("")
async def inbound_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None, alias=SECRET_HEADER),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Accept a single Telegram Update via setWebhook delivery.

    Responses (contract):
      * 403 – secret missing / mismatch (fail closed unless env is empty -> 401)
      * 401 – TELEGRAM_WEBHOOK_SECRET env not configured
      * 400 – payload cannot be decoded as a Telegram Update (malformed / unsupported)
      * 200 – everything else (claimed, dispatched, replay-short-circuited,
              permanently failed, retryable). This is the only status Telegram
              treats as "delivered, please proceed to the next update" so we
              do NOT leak internals as 5xx.
    """
    configured = (settings.telegram_webhook_secret or "").strip()
    if not configured:
        return JSONResponse(
            status_code=401,
            content={"ok": False, "error": "webhook_not_configured"},
        )
    received = x_telegram_bot_api_secret_token or ""
    if not received or received != configured:
        logger.warning(
            "webhook secret mismatch: header_present=%s configured_len=%d received_len=%d",
            bool(x_telegram_bot_api_secret_token), len(configured), len(received),
        )
        return JSONResponse(
            status_code=403,
            content={"ok": False, "error": "forbidden"},
        )
    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("webhook body parse failed: %s: %s", type(exc).__name__, exc)
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "invalid_json",
                     "error_type": type(exc).__name__},
        )
    status, body = await wh_service.process_telegram_update_payload(db, payload)
    return JSONResponse(status_code=status, content=body)
