"""Conversation state machine for rent entry (amount -> date -> method).

State lives in the SQLite store (design §7) with a 15-minute TTL and a
(chat_id, user_id) composite key so group chats don't cross wires.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from telegram import Update
from telegram.ext import ContextTypes

from pasay_bot.keyboards import payment_method_keyboard
from pasay_bot.render import html as H
from pasay_bot.render.i18n import t
from pasay_bot.roles import locale_for, role_for_telegram_id

HTML = "HTML"
MAX_AMOUNT = Decimal("999999999999.99")  # Numeric(14,2)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    user_id = user.id if user else None
    role = role_for_telegram_id(user_id)
    locale = locale_for(role)
    store = context.bot_data["store"]
    text = (update.effective_message.text or "").strip()

    if text.lower() in ("取消", "cancel", "quit", "exit"):
        conv = store.get_conversation(chat_id, user_id)
        if conv:
            store.delete_conversation(chat_id, user_id)
            await context.bot.send_message(chat_id, H.escape(t("rent.cancelled", locale)))
        return

    conv = store.get_conversation(chat_id, user_id)
    if conv is None:
        from pasay_bot.handlers import nl_bridge

        await nl_bridge.handle_nl(update, context)
        return

    state = conv["state"]
    payload = dict(conv["payload"] or {})

    if state == "rent_amount":
        await _enter_amount(update, context, payload, locale)
    elif state == "rent_date":
        await _enter_date(update, context, payload, locale)
    else:
        from pasay_bot.handlers import nl_bridge

        await nl_bridge.handle_nl(update, context)


async def _enter_amount(update, context, payload, locale):
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
            await context.bot.send_message(chat_id, H.escape(t("rent.amount_invalid", locale)))
            return
    if amount <= 0 or amount > MAX_AMOUNT or _has_too_many_decimals(amount):
        await context.bot.send_message(chat_id, H.escape(t("rent.amount_invalid", locale)))
        return
    amount = amount.quantize(Decimal("0.01"))
    payload["amount"] = str(amount)
    store.save_conversation(chat_id, user_id, "rent_date", payload)
    default = H.format_date(date.today())
    await context.bot.send_message(
        chat_id,
        t("rent.ask_date", locale, default=default),
        parse_mode=HTML,
    )


async def _enter_date(update, context, payload, locale):
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
            await context.bot.send_message(chat_id, H.escape(t("rent.date_invalid", locale)))
            return
    payload["received_date"] = received.isoformat()
    store.save_conversation(chat_id, user_id, "rent_method", payload)
    await context.bot.send_message(
        chat_id,
        H.escape(t("rent.ask_method", locale)),
        parse_mode=HTML,
        reply_markup=payment_method_keyboard(locale),
    )


def _has_too_many_decimals(value: Decimal) -> bool:
    return value != value.quantize(Decimal("0.01"))
