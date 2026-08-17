"""PASAY-V2-FOUNDATION-001 reminder jobs: daily Active Tasks Digest + the
next_check reminder loop.

JOB-SERVICE-AUTH-002: both jobs authenticate as a real SYSTEM principal via a
dedicated SYSTEM-keyed API client (``pasay_job_api_key``) — they never bind a
HUMAN Owner's Telegram id and never send ``X-Telegram-User-Id``. The backend
resolves the SYSTEM credential as the ``scheduler`` SYSTEM principal on the
read-only endpoints (see app/api/deps.py::get_operations_reader). If no SYSTEM
credential is configured the jobs are disabled (fail closed): there is no
fallback to any other identity.

Both endpoints are deterministic (the /operations/digest and
/operations/quick/tasks endpoints never call an LLM). Every job is guarded so
a failure can never cancel polling. The digest is delivered to every known
group chat (the bot remembers groups it was added to / interacted with via the
state store); when no group is known yet, the digest is skipped silently (no
junk).
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone

from telegram.ext import Application

from pasay_bot.api_client import PasayApiClient, PasayApiError
from pasay_bot.config import Settings
from pasay_bot.render import cards
from pasay_bot.roles import ROLE_LOCALES, Role, telegram_id_for_role
from pasay_bot.state.store import StateStore

logger = logging.getLogger(__name__)

DIGEST_HOUR = 8
DIGEST_MINUTE = 0
NEXT_CHECK_INTERVAL_SECONDS = 600


def _digest_locale() -> str:
    return "bi"  # group digest is always bilingual


def _digest_recipients() -> list[tuple[int, str]]:
    """Per-user private-chat digest recipients with the role's single-language
    locale (DAILY-DIGEST-TRUTH-CLEANUP-006 PHASE 8/9/10):
    Owner -> zh, Secretary -> en. chat_id of a private chat == the user id."""
    recipients: list[tuple[int, str]] = []
    for role in (Role.OWNER, Role.SECRETARY):
        chat_id = telegram_id_for_role(role)
        if chat_id is not None:
            recipients.append((int(chat_id), ROLE_LOCALES.get(role, "zh")))
    return recipients


async def _send_digest(app: Application, api: PasayApiClient, store: StateStore) -> None:
    try:
        data = await api.get_digest()
    except PasayApiError as exc:
        logger.warning("digest fetch failed: %s", exc)
        return
    if not (data.get("pending") or data.get("in_progress")
            or data.get("act_now") or data.get("upcoming")
            or data.get("done_today")):
        return  # no active tasks -> nothing to push today
    # Per-user private chats: Owner Chinese single-language, Secretary English
    # single-language.
    for chat_id, locale in _digest_recipients():
        text = cards.active_tasks_digest_card(data, locale)
        try:
            await app.bot.send_message(chat_id, text, parse_mode="HTML")
        except Exception as exc:  # noqa: BLE001 - one bad recipient never blocks
            logger.warning("digest send to %s failed: %s", chat_id, exc)
    # Shared group broadcast stays bilingual (mixed audience).
    groups = store.list_known_groups()
    if not groups:
        return
    text = cards.active_tasks_digest_card(data, _digest_locale())
    for group in groups:
        try:
            await app.bot.send_message(
                group["chat_id"], text, parse_mode="HTML"
            )
        except Exception as exc:  # noqa: BLE001 - one bad group never blocks the rest
            logger.warning("digest send to %s failed: %s", group["chat_id"], exc)


async def _send_next_check_reminders(
    app: Application, api: PasayApiClient, store: StateStore
) -> None:
    """Active tasks whose next_check_at is due get a bilingual reminder card.
    The backend remains the source of truth; this job only reads.

    CONVERGENCE-003 §1.3/§1.4: the job may scan as often as the scheduler
    wants, but the SAME task + group chat + PH local date is pushed at most
    once per Philippines natural day — the SQLite ``daily_marks`` table makes
    the dedupe persistent and restart-safe."""
    try:
        tasks = await api.get_quick_tasks()
    except PasayApiError as exc:
        logger.debug("next_check scan failed: %s", exc)
        return
    from pasay_bot.state.store import ph_local_date

    local_date = ph_local_date()
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
            # Same-day dedupe per (task, group chat): one push per PH day.
            mark_key = f"next_check:{task.get('id')}:{group['chat_id']}:{local_date}"
            if not store.mark_daily(mark_key):
                continue
            try:
                await app.bot.send_message(group["chat_id"], text, parse_mode="HTML")
            except Exception as exc:  # noqa: BLE001
                logger.warning("next_check reminder to %s failed: %s", group["chat_id"], exc)


def _build_job_api(settings: Settings) -> PasayApiClient | None:
    """One SYSTEM-keyed client for the background jobs, or None (fail closed).

    The client is never bound to a Telegram user id: every request carries
    only the SYSTEM credential, so the backend sees the SYSTEM principal.
    """
    if not getattr(settings, "pasay_job_api_key", ""):
        return None
    return PasayApiClient(
        settings.pasay_api_base,
        settings.pasay_job_api_key,
        timeout=settings.pasay_http_timeout_seconds,
    )


def register_jobs(
    app: Application,
    api,
    store: StateStore,
    settings: Settings,
    job_api: PasayApiClient | None = None,
) -> None:
    """Register the V2 reminder jobs. Called defensively from main.py (a
    missing module must never prevent the bot from starting).

    The jobs run ONLY with a dedicated SYSTEM credential (``job_api`` or
    ``settings.pasay_job_api_key``). Without one the jobs are disabled — they
    must never reuse the human-bound interactive client or fall back to the
    Owner's identity (JOB-SERVICE-AUTH-002).
    """
    job_queue = getattr(app, "job_queue", None)
    if job_queue is None:
        logger.warning("no JobQueue available; digest/next_check jobs disabled")
        return

    job_client = job_api or _build_job_api(settings)
    if job_client is None:
        logger.warning(
            "PASSAY_JOB_API_KEY is not configured; "
            "v2_daily_digest / v2_next_check jobs disabled (fail closed)"
        )
        return

    async def _digest_job(context):
        await _send_digest(app, job_client, store)

    async def _next_check_job(context):
        await _send_next_check_reminders(app, job_client, store)

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
