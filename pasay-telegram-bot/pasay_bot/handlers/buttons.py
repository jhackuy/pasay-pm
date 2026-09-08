"""Deterministic fixed bottom-menu button routing (never NL/LLM).

The persistent Reply Keyboard is a set of UI commands, not natural language.
``handle_fixed_menu_button`` exact-matches a route produced by
``keyboards.fixed_menu_route_for`` and dispatches straight to the
deterministic Quick View page builders (V2) or the legacy pages for old
aliases. It is called from
``conversation.handle_message`` BEFORE any NL/NLU/LLM path can run.
"""
from __future__ import annotations

import logging
import time

from telegram import Update
from telegram.ext import ContextTypes

from pasay_bot.handlers import commands as pages
from pasay_bot.handlers.edit_utils import edit_message_text_idempotent
from pasay_bot.keyboards import error_keyboard
from pasay_bot.render import html as H
from pasay_bot.render.i18n import t
from pasay_bot.roles import (
    has_read_permission,
    locale_for_chat,
    role_for_telegram_id,
)

# Issue #119 P0 WARM-PATH TRACE: dedicated logger so the structured
# ``pasay_menu_button`` line surfaces under a stable namespace and is
# filterable in Cloudflare Container log UI.
_handler_logger = logging.getLogger("pasay_bot.handlers.buttons")

logger = logging.getLogger(__name__)

HTML = "HTML"

# Reply-keyboard routes that load backend data (may take >1s): they get a
# durable "processing" status message that the page render then edits in
# place, so the user always sees 处理中 -> result (never "did my tap work?").
#
# IMPORTANT Telegram constraint (live UX bug OWNER-UX-FAILURE-LIVE-TRACE-001):
# a message sent WITH a non-inline ReplyKeyboardMarkup can NOT be edited later
# (editMessageText -> 400 "Message can't be edited"). The status message is
# therefore sent WITHOUT reply_markup; the persistent keyboard remains pinned
# client-side because it was previously sent with is_persistent=True.
_SLOW_ROUTES = frozenset(
    {"finance", "overdue", "pending"}
)

# PASAY-V2-FOUNDATION-001 / 006A: Home plus the business entry buttons are
# fast deterministic first-level routes — they render directly and never show
# the processing stub.
_QUICK_ROUTES = frozenset({"home", "properties", "tasks", "rent", "expense", "archive"})
_INLINE_MENU_REFRESH_ROUTES = frozenset({"home", "properties", "tasks", "rent", "expense", "archive"})


def _track_phases(
    context, route: str, *,
    callback_ack_ms: float, backend_fetch_ms: float,
    render_ms: float, telegram_edit_ms: float,
    business_completed_ms: float, total_ms: float,
    outcome: str = "ok", detail: str = "",
) -> None:
    """Issue #119 P0 latency — emit the per-phase breakdown for a frozen
    bottom-menu tap (callback_ack / backend_fetch / render / telegram_edit /
    business_completed / total), matching the shape the 007A callback path
    already emits for inline callbacks."""
    tracker = context.bot_data.get("latency")
    if tracker is None:
        return
    try:
        tracker.record_phases(
            "menu_button", route,
            callback_ack_ms=callback_ack_ms,
            backend_fetch_ms=backend_fetch_ms,
            render_ms=render_ms,
            telegram_edit_ms=telegram_edit_ms,
            business_completed_ms=business_completed_ms,
            total_ms=total_ms,
            outcome=outcome, detail=detail,
        )
    except Exception:  # noqa: BLE001 - instrumentation must never break UX
        logger.debug("latency menu_button phase record failed", exc_info=True)
    # Issue #119 P0 WARM-PATH TRACE: structured single-line record so
    # operator grep can join the per-route breakdown with the Worker
    # ``pasay_worker_latency`` and Container ``pasay_ingest_latency``
    # records on the SAME ``trace_id``. The trace_id flows from the
    # Worker via the Container's ``app.services.telegram_webhook``
    # ContextVar (``current_trace_id``) and is empty when the bot runs
    # outside the webhook path (legacy tests).
    try:
        from app.services.telegram_webhook import current_trace_id  # type: ignore
        trace_id = current_trace_id() or ""
    except Exception:  # noqa: BLE001 - observability never breaks the handler
        trace_id = ""
    try:
        _handler_logger.info(
            "pasay_menu_button trace_id=%s route=%s outcome=%s "
            "total_ms=%.3f callback_ack_ms=%.3f backend_fetch_ms=%.3f "
            "render_ms=%.3f telegram_edit_ms=%.3f business_completed_ms=%.3f "
            "detail=%s",
            trace_id, route, outcome, total_ms, callback_ack_ms,
            backend_fetch_ms, render_ms, telegram_edit_ms,
            business_completed_ms, detail[:200],
        )
    except Exception:  # noqa: BLE001
        pass


def _track(context, route: str, elapsed_ms: float, outcome: str = "ok", detail: str = "") -> None:
    """Backwards-compatible single-elapsed tracker for menu taps that did not
    run through :func:`handle_fixed_menu_button`'s full phase profile (kept so
    older callers keep working; new code should call :func:`_track_phases`)."""
    tracker = context.bot_data.get("latency")
    if tracker is not None:
        try:
            tracker.record("menu_button", route, elapsed_ms, outcome=outcome, detail=detail)
        except Exception:  # noqa: BLE001 - instrumentation must never break UX
            logger.debug("latency record failed", exc_info=True)


async def handle_fixed_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE, route: str):
    """Deterministic dispatch for an exact-matched fixed menu button.

    Issue #119 P0 latency: a frozen-menu tap is profiled with the SAME phase
    shape as inline callbacks (callback_ack / backend_fetch / render /
    telegram_edit / business_completed / total). The probe is bound on the
    ContextVar so :class:`pasay_bot.api_client.PasayApiClient._request`
    accumulates the backend round-trip time into the same sample.
    """
    # Bind a PhaseProbe BEFORE we await anything, so the V1 backend fetches
    # below (and the Telegram send) get attributable ms. The bound probe
    # stays scoped to this single async task thanks to ContextVar isolation.
    from pasay_bot.state.latency import PhaseProbe, bind_phase

    probe = PhaseProbe()
    bind_phase(probe)
    started = time.monotonic()
    user = update.effective_user
    role = role_for_telegram_id(user.id if user else None)
    locale = locale_for_chat(
        update.effective_chat.type if update.effective_chat else None, role
    )
    chat_id = update.effective_chat.id if update.effective_chat else (user.id if user else None)
    if chat_id is None:
        total_elapsed_ms = (time.monotonic() - started) * 1000
        probe.business_completed_ms = total_elapsed_ms
        _track_phases(
            context, route,
            callback_ack_ms=probe.callback_ack_ms,
            backend_fetch_ms=probe.backend_fetch_ms,
            render_ms=probe.render_ms,
            telegram_edit_ms=probe.telegram_edit_ms,
            business_completed_ms=probe.business_completed_ms,
            total_ms=total_elapsed_ms,
            outcome="no_chat",
        )
        bind_phase(None)
        return

    print(f"[TRACE] button route={route} role={role.value if role else None} locale={locale} chat_id={chat_id} "
          f"user_id={update.effective_user.id if update.effective_user else None}", flush=True)

    if not has_read_permission(role):
        await context.bot.send_message(
            chat_id,
            H.escape(t("common.no_permission", locale)),
            parse_mode=HTML,
        )
        # The Telegram send_message above IS the ACK for a bottom-menu tap;
        # mark the ack moment before we record phases.
        probe.mark_ack()
        total_elapsed_ms = (time.monotonic() - started) * 1000
        probe.business_completed_ms = total_elapsed_ms
        _track_phases(
            context, route,
            callback_ack_ms=probe.callback_ack_ms,
            backend_fetch_ms=probe.backend_fetch_ms,
            render_ms=probe.render_ms,
            telegram_edit_ms=probe.telegram_edit_ms,
            business_completed_ms=probe.business_completed_ms,
            total_ms=total_elapsed_ms,
            outcome="no_permission",
        )
        bind_phase(None)
        return

    # Telegram persistent keyboards are sticky on the client. A deployed menu
    # change only replaces an already pinned old keyboard when we send a fresh
    # ReplyKeyboardMarkup in a later ordinary interaction.
    if (
        update.effective_chat is not None
        and route in _INLINE_MENU_REFRESH_ROUTES
        and not pages._is_menu_initialized(context, chat_id)
    ):
        await pages._send_persistent_menu(
            context,
            chat_id,
            role,
            locale,
            chat_type=update.effective_chat.type,
        )

    status = None
    outcome = "ok"
    detail = ""
    try:
        if route in _SLOW_ROUTES:
            status = await context.bot.send_message(
                chat_id,
                H.escape(t("common.working", locale)),
                parse_mode=HTML,
            )
            message_id = status.message_id
        else:
            message_id = None
        if route in _QUICK_ROUTES:
            # Fast deterministic Quick View: single reply, no stub message.
            if route == "home":
                await pages.show_home(context, chat_id, role, locale)
            elif route == "properties":
                await pages.show_quick_properties(context, chat_id, role, locale)
            elif route == "tasks":
                await pages.show_quick_tasks(context, chat_id, role, locale)
            elif route == "rent":
                await pages.show_quick_rent(context, chat_id, role, locale)
            elif route == "expense":
                await pages.show_quick_expense(context, chat_id, role, locale)
            else:  # archive
                await pages.show_archive_launcher(context, chat_id, role, locale)
        elif route == "home":
            await pages.show_home(context, chat_id, role, locale, message_id=message_id)
        elif route == "finance":
            await pages.show_finance(context, chat_id, locale, message_id=message_id)
        elif route == "overdue":
            await pages.show_overdue(
                context, chat_id, locale, page=1, message_id=message_id
            )
        elif route == "pending":
            await pages.show_todo(context, chat_id, role, locale, message_id=message_id)
        elif route == "tenants":
            await context.bot.send_message(
                chat_id,
                H.escape(t("menu.tenants_hint", locale)),
                parse_mode=HTML,
                reply_markup=pages._menu_reply_keyboard(
                    update.effective_chat.type if update.effective_chat else None,
                    role,
                ),
            )
        elif route == "maintenance":
            await context.bot.send_message(
                chat_id,
                H.escape(t("menu.maintenance_hint", locale)),
                parse_mode=HTML,
                reply_markup=pages._menu_reply_keyboard(
                    update.effective_chat.type if update.effective_chat else None,
                    role,
                ),
            )
        else:
            await context.bot.send_message(
                chat_id,
                H.escape(t("common.unknown", locale)),
                parse_mode=HTML,
                reply_markup=pages._menu_reply_keyboard(
                    update.effective_chat.type if update.effective_chat else None,
                    role,
                ),
            )
        # The PhaseProbe is still bound here; PasayApiClient kept adding
        # backend_fetch_ms while show_* did its work, and _render() tracked
        # render_ms + telegram_edit_ms. Close the business-completed
        # moment and the total-elapsed moment from the SAME monotonic
        # reading so a test asserting ``business_completed <= total``
        # never races a 1-microsecond off-by-one (Issue #119 P0).
        total_elapsed_ms = (time.monotonic() - started) * 1000
        probe.business_completed_ms = total_elapsed_ms
        _track_phases(
            context, route,
            callback_ack_ms=probe.callback_ack_ms,
            backend_fetch_ms=probe.backend_fetch_ms,
            render_ms=probe.render_ms,
            telegram_edit_ms=probe.telegram_edit_ms,
            business_completed_ms=probe.business_completed_ms,
            total_ms=total_elapsed_ms,
            outcome=outcome, detail=detail,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed with user feedback
        logger.exception("fixed menu button route %s failed", route)
        outcome, detail = "error", str(exc)
        total_elapsed_ms = (time.monotonic() - started) * 1000
        probe.business_completed_ms = total_elapsed_ms
        _track_phases(
            context, route,
            callback_ack_ms=probe.callback_ack_ms,
            backend_fetch_ms=probe.backend_fetch_ms,
            render_ms=probe.render_ms,
            telegram_edit_ms=probe.telegram_edit_ms,
            business_completed_ms=probe.business_completed_ms,
            total_ms=total_elapsed_ms,
            outcome=outcome, detail=detail,
        )
        try:
            if status is not None:
                # Mutate the processing status into the error state (no junk);
                # the status message carries no reply keyboard so it is
                # editable. If it was already deleted, fall back to sending.
                try:
                    await edit_message_text_idempotent(
                        context.bot,
                        chat_id=chat_id,
                        message_id=status.message_id,
                        text=H.escape(t("common.unexpected", locale)),
                        parse_mode=HTML,
                        reply_markup=error_keyboard("home", locale),
                    )
                except Exception:  # noqa: BLE001 - fallback must never lose feedback
                    await context.bot.send_message(
                        chat_id,
                        H.escape(t("common.unexpected", locale)),
                        parse_mode=HTML,
                        reply_markup=pages._menu_reply_keyboard(
                            update.effective_chat.type if update.effective_chat else None,
                            role,
                        ),
                    )
            else:
                await context.bot.send_message(
                    chat_id,
                    H.escape(t("common.unexpected", locale)),
                    parse_mode=HTML,
                    reply_markup=pages._menu_reply_keyboard(
                        update.effective_chat.type if update.effective_chat else None,
                        role,
                    ),
                )
        except Exception:  # noqa: BLE001
            logger.exception("fixed menu button fallback message failed")
    finally:
        # Issue #119 P0 latency: always release the PhaseProbe binding on
        # this async task so a subsequent sequential handler does not see
        # our probe and attribute its own backend calls to the wrong route.
        try:
            bind_phase(None)
        except Exception:  # noqa: BLE001 - cleanup never blocks UX
            logger.debug("bind_phase(None) cleanup failed", exc_info=True)
