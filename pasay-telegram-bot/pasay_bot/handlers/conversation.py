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

from telegram import Chat, Update
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
    locale_for_chat,
    role_for_telegram_id,
)

HTML = "HTML"
MAX_AMOUNT = Decimal("999999999999.99")  # Numeric(14,2)

_WRITE_STATES = (
    "rent_amount", "rent_date", "rent_method", "rent_confirm",
    "rent_edit", "rent_edit_amount", "rent_edit_date", "rent_edit_method",
    # BOT-V1-USABLE-001 P0-2: expense create flow.
    "expense_confirm", "expense_edit_amount", "expense_edit_category",
    "expense_edit_unit", "ai_expense_partial",
)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from pasay_bot.handlers.commands import _bind_identity

    # AI-OPS-FOUNDATION-001 §12: a channel_post / anonymous update has no
    # effective_user and is NOT a user action — ignore it gracefully instead
    # of crashing the handler (the archive channel delivers channel posts).
    if update.effective_user is None:
        _bind_identity(update, context)
        return
    _bind_identity(update, context)
    user = update.effective_user
    chat_id = update.effective_chat.id
    user_id = user.id if user else None
    store = context.bot_data["store"]
    text = (update.effective_message.text or "").strip()
    import time as _t_trace
    print(f"[TRACE] msg entry update_id={update.update_id} "
          f"message_id={update.effective_message.message_id} "
          f"chat_id={update.effective_chat.id if update.effective_chat else None} "
          f"user_id={update.effective_user.id if update.effective_user else None} "
          f"text={text!r} ts={_t_trace.time()}", flush=True)

    # PASAY-V2-FOUNDATION-001: remember groups so the daily digest + next_check
    # reminders have a delivery target (never spam; groups only).
    if update.effective_chat.type in ("group", "supergroup"):
        store.remember_group(chat_id, title=(update.effective_chat.title or ""))

    # Fixed bottom Reply Keyboard buttons are exact-match UI commands: they
    # are routed deterministically BEFORE any conversation-state or NL/LLM
    # processing can run (buttons are never natural language).
    from pasay_bot.handlers import buttons as button_handlers
    from pasay_bot.keyboards import fixed_menu_route_for
    route = fixed_menu_route_for(text)
    if route is not None:
        await button_handlers.handle_fixed_menu_button(update, context, route)
        return

    role = role_for_telegram_id(user_id)
    locale = locale_for_chat(
        update.effective_chat.type if update.effective_chat else None, role
    )

    if text.lower() in ("取消", "cancel", "quit", "exit"):
        conv = store.get_conversation(chat_id, user_id)
        if conv and conv["state"] in _WRITE_STATES:
            store.delete_conversation(chat_id, user_id)
            await context.bot.send_message(
                chat_id, H.escape(t("rent.cancelled", locale)),
                parse_mode=HTML, reply_markup=home_keyboard(locale),
            )
        return

    # SLICE3-UX-PERSISTENT-MENU-002: an identified user sending ANY normal
    # message auto-restores the persistent keyboard when the menu was never
    # initialized (private chat only; groups fail closed). Never asks for
    # /start and never depends on backend/dashboard data.
    if update.effective_chat.type == Chat.PRIVATE:
        from pasay_bot.handlers.commands import _send_persistent_menu

        await _send_persistent_menu(context, chat_id, role, locale)

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
    elif state == "expense_edit_amount":
        await _enter_expense_amount(update, context, payload, locale, edit_mode=True)
    elif state == "expense_edit_category":
        await _enter_expense_category(update, context, payload, locale)
    elif state == "expense_edit_unit":
        await _enter_expense_unit(update, context, payload, locale)
    elif state == "ai_expense_partial":
        await _enter_ai_expense_partial(update, context, payload, locale)
    elif state == "ops_snooze_custom":
        await _enter_ops_snooze_custom(update, context, payload, locale)
    elif state == "sec_promise":
        await _enter_sec_promise(update, context, payload, locale)
    elif state == "archive_caption":
        await _enter_archive_caption(update, context, payload, locale)
    elif state == "copilot_ask":
        await _handle_copilot_ask_question(update, context, locale)
    else:
        from pasay_bot.handlers import nl_bridge

        await nl_bridge.handle_nl(update, context)


async def _enter_archive_caption(update, context, payload, locale: str):
    """PASAY-AI-EMPLOYEE-FOUNDATION-007 §6.2: the user supplied the semantic
    caption for a pending photo ("1608 水表"). Resolve unit + category, publish
    to the archive and index it as an ArchiveAsset (Evidence) with the semantic
    title/caption. Never a floating file."""
    from pasay_bot.api_client import PasayApiError

    store = context.bot_data["store"]
    api = context.bot_data["api_client"]
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    text = (update.effective_message.text or "").strip()
    store.delete_conversation(chat_id, user_id)
    if text.lower() in ("取消", "cancel"):
        await context.bot.send_message(
            chat_id, H.escape(t("rent.cancelled", locale)), parse_mode=HTML,
            reply_markup=home_keyboard(locale),
        )
        return
    unit_token, category, title = _parse_archive_caption(text)
    if unit_token is None:
        await context.bot.send_message(
            chat_id, H.escape(t("v2.archive_caption_invalid", locale)),
            parse_mode=HTML, reply_markup=home_keyboard(locale),
        )
        return
    # Resolve the unit id for the semantic link.
    unit_id = None
    try:
        units = await api.get_units()
        key = unit_token.split("-")[-1]
        unit = next((u for u in units if (u.unit_number or "").split("-")[-1] == key), None)
        if unit is not None:
            unit_id = unit.id
    except PasayApiError:
        pass
    file_id = payload.get("file_id")
    if not file_id:
        await context.bot.send_message(
            chat_id, H.escape(t("v2.archive_caption_lost", locale)), parse_mode=HTML,
            reply_markup=home_keyboard(locale),
        )
        return
    # Publish to the archive channel (send the stored file_id under the title).
    settings = context.bot_data["settings"]
    archive_chat = (settings.archive_chat_id or "").strip()
    forwarded_id = None
    if archive_chat:
        try:
            if payload.get("media_type") == "document":
                fwd = await update.get_bot().send_document(
                    archive_chat, document=file_id, caption=title[:200] or None,
                )
            elif payload.get("media_type") == "video":
                fwd = await update.get_bot().send_video(
                    archive_chat, video=file_id, caption=title[:200] or None,
                )
            else:
                fwd = await update.get_bot().send_photo(
                    archive_chat, photo=file_id, caption=title[:200] or None,
                )
            forwarded_id = getattr(fwd, "message_id", None)
        except Exception as exc:  # noqa: BLE001 - publication failure is real
            logger = context.bot_data.get("logger")
            if logger:
                logger.warning("archive caption publish failed: %s", exc)
            await context.bot.send_message(
                chat_id, H.escape(t("v2.archive_publish_failed", locale)),
                parse_mode=HTML, reply_markup=home_keyboard(locale),
            )
            return
    # Index as an ArchiveAsset with the semantic caption.
    try:
        await api.create_evidence(
            external_file_id=file_id,
            external_message_id=forwarded_id,
            media_type=payload.get("media_type") or "photo",
            mime_type=payload.get("mime_type"),
            filename=payload.get("filename"),
            size_bytes=payload.get("size_bytes"),
            category=_EVIDENCE_CATEGORY.get(category, "other"),
            unit_id=unit_id,
            entity_type="unit" if unit_id else None,
            entity_id=unit_id,
        )
    except PasayApiError:
        pass
    await context.bot.send_message(
        chat_id,
        H.escape(t("v2.media_archived", locale)),
        parse_mode=HTML, reply_markup=home_keyboard(locale),
    )


# Archive category hint -> evidence category (PASAY-AI-EMPLOYEE-FOUNDATION-007).
_EVIDENCE_CATEGORY = {
    "水表": "water_meter", "电表": "electric_meter",
    "房租手机": "rent_phone", "钥匙": "key_access",
    "租约": "lease", "合同": "lease", "入住": "move_in", "迁出": "move_out",
    "物业": "property_management", "维修": "before_repair", "设备": "equipment",
}


def _parse_archive_caption(text: str):
    """Parse a semantic caption ("1608 水表") -> (unit_token, category, title)."""
    tokens = [t for t in text.replace("，", " ").replace(",", " ").split() if t]
    unit_token = None
    rest = []
    for token in tokens:
        if any(c.isdigit() for c in token) and not unit_token:
            unit_token = token
        else:
            rest.append(token)
    caption_words = "".join(rest)
    category = None
    for keyword in _EVIDENCE_CATEGORY:
        if keyword in text:
            category = keyword
            break
    if unit_token is None or not category:
        return None, None, None
    return unit_token, category, caption_words or category


async def _enter_sec_promise(update, context, payload, locale: str):
    """PASAY-AI-EMPLOYEE-FOUNDATION-007 §17: the Secretary replied with a
    payment promise (e.g. ``周五`` / ``明天付30000``). We record it via the
    backend so the workflow auto-checks at the promised date, then confirm."""
    from datetime import datetime as _dt, timedelta as _td
    from decimal import Decimal as _Dec, InvalidOperation as _Invalid

    from pasay_bot.api_client import PasayApiError

    store = context.bot_data["store"]
    api = context.bot_data["api_client"]
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    task_id = int(payload.get("task_id") or 0)
    text = (update.effective_message.text or "").strip()
    store.delete_conversation(chat_id, user_id)
    if text.lower() in ("取消", "cancel"):
        await context.bot.send_message(chat_id, H.escape(t("rent.cancelled", locale)),
                                       parse_mode=HTML, reply_markup=home_keyboard(locale))
        return
    if not task_id:
        await context.bot.send_message(chat_id, H.escape(t("common.invalid", locale)),
                                       parse_mode=HTML, reply_markup=home_keyboard(locale))
        return
    promised_date, amount = _parse_promise(text)
    if promised_date is None:
        await context.bot.send_message(
            chat_id,
            H.escape(t("v2.sec_promise_invalid", locale)),
            parse_mode=HTML, reply_markup=home_keyboard(locale),
        )
        return
    # Resolve lease_id from the task.
    lease_id = None
    try:
        task = await api.get_operational_task(task_id)
        lease_id = getattr(task, "lease_id", None)
    except PasayApiError:
        pass
    try:
        recorded = await api.record_payment_promise(
            lease_id=lease_id,
            amount=float(amount) if amount is not None else None,
            promised_date=promised_date.isoformat(), note=text,
        )
    except PasayApiError:
        await context.bot.send_message(
            chat_id, f"⚠️ {H.escape(t('common.unexpected', locale))}",
            parse_mode=HTML, reply_markup=home_keyboard(locale),
        )
        return
    amount_txt = f"₱{amount:,.2f}" if amount else ""
    date_txt = promised_date.strftime("%m-%d")
    reply = t("v2.sec_promise_saved", locale, date=date_txt, amount=amount_txt)
    await context.bot.send_message(chat_id, H.escape(reply), parse_mode=HTML,
                                   reply_markup=home_keyboard(locale))


def _parse_promise(text: str):
    """Parse a payment-promise reply into (promised_date, amount|None).

    Supports: 周五/friday -> next Friday 18:00; 明天/tomorrow -> tomorrow 18:00;
    ``明天付30000`` -> tomorrow + amount; ``2026-08-18`` etc. Deterministic;
    returns (None, None) when nothing usable."""
    from datetime import datetime as _dt, timedelta as _td
    from decimal import Decimal as _Dec, InvalidOperation as _Invalid

    lowered = text.lower()
    now = _dt.now()
    # Find an amount first.
    amount = None
    import re as _re
    m = _re.search(r"(\d[\d,]*)", text.replace("，", ","))
    if m:
        try:
            v = _Dec(m.group(1).replace(",", ""))
            if v > 0:
                amount = v
        except _Invalid:
            amount = None
    # Absolute date.
    try:
        return _dt.strptime(text.strip(), "%Y-%m-%d").replace(hour=18), amount
    except ValueError:
        pass
    base = now.replace(hour=18, minute=0, second=0, microsecond=0)
    if "周五" in text or "friday" in lowered or "fri" in lowered:
        days = (4 - now.weekday()) % 7
        if days == 0:
            days = 7
        return base + _td(days=days), amount
    if "明天" in text or "tomorrow" in lowered:
        return base + _td(days=1), amount
    if "后天" in text:
        return base + _td(days=2), amount
    return None, None


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


async def _enter_expense_amount(update, context, payload, locale, edit_mode: bool):
    """Expense amount follow-up (edit or AI-partial): one number, re-render."""
    from pasay_bot.handlers.expense_flow import render_expense_confirm

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    text = (update.effective_message.text or "").strip()
    try:
        amount = Decimal(text.replace(",", ""))
    except InvalidOperation:
        await context.bot.send_message(
            chat_id, H.escape(t("expense.amount_invalid", locale)),
            parse_mode=HTML, reply_markup=home_keyboard(locale),
        )
        return
    if amount <= 0 or amount > MAX_AMOUNT or amount != amount.quantize(Decimal("0.01")):
        await context.bot.send_message(
            chat_id, H.escape(t("expense.amount_invalid", locale)),
            parse_mode=HTML, reply_markup=home_keyboard(locale),
        )
        return
    payload["amount"] = str(amount.quantize(Decimal("0.01")))
    payload["expense_date"] = payload.get("expense_date") or date.today().isoformat()
    # Re-render the original confirmation card (edit-first).
    role = role_for_telegram_id(user_id)
    message_id = int(payload.get("confirm_message_id") or 0)
    await render_expense_confirm(
        update, context, payload, role, locale,
        message_id=message_id or None,
    )


async def _enter_expense_category(update, context, payload, locale):
    """Expense category follow-up: canonical label or retry."""
    from pasay_bot.handlers.expense_flow import normalize_category, render_expense_confirm

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    text = (update.effective_message.text or "").strip()
    category = normalize_category(text)
    if not category:
        await context.bot.send_message(
            chat_id, H.escape(t("expense.ask_category", locale)),
            parse_mode=HTML,
        )
        return
    payload["category"] = category
    role = role_for_telegram_id(user_id)
    message_id = int(payload.get("confirm_message_id") or 0)
    await render_expense_confirm(
        update, context, payload, role, locale,
        message_id=message_id or None,
    )


async def _enter_expense_unit(update, context, payload, locale):
    """Expense unit follow-up: unit number or '无' (not tied to a unit)."""
    from pasay_bot.handlers.expense_flow import render_expense_confirm, resolve_unit

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    text = (update.effective_message.text or "").strip()
    if text.lower() in ("无", "none", "没有", "不填", "-"):
        payload["unit_id"] = None
        payload["unit_number"] = ""
        payload["property_name"] = ""
    else:
        unit_id, unit_number, property_name = await resolve_unit(context, text)
        if unit_id is None:
            await context.bot.send_message(
                chat_id,
                H.escape(t("expense.unit_not_found", locale, unit=text)),
                parse_mode=HTML,
            )
            return
        payload["unit_id"] = unit_id
        payload["unit_number"] = unit_number
        payload["property_name"] = property_name
    role = role_for_telegram_id(user_id)
    message_id = int(payload.get("confirm_message_id") or 0)
    await render_expense_confirm(
        update, context, payload, role, locale,
        message_id=message_id or None,
    )


async def _enter_ai_expense_partial(update, context, payload, locale):
    """P0-5 follow-up: the user answered the missing-field question."""
    from pasay_bot.handlers.expense_flow import (
        normalize_category,
        render_expense_confirm,
        resolve_unit,
    )

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    text = (update.effective_message.text or "").strip()
    missing = list(payload.get("missing") or [])
    if "amount" in missing or not payload.get("amount"):
        try:
            amount = Decimal(text.replace(",", ""))
        except InvalidOperation:
            amount = None
        if amount is None or amount <= 0 or amount > MAX_AMOUNT:
            await context.bot.send_message(
                chat_id, H.escape(t("expense.amount_invalid", locale)),
                parse_mode=HTML,
            )
            return
        payload["amount"] = str(amount.quantize(Decimal("0.01")))
        missing = [m for m in missing if m != "amount"]
    elif "category" in missing or not payload.get("category"):
        category = normalize_category(text)
        if not category:
            await context.bot.send_message(
                chat_id, H.escape(t("ai.ask_category", locale)),
                parse_mode=HTML,
            )
            return
        payload["category"] = category
        missing = [m for m in missing if m != "category"]
    elif "unit" in missing or not payload.get("unit_number"):
        if text.lower() in ("无", "none", "没有", "-"):
            payload["unit_id"] = None
            payload["unit_number"] = ""
            payload["property_name"] = ""
        else:
            unit_id, unit_number, property_name = await resolve_unit(context, text)
            if unit_id is None:
                await context.bot.send_message(
                    chat_id,
                    H.escape(t("expense.unit_not_found", locale, unit=text)),
                    parse_mode=HTML,
                )
                return
            payload["unit_id"] = unit_id
            payload["unit_number"] = unit_number
            payload["property_name"] = property_name
        missing = [m for m in missing if m != "unit"]

    if missing:
        payload["missing"] = missing
        store = context.bot_data["store"]
        store.save_conversation(chat_id, user_id, "ai_expense_partial", payload)
        if "amount" in missing:
            await context.bot.send_message(
                chat_id, H.escape(t("expense.ask_amount", locale)),
                parse_mode=HTML,
            )
        elif "category" in missing:
            await context.bot.send_message(
                chat_id, H.escape(t("ai.ask_category", locale)),
                parse_mode=HTML,
            )
        else:
            await context.bot.send_message(
                chat_id, H.escape(t("ai.ask_unit", locale)),
                parse_mode=HTML,
            )
        return
    payload.pop("missing", None)
    role = role_for_telegram_id(user_id)
    await render_expense_confirm(update, context, payload, role, locale)


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
    title = H.escape(task.title or t("ops.task", locale))
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
