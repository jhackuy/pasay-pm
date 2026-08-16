"""PASAY-V2-FOUNDATION-001 reminder jobs: daily Active Tasks Digest + the
next_check reminder loop.

Both are deterministic (the /operations/digest and /operations/quick/tasks
endpoints never call an LLM). Every job is guarded so a failure can never
cancel polling. The digest is delivered to every known group chat (the bot
remembers groups it was added to / interacted with via the state store);
when no group is known yet, the digest is skipped silently (no junk).
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone

from telegram.ext import Application

from pasay_bot.api_client import PasayApiError
from pasay_bot.config import Settings
from pasay_bot.render import cards
from pasay_bot.state.store import StateStore

logger = logging.getLogger(__name__)

DIGEST_HOUR = 8
DIGEST_MINUTE = 0
NEXT_CHECK_INTERVAL_SECONDS = 600


def _digest_locale() -> str:
    return "bi"  # group digest is always bilingual


def _bind_owner_for_read(api, settings) -> bool:
    """Bind a verified HUMAN telegram id so the backend can resolve a subject.

    Background proactive jobs (digest / next_check) call authenticated reads
    that the backend resolves against a HUMAN subject via ``X-Telegram-User-Id``
    (BOT-BACKEND-AUTH-001). An unbound call is rejected 401, so these jobs bind
    the configured verified Owner telegram id for the read, then always clear
    it afterward so no identity leaks into another async task. Returns False
    (caller should skip the read) when no verified id is configured.
    """
    job_tg = getattr(settings, "pasay_job_owner_telegram_id", 0) or 0
    if job_tg <= 0:
        return False
    try:
        api.bind_telegram_user(job_tg)
        return True
    except (TypeError, ValueError):
        return False


def _clear_owner_for_read(api) -> None:
    try:
        api.clear_telegram_user()
    except Exception:  # noqa: BLE001 - best-effort cleanup, never fatal
        pass


async def _send_digest(app: Application, api, store: StateStore, settings: Settings) -> None:
    if not _bind_owner_for_read(api, settings):
        return
    try:
        try:
            data = await api.get_digest()
        except PasayApiError as exc:
            logger.warning("digest fetch failed: %s", exc)
            return
        if not (data.get("pending") or data.get("in_progress")):
            return  # no active tasks -> nothing to push today
        text = cards.active_tasks_digest_card(data, _digest_locale())
        groups = store.list_known_groups()
        if not groups:
            return
        for group in groups:
            try:
                await app.bot.send_message(
                    group["chat_id"], text, parse_mode="HTML"
                )
            except Exception as exc:  # noqa: BLE001 - one bad group never blocks the rest
                logger.warning("digest send to %s failed: %s", group["chat_id"], exc)
    finally:
        _clear_owner_for_read(api)


async def _send_next_check_reminders(app: Application, api, store: StateStore, settings: Settings) -> None:
    """Active tasks whose next_check_at is due get a bilingual reminder card.
    The backend remains the source of truth; this job only reads."""
    if not _bind_owner_for_read(api, settings):
        return
    try:
        try:
            tasks = await api.get_quick_tasks()
        except PasayApiError as exc:
            logger.debug("next_check scan failed: %s", exc)
            return
        now = datetime.now(timezone.utc)
        due = [
            t for t in tasks
            if t.get("next_check_at")
            and datetime.fromisoformat(t["next_check_at"]).astimezone(timezone.utc) <= now
        ]
        if not due:
            return
        groups = store.list_known_groups()
        if not groups:
            return
        for task in due:
            text = cards.task_event_card("updated", task, _digest_locale())
            for group in groups:
                try:
                    await app.bot.send_message(group["chat_id"], text, parse_mode="HTML")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("next_check reminder to %s failed: %s", group["chat_id"], exc)
    finally:
        _clear_owner_for_read(api)


def register_jobs(
    app: Application,
    api,
    store: StateStore,
    settings: Settings,
) -> None:
    """Register the V2 reminder jobs. Called defensively from main.py (a
    missing module must never prevent the bot from starting)."""
    job_queue = getattr(app, "job_queue", None)
    if job_queue is None:
        logger.warning("no JobQueue available; digest/next_check jobs disabled")
        return

    async def _digest_job(context):
        await _send_digest(app, api, store, settings)

    async def _next_check_job(context):
        await _send_next_check_reminders(app, api, store, settings)

    # Daily digest (first run tomorrow morning; repeat every 24h).
    job_queue.run_daily(
        _digest_job,
        time=time(DIGEST_HOUR, DIGEST_MINUTE, 0),
        name="v2_daily_digest",
    )
    # next_check reminder scan every 10 minutes.
    job_queue.run_repeating(
        _next_check_job,
        interval=NEXT_CHECK_INTERVAL_SECONDS,
        first=30,
        name="v2_next_check",
    )
