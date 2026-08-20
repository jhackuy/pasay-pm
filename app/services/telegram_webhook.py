"""Telegram webhook inbound-update pipeline: idempotency, PTB dispatch, retries.

Scope (per PASAY-WEBHOOK-ARCH-P0-001):
  * Update → handler → Neon/PostgreSQL → Telegram reply 主链路
  * update_id 幂等重放防护 (持久化, 重启后仍生效)
  * 异常隔离 (DB/Telegram/handler/unsupported/malformed 不导致服务退出)
  * 仅安全可重试的临时失败有限重试
  * 结构化日志 (update_id / chat_id / 状态 / 错误类型)
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy import func, select, update
from telegram import Update as TelegramUpdate
from telegram.error import (
    BadRequest,
    Conflict,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TimedOut,
)

# SessionLocal is ONLY used as a FALLBACK when the caller has not injected a db
# session (e.g. startup scripts / one-off REPLs). The FastAPI router always injects
# a session via Depends(get_db) so tests + production share the same transaction
# scope and test-engine overrides work correctly.
from app.config import settings as _backend_settings
from app.database import SessionLocal  # noqa: F401  (re-exported for rare direct use)
from app.models.telegram_webhook import (
    CLAIM_STALE_SECONDS,
    TelegramWebhookState,
    TelegramWebhookUpdate,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PTB pasay_bot wiring. We import lazily so the test suite / backend can boot
# without the pasay-telegram-bot subtree actually being importable.
# ---------------------------------------------------------------------------
_PTB_APP_READY = False
_PTB_APP = None
_PTB_APP_INIT_ERR: str | None = None


def _import_pasay_bot_subtree() -> None:
    """Add ``pasay-telegram-bot/`` to ``sys.path`` so ``import pasay_bot`` works
    even when the backend wheel / install has not been built.

    Best-effort only; if it fails the caller receives the exception and the
    webhook endpoint returns a structured error without taking the process down.
    """
    here = Path(__file__).resolve().parent.parent.parent  # repo root
    bot_root = here / "pasay-telegram-bot"
    if bot_root.exists() and str(bot_root) not in sys.path:
        sys.path.insert(0, str(bot_root))


def _build_bot_settings_overlay() -> Any:
    """Translate backend env vars into pasay_bot Settings.

    The bot reads ``PASSAY_TG_BOT_TOKEN``/etc via :func:`pasay_bot.config.get_settings`.
    If the operator provided tokens through the backend env var names we still
    honour them; the bot's own loader falls back to process-env, so forwarding
    is not required. Returning the result of ``get_settings()`` keeps the single
    source of truth.
    """
    from pasay_bot.config import get_settings  # type: ignore

    return get_settings()


async def get_ptb_application():
    """Return the singleton PTB Application, booting it on first call.

    The application is built exactly once per process. Any wiring error is
    captured and re-raised to the caller on every subsequent request; this
    keeps the process alive and lets operators fix configuration without a
    crash-loop.
    """
    global _PTB_APP_READY, _PTB_APP, _PTB_APP_INIT_ERR
    if _PTB_APP_READY:
        if _PTB_APP is None:
            raise RuntimeError(
                f"PTB application boot failed previously: {_PTB_APP_INIT_ERR}"
            )
        return _PTB_APP

    try:
        _import_pasay_bot_subtree()
        from pasay_bot.api_client import PasayApiClient  # type: ignore
        from pasay_bot.main import build_application  # type: ignore
        from pasay_bot.state.store import StateStore  # type: ignore

        bot_settings = _build_bot_settings_overlay()

        # build_application() returns an *uninitialized* Application. We need
        # the store + api clients so handlers reach the backend; we do NOT call
        # run_polling() here — inbound updates come from the HTTP webhook.
        store = StateStore(bot_settings.state_db)
        store.init()
        # Recovery: any stale in-flight idempotency marks in the bot's OWN
        # idempotency table (conversation/daily marks) are reset on startup so
        # crashes do not permanently pin a conversation key.
        try:
            store.recover_stale_in_flight()
        except Exception as exc:  # noqa: BLE001
            logger.warning("store.recover_stale_in_flight failed: %s", exc)

        api_client = PasayApiClient(
            base_url=bot_settings.pasay_api_base,
            api_key=bot_settings.pasay_api_key,
            timeout=bot_settings.pasay_http_timeout_seconds,
        )
        admin_api_client = None
        if bot_settings.pasay_admin_api_key:
            admin_api_client = PasayApiClient(
                base_url=bot_settings.pasay_api_base,
                api_key=bot_settings.pasay_admin_api_key,
                timeout=bot_settings.pasay_http_timeout_seconds,
            )
        job_api_client = None
        if bot_settings.pasay_job_api_key:
            job_api_client = PasayApiClient(
                base_url=bot_settings.pasay_api_base,
                api_key=bot_settings.pasay_job_api_key,
                timeout=bot_settings.pasay_http_timeout_seconds,
            )

        app = build_application(
            settings=bot_settings,
            api_client=api_client,
            store=store,
            admin_api_client=admin_api_client,
            job_api_client=job_api_client,
        )
        # ``process_update`` requires the Application to have been through
        # initialize() + start() so bot_data / bot / job_queue etc. are ready.
        await app.initialize()
        await app.start()

        _PTB_APP = app
        _PTB_APP_READY = True
        _PTB_APP_INIT_ERR = None
        return app
    except Exception as exc:  # noqa: BLE001
        _PTB_APP = None
        _PTB_APP_READY = True  # do not keep retrying boot per request
        _PTB_APP_INIT_ERR = f"{type(exc).__name__}: {exc}"
        logger.error("PTB application boot failed: %s", _PTB_APP_INIT_ERR, exc_info=True)
        raise


# ---------------------------------------------------------------------------
# Idempotency + state machine (PostgreSQL-backed)
# ---------------------------------------------------------------------------

class ReplayOutcome:
    """What the idempotency layer decided about this inbound update."""

    NEW = "new"                     # we claimed it, caller must dispatch
    DONE = "done"                   # already finished; short-circuit 200 OK
    FAILED = "failed"               # terminal failure recorded; short-circuit
    RETRY_ALLOWED = "retry_allowed" # retryable state / stale claim re-claimed


def _effective_chat_user(update: TelegramUpdate) -> tuple[int | None, int | None, str | None]:
    chat_id: int | None = None
    user_id: int | None = None
    utype: str | None = None
    for attr, name in (
        ("message", "message"),
        ("edited_message", "edited_message"),
        ("callback_query", "callback_query"),
        ("inline_query", "inline_query"),
        ("chosen_inline_result", "chosen_inline_result"),
        ("channel_post", "channel_post"),
        ("edited_channel_post", "edited_channel_post"),
        ("shipping_query", "shipping_query"),
        ("pre_checkout_query", "pre_checkout_query"),
        ("poll", "poll"),
        ("poll_answer", "poll_answer"),
        ("my_chat_member", "my_chat_member"),
        ("chat_member", "chat_member"),
        ("chat_join_request", "chat_join_request"),
    ):
        if getattr(update, attr, None) is not None:
            utype = name
            break
    if update.effective_chat is not None:
        chat_id = int(update.effective_chat.id)
    if update.effective_user is not None:
        user_id = int(update.effective_user.id)
    return chat_id, user_id, utype


def claim_update_or_short_circuit(
    db,
    update_id: int,
    chat_id: int | None,
    user_id: int | None,
    update_type: str | None,
) -> tuple[str, TelegramWebhookUpdate | None]:
    """Try to claim this ``update_id`` for processing RIGHT NOW.

    ``db`` is a SQLAlchemy Session (typically injected from the FastAPI Depends
    pipeline). The call is responsible for its own commits because the claim
    decision must be persisted to the DB *before* we run any handler so a
    concurrent replay cannot slip through.

    Returns (outcome, row):
      * ``(NEW, row)``            — caller should dispatch handlers
      * ``(DONE/FAILED, row)``    — terminal; caller returns 200 OK without dispatch
      * ``(RETRY_ALLOWED, row)``  — stale claim / retryable state; caller dispatches
      * ``(RETRY_ALLOWED, None)`` — concurrent claim race lost (rare)
    """
    cutoff = TelegramWebhookUpdate.claim_stale_cutoff()
    # Fast path: INSERT the claim. Primary key violation = we've seen it.
    row = TelegramWebhookUpdate(
        update_id=update_id,
        chat_id=chat_id,
        user_id=user_id,
        update_type=update_type,
        state=TelegramWebhookState.claimed.value,
        attempt_count=1,
    )
    db.add(row)
    try:
        db.commit()
        db.refresh(row)
        return ReplayOutcome.NEW, row
    except IntegrityError:
        db.rollback()

    # Conflict => row exists; load it and decide.
    stmt = select(TelegramWebhookUpdate).where(
        TelegramWebhookUpdate.update_id == update_id
    )
    existing = db.execute(stmt).scalar_one_or_none()
    if existing is None:
        return ReplayOutcome.NEW, None

    state = existing.state
    if state in (TelegramWebhookState.done.value, TelegramWebhookState.failed.value):
        return (
            ReplayOutcome.DONE if state == TelegramWebhookState.done.value else ReplayOutcome.FAILED,
            existing,
        )

    # claimed or retryable => only re-claim if we are past the staleness window
    if state == TelegramWebhookState.claimed.value and existing.created_at >= cutoff:
        # Another live worker has it: do NOT steal. Tell caller "retry allowed
        # with no row" => caller returns 200 OK, Telegram will replay later.
        return ReplayOutcome.RETRY_ALLOWED, None

    # Either: retryable state, or stale claimed. Bump attempt_count and re-claim.
    new_count = int(existing.attempt_count or 0) + 1
    existing.state = TelegramWebhookState.claimed.value
    existing.attempt_count = new_count
    existing.last_error = None
    existing.last_error_type = None
    db.add(existing)
    try:
        db.commit()
        db.refresh(existing)
        return ReplayOutcome.RETRY_ALLOWED, existing
    except Exception:
        db.rollback()
        return ReplayOutcome.RETRY_ALLOWED, None


def transition_update(db, update_id: int, state: str, *, last_error: str | None = None,
                      last_error_type: str | None = None,
                      handler_result_summary: str | None = None) -> None:
    stmt = (
        update(TelegramWebhookUpdate)
        .where(TelegramWebhookUpdate.update_id == update_id)
        .values(
            state=state,
            last_error=last_error,
            last_error_type=last_error_type,
            handler_result_summary=handler_result_summary,
            processed_at=func.now() if state in (
                TelegramWebhookState.done.value,
                TelegramWebhookState.failed.value,
            ) else None,
        )
        .execution_options(synchronize_session=False)
    )
    db.execute(stmt)
    db.commit()


# ---------------------------------------------------------------------------
# Temporary vs permanent error classification.
# Only TEMPORARY failures get retried (either immediately in-process with
# small backoff, or via Telegram's delivery replay once we return 200 and leave
# the row in ``retryable``).
# ---------------------------------------------------------------------------

def _is_temporary_error(exc: BaseException) -> bool:
    # PERMANENT telegram API failures (check FIRST because python-telegram-bot
    # v22.8 has BadRequest -> NetworkError in the MRO, so we must short-circuit
    # the more general NetworkError check below).
    if isinstance(exc, (BadRequest, Forbidden, InvalidToken)):
        return False
    # Telegram SDK transients
    if isinstance(exc, (NetworkError, TimedOut, RetryAfter)):
        return True
    if isinstance(exc, Conflict):
        # webhook vs polling fight -> operator needs to stop the poller.
        # Temporary from our POV: a second attempt *may* succeed.
        return True
    # DB
    if isinstance(exc, OperationalError):
        return True
    if isinstance(exc, DBAPIError):
        # best-effort subclassification for psycopg2 transient classes
        cls_name = type(exc).__name__
        if "timeout" in cls_name.lower() or "connection" in cls_name.lower():
            return True
    # PasayApiClient transient wrappers
    cls_name = type(exc).__name__
    if "Timeout" in cls_name or "NetworkError" in cls_name:
        return True
    return False


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

async def process_telegram_update_payload(
    db,
    raw_json: dict[str, Any],
    *,
    now: datetime | None = None,
) -> tuple[int, dict[str, Any]]:
    """Validate + idempotency-check + dispatch an inbound webhook payload.

    ``db`` is the caller-injected SQLAlchemy Session (from the FastAPI Depends
    pipeline). All state mutations go through this session so test fixtures and
    production share the same wiring.

    Returns (HTTP status code, JSON body). The endpoint contract is:
      * 200   — accepted (dispatched or replay-short-circuited or permanently failed)
      * 400   — malformed / unknown payload
      * 401   — webhook disabled / not configured
      * 500   — boot wiring broken (operator action required)
    """
    now = now or datetime.now(timezone.utc)

    # 1) Configuration gating: if no secret is set the operator MUST NOT enable
    #    webhooks; fail closed.
    configured_secret = (_backend_settings.telegram_webhook_secret or "").strip()
    if not configured_secret:
        logger.error("TELEGRAM_WEBHOOK_SECRET not configured — rejecting webhook (fail closed)")
        return 401, {"ok": False, "error": "webhook_not_configured"}

    # 2) Parse payload into a Telegram.Update object. Failure here = malformed,
    #    short-circuit with 400 and never touch the idempotency table.
    try:
        tg_update = TelegramUpdate.de_json(raw_json, None)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "webhook malformed update payload: %s: %s",
            type(exc).__name__, exc,
        )
        return 400, {"ok": False, "error": "malformed_update",
                     "error_type": type(exc).__name__}

    if tg_update is None:
        logger.warning("webhook payload did not deserialize to a Telegram Update (None)")
        return 400, {"ok": False, "error": "unsupported_update"}

    update_id: int = int(tg_update.update_id)
    chat_id, user_id, utype = _effective_chat_user(tg_update)

    # 3) Idempotency / staleness decision BEFORE any handler runs.
    outcome, row = claim_update_or_short_circuit(
        db,
        update_id=update_id,
        chat_id=chat_id,
        user_id=user_id,
        update_type=utype,
    )
    if outcome == ReplayOutcome.DONE:
        logger.info(
            "webhook replay update_id=%s chat_id=%s state=done short_circuit=yes",
            update_id, chat_id,
        )
        return 200, {"ok": True, "replay": True, "state": "done"}
    if outcome == ReplayOutcome.FAILED:
        logger.info(
            "webhook replay update_id=%s chat_id=%s state=failed short_circuit=yes last_error_type=%s",
            update_id, chat_id, row.last_error_type if row else None,
        )
        return 200, {"ok": False, "replay": True, "state": "failed",
                     "error_type": row.last_error_type if row else None}
    if outcome == ReplayOutcome.RETRY_ALLOWED and row is None:
        # Concurrent live claim lost — return 200 OK silently; Telegram's
        # delivery mechanism will replay when our peer finishes.
        logger.info(
            "webhook update_id=%s chat_id=%s claimed elsewhere (not stale) short_circuit=yes",
            update_id, chat_id,
        )
        return 200, {"ok": True, "replay": False, "state": "claimed_elsewhere"}

    attempt_no = int(row.attempt_count) if row else 1

    # 4) Boot PTB application (once per process). Any error here is a wiring
    #    problem; we mark the update retryable so a fixed deployment can pick
    #    it back up via Telegram replay.
    try:
        ptb_app = await get_ptb_application()
    except Exception as exc:  # noqa: BLE001
        err_type = type(exc).__name__
        logger.error(
            "webhook update_id=%s chat_id=%s PTB boot failed: %s: %s",
            update_id, chat_id, err_type, exc,
        )
        _finalize_after_dispatch_error(
            db,
            update_id=update_id, attempt_no=attempt_no, exc=exc,
            is_temp=True, force_failed=True,
        )
        return 200, {"ok": False, "error": "bot_wiring_unavailable",
                     "error_type": err_type}

    # 5) Dispatch to existing handlers (the EXACT same code path polling uses,
    #    via ``Application.process_update``).
    max_attempts = max(1, int(_backend_settings.telegram_webhook_max_attempts or 1))
    attempt = 0
    last_exc: BaseException | None = None
    last_temp = False
    while attempt < max_attempts:
        attempt += 1
        started = time.perf_counter()
        try:
            await ptb_app.process_update(tg_update)
            dur_ms = int((time.perf_counter() - started) * 1000)
            transition_update(
                db,
                update_id,
                TelegramWebhookState.done.value,
                handler_result_summary=f"process_update ok in {dur_ms}ms",
            )
            logger.info(
                "webhook update_id=%s chat_id=%s user_id=%s type=%s state=done attempt=%d/%d dur_ms=%d",
                update_id, chat_id, user_id, utype, attempt, max_attempts, dur_ms,
            )
            return 200, {"ok": True, "state": "done", "attempts": attempt,
                         "dur_ms": dur_ms}
        except Exception as exc:  # noqa: BLE001 - 异常隔离: 任何 handler 异常都不能带出进程
            dur_ms = int((time.perf_counter() - started) * 1000)
            last_exc = exc
            last_temp = _is_temporary_error(exc)
            err_type = type(exc).__name__
            logger.warning(
                "webhook update_id=%s chat_id=%s attempt=%d/%d state=exception temp=%s dur_ms=%s %s: %s",
                update_id, chat_id, attempt, max_attempts, last_temp, dur_ms, err_type, exc,
                exc_info=False,
            )
            if not last_temp:
                # Permanent failure: no point retrying in-process.
                break
            # Backoff before in-process retry.
            sleep_s = min(2 ** (attempt - 1), 5)
            await asyncio.sleep(sleep_s)

    # 6) Post-loop: either permanent failure or temp budget exhausted.
    assert last_exc is not None
    # Permanent failure OR in-process retries used the full budget => mark
    # permanently failed so Telegram stops replaying it. If we still have
    # budget via *cross-request* replays (attempt_no from the claim counter),
    # leave retryable so the next Telegram delivery picks it up (fresh claim
    # will bump attempt_count and give us another in-process budget cycle).
    budget_spent_this_request = attempt >= max_attempts
    cross_request_budget_spent = attempt_no >= max_attempts
    force_failed = (not last_temp) or budget_spent_this_request or cross_request_budget_spent
    _finalize_after_dispatch_error(
        db,
        update_id=update_id,
        attempt_no=attempt_no,
        exc=last_exc,
        is_temp=last_temp,
        force_failed=force_failed,
    )
    err_type = type(last_exc).__name__
    final_state = (
        TelegramWebhookState.failed.value
        if force_failed else TelegramWebhookState.retryable.value
    )
    logger.error(
        "webhook update_id=%s chat_id=%s final_state=%s attempts=%d/%d error_type=%s temp=%s",
        update_id, chat_id, final_state, attempt, max_attempts, err_type, last_temp,
    )
    return 200, {
        "ok": False,
        "state": final_state,
        "error_type": err_type,
        "attempts": attempt,
        "retryable": not force_failed,
    }


def _finalize_after_dispatch_error(
    db,
    *,
    update_id: int,
    attempt_no: int,
    exc: BaseException,
    is_temp: bool,
    force_failed: bool,
) -> None:
    max_attempts = max(1, int(_backend_settings.telegram_webhook_max_attempts or 1))
    next_state: str
    if force_failed or attempt_no >= max_attempts:
        next_state = TelegramWebhookState.failed.value
    else:
        next_state = (
            TelegramWebhookState.retryable.value
            if is_temp else TelegramWebhookState.failed.value
        )
    err_str = str(exc)
    if len(err_str) > 10_000:
        err_str = err_str[:10_000]
    try:
        transition_update(
            db,
            update_id,
            next_state,
            last_error=err_str,
            last_error_type=type(exc).__name__,
        )
    except Exception as inner:  # noqa: BLE001
        # Never ever propagate DB write errors back to the caller.
        logger.error(
            "webhook update_id=%s failed to persist error state: %s: %s",
            update_id, type(inner).__name__, inner,
        )
