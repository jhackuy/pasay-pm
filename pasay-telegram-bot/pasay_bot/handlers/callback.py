"""Callback router: nav / pagination / rent flow / confirm / reverse / cancel.

Write paths (confirm/reverse) implement design §8/§13/§14:
- per-card nonce -> idempotency key (in_flight/done/failed)
- card ts -> 15-minute expiry, backend state is the final arbiter
- timeout -> reconcile via GET /incomes/{id}; NEVER claim "nothing changed"
"""
from __future__ import annotations

import logging
import time
from datetime import date
from decimal import Decimal, InvalidOperation

from telegram import Update
from telegram.ext import ContextTypes

from pasay_bot.api_client import (
    PasayApiConflictError,
    PasayApiError,
    PasayApiPermissionError,
    PasayApiTimeoutError,
    Income,
)
from pasay_bot.handlers import commands as pages
from pasay_bot.handlers.commands import (
    build_finance_page,
    build_overdue_page,
    build_properties_page,
    build_rent_property_list,
    show_menu,
)
from pasay_bot.keyboards import (
    ACTION_CANCEL,
    ACTION_CONFIRM,
    ACTION_DETAIL,
    ACTION_METHOD,
    ACTION_NAV,
    ACTION_PAGE,
    ACTION_RENT,
    ACTION_REVERSE,
    METHOD_LABELS,
    confirm_income_keyboard,
    confirm_rent_keyboard,
    decode,
    encode,
    new_nonce,
    now_ts,
    payment_method_keyboard,
    unit_page_keyboard,
)
from pasay_bot.handlers.edit_utils import edit_message_text_idempotent
from pasay_bot.render import cards, html as H
from pasay_bot.render.i18n import t
from pasay_bot.roles import (
    PERMISSION_RENT_CONFIRM,
    PERMISSION_RENT_ENTRY,
    PERMISSION_REVERSE,
    has_permission,
    has_read_permission,
    locale_for,
    role_for_telegram_id,
)

HTML = "HTML"

logger = logging.getLogger(__name__)


def _expired(ts, settings) -> bool:
    if ts is None:
        return True
    return (int(time.time()) - int(ts)) > settings.callback_ttl_seconds


async def _answer(update: Update, text: str):
    cq = update.callback_query
    try:
        await cq.answer(text)
    except Exception:
        pass


async def _edit(update: Update, text: str, keyboard=None):
    cq = update.callback_query
    await edit_message_text_idempotent(
        update.get_bot(),
        chat_id=update.effective_chat.id,
        message_id=cq.message.message_id,
        text=H.truncate(text),
        parse_mode=HTML,
        reply_markup=keyboard,
    )


def _payload(conv: dict) -> dict:
    return conv.get("payload") or {}


def _can_reverse(context, role) -> bool:
    """Reverse needs OWNER permission AND an admin API key (F2). Without the
    admin key the button is hidden and hand-crafted callbacks are refused."""
    return (
        has_permission(role, PERMISSION_REVERSE)
        and context.bot_data.get("admin_api_client") is not None
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    parsed = decode(cq.data)
    if parsed is None:
        await _answer(update, t("common.invalid"))
        return
    action = parsed["action"]
    entity = parsed["entity"]
    ref = parsed["ref"]
    nonce = parsed["nonce"]
    ts = parsed["ts"]
    user = update.effective_user
    user_id = user.id if user else None
    chat_id = update.effective_chat.id if update.effective_chat else user_id
    role = role_for_telegram_id(user_id)
    locale = locale_for(role)

    if action == ACTION_NAV:
        await _handle_nav(update, context, entity, role, locale)
    elif action == ACTION_PAGE:
        await _handle_page(update, context, entity, ref, role, locale)
    elif action == ACTION_RENT:
        await _handle_rent(update, context, entity, ref, role, locale)
    elif action == ACTION_METHOD:
        await _handle_method(update, context, entity, role, locale)
    elif action == ACTION_CONFIRM:
        await _handle_confirm(update, context, entity, ref, nonce, ts, role, locale)
    elif action == ACTION_REVERSE:
        await _handle_reverse(update, context, ref, nonce, ts, role, locale)
    elif action == ACTION_CANCEL:
        await _handle_cancel(update, context, locale)
    elif action == ACTION_DETAIL:
        await _handle_detail(update, context, ref, role, locale)
    else:
        await _answer(update, t("common.invalid", locale))


async def _handle_nav(update, context, entity, role, locale):
    if not has_read_permission(role):
        await _answer(update, t("common.no_permission", locale))
        return
    chat_id = update.effective_chat.id
    if entity == "properties":
        await pages.show_properties(context, chat_id, role, locale, page=1)
    elif entity == "finance":
        await pages.show_finance(context, chat_id, locale)
    elif entity == "overdue":
        await pages.show_overdue(context, chat_id, locale, page=1)
    elif entity == "rent":
        await pages.show_rent(context, chat_id, locale)
    else:
        await show_menu(context, chat_id, locale)
    await _answer(update, "")


async def _handle_page(update, context, entity, ref, role, locale):
    if not has_read_permission(role):
        await _answer(update, t("common.no_permission", locale))
        return
    try:
        page = int(ref) if ref.isdigit() else 1
    except ValueError:
        page = 1
    chat_id = update.effective_chat.id
    if entity == "prop":
        await pages.show_properties(context, chat_id, None, locale, page=page)
    elif entity == "ovd":
        await pages.show_overdue(context, chat_id, locale, page=page)
    else:
        await show_menu(context, chat_id, locale)
    await _answer(update, "")


async def _handle_rent(update, context, entity, ref, role, locale):
    """rent flow: prop -> unit list; unit -> unit page; go -> start entry."""
    if not has_read_permission(role):
        await _answer(update, t("common.no_permission", locale))
        return
    chat_id = update.effective_chat.id
    message_id = update.callback_query.message.message_id
    if entity == "prop" and ref.isdigit():
        await pages.show_rent_units(context, chat_id, message_id, int(ref), locale)
        await _answer(update, "")
        return
    if entity == "unit" and ref.isdigit():
        unit_id = int(ref)
        can_rent = has_permission(role, PERMISSION_RENT_ENTRY)
        await pages.show_unit_page(
            context, chat_id, message_id, unit_id, can_rent, locale
        )
        await _answer(update, "")
        return
    if entity == "go" and ref.isdigit():
        if not has_permission(role, PERMISSION_RENT_ENTRY):
            await _answer(update, t("common.no_permission", locale))
            return
        await _begin_rent_entry(update, context, int(ref), role, locale)
        return
    await _answer(update, t("common.invalid", locale))


async def _begin_rent_entry(update, context, unit_id: int, role, locale):
    """Fetch unit + active lease, save conversation, ask for amount."""
    api = context.bot_data["api_client"]
    store = context.bot_data["store"]
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    try:
        unit = await api.get_unit(unit_id)
        properties = await api.get_properties()
        leases = await api.get_leases()
    except PasayApiError as exc:
        await _answer(update, t("common.load_error", locale, detail=exc.detail))
        return
    prop = next((p for p in properties if p.id == unit.property_id), None)
    lease = next(
        (l for l in leases if l.unit_id == unit.id and l.status == "active"), None
    )
    if lease is None:
        await _edit(update, t("rent.no_active_lease", locale))
        await _answer(update, "无活跃租约")
        return
    payload = {
        "unit_id": unit.id,
        "lease_id": lease.id,
        "property_id": unit.property_id,
        "property_name": prop.name if prop else "?",
        "unit_number": unit.unit_number,
        "monthly_rent": str(lease.monthly_rent),
    }
    store.save_conversation(chat_id, user_id, "rent_amount", payload)
    default = H.money(lease.monthly_rent)
    text = t("rent.ask_amount", locale, unit=H.escape(unit.unit_number), default=default)
    await _edit(update, text)


async def _handle_method(update, context, method_key: str, role, locale):
    """Payment method -> save payload -> show confirmation card."""
    store = context.bot_data["store"]
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    conv = store.get_conversation(chat_id, user_id)
    if conv is None or conv["state"] != "rent_method":
        await _answer(update, t("common.expired", locale))
        return
    method = METHOD_LABELS.get(method_key)
    if method is None:
        await _answer(update, t("common.invalid", locale))
        return
    payload = _payload(conv)
    payload["method"] = method
    nonce = new_nonce()
    ts = now_ts()
    store.save_conversation(chat_id, user_id, "rent_confirm", payload, nonce=nonce)
    can_confirm = has_permission(role, PERMISSION_RENT_CONFIRM)
    text = cards.rent_confirm_card(
        payload["property_name"],
        payload["unit_number"],
        payload["amount"],
        payload["received_date"],
        method,
        locale,
    )
    await _edit(
        update,
        text,
        confirm_rent_keyboard(nonce, ts, can_confirm, locale),
    )


async def _handle_confirm(update, context, entity, ref, nonce, ts, role, locale):
    if entity == "ren":
        await _confirm_rent_entry(update, context, nonce, ts, role, locale)
    elif entity == "inc":
        await _confirm_income(update, context, ref, nonce, ts, role, locale)
    else:
        await _answer(update, t("common.invalid", locale))


async def _confirm_rent_entry(update, context, nonce, ts, role, locale):
    store = context.bot_data["store"]
    api = context.bot_data["api_client"]
    guard = context.bot_data["idempotency"]
    settings = context.bot_data["settings"]
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if _expired(ts, settings):
        await _answer(update, t("common.expired", locale))
        return
    if not has_permission(role, PERMISSION_RENT_ENTRY):
        await _answer(update, t("common.no_permission", locale))
        return
    key = f"ik:cnf:ren:{nonce}"
    # Ownership check BEFORE acquiring the idempotency key (F6): in a group
    # chat, another member clicking this card must not burn/lock our key.
    conv = store.get_conversation(chat_id, user_id)
    if (
        conv is None
        or conv["state"] != "rent_confirm"
        or conv.get("nonce") != nonce
    ):
        await _answer(update, t("common.expired", locale))
        return
    payload = _payload(conv)
    can_confirm = has_permission(role, PERMISSION_RENT_CONFIRM)

    # Idempotency: done/in_flight replay without touching the API, so a second
    # click after a successful write never re-writes.
    status = guard.acquire(key, kind="income", resource="")
    if status == "done":
        result = guard.result(key) or {}
        income = Income.from_dict(result) if isinstance(result, dict) else None
        await _render_done_card(update, context, income, payload, role, locale)
        await _answer(update, t("rent.processed_toast", locale))
        return
    if status == "in_flight":
        await _answer(update, t("common.processing", locale))
        return

    income = None
    try:
        # Resume from a previous attempt that created a pending income but did
        # not confirm it (network timeout). Never create a second income.
        existing_id = guard.resource(key)
        if existing_id and existing_id.isdigit():
            current = await api.get_income(int(existing_id))
            income = current
            if current.status == "pending" and can_confirm:
                confirmed = await api.confirm_income(current.id)
                guard.settle(key, confirmed.as_dict(), resource=str(current.id))
                await _render_done_card(update, context, confirmed, payload, role, locale)
                await _answer(update, t("rent.confirmed_toast", locale))
                return
            if current.status == "pending":
                guard.settle(key, current.as_dict(), resource=str(current.id))
                await _render_pending_card(update, context, current, payload, role, locale)
                await _answer(update, t("rent.recorded_toast", locale))
                return
            guard.settle(key, current.as_dict(), resource=str(current.id))
            await _render_done_card(update, context, current, payload, role, locale)
            await _answer(update, t("rent.processed_toast", locale))
            return

        # No known resource: reconcile against the full income list BEFORE
        # creating, so a write that landed during a previous timeout or crash
        # (possibly on a NEW card / new nonce) is reused, never duplicated (F1).
        matched = await api.find_income(
            lease_id=payload.get("lease_id"),
            amount=payload.get("amount"),
            received_date=payload.get("received_date"),
            payment_method=payload.get("method"),
        )
        if matched is not None:
            income = matched
            if matched.status == "pending" and can_confirm:
                confirmed = await api.confirm_income(matched.id)
                guard.settle(key, confirmed.as_dict(), resource=str(matched.id))
                await _render_done_card(update, context, confirmed, payload, role, locale)
                await _answer(update, t("rent.confirmed_toast", locale))
                return
            guard.settle(key, matched.as_dict(), resource=str(matched.id))
            if matched.status == "pending":
                await _render_pending_card(update, context, matched, payload, role, locale)
                await _answer(update, t("rent.recorded_toast", locale))
            else:
                await _render_done_card(update, context, matched, payload, role, locale)
                await _answer(update, t("rent.processed_toast", locale))
            return

        income = await api.create_income(
            lease_id=payload.get("lease_id"),
            amount=payload.get("amount"),
            received_date=payload.get("received_date"),
            payment_method=payload.get("method"),
            description=f"rent {str(payload.get('received_date', ''))[:7]}",
        )
        income_id = income.id
        if can_confirm:
            confirmed = await api.confirm_income(income_id)
            guard.settle(key, confirmed.as_dict(), resource=str(income_id))
            await _render_done_card(update, context, confirmed, payload, role, locale)
            await _answer(update, t("rent.confirmed_toast", locale))
        else:
            guard.settle(key, income.as_dict(), resource=str(income_id))
            await _render_pending_card(update, context, income, payload, role, locale)
            await _answer(update, t("rent.recorded_toast", locale))
    except PasayApiConflictError:
        if income is not None:
            current = await api.get_income(income.id)
            guard.settle(key, current.as_dict(), resource=str(current.id))
            await _render_done_card(update, context, current, payload, role, locale)
            await _answer(update, t("rent.processed_toast", locale))
        else:
            guard.fail(key)
            await _answer(update, t("common.error", locale, detail="conflict"))
    except PasayApiTimeoutError:
        if income is not None:
            await _reconcile_after_timeout(
                update, context, key, income.id, payload, role, locale
            )
        else:
            await _reconcile_create_after_timeout(
                update, context, key, payload, role, locale
            )
    except PasayApiPermissionError:
        guard.fail(key, resource=str(income.id) if income else None)
        await _answer(update, t("common.no_permission", locale))
    except PasayApiError as exc:
        guard.fail(key, resource=str(income.id) if income else None)
        await _answer(update, t("common.error", locale, detail=exc.detail))


async def _confirm_income(update, context, ref, nonce, ts, role, locale):
    """Confirm an already-created pending income (double-click safe)."""
    store = context.bot_data["store"]
    api = context.bot_data["api_client"]
    guard = context.bot_data["idempotency"]
    settings = context.bot_data["settings"]
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if _expired(ts, settings):
        await _answer(update, t("common.expired", locale))
        return
    if not has_permission(role, PERMISSION_RENT_CONFIRM):
        await _answer(update, t("common.no_permission", locale))
        return
    if not ref.isdigit():
        await _answer(update, t("common.invalid", locale))
        return
    income_id = int(ref)
    key = f"ik:cnf:inc:{income_id}:{nonce or '0'}"
    status = guard.acquire(key, kind="income", resource=str(income_id))
    if status == "done":
        result = guard.result(key) or {}
        current = Income.from_dict(result) if isinstance(result, dict) else await api.get_income(income_id)
        await _render_income_state(update, context, current, role, locale)
        await _answer(update, t("rent.processed_toast", locale))
        return
    if status == "in_flight":
        await _answer(update, t("common.processing", locale))
        return

    try:
        confirmed = await api.confirm_income(income_id)
        guard.settle(key, confirmed.as_dict(), resource=str(income_id))
        await _render_income_state(update, context, confirmed, role, locale)
        await _answer(update, t("rent.confirmed_toast", locale))
    except PasayApiConflictError:
        # 409 = only pending incomes can be confirmed -> already handled.
        current = await api.get_income(income_id)
        guard.settle(key, current.as_dict(), resource=str(income_id))
        await _render_income_state(update, context, current, role, locale)
        await _answer(update, t("rent.processed_toast", locale))
    except PasayApiTimeoutError:
        await _reconcile_income_after_timeout(
            update, context, key, income_id, role, locale
        )
    except PasayApiPermissionError:
        guard.fail(key, resource=str(income_id))
        await _answer(update, t("common.no_permission", locale))
    except PasayApiError as exc:
        guard.fail(key, resource=str(income_id))
        await _answer(update, t("common.error", locale, detail=exc.detail))


async def _handle_reverse(update, context, ref, nonce, ts, role, locale):
    api = context.bot_data["api_client"]
    admin_api = context.bot_data.get("admin_api_client")
    guard = context.bot_data["idempotency"]
    settings = context.bot_data["settings"]
    if _expired(ts, settings):
        await _answer(update, t("common.expired", locale))
        return
    if not has_permission(role, PERMISSION_REVERSE):
        await _answer(update, t("common.no_permission", locale))
        return
    if admin_api is None:
        logger.warning("PASSAY_ADMIN_API_KEY is not configured; OWNER reverse refused")
        await _answer(update, t("common.reverse_unavailable", locale))
        return
    if not ref.isdigit():
        await _answer(update, t("common.invalid", locale))
        return
    income_id = int(ref)
    key = f"ik:rv:{income_id}:{nonce or '0'}"
    status = guard.acquire(key, kind="income", resource=str(income_id))
    if status == "done":
        result = guard.result(key) or {}
        current = Income.from_dict(result) if isinstance(result, dict) else await api.get_income(income_id)
        await _render_income_state(update, context, current, role, locale)
        await _answer(update, t("rent.processed_toast", locale))
        return
    if status == "in_flight":
        await _answer(update, t("common.processing", locale))
        return
    try:
        # Reverse is admin-only on the backend -> use the admin-key client.
        reversed_ = await admin_api.reverse_income(income_id)
        guard.settle(key, reversed_.as_dict(), resource=str(income_id))
        await _render_income_state(update, context, reversed_, role, locale)
        await _answer(update, t("rent.reversed_toast", locale))
    except PasayApiConflictError:
        current = await api.get_income(income_id)
        guard.settle(key, current.as_dict(), resource=str(income_id))
        await _render_income_state(update, context, current, role, locale)
        if current.status == "reversed":
            await _answer(update, t("rent.reversed_toast", locale))
        else:
            await _answer(update, t("rent.reverse_conflict_toast", locale))
    except PasayApiTimeoutError:
        await _reconcile_income_after_timeout(
            update, context, key, income_id, role, locale, reverse=True
        )
    except PasayApiPermissionError:
        guard.fail(key)
        await _answer(update, t("common.no_permission", locale))
    except PasayApiError as exc:
        guard.fail(key)
        await _answer(update, t("common.error", locale, detail=exc.detail))


async def _handle_cancel(update, context, locale):
    store = context.bot_data["store"]
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    conv = store.get_conversation(chat_id, user_id)
    if conv and conv["state"] in ("rent_amount", "rent_date", "rent_method", "rent_confirm"):
        store.delete_conversation(chat_id, user_id)
        await _edit(update, H.escape(t("rent.cancelled", locale)))
    else:
        await show_menu(context, chat_id, locale)
    await _answer(update, t("rent.cancelled", locale))


async def _handle_detail(update, context, ref, role, locale):
    if not has_read_permission(role):
        await _answer(update, t("common.no_permission", locale))
        return
    if ref.isdigit():
        await pages.show_unit_page(
            context,
            update.effective_chat.id,
            update.callback_query.message.message_id,
            int(ref),
            can_rent=has_permission(role, PERMISSION_RENT_ENTRY),
            locale=locale,
        )
        await _answer(update, "")
        return
    await _answer(update, t("common.invalid", locale))


# --- timeout reconciliation (design §13) ---

async def _reconcile_after_timeout(
    update, context, key, income_id, payload, role, locale
):
    """Create/confirm timed out; the income may already exist -> never lie."""
    guard = context.bot_data["idempotency"]
    store = context.bot_data["store"]
    api = context.bot_data["api_client"]
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    try:
        current = await api.get_income(income_id)
    except PasayApiError:
        guard.fail(key)
        await _answer(update, t("common.timeout", locale))
        return
    if current.status in ("confirmed", "reversed"):
        guard.settle(key, current.as_dict(), resource=str(income_id))
        await _render_done_card(update, context, current, payload, role, locale)
        await _answer(update, t("rent.processed_toast", locale))
    elif current.status == "pending":
        guard.fail(key, resource=str(income_id))
        await _render_pending_card(update, context, current, payload, role, locale)
        await _answer(update, t("common.timeout_pending", locale))
    else:
        guard.fail(key, resource=str(income_id))
        await _answer(update, t("common.timeout", locale))


async def _reconcile_create_after_timeout(
    update, context, key, payload, role, locale
):
    """Create timed out with an UNKNOWN outcome: the pending income may have
    landed server-side. Reconcile via the full income list matching
    (lease_id, received_date, amount, method); if found, reuse and settle it —
    NEVER create a second income (F1)."""
    guard = context.bot_data["idempotency"]
    api = context.bot_data["api_client"]
    try:
        matched = await api.find_income(
            lease_id=payload.get("lease_id"),
            amount=payload.get("amount"),
            received_date=payload.get("received_date"),
            payment_method=payload.get("method"),
        )
    except PasayApiError:
        guard.fail(key)
        await _answer(update, t("common.timeout", locale))
        return
    if matched is None:
        guard.fail(key)
        await _answer(update, t("common.timeout", locale))
        return
    can_confirm = has_permission(role, PERMISSION_RENT_CONFIRM)
    if matched.status == "pending" and can_confirm:
        confirmed = await api.confirm_income(matched.id)
        guard.settle(key, confirmed.as_dict(), resource=str(matched.id))
        await _render_done_card(update, context, confirmed, payload, role, locale)
        await _answer(update, t("rent.confirmed_toast", locale))
        return
    guard.settle(key, matched.as_dict(), resource=str(matched.id))
    if matched.status == "pending":
        await _render_pending_card(update, context, matched, payload, role, locale)
        await _answer(update, t("rent.recorded_toast", locale))
    else:
        await _render_done_card(update, context, matched, payload, role, locale)
        await _answer(update, t("rent.processed_toast", locale))


async def _reconcile_income_after_timeout(
    update, context, key, income_id, role, locale, reverse=False
):
    guard = context.bot_data["idempotency"]
    api = context.bot_data["api_client"]
    try:
        current = await api.get_income(income_id)
    except PasayApiError:
        guard.fail(key)
        await _answer(update, t("common.timeout", locale))
        return
    if current.status in ("confirmed", "reversed"):
        guard.settle(key, current.as_dict(), resource=str(income_id))
        await _render_income_state(update, context, current, role, locale)
        if reverse and current.status == "confirmed":
            # Reverse timed out and the income is still confirmed: the reversal
            # did NOT land. Never tell the user it was processed (F7).
            await _answer(update, t("rent.reverse_failed_toast", locale))
        else:
            await _answer(update, t("rent.processed_toast", locale))
    else:
        guard.fail(key, resource=str(income_id))
        await _answer(update, t("common.timeout", locale))


# --- result rendering ---

async def _render_done_card(update, context, income, payload, role, locale):
    if income is None:
        await _edit(update, H.escape(t("common.timeout", locale)))
        return
    if income.status == "reversed":
        text = cards.reversed_card(income, locale)
        await _edit(update, text)
        return
    if income.status == "pending":
        await _render_pending_card(update, context, income, payload, role, locale)
        return
    text = cards.rent_success_card(
        income, payload.get("property_name", ""), payload.get("unit_number", ""), locale
    )
    keyboard = None
    if _can_reverse(context, role):
        keyboard = confirm_income_keyboard(
            income.id, new_nonce(), now_ts(), can_reverse=True, locale=locale
        )
    await _edit(update, text, keyboard)


async def _render_pending_card(update, context, income, payload, role, locale):
    text = cards.pending_recorded_card(
        income, payload.get("property_name", ""), payload.get("unit_number", ""), locale
    )
    keyboard = None
    if has_permission(role, PERMISSION_RENT_CONFIRM):
        keyboard = confirm_income_keyboard(
            income.id, new_nonce(), now_ts(), can_reverse=False, locale=locale
        )
    await _edit(update, text, keyboard)


async def _render_income_state(update, context, income: Income, role, locale):
    """Render a known income state onto the current card."""
    if income.status == "confirmed":
        text = t(
            "rent.success",
            locale,
            property="",
            unit="",
            amount=H.money(income.amount),
            date=H.format_date(income.received_date),
            method=H.escape(income.payment_method or "-"),
            income_id=income.id,
        )
        keyboard = (
            confirm_income_keyboard(income.id, new_nonce(), now_ts(), can_reverse=True, locale=locale)
            if _can_reverse(context, role)
            else None
        )
        await _edit(update, text, keyboard)
    elif income.status == "reversed":
        await _edit(update, cards.reversed_card(income, locale))
    else:
        await _edit(update, t("rent.pending_card", locale, income_id=income.id))
