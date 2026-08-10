"""Deterministic keyword/command parsing (no LLM this phase).

The Hermes NLU adapter is intentionally NOT wired up in this phase. Recognized
Chinese/English phrases route to the same deterministic pages as the buttons;
anything else gets a "use the buttons" reply.
"""
from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from pasay_bot.handlers import commands as pages
from pasay_bot.keyboards import menu_keyboard
from pasay_bot.render import html as H
from pasay_bot.render.i18n import t
from pasay_bot.roles import (
    has_permission,
    has_read_permission,
    locale_for,
    role_for_telegram_id,
)

HTML = "HTML"

_ROUTES = [
    (("房源", "property", "properties"), "properties"),
    (("财务", "finance", "summary", "收支", "报表"), "finance"),
    (("逾期", "overdue", "欠租", "overdue rent"), "overdue"),
    (("收租", "rent", "登记"), "rent"),
    (("菜单", "menu", "主菜单", "home", "start"), "menu"),
    (("帮助", "help", "帮助"), "help"),
]


def route_for_text(text: str):
    lowered = (text or "").strip().lower()
    if lowered.startswith("/"):
        return None  # real commands are handled by CommandHandler
    for keywords, route in _ROUTES:
        for kw in keywords:
            if kw in lowered:
                return route
    return None


async def handle_nl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle a free-text message that has no active conversation state."""
    user = update.effective_user
    role = role_for_telegram_id(user.id if user else None)
    locale = locale_for(role)
    chat_id = update.effective_chat.id
    text = update.effective_message.text or ""
    route = route_for_text(text)
    if route in ("properties", "finance", "overdue", "rent", "menu"):
        if not has_read_permission(role):
            await context.bot.send_message(
                chat_id,
                H.escape(t("common.no_permission", locale)),
                parse_mode=HTML,
            )
            return
    if route == "properties":
        await pages.show_properties(context, chat_id, role, locale, page=1)
    elif route == "finance":
        await pages.show_finance(context, chat_id, locale)
    elif route == "overdue":
        await pages.show_overdue(context, chat_id, locale, page=1)
    elif route == "rent":
        await pages.show_rent(context, chat_id, locale)
    elif route == "menu":
        await pages.show_menu(context, chat_id, locale)
    elif route == "help":
        await context.bot.send_message(
            chat_id,
            f"📖 <b>{H.escape(t('help.title', locale))}</b>\n\n{H.escape(t('help.text', locale))}",
            parse_mode=HTML,
        )
    else:
        await context.bot.send_message(
            chat_id,
            H.escape(t("common.unknown", locale)),
            parse_mode=HTML,
            reply_markup=menu_keyboard(locale),
        )
