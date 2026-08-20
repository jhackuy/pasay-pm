"""Telegram webhook inbound-update pipeline: idempotency, PTB dispatch, retries.

Scope (per PASAY-WEBHOOK-ARCH-P0-001):
  * Update → handler → Neon/PostgreSQL → Telegram reply 主链路
  * update_id 幂等重放防护 (持久化, 重启后仍生效)
  * 异常隔离 (DB/Telegram/handler/unsupported/malformed 不导致服务退出)
  * 仅安全可重试的临时失败有限重试
  * 结构化日志 (update_id / chat_id / 状态 / 错误类型)

Key contracts (after Owner review — Issue #18 PR #19 fixes):
  * Telegram webhook redelivery: Telegram RETRIES when the HTTP response is
    NOT a 2xx. Therefore retryable outcomes (temp failure, cross-request
    budget not exhausted) return HTTP 503 so Telegram replays the update
    later. Only terminal outcomes (done, permanently failed, claim lost to
    a live peer, replay short-circuits) return 2xx.
  * Stale-claim detection uses ``updated_at`` (NOT ``created_at``). A stale
    reclaim atomically bumps ``updated_at`` via CAS-style conditional UPDATE
    so no two concurrent replays both think they own the row.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy import DateTime, func, select, update
from telegram import Bot, Update as TelegramUpdate
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
# Concurrent-first-call protection: module-level asyncio.Lock + re-check after
# acquisition so exactly one coroutine runs the boot sequence.
# ---------------------------------------------------------------------------
_PTB_APP_READY = False
_PTB_APP: Any = None
_PTB_INIT_ERR_CLASS: str | None = None     # "NetworkError" / "InvalidToken" / ...
_PTB_INIT_ERR_MSG: str | None = None
_PTB_INIT_FAIL_PERMANENT: bool = False    # True = config/auth error, never retry
_PTB_LAST_FAIL_AT: float | None = None    # epoch seconds, used for a tiny
                                          # cooldown so temp failures do not
                                          # hammer boot on every single request
_PTB_INIT_LOCK = asyncio.Lock()
# After a *temporary* boot failure we wait at least this many seconds before
# re-attempting the full boot sequence. Prevents concurrent requests from
# duplicating doomed boot work.
_PTB_RETRY_COOLDOWN_SECONDS = 15.0


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


def _classify_ptb_boot_exception(exc: BaseException) -> tuple[bool, str, str]:
    """Return (permanent, class_name, message) for a PTB boot exception.

    ``permanent=True`` means retrying later will never help (e.g. the bot token
    is invalid or the operator forgot to set one). ``permanent=False`` means
    the next request (after a cooldown) MAY succeed (e.g. network blip,
    PasayApiClient timeout, Neon connection issue).
    """
    cls_name = type(exc).__name__
    msg = str(exc)
    if isinstance(exc, (InvalidToken, Forbidden, BadRequest)):
        return True, cls_name, msg
    # Telegram-side transients.
    if isinstance(exc, (NetworkError, TimedOut, RetryAfter, Conflict)):
        return False, cls_name, msg
    # DB transients.
    if isinstance(exc, OperationalError):
        return False, cls_name, msg
    if isinstance(exc, DBAPIError):
        if any(k in cls_name.lower() for k in ("timeout", "connection")):
            return False, cls_name, msg
    # PasayApiClient / httpx transients (duck-typed by class name to avoid a
    # hard import of the bot subtree here).
    low = cls_name.lower()
    if "timeout" in low or "network" in low or "connect" in low:
        return False, cls_name, msg
    # Missing TOKEN / missing pasay_bot subtree / broken config files are
    # permanent from our POV; operator must intervene.
    missing_sentinels = ("token", "not set", "not configured", "could not be parsed",
                         "no module named 'pasay_bot", "environment variable")
    low_msg = msg.lower()
    if any(s in low_msg for s in missing_sentinels):
        return True, cls_name, msg
    # ND_RETURN #2: unclassified exceptions default to TEMPORARY. Only explicit
    # token/auth/config evidence above can classify as permanent.
    return False, cls_name, msg


async def get_ptb_application():
    """Return the singleton PTB Application, booting it on first call.

    Concurrency safe:
      * Fast path without lock when boot is known-good.
      * Slow path serialised under ``_PTB_INIT_LOCK`` with a re-check after
        acquisition (double-checked locking pattern for async). Exactly ONE
        coroutine runs the boot; everybody else waits then uses the result.

    Boot outcome classification (affects subsequent retry policy):
      * SUCCESS                 → cache app, return it forever.
      * TEMPORARY failure       → cache failure info, but only refuse re-boots
                                  for ``_PTB_RETRY_COOLDOWN_SECONDS`` seconds.
      * PERMANENT failure       → cache failure forever; callers receive a
                                  RuntimeError with the original class/message.
    """
    # --- Fast path (no lock, outside contention). ---
    global _PTB_APP_READY, _PTB_APP, _PTB_INIT_ERR_CLASS, _PTB_INIT_ERR_MSG
    global _PTB_INIT_FAIL_PERMANENT, _PTB_LAST_FAIL_AT
    if _PTB_APP_READY and _PTB_APP is not None:
        return _PTB_APP
    if _PTB_APP_READY and _PTB_INIT_FAIL_PERMANENT:
        raise RuntimeError(
            f"PTB application boot failed permanently: {_PTB_INIT_ERR_CLASS}: {_PTB_INIT_ERR_MSG}"
        )
    now_epoch = time.time()
    if (_PTB_APP_READY and not _PTB_INIT_FAIL_PERMANENT and _PTB_LAST_FAIL_AT is not None
            and (now_epoch - _PTB_LAST_FAIL_AT) < _PTB_RETRY_COOLDOWN_SECONDS):
        # Temporary boot failure + still within cooldown: re-raise so the
        # caller can mark the update retryable and Telegram will replay later.
        raise RuntimeError(
            f"PTB boot failed recently (temp); cooldown active: {_PTB_INIT_ERR_CLASS}: {_PTB_INIT_ERR_MSG}"
        )

    # --- Slow path: serialise under the module-level lock. ---
    async with _PTB_INIT_LOCK:
        # Re-check AFTER acquiring the lock — the winning coroutine may have
        # finished booting while we were waiting.
        if _PTB_APP_READY and _PTB_APP is not None:
            return _PTB_APP
        if _PTB_APP_READY and _PTB_INIT_FAIL_PERMANENT:
            raise RuntimeError(
                f"PTB application boot failed permanently: {_PTB_INIT_ERR_CLASS}: {_PTB_INIT_ERR_MSG}"
            )
        # ND_RETURN #3 (lock-inner cooldown recheck): after waiting for the
        # lock another caller may have recorded a temp-fail cooldown. Respect
        # it here so we don't duplicate a doomed boot that just finished.
        now_epoch_inner = time.time()
        if (_PTB_APP_READY and not _PTB_INIT_FAIL_PERMANENT
                and _PTB_LAST_FAIL_AT is not None
                and (now_epoch_inner - _PTB_LAST_FAIL_AT) < _PTB_RETRY_COOLDOWN_SECONDS):
            raise RuntimeError(
                f"PTB boot failed recently (temp); cooldown active (inner recheck): {_PTB_INIT_ERR_CLASS}: {_PTB_INIT_ERR_MSG}"
            )
        # Reset state so a temp failure from a previous epoch does not poison.
        _PTB_APP = None
        _PTB_APP_READY = False

        store = None
        api_client = None
        admin_api_client = None
        job_api_client = None
        app = None
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
            if bot_settings.pasay_admin_api_key:
                admin_api_client = PasayApiClient(
                    base_url=bot_settings.pasay_api_base,
                    api_key=bot_settings.pasay_admin_api_key,
                    timeout=bot_settings.pasay_http_timeout_seconds,
                )
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

            # HAPPY PATH: publish the singleton. All subsequent fast-paths win.
            _PTB_APP = app
            _PTB_APP_READY = True
            _PTB_INIT_ERR_CLASS = None
            _PTB_INIT_ERR_MSG = None
            _PTB_INIT_FAIL_PERMANENT = False
            _PTB_LAST_FAIL_AT = None
            return app
        except Exception as exc:  # noqa: BLE001
            # ND_RETURN #3: best-effort teardown of partially-built resources so
            # the next boot attempt (after cooldown or caller retry) doesn't
            # inherit leaked sockets / state files / half-open DB connections.
            if app is not None:
                try:
                    try:
                        await app.stop()
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        await app.shutdown()
                    except Exception:  # noqa: BLE001
                        pass
                except Exception:  # noqa: BLE001
                    pass
            # PasayApiClient typically has no .close() contract today; keep the
            # branch for future-proofing without hard-requiring the method.
            for _c in (job_api_client, admin_api_client, api_client):
                if _c is None:
                    continue
                _closer = getattr(_c, "close", None)
                if callable(_closer):
                    try:
                        _closer()
                    except Exception:  # noqa: BLE001
                        pass
            if store is not None:
                _sclose = getattr(store, "close", None)
                if callable(_sclose):
                    try:
                        _sclose()
                    except Exception:  # noqa: BLE001
                        pass

            perm, cls_name, msg = _classify_ptb_boot_exception(exc)
            _PTB_APP = None
            _PTB_APP_READY = True
            _PTB_INIT_ERR_CLASS = cls_name
            _PTB_INIT_ERR_MSG = msg
            _PTB_INIT_FAIL_PERMANENT = perm
            _PTB_LAST_FAIL_AT = time.time()
            logger.error(
                "PTB application boot failed (perm=%s): %s: %s",
                perm, cls_name, msg, exc_info=True,
            )
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
                                    #   with row = None  ⇒ live peer owns it
                                    #   with row != None ⇒ we now own it
    DB_TRANSIENT = "db_transient"   # claim step hit a DB transient; caller
                                    #   returns HTTP 503 to trigger Telegram
                                    #   replay WITHOUT persisting any row state


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


def _safe_rollback(db) -> None:
    """Roll back the session; swallow and log any secondary error so a DB
    failure never propagates out of this module."""
    try:
        db.rollback()
    except Exception as inner:  # noqa: BLE001
        logger.error("claim_update_or_short_circuit session rollback failed: %s: %s",
                     type(inner).__name__, inner)


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

    DB failure policy (Issue #18 Owner review F4):
      * ANY DB exception (IntegrityError, OperationalError, or other
        SQLAlchemyError) is caught, the session is rolled back, and the
        caller receives ``(DB_TRANSIENT, None)`` so the endpoint returns
        HTTP 503 — Telegram will replay, and no leaked broken state is
        visible to the outer layers. No SQLAlchemyError reaches the router.

    Stale reclaim policy (Issue #18 Owner review F8):
      * Staleness is measured against ``updated_at`` (NOT ``created_at``),
        and every reclaim atomically bumps ``updated_at`` via a conditional
        UPDATE so two concurrent replays cannot both "win" the same row.
      * A live claim (``updated_at >= cutoff``) is never stolen; we return
        ``(RETRY_ALLOWED, None)`` so the caller short-circuits.

    Returns (outcome, row):
      * ``(NEW, row)``           — first time seen; caller should dispatch
      * ``(DONE/FAILED, row)``   — terminal; caller returns 200 no dispatch
      * ``(RETRY_ALLOWED, row)`` — stale/retryable; caller now owns it, dispatch
      * ``(RETRY_ALLOWED, None)``— live peer owns it, caller returns 200 no dispatch
      * ``(DB_TRANSIENT, None)`` — DB transient, caller returns 503 no dispatch
    """
    cutoff = TelegramWebhookUpdate.claim_stale_cutoff()

    # ------------------------------------------------------------------
    # 1) Fast path: INSERT the claim. PK violation = we've seen it before.
    # ------------------------------------------------------------------
    row = TelegramWebhookUpdate(
        update_id=update_id,
        chat_id=chat_id,
        user_id=user_id,
        update_type=update_type,
        state=TelegramWebhookState.claimed.value,
        delivery_count=1,
        attempt_count=1,
    )
    db.add(row)
    try:
        db.commit()
        db.refresh(row)
        return ReplayOutcome.NEW, row
    except IntegrityError:
        _safe_rollback(db)
    except SQLAlchemyError as exc:
        logger.warning(
            "webhook claim_insert DB transient update_id=%s %s: %s",
            update_id, type(exc).__name__, exc,
        )
        _safe_rollback(db)
        return ReplayOutcome.DB_TRANSIENT, None
    except Exception as exc:  # noqa: BLE001 - absolute safety net
        logger.exception("webhook claim_insert unexpected error update_id=%s", update_id)
        _safe_rollback(db)
        return ReplayOutcome.DB_TRANSIENT, None

    # ------------------------------------------------------------------
    # 2) Conflict => row exists; load it and classify.
    # ------------------------------------------------------------------
    existing: TelegramWebhookUpdate | None
    try:
        stmt = select(TelegramWebhookUpdate).where(
            TelegramWebhookUpdate.update_id == update_id
        )
        existing = db.execute(stmt).scalar_one_or_none()
    except SQLAlchemyError as exc:
        logger.warning(
            "webhook claim_select DB transient update_id=%s %s: %s",
            update_id, type(exc).__name__, exc,
        )
        _safe_rollback(db)
        return ReplayOutcome.DB_TRANSIENT, None
    except Exception as exc:  # noqa: BLE001
        logger.exception("webhook claim_select unexpected error update_id=%s", update_id)
        _safe_rollback(db)
        return ReplayOutcome.DB_TRANSIENT, None

    if existing is None:
        # Extremely rare race: another thread committed + deleted between our
        # failed INSERT and our SELECT. Let caller short-circuit cleanly.
        return ReplayOutcome.RETRY_ALLOWED, None

    state = existing.state
    if state in (TelegramWebhookState.done.value, TelegramWebhookState.failed.value):
        return (
            ReplayOutcome.DONE if state == TelegramWebhookState.done.value else ReplayOutcome.FAILED,
            existing,
        )

    # Live (non-stale) claim by peer => never steal.
    # STALENESS USES updated_at (Issue #18 F8). updated_at is touched on every
    # state transition and every reclaim so a long-lived row can't be "born
    # stale" the moment CLAIM_STALE_SECONDS elapses since creation.
    if state == TelegramWebhookState.claimed.value and existing.updated_at >= cutoff:
        return ReplayOutcome.RETRY_ALLOWED, None

    # ------------------------------------------------------------------
    # 3) Either retryable OR stale claimed. Atomically re-claim via CAS
    #    UPDATE so at most one concurrent request wins this row.
    # ------------------------------------------------------------------
    new_attempt_count = int(existing.attempt_count or 0) + 1
    new_delivery_count = int(existing.delivery_count or 0) + 1
    # Conditional UPDATE: row must STILL be in (claimed, retryable) AND still
    # stale, otherwise another request already grabbed it. updated_at bumps
    # via func.now() so the winner immediately becomes "freshly claimed" and
    # the next concurrent requester sees updated_at >= cutoff and backs off.
    try:
        upd_stmt = (
            update(TelegramWebhookUpdate)
            .where(TelegramWebhookUpdate.update_id == update_id)
            .where(TelegramWebhookUpdate.state.in_(
                (TelegramWebhookState.claimed.value, TelegramWebhookState.retryable.value)
            ))
            # If state==claimed: updated_at must be stale. For retryable rows
            # we allow reclaim regardless (Telegram redelivery semantics).
            .where(
                (TelegramWebhookUpdate.state == TelegramWebhookState.retryable.value)
                | (TelegramWebhookUpdate.updated_at < cutoff)
            )
            .values(
                state=TelegramWebhookState.claimed.value,
                delivery_count=new_delivery_count,
                attempt_count=new_attempt_count,
                last_error=None,
                last_error_type=None,
                updated_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        result = db.execute(upd_stmt)
        db.commit()
    except SQLAlchemyError as exc:
        logger.warning(
            "webhook claim_reclaim DB transient update_id=%s %s: %s",
            update_id, type(exc).__name__, exc,
        )
        _safe_rollback(db)
        return ReplayOutcome.DB_TRANSIENT, None
    except Exception as exc:  # noqa: BLE001
        logger.exception("webhook claim_reclaim unexpected error update_id=%s", update_id)
        _safe_rollback(db)
        return ReplayOutcome.DB_TRANSIENT, None

    rows_affected = int(getattr(result, "rowcount", 0) or 0)
    if rows_affected <= 0:
        # CAS lost → a live peer won the race.
        return ReplayOutcome.RETRY_ALLOWED, None

    # We won! Re-read the row so the caller has attempt_count/etc.
    try:
        db.refresh(existing)
    except SQLAlchemyError:
        _safe_rollback(db)
        try:
            existing = db.execute(select(TelegramWebhookUpdate).where(
                TelegramWebhookUpdate.update_id == update_id
            )).scalar_one_or_none()
        except SQLAlchemyError:
            return ReplayOutcome.DB_TRANSIENT, None
    # ND_RETURN #4: CAS UPDATE succeeded but the row object can't be re-read
    # (identity_map corruption, extreme DB race, or the fallback SELECT also
    # got nothing). Treat the update as "not deterministically claimed" and
    # return DB_TRANSIENT → HTTP 503 → Telegram replays. We MUST NOT fall
    # through to RETRY_ALLOWED None because the caller treats (row=None) as
    # "live peer owns it → HTTP 200 → Telegram drops delivery permanently".
    if existing is None:
        return ReplayOutcome.DB_TRANSIENT, None
    return ReplayOutcome.RETRY_ALLOWED, existing


def transition_update(db, update_id: int, state: str, *, last_error: str | None = None,
                      last_error_type: str | None = None,
                      handler_result_summary: str | None = None,
                      bump_attempt: bool = False,
                      add_attempt: int = 0) -> None:
    """Persist the result of a dispatch.

    ``bump_attempt`` is used when cross-request redelivery semantics require
    the attempt count to reflect THIS attempt (the claim layer already bumped
    once for the fresh claim, but dispatch failures may add per-request
    in-process attempt deltas via ``add_attempt``).

    ``updated_at`` is ALWAYS set via ``func.now()`` at the SQL layer so ORM
    ``onupdate`` semantics (which do not fire for bulk UPDATE) are honoured.
    Staleness in the reclaim path depends on this field being fresh.
    """
    values: dict[str, Any] = {
        "state": state,
        "updated_at": func.now(),
    }
    if last_error is not None:
        values["last_error"] = last_error
    if last_error_type is not None:
        values["last_error_type"] = last_error_type
    if handler_result_summary is not None:
        values["handler_result_summary"] = handler_result_summary
    if bump_attempt or add_attempt > 0:
        from sqlalchemy import literal_column
        incr = 1 if bump_attempt else 0
        incr += int(add_attempt)
        values["attempt_count"] = TelegramWebhookUpdate.attempt_count + literal_column(str(incr))
    if state in (TelegramWebhookState.done.value, TelegramWebhookState.failed.value):
        values["processed_at"] = func.now()
    stmt = (
        update(TelegramWebhookUpdate)
        .where(TelegramWebhookUpdate.update_id == update_id)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    try:
        db.execute(stmt)
        db.commit()
    except Exception as inner:  # noqa: BLE001 — never propagate secondary DB errors
        _safe_rollback(db)
        logger.error(
            "webhook update_id=%s transition_update state=%s failed %s: %s",
            update_id, state, type(inner).__name__, inner,
        )


# ---------------------------------------------------------------------------
# Temporary vs permanent error classification.
# Only TEMPORARY failures get retried. Two retry tiers exist:
#   (a) IN-PROCESS small backoff (0.2s … 1.0s capped) — inside the same HTTP
#       request, before returning — used for fast transients (flaky TCP).
#   (b) CROSS-REQUEST via Telegram's webhook redelivery (HTTP 503 + state=retryable)
#       — used when the in-process budget was not enough.
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

    HTTP status contract (Issue #18 Owner review F7 — critical):
      * 200 — terminal / delivered: dispatch succeeded, permanent failure,
              replay-short-circuit (done/failed), or live peer owns the claim.
              Telegram considers these updates DELIVERED and moves on.
      * 503 — retryable: claim hit a DB transient, PTB boot is temporarily
              down, or the handler returned a temporary failure AND we still
              have cross-request attempt budget left. Telegram WILL redeliver.
      * 400 — malformed / unsupported payload (de_json failure / None update).
      * 401 — TELEGRAM_WEBHOOK_SECRET not configured (fail closed).
    """
    now = now or datetime.now(timezone.utc)
    max_attempts_cross = max(
        1, int(_backend_settings.telegram_webhook_max_attempts or 1)
    )
    # In-process attempts are capped at the same budget but kept short so the
    # response doesn't block Telegram's redelivery timer.
    max_in_process = max_attempts_cross

    # 1) Configuration gating: if no secret is set the operator MUST NOT enable
    #    webhooks; fail closed.
    configured_secret = (_backend_settings.telegram_webhook_secret or "").strip()
    if not configured_secret:
        logger.error("TELEGRAM_WEBHOOK_SECRET not configured — rejecting webhook (fail closed)")
        return 401, {"ok": False, "error": "webhook_not_configured"}

    # 2) Boot PTB application ONCE before deserialization so we have a bound
    #    Bot object to pass into TelegramUpdate.de_json. This fixes Issue #18
    #    F5: CallbackQuery.answer() shortcuts require get_bot() to resolve.
    #
    #    Order: boot → de_json(bot=ptb_app.bot). If boot is temporarily down
    #    we return HTTP 503 so Telegram replays after our cooldown. If boot
    #    is permanently broken (token invalid etc.) we return HTTP 500 and
    #    mark update as permanently failed (same payload would fail again).
    try:
        ptb_app = await get_ptb_application()
    except Exception as exc:  # noqa: BLE001
        # We haven't parsed the update yet, so we don't know update_id. If
        # raw_json has one we still try to write a state row for audit logs;
        # if claim fails for any reason we just log it and fall through.
        raw_update_id = raw_json.get("update_id") if isinstance(raw_json, dict) else None
        err_type = type(exc).__name__
        perm_cls_names = {"InvalidToken", "Forbidden", "BadRequest"}
        # Use cooldown/permanent flag saved by get_ptb_application: if the
        # failure was permanent => mark permanently failed once we have update_id
        perm_fail = (raw_update_id is not None) and (
            _PTB_INIT_FAIL_PERMANENT or err_type in perm_cls_names
        )
        temp_fail = not perm_fail
        final_state = TelegramWebhookState.retryable.value if temp_fail else TelegramWebhookState.failed.value
        if raw_update_id is not None:
            try:
                uid_int = int(raw_update_id)
            except (TypeError, ValueError):
                uid_int = None
            if uid_int is not None:
                try:
                    chat_id = user_id = utype = None
                    # Minimal extraction from raw JSON without de_json.
                    if isinstance(raw_json, dict):
                        msg = raw_json.get("message")
                        if isinstance(msg, dict):
                            ch = msg.get("chat")
                            fr = msg.get("from")
                            if isinstance(ch, dict) and "id" in ch:
                                try: chat_id = int(ch["id"])
                                except Exception: pass  # noqa: E722
                            if isinstance(fr, dict) and "id" in fr:
                                try: user_id = int(fr["id"])
                                except Exception: pass  # noqa: E722
                            utype = "message"
                        cq = raw_json.get("callback_query")
                        if isinstance(cq, dict):
                            fr = cq.get("from")
                            msg = cq.get("message")
                            if isinstance(fr, dict) and "id" in fr:
                                try: user_id = int(fr["id"])
                                except Exception: pass  # noqa: E722
                            if isinstance(msg, dict):
                                ch = msg.get("chat")
                                if isinstance(ch, dict) and "id" in ch:
                                    try: chat_id = int(ch["id"])
                                    except Exception: pass  # noqa: E722
                            utype = "callback_query"
                except Exception:  # noqa: BLE001
                    chat_id = user_id = utype = None
                # Try to claim (no-op if already present).
                outcome, _row = claim_update_or_short_circuit(
                    db, uid_int, chat_id, user_id, utype
                )
                # ND_RETURN #1: terminal replay preservation. If the row was
                # already in a terminal state (done/failed), the boot failure
                # this attempt MUST NOT overwrite it. Telegram replay of a
                # completed update gets the standard terminal short-circuit
                # response (HTTP 200), not the boot-error rewrite.
                if outcome in {ReplayOutcome.DONE, ReplayOutcome.FAILED}:
                    logger.info(
                        "webhook PTB boot unavailable but row already terminal update_id=%s state=%s — NOT overwriting",
                        uid_int, outcome,
                    )
                    if outcome == ReplayOutcome.DONE:
                        return 200, {"ok": True, "replay": True, "state": "done"}
                    else:
                        return 200, {
                            "ok": False, "replay": True, "state": "failed",
                            "error_type": _row.last_error_type if _row else None,
                        }
                if outcome != ReplayOutcome.DB_TRANSIENT:
                    err_str = str(exc)
                    if len(err_str) > 10_000:
                        err_str = err_str[:10_000]
                    # Claim may have succeeded/failed via any route; force a
                    # terminal state write for permanent boot failures so
                    # the same broken payload doesn't 503 forever.
                    try:
                        transition_update(
                            db, uid_int,
                            state=final_state,
                            last_error=err_str,
                            last_error_type=err_type,
                            bump_attempt=False,
                        )
                    except Exception:  # noqa: BLE001
                        pass
        logger.error(
            "webhook PTB boot unavailable perm=%s update_id=%s %s: %s",
            perm_fail, raw_update_id, err_type, exc,
        )
        if temp_fail:
            # Cross-request replay wanted: HTTP 503 triggers Telegram redelivery.
            http_status = 503
        else:
            # Permanent config issue: accept the delivery so Telegram stops.
            http_status = 200
        body = {
            "ok": False,
            "error": "bot_wiring_unavailable",
            "error_type": err_type,
            "state": final_state,
            "retryable": temp_fail,
        }
        return http_status, body

    # 3) Parse payload into a Telegram.Update object bound to ptb_app.bot.
    bot: Bot | None = getattr(ptb_app, "bot", None)
    try:
        tg_update = TelegramUpdate.de_json(raw_json, bot)
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

    # 4) Idempotency / staleness decision BEFORE any handler runs.
    outcome, row = claim_update_or_short_circuit(
        db,
        update_id=update_id,
        chat_id=chat_id,
        user_id=user_id,
        update_type=utype,
    )
    if outcome == ReplayOutcome.DB_TRANSIENT:
        # Claim layer hit a DB transient → return HTTP 503, Telegram replays.
        # Do NOT persist any row because we can't trust session state.
        logger.warning(
            "webhook update_id=%s chat_id=%s claim DB transient → returning 503 to Telegram",
            update_id, chat_id,
        )
        return 503, {"ok": False, "error": "db_transient", "retryable": True,
                     "state": TelegramWebhookState.retryable.value}
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
        # Concurrent live claim lost — return 200 OK (delivery accepted); the
        # winning peer dispatches and Telegram doesn't need to replay.
        logger.info(
            "webhook update_id=%s chat_id=%s claimed elsewhere (not stale) short_circuit=yes",
            update_id, chat_id,
        )
        return 200, {"ok": True, "replay": False, "state": "claimed_elsewhere"}

    attempt_cross = int(row.delivery_count) if row else 1

    # 5) Dispatch to existing handlers (the EXACT same code path polling uses,
    #    via ``Application.process_update``).
    attempt_in = 0
    last_exc: BaseException | None = None
    last_temp = False
    while attempt_in < max_in_process:
        attempt_in += 1
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
                "webhook update_id=%s chat_id=%s user_id=%s type=%s state=done attempts_in=%d/%d cross_attempt=%d dur_ms=%d",
                update_id, chat_id, user_id, utype, attempt_in, max_in_process, attempt_cross, dur_ms,
            )
            return 200, {"ok": True, "state": "done", "attempts": attempt_in,
                         "cross_attempt": attempt_cross, "dur_ms": dur_ms}
        except Exception as exc:  # noqa: BLE001 - 异常隔离: 任何 handler 异常都不能带出进程
            dur_ms = int((time.perf_counter() - started) * 1000)
            last_exc = exc
            last_temp = _is_temporary_error(exc)
            err_type = type(exc).__name__
            logger.warning(
                "webhook update_id=%s chat_id=%s in_attempt=%d/%d state=exception temp=%s dur_ms=%s %s: %s",
                update_id, chat_id, attempt_in, max_in_process, last_temp, dur_ms, err_type, exc,
                exc_info=False,
            )
            if not last_temp:
                # Permanent failure: no point retrying in-process.
                break
            if attempt_in < max_in_process:
                # CodeRabbit + Owner guidance: keep in-request backoff SHORT
                # so Telegram redelivery handles longer waits (cross-request).
                # Also honour RetryAfter.retry_after explicitly if present.
                sleep_s = min(0.2 * (2 ** (attempt_in - 1)), 1.0)
                if isinstance(exc, RetryAfter):
                    override = getattr(exc, "retry_after", None)
                    if override is not None:
                        try:
                            override_f = float(override)
                            sleep_s = min(override_f, 1.0)
                        except Exception:  # noqa: BLE001
                            pass
                await asyncio.sleep(sleep_s)

    # 6) Post-loop: either permanent failure or temp budget exhausted.
    assert last_exc is not None
    last_err_type = type(last_exc).__name__
    err_str = str(last_exc)
    if len(err_str) > 10_000:
        err_str = err_str[:10_000]

    # Classification:
    #   * permanent handler error                 → force failed (HTTP 200)
    #   * cross_request budget spent (attempt_cross >= max_attempts_cross)
    #                                              → force failed (HTTP 200)
    #   * otherwise temp + budget left             → retryable (HTTP 503)
    permanent_fail = not last_temp
    cross_budget_spent = attempt_cross >= max_attempts_cross
    force_failed = permanent_fail or cross_budget_spent

    if force_failed:
        final_state = TelegramWebhookState.failed.value
        http_status = 200
    else:
        final_state = TelegramWebhookState.retryable.value
        http_status = 503  # triggers Telegram webhook redelivery (Issue #18 F7)

    # Transition state. attempt_count is already bumped by the reclaim/claim
    # step that loaded this request; we ADD our in-process attempts so a row
    # that ran N in-process attempts reflects the true effort spent.
    try:
        transition_update(
            db,
            update_id,
            final_state,
            last_error=err_str,
            last_error_type=last_err_type,
            add_attempt=max(0, attempt_in - 1),
        )
    except Exception:  # noqa: BLE001
        # transition_update already catches and logs, but we add another safety
        # net so the HTTP response never 500s for state-persist reasons.
        pass
    logger.error(
        "webhook update_id=%s chat_id=%s final_state=%s http=%s cross_attempt=%d/%d in_attempt=%d error_type=%s temp=%s",
        update_id, chat_id, final_state, http_status, attempt_cross, max_attempts_cross,
        attempt_in, last_err_type, last_temp,
    )
    return http_status, {
        "ok": False,
        "state": final_state,
        "error_type": last_err_type,
        "attempts": attempt_in,
        "cross_attempt": attempt_cross,
        "retryable": not force_failed,
    }
