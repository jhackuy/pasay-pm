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
    {"home", "finance", "overdue", "pending", "more"}
)

# PASAY-V2-FOUNDATION-001: the four fixed buttons are fast deterministic
# Quick Views (single backend read, target < 1s) — they render directly and
# never show the processing stub.
_QUICK_ROUTES = frozenset({"properties", "tasks", "rent", "expense"})


def _track(context, route: str, elapsed_ms: float, outcome: str = "ok", detail: str = "") -> None:
    tracker = context.bot_data.get("latency")
    if tracker is not None:
        try:
            tracker.record("menu_button", route, elapsed_ms, outcome=outcome, detail=detail)
        except Exception:  # noqa: BLE001 - instrumentation must never break UX
            logger.debug("latency record failed", exc_info=True)


async def handle_fixed_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE, route: str):
    """Deterministic dispatch for an exact-matched fixed menu button."""
    started = time.monotonic()
    user = update.effective_user
    role = role_for_telegram_id(user.id if user else None)
    locale = locale_for_chat(
        update.effective_chat.type if update.effective_chat else None, role
    )
    chat_id = update.effective_chat.id if update.effective_chat else (user.id if user else None)
    if chat_id is None:
        _track(context, route, (time.monotonic() - started) * 1000, outcome="no_chat")
        return

    print(f"[TRACE] button route={route} role={role.value if role else None} locale={locale} chat_id={chat_id} "
          f"user_id={update.effective_user.id if update.effective_user else None}", flush=True)

    if not has_read_permission(role):
        await context.bot.send_message(
            chat_id,
            H.escape(t("common.no_permission", locale)),
            parse_mode=HTML,
        )
        _track(context, route, (time.monotonic() - started) * 1000, outcome="no_permission")
        return

    status = None
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
            if route == "properties":
                await pages.show_quick_properties(context, chat_id, role, locale)
            elif route == "tasks":
                await pages.show_quick_tasks(context, chat_id, role, locale)
            elif route == "rent":
                await pages.show_quick_rent(context, chat_id, role, locale)
            else:  # expense
                await pages.show_quick_expense(context, chat_id, role, locale)
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
        elif route == "more":
            # CONVERGENCE-003 §2.1: "☰ 更多" (legacy) opens the ONE Home.
            await pages.show_home(
                context, chat_id, role, locale, message_id=message_id
            )
        elif route == "tenants":
            await context.bot.send_message(
                chat_id,
                H.escape(t("menu.tenants_hint", locale)),
                parse_mode=HTML,
                reply_markup=pages.reply_keyboard(role),
            )
        elif route == "maintenance":
            await context.bot.send_message(
                chat_id,
                H.escape(t("menu.maintenance_hint", locale)),
                parse_mode=HTML,
                reply_markup=pages.reply_keyboard(role),
            )
        else:
            await context.bot.send_message(
                chat_id,
                H.escape(t("common.unknown", locale)),
                parse_mode=HTML,
                reply_markup=pages.reply_keyboard(role),
            )
        _track(context, route, (time.monotonic() - started) * 1000)
    except Exception as exc:  # noqa: BLE001 - fail closed with user feedback
        logger.exception("fixed menu button route %s failed", route)
        _track(context, route, (time.monotonic() - started) * 1000, outcome="error", detail=str(exc))
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
                        reply_markup=pages.reply_keyboard(role),
                    )
            else:
                await context.bot.send_message(
                    chat_id,
                    H.escape(t("common.unexpected", locale)),
                    parse_mode=HTML,
                    reply_markup=pages.reply_keyboard(role),
                )
        except Exception:  # noqa: BLE001
            logger.exception("fixed menu button fallback message failed")
