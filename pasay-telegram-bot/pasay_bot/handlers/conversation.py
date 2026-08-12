"""Conversation state machine for rent entry.

V1.1 UX: new entries jump straight to the final confirmation card with smart
defaults; the free-text states below are only reached via [✏️修改] (amount /
date) or by in-flight legacy conversations. Legacy chain:
rent_amount -> rent_date -> rent_method -> rent_confirm (kept for old cards).

State lives in the SQLite store (design §7) with a 15-minute TTL and a
(chat_id, user_id) composite key so group chats don't cross wires.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from telegram import Update
from telegram.ext import ContextTypes

from pasay_bot.handlers.edit_utils import edit_message_text_idempotent
from pasay_bot.keyboards import (
    confirm_rent_keyboard,
    home_keyboard,
    new_nonce,
    now_ts,
    ops_back_keyboard,
    payment_method_keyboard,
)
from pasay_bot.render import cards
from pasay_bot.render import html as H
from pasay_bot.render.i18n import t
from pasay_bot.roles import (
    PERMISSION_OPERATIONS,
    PERMISSION_RENT_CONFIRM,
    has_permission,
    locale_for,
    role_for_telegram_id,
)

HTML = "HTML"
MAX_AMOUNT = Decimal("999999999999.99")  # Numeric(14,2)

_WRITE_STATES = (
    "rent_amount", "rent_date", "rent_method", "rent_confirm",
    "rent_edit", "rent_edit_amount", "rent_edit_date", "rent_edit_method",
)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from pasay_bot.handlers.commands import _bind_identity
    _bind_identity(update, context)
    user = update.effective_user
    chat_id = update.effective_chat.id
    user_id = user.id if user else None
    role = role_for_telegram_id(user_id)
    locale = locale_for(role)
    store = context.bot_data["store"]
    text = (update.effective_message.text or "").strip()

    if text.lower() in ("取消", "cancel", "quit", "exit"):
        conv = store.get_conversation(chat_id, user_id)
        if conv and conv["state"] in _WRITE_STATES:
            store.delete_conversation(chat_id, user_id)
            await context.bot.send_message(
                chat_id, H.escape(t("rent.cancelled", locale)),
                parse_mode=HTML, reply_markup=home_keyboard(locale),
            )
        return

    conv = store.get_conversation(chat_id, user_id)
    if conv is None:
        from pasay_bot.handlers import nl_bridge

        await nl_bridge.handle_nl(update, context)
        return

    state = conv["state"]
    payload = dict(conv["payload"] or {})

    if state in ("rent_amount", "rent_edit_amount"):
        await _enter_amount(update, context, payload, locale, edit_mode=state == "rent_edit_amount")
    elif state in ("rent_date", "rent_edit_date"):
        await _enter_date(update, context, payload, locale, edit_mode=state == "rent_edit_date")
    elif state == "ops_snooze_custom":
        await _enter_ops_snooze_custom(update, context, payload, locale)
    elif state == "copilot_ask":
        await _handle_copilot_ask_question(update, context, locale)
    else:
        from pasay_bot.handlers import nl_bridge

        await nl_bridge.handle_nl(update, context)


async def _handle_copilot_ask_question(update, context, locale):
    """[问运营助手] Q&A: user typed a question -> /copilot/ask (friendly fallback)."""
    store = context.bot_data["store"]
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    question = (update.effective_message.text or "").strip()
    store.delete_conversation(chat_id, user_id)
    if not question:
        await context.bot.send_message(
            chat_id, H.escape(t("copilot.ask_empty", locale)),
            parse_mode=HTML, reply_markup=home_keyboard(locale),
        )
        return
    from pasay_bot.handlers import commands

    await commands.ask_copilot(context, chat_id, locale, question)


async def _enter_amount(update, context, payload, locale, edit_mode: bool):
    store = context.bot_data["store"]
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    text = (update.effective_message.text or "").strip()
    if text.lower() in ("默认", "default"):
        amount = Decimal(payload.get("monthly_rent") or "0")
    else:
        try:
            amount = Decimal(text)
        except InvalidOperation:
            await context.bot.send_message(
                chat_id, H.escape(t("rent.amount_invalid", locale)),
                parse_mode=HTML, reply_markup=home_keyboard(locale),
            )
            return
    if amount <= 0 or amount > MAX_AMOUNT or _has_too_many_decimals(amount):
        await context.bot.send_message(
            chat_id, H.escape(t("rent.amount_invalid", locale)),
            parse_mode=HTML, reply_markup=home_keyboard(locale),
        )
        return
    amount = amount.quantize(Decimal("0.01"))
    payload["amount"] = str(amount)
    if edit_mode:
        await _return_to_confirm(update, context, payload, locale)
        return
    store.save_conversation(chat_id, user_id, "rent_date", payload)
    default = H.format_date(date.today())
    await context.bot.send_message(
        chat_id,
        t("rent.ask_date", locale, default=default),
        parse_mode=HTML,
    )


async def _enter_date(update, context, payload, locale, edit_mode: bool):
    store = context.bot_data["store"]
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    text = (update.effective_message.text or "").strip()
    if text.lower() in ("默认", "今天", "today", "default"):
        received = date.today()
    else:
        try:
            received = datetime.strptime(text, "%Y-%m-%d").date()
        except ValueError:
            await context.bot.send_message(
                chat_id, H.escape(t("rent.date_invalid", locale)),
                parse_mode=HTML, reply_markup=home_keyboard(locale),
            )
            return
    payload["received_date"] = received.isoformat()
    if edit_mode:
        await _return_to_confirm(update, context, payload, locale)
        return
    store.save_conversation(chat_id, user_id, "rent_method", payload)
    await context.bot.send_message(
        chat_id,
        H.escape(t("rent.ask_method", locale)),
        parse_mode=HTML,
        reply_markup=payment_method_keyboard(locale),
    )


async def _return_to_confirm(update, context, payload, locale):
    """Re-render the final confirmation card (edit-first on the same message)."""
    store = context.bot_data["store"]
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    nonce = new_nonce()
    ts = now_ts()
    store.save_conversation(chat_id, user_id, "rent_confirm", payload, nonce=nonce)
    role = role_for_telegram_id(user_id)
    can_confirm = has_permission(role, PERMISSION_RENT_CONFIRM)
    text = cards.rent_confirm_card(
        payload["property_name"],
        payload["unit_number"],
        payload["amount"],
        payload["received_date"],
        payload["method"],
        locale,
    )
    try:
        message_id = int(payload.get("confirm_message_id") or 0)
    except (TypeError, ValueError):
        message_id = 0
    if message_id:
        await edit_message_text_idempotent(
            context.bot,
            chat_id=chat_id,
            message_id=message_id,
            text=H.truncate(text),
            parse_mode=HTML,
            reply_markup=confirm_rent_keyboard(nonce, ts, can_confirm, locale),
        )
    else:
        await context.bot.send_message(
            chat_id, H.truncate(text), parse_mode=HTML,
            reply_markup=confirm_rent_keyboard(nonce, ts, can_confirm, locale),
        )


def _has_too_many_decimals(value: Decimal) -> bool:
    return value != value.quantize(Decimal("0.01"))



async def _enter_ops_snooze_custom(update, context, payload, locale: str):
    """V1.2 custom snooze: free-text YYYY-MM-DD [HH:MM] (or a preset word)."""
    from pasay_bot.api_client import PasayApiError

    store = context.bot_data["store"]
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    api = context.bot_data["api_client"]
    text = (update.effective_message.text or "").strip().lower()
    message_id = payload.get("message_id")
    task_id = int(payload.get("task_id") or 0)

    if text in ("取消", "cancel", "quit", "exit"):
        store.delete_conversation(chat_id, user_id)
        if message_id:
            await edit_message_text_idempotent(
                context.bot, chat_id=chat_id, message_id=message_id,
                text=H.escape(t("rent.cancelled", locale)), parse_mode=HTML,
                reply_markup=ops_back_keyboard(locale),
            )
        return

    until = _parse_snooze_input(text)
    if until is None:
        await context.bot.send_message(
            chat_id, H.escape(t("ops.snooze_invalid", locale)),
            parse_mode=HTML, reply_markup=home_keyboard(locale),
        )
        return

    try:
        task = await api.snooze_operational_task(task_id, until=until.isoformat())
    except PasayApiError as exc:
        await context.bot.send_message(
            chat_id, f"⚠️ {H.escape(exc.detail)}",
            parse_mode=HTML, reply_markup=ops_back_keyboard(locale),
        )
        return
    store.delete_conversation(chat_id, user_id)
    title = H.escape(task.title or f"#{task.id}")
    until_str = str(task.snoozed_until or "")[:16].replace("T", " ")
    text_out = t("ops.snoozed_card", locale, title=title, until=H.escape(until_str))
    if message_id:
        await edit_message_text_idempotent(
            context.bot, chat_id=chat_id, message_id=message_id,
            text=H.truncate(text_out), parse_mode=HTML,
            reply_markup=ops_back_keyboard(locale),
        )
    else:
        await context.bot.send_message(chat_id, text_out, parse_mode=HTML)


def _parse_snooze_input(text: str):
    """Accept YYYY-MM-DD [HH:MM] or preset words (1h/今天下午/明天上午/3天/3天后)."""
    from datetime import datetime, timedelta

    now = datetime.now()
    lowered = text.strip().lower()
    if lowered in ("1h", "1小时", "一小时"):
        return now + timedelta(hours=1)
    if lowered in ("今天下午", "this afternoon", "afternoon"):
        target = now.replace(hour=17, minute=0, second=0, microsecond=0)
        return target if target > now else target + timedelta(days=1)
    if lowered in ("明天上午", "tomorrow morning", "tomorrow"):
        return (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    if lowered in ("3天", "3天后", "3 days", "3d"):
        return now + timedelta(days=3)
    try:
        return datetime.fromisoformat(text.strip())
    except ValueError:
        return None
