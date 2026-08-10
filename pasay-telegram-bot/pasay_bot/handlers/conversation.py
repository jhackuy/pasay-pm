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
    payment_method_keyboard,
)
from pasay_bot.render import cards
from pasay_bot.render import html as H
from pasay_bot.render.i18n import t
from pasay_bot.roles import (
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
    else:
        from pasay_bot.handlers import nl_bridge

        await nl_bridge.handle_nl(update, context)


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
