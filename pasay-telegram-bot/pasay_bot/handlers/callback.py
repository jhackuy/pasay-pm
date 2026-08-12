"""Callback router: nav / pagination / rent flow / confirm / reverse / cancel.

Write paths (confirm/reverse) implement design §8/§13/§14:
- per-card nonce -> idempotency key (in_flight/done/failed)
- card ts -> 15-minute expiry, backend state is the final arbiter
- timeout -> reconcile via GET /incomes/{id}; NEVER claim "nothing changed"
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from datetime import date

from telegram import Update
from telegram.ext import ContextTypes

from pasay_bot.api_client import (
    CopilotExecute,
    CopilotRecommend,
    PasayApiConflictError,
    PasayApiError,
    PasayApiPermissionError,
    PasayApiTimeoutError,
    Income,
)
from pasay_bot.handlers import commands as pages
from pasay_bot.handlers.commands import (
    _current_month,
    show_dashboard,
    show_operations_center,
    show_operations_section,
)
from pasay_bot.keyboards import (
    ACTION_CANCEL,
    ACTION_CONFIRM,
    ACTION_COPILOT_ASK,
    ACTION_COPILOT_ASSIGNEE_PICK,
    ACTION_COPILOT_CONFIRM,
    ACTION_COPILOT_DECLINE,
    ACTION_COPILOT_EDIT,
    ACTION_COPILOT_NAV,
    ACTION_COPILOT_RECOMMEND_BACK,
    ACTION_COPILOT_SNOOZE_PICK,
    ACTION_COPILOT_SUGGEST,
    ACTION_COPILOT_WHY,
    ACTION_DETAIL,
    ACTION_EDIT,
    ACTION_EXPENSE_APPROVE,
    ACTION_EXPENSE_DETAIL,
    ACTION_EXPENSE_REJECT,
    ACTION_METHOD,
    ACTION_NAV,
    ACTION_OPS_NAV,
    ACTION_PAGE,
    ACTION_RENT,
    ACTION_REVERSE,
    ACTION_TASK_COMPLETE,
    ACTION_TASK_DETAIL,
    ACTION_TASK_SNOOZE,
    ACTION_TASK_SNOOZE_PICK,
    METHOD_LABELS,
    OPS_OVERVIEW,
    SNOOZE_PRESET_MAP,
    confirm_income_keyboard,
    confirm_rent_keyboard,
    copilot_back_today_keyboard,
    copilot_confirm_keyboard,
    copilot_due_keyboard,
    copilot_edit_menu_keyboard,
    copilot_stale_keyboard,
    copilot_success_keyboard,
    copilot_who_keyboard,
    decode,
    edit_date_keyboard,
    edit_input_keyboard,
    edit_menu_keyboard,
    error_keyboard,
    expense_approval_keyboard,
    expense_detail_keyboard,
    expense_result_keyboard,
    expired_keyboard,
    home_keyboard,
    new_nonce,
    now_ts,
    ops_back_keyboard,
    payment_method_keyboard,
    retry_confirm_keyboard,
    snooze_preset_keyboard,
    task_action_keyboard,
)
from pasay_bot.handlers.edit_utils import (
    edit_message_text_idempotent,
    edit_message_text_or_send,
)
from pasay_bot.render import cards, html as H
from pasay_bot.render.i18n import t
from pasay_bot.roles import (
    PERMISSION_OPERATIONS,
    PERMISSION_RENT_CONFIRM,
    PERMISSION_RENT_ENTRY,
    PERMISSION_REVERSE,
    Role,
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


def _load_error(detail: str, locale: str) -> str:
    return f"⚠️ {H.escape(t('common.load_error', locale, detail=str(detail)))}"


def _remember_default_method(update, context, payload: dict) -> None:
    """Persist the user's last-used payment method (B4 smart default)."""
    try:
        user_id = update.effective_user.id if update.effective_user else None
        method = payload.get("method")
        if user_id is not None and method:
            context.bot_data["store"].set_user_default_method(user_id, method)
    except Exception:  # noqa: BLE001 - defaults are best-effort
        pass


def _can_reverse(context, role) -> bool:
    """Reverse is shown only to OWNER; the backend re-authorizes the subject."""
    return has_permission(role, PERMISSION_REVERSE)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pages._bind_identity(update, context)
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
        await _handle_detail(update, context, entity, ref, role, locale)
    elif action == ACTION_EXPENSE_APPROVE:
        await _handle_expense_approve(update, context, entity, nonce, ts, role, locale)
    elif action == ACTION_EXPENSE_REJECT:
        await _handle_expense_reject(update, context, entity, nonce, ts, role, locale)
    elif action == ACTION_EXPENSE_DETAIL:
        await _handle_expense_detail(update, context, entity, role, locale)
    elif action == ACTION_EDIT:
        await _handle_edit(update, context, entity, role, locale)
    elif action == ACTION_OPS_NAV:
        await _handle_ops_nav(update, context, entity, role, locale)
    elif action == ACTION_TASK_COMPLETE:
        await _handle_task_complete(update, context, ref, role, locale)
    elif action == ACTION_TASK_SNOOZE:
        await _handle_task_snooze(update, context, ref, role, locale)
    elif action == ACTION_TASK_SNOOZE_PICK:
        await _handle_task_snooze_pick(update, context, entity, ref, role, locale)
    elif action == ACTION_TASK_DETAIL:
        await _handle_task_detail(update, context, ref, role, locale)
    elif action == ACTION_COPILOT_NAV:
        await _handle_copilot_nav(update, context, role, locale)
    elif action == ACTION_COPILOT_WHY:
        await _handle_copilot_why(update, context, entity, role, locale)
    elif action == ACTION_COPILOT_ASK:
        await _handle_copilot_ask(update, context, role, locale)
    elif action == ACTION_COPILOT_SUGGEST:
        await _handle_copilot_suggest(update, context, entity, ref, nonce, ts, role, locale)
    elif action == ACTION_COPILOT_CONFIRM:
        await _handle_copilot_confirm(update, context, entity, ref, nonce, ts, role, locale)
    elif action == ACTION_COPILOT_DECLINE:
        await _handle_copilot_decline(update, context, entity, ref, nonce, ts, role, locale)
    elif action == ACTION_COPILOT_EDIT:
        await _handle_copilot_edit(update, context, entity, ref, nonce, ts, role, locale)
    elif action == ACTION_COPILOT_RECOMMEND_BACK:
        await _handle_copilot_recommend_back(update, context, role, locale)
    elif action == ACTION_COPILOT_SNOOZE_PICK:
        await _handle_copilot_snooze_pick(update, context, entity, ref, nonce, ts, role, locale)
    elif action == ACTION_COPILOT_ASSIGNEE_PICK:
        await _handle_copilot_assignee_pick(update, context, entity, ref, nonce, ts, role, locale)
    else:
        await _answer(update, t("common.invalid", locale))


async def _handle_nav(update, context, entity, role, locale):
    if not has_read_permission(role):
        await _answer(update, t("common.no_permission", locale))
        return
    # B11: acknowledge immediately so Telegram never keeps the spinner up
    # while dashboard data loads.
    await _answer(update, "")
    chat_id = update.effective_chat.id
    message_id = update.callback_query.message.message_id
    if entity == "properties":
        await pages.show_properties(context, chat_id, role, locale, page=1, message_id=message_id)
    elif entity == "finance":
        await pages.show_finance(context, chat_id, locale, message_id=message_id)
    elif entity == "overdue":
        await pages.show_overdue(context, chat_id, locale, page=1, message_id=message_id)
    elif entity == "rent":
        await pages.show_rent(context, chat_id, locale, message_id=message_id)
    elif entity == "pending":
        await pages.show_todo(context, chat_id, role, locale, message_id=message_id)
    else:  # home / menu
        await show_dashboard(context, chat_id, locale, message_id=message_id)


async def _handle_page(update, context, entity, ref, role, locale):
    if not has_read_permission(role):
        await _answer(update, t("common.no_permission", locale))
        return
    try:
        page = int(ref) if ref.isdigit() else 1
    except ValueError:
        page = 1
    chat_id = update.effective_chat.id
    message_id = update.callback_query.message.message_id
    await _answer(update, "")
    if entity == "prop":
        await pages.show_properties(context, chat_id, None, locale, page=page, message_id=message_id)
    elif entity == "ovd":
        await pages.show_overdue(context, chat_id, locale, page=page, message_id=message_id)
    else:
        await show_dashboard(context, chat_id, locale, message_id=message_id)


async def _handle_rent(update, context, entity, ref, role, locale):
    """rent flow: prop -> unit list; unit -> unit page; go -> start entry."""
    if not has_read_permission(role):
        await _answer(update, t("common.no_permission", locale))
        return
    chat_id = update.effective_chat.id
    message_id = update.callback_query.message.message_id
    if entity == "prop" and ref.isdigit():
        await _answer(update, "")
        await pages.show_rent_units(context, chat_id, message_id, int(ref), locale)
        return
    if entity == "unit" and ref.isdigit():
        await _answer(update, "")
        unit_id = int(ref)
        can_rent = has_permission(role, PERMISSION_RENT_ENTRY)
        await pages.show_unit_page(
            context, chat_id, message_id, unit_id, can_rent, locale
        )
        return
    if entity == "go" and ref.isdigit():
        if not has_permission(role, PERMISSION_RENT_ENTRY):
            await _answer(update, t("common.no_permission", locale))
            return
        await _begin_rent_entry(update, context, int(ref), role, locale)
        return
    await _answer(update, t("common.invalid", locale))


async def _begin_rent_entry(update, context, unit_id: int, role, locale):
    """Compressed rent entry (B4): apply smart defaults (period = this month,
    date = today, amount = current receivable, method = last used) and jump
    straight to the final confirmation card. The final confirm is never
    skipped; stale clicks on already-paid units are refused."""
    api = context.bot_data["api_client"]
    store = context.bot_data["store"]
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    await _answer(update, "")
    try:
        unit, properties, leases, incomes = await asyncio.gather(
            api.get_unit(unit_id),
            api.get_properties(),
            api.get_leases(),
            api.list_incomes(),
        )
    except PasayApiError as exc:
        await _edit(update, _load_error(exc.detail, locale), error_keyboard("rent", locale))
        return
    prop = next((p for p in properties if p.id == unit.property_id), None)
    lease = next(
        (l for l in leases if l.unit_id == unit.id and l.status == "active"), None
    )
    if lease is None:
        await _edit(update, t("rent.no_active_lease", locale), home_keyboard(locale))
        return
    month = _current_month()
    covered = False
    for inc in incomes:
        if inc.lease_id != lease.id or inc.status != "confirmed":
            continue
        if month in (inc.description or "") or (
            inc.received_date and inc.received_date.strftime("%Y-%m") == month
        ):
            covered = True
            break
    if covered:
        await _edit(update, t("unit.payment_paid", locale), home_keyboard(locale))
        return
    payload = {
        "unit_id": unit.id,
        "lease_id": lease.id,
        "property_id": unit.property_id,
        "property_name": prop.name if prop else "?",
        "unit_number": unit.unit_number,
        "monthly_rent": str(lease.monthly_rent),
        "amount": str(lease.monthly_rent),
        "received_date": date.today().isoformat(),
        "period": month,
        "method": store.get_user_default_method(user_id),
        "confirm_message_id": str(update.callback_query.message.message_id),
    }
    await _render_confirm_from_payload(update, context, payload, role, locale)


async def _render_confirm_from_payload(update, context, payload, role, locale):
    """Render the final confirmation card onto the current message (B4)."""
    store = context.bot_data["store"]
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    nonce = new_nonce()
    ts = now_ts()
    store.save_conversation(chat_id, user_id, "rent_confirm", payload, nonce=nonce)
    can_confirm = has_permission(role, PERMISSION_RENT_CONFIRM)
    text = cards.rent_confirm_card(
        payload["property_name"],
        payload["unit_number"],
        payload["amount"],
        payload["received_date"],
        payload["method"],
        locale,
    )
    await _edit(update, text, confirm_rent_keyboard(nonce, ts, can_confirm, locale))


async def _handle_method(update, context, method_key: str, role, locale):
    """Payment method -> save payload -> show confirmation card.

    Accepts both the legacy 'rent_method' state and the V1.1 edit state
    'rent_edit_method', then returns to the final confirmation card."""
    store = context.bot_data["store"]
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    conv = store.get_conversation(chat_id, user_id)
    if conv is None or conv["state"] not in ("rent_method", "rent_edit_method"):
        await _answer(update, t("common.expired", locale))
        return
    method = METHOD_LABELS.get(method_key)
    if method is None:
        await _answer(update, t("common.invalid", locale))
        return
    payload = _payload(conv)
    payload["method"] = method
    await _render_confirm_from_payload(update, context, payload, role, locale)


async def _handle_edit(update, context, sub, role, locale):
    """B4: [✏️修改] sub-flow on the final confirmation card.

    sub: menu / amount / date / method / back. Any expired or missing
    conversation is rendered as an expired card with a home button."""
    store = context.bot_data["store"]
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    conv = store.get_conversation(chat_id, user_id)
    payload = _payload(conv) if conv else {}
    allowed = (
        "rent_confirm", "rent_edit", "rent_edit_amount",
        "rent_edit_date", "rent_edit_method",
    )
    if conv is None or conv["state"] not in allowed or not payload.get("amount"):
        await _answer(update, t("common.expired", locale))
        await _edit(update, t("common.expired", locale), expired_keyboard(locale))
        return
    await _answer(update, "")
    if sub == "menu":
        store.save_conversation(chat_id, user_id, "rent_edit", payload)
        await _edit(update, H.escape(t("rent.edit_title", locale)), edit_menu_keyboard(locale))
        return
    if sub == "amount":
        store.save_conversation(chat_id, user_id, "rent_edit_amount", payload)
        await _edit(
            update,
            t("rent.ask_edit_amount", locale, current=H.money(payload.get("amount"))),
            edit_input_keyboard(locale),
        )
        return
    if sub == "date":
        store.save_conversation(chat_id, user_id, "rent_edit_date", payload)
        await _edit(
            update,
            t("rent.ask_edit_date", locale, current=payload.get("received_date", "")),
            edit_date_keyboard(locale),
        )
        return
    if sub == "method":
        store.save_conversation(chat_id, user_id, "rent_edit_method", payload)
        await _edit(
            update, H.escape(t("rent.ask_method", locale)),
            payment_method_keyboard(locale),
        )
        return
    if sub == "today" and conv["state"] == "rent_edit_date":
        payload["received_date"] = date.today().isoformat()
        await _render_confirm_from_payload(update, context, payload, role, locale)
        return
    # back -> re-render the confirmation card with the edited payload
    await _render_confirm_from_payload(update, context, payload, role, locale)


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
        await _edit(update, t("common.expired", locale), expired_keyboard(locale))
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

        period = payload.get("period") or str(payload.get("received_date", ""))[:7]
        income = await api.create_income(
            lease_id=payload.get("lease_id"),
            amount=payload.get("amount"),
            received_date=payload.get("received_date"),
            payment_method=payload.get("method"),
            description=f"rent {period}",
            # Backend-level idempotency (P0): the same guard key is sent so a
            # timeout-after-commit retry or stale card replay reuses the row
            # the backend already committed, instead of creating a second one.
            idempotency_key=key,
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
        await _edit(update, _load_error(exc.detail, locale),
                    retry_confirm_keyboard(nonce, ts, locale))


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
        await _edit(update, t("common.expired", locale), expired_keyboard(locale))
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
        await _edit(update, _load_error(exc.detail, locale),
                    retry_confirm_keyboard(nonce, ts, locale, entity="inc", ref=str(income_id)))


async def _handle_reverse(update, context, ref, nonce, ts, role, locale):
    api = context.bot_data["api_client"]
    guard = context.bot_data["idempotency"]
    settings = context.bot_data["settings"]
    if _expired(ts, settings):
        await _answer(update, t("common.expired", locale))
        await _edit(update, t("common.expired", locale), expired_keyboard(locale))
        return
    if not has_permission(role, PERMISSION_REVERSE):
        await _answer(update, t("common.no_permission", locale))
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
        # Confirm and reverse use the same Native Bot SERVICE credential. The
        # bound Telegram HUMAN subject, not a second credential, supplies the
        # Owner authority at the backend boundary.
        reversed_ = await api.reverse_income(income_id)
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
    write_states = (
        "rent_amount", "rent_date", "rent_method", "rent_confirm",
        "rent_edit", "rent_edit_amount", "rent_edit_date", "rent_edit_method",
    )
    if conv and conv["state"] in write_states:
        store.delete_conversation(chat_id, user_id)
        await _edit(update, H.escape(t("rent.cancelled", locale)), home_keyboard(locale))
    else:
        await show_dashboard(
            context, chat_id, locale,
            message_id=update.callback_query.message.message_id,
        )
    await _answer(update, t("rent.cancelled", locale))


async def _handle_detail(update, context, entity, ref, role, locale):
    if not has_read_permission(role):
        await _answer(update, t("common.no_permission", locale))
        return
    await _answer(update, "")
    if entity == "unit" and ref.isdigit():
        await pages.show_unit_page(
            context,
            update.effective_chat.id,
            update.callback_query.message.message_id,
            int(ref),
            can_rent=has_permission(role, PERMISSION_RENT_ENTRY),
            locale=locale,
            back_entity="overdue",
        )
        return
    if entity == "inc" and ref.isdigit():
        api = context.bot_data["api_client"]
        try:
            income = await api.get_income(int(ref))
        except PasayApiError as exc:
            await _edit(update, _load_error(exc.detail, locale),
                        error_keyboard("home", locale))
            return
        await _edit(update, cards.payment_detail_card(income, locale),
                    home_keyboard(locale))
        return
    await _answer(update, t("common.invalid", locale))


# --- V1.3 expense approval (exa / exr / exd) --------------------------------

async def _expense_location(update, context, expense) -> str:
    """Best-effort Property · Unit label for an expense card; empty when the
    expense has no unit or the lookup fails (expense_id stays internal)."""
    if not getattr(expense, "unit_id", None):
        return ""
    try:
        units, properties = await asyncio.gather(
            context.bot_data["api_client"].get_units(),
            context.bot_data["api_client"].get_properties(),
        )
    except PasayApiError:
        return ""
    return pages._expense_location(expense, units, properties)


async def _render_expense_state(update, context, expense, locale):
    """Render the CURRENT expense state onto the tapped card (message
    mutation). Pending -> fresh approval card; otherwise the human result card."""
    location = await _expense_location(update, context, expense)
    if (expense.status or "").lower() == "pending":
        text = cards.expense_approval_card(expense, locale, location=location)
        kb = expense_approval_keyboard(
            expense.id, locale, has_receipt=bool(expense.receipt_attachment_id)
        )
    else:
        text = cards.expense_result_card(expense, locale)
        kb = expense_result_keyboard(locale)
    await edit_message_text_or_send(
        update.get_bot(),
        chat_id=update.effective_chat.id,
        message_id=update.callback_query.message.message_id,
        text=H.truncate(text),
        parse_mode=HTML,
        reply_markup=kb,
    )


async def _handle_expense_action(
    update, context, action: str, expense_id_raw: str, nonce: str, ts, role, locale
):
    """Approve/reject core (V1.3): answer first (no loading spinner), Owner
    only, idempotency-guarded, original message mutated to the result card.
    Backend errors never destroy the original card."""
    await _answer(update, "")
    if role != Role.OWNER:
        await _answer(update, t("expense.owner_only", locale))
        return
    if not expense_id_raw.isdigit():
        await _answer(update, t("common.invalid", locale))
        return
    settings = context.bot_data["settings"]
    if _expired(ts, settings):
        await _answer(update, t("common.expired", locale))
        return
    expense_id = int(expense_id_raw)
    api = context.bot_data["api_client"]
    guard = context.bot_data["idempotency"]
    key = f"ik:exp:{action}:{expense_id}:{nonce or '0'}"
    status = guard.acquire(key, kind="expense", resource=str(expense_id))
    if status == "done":
        await _answer(update, t("expense.already_processed", locale))
        try:
            expense = await api.get_expense(expense_id)
        except PasayApiError:
            return
        await _render_expense_state(update, context, expense, locale)
        return
    if status == "in_flight":
        await _answer(update, t("common.processing", locale))
        return
    try:
        current = await api.get_expense(expense_id)
    except PasayApiError as exc:
        guard.fail(key, resource=str(expense_id))
        await _answer(update, f"⚠️ {H.escape(str(exc.detail) or '')}")
        return
    if (current.status or "").lower() != "pending":
        guard.settle(key, current.as_dict(), resource=str(expense_id))
        await _answer(update, t("expense.already_processed", locale))
        await _render_expense_state(update, context, current, locale)
        return
    try:
        if action == "approve":
            updated = await api.approve_expense(expense_id)
        else:
            updated = await api.reject_expense(expense_id)
        guard.settle(key, updated.as_dict(), resource=str(expense_id))
        await _render_expense_state(update, context, updated, locale)
        await _answer(update, "")
    except PasayApiConflictError:
        # 409 = only pending expenses can change -> processed elsewhere.
        try:
            current = await api.get_expense(expense_id)
        except PasayApiError:
            guard.fail(key, resource=str(expense_id))
            return
        guard.settle(key, current.as_dict(), resource=str(expense_id))
        await _answer(update, t("expense.already_processed", locale))
        await _render_expense_state(update, context, current, locale)
    except PasayApiTimeoutError:
        guard.fail(key, resource=str(expense_id))
        await _answer(update, t("common.timeout", locale))
    except PasayApiPermissionError:
        guard.fail(key, resource=str(expense_id))
        await _answer(update, t("common.no_permission", locale))
    except PasayApiError as exc:
        guard.fail(key, resource=str(expense_id))
        await _answer(update, f"⚠️ {H.escape(str(exc.detail) or '')}")


async def _handle_expense_approve(update, context, expense_id_raw, nonce, ts, role, locale):
    await _handle_expense_action(
        update, context, "approve", expense_id_raw, nonce, ts, role, locale
    )


async def _handle_expense_reject(update, context, expense_id_raw, nonce, ts, role, locale):
    await _handle_expense_action(
        update, context, "reject", expense_id_raw, nonce, ts, role, locale
    )


async def _handle_expense_detail(update, context, expense_id_raw, role, locale):
    """[📎 查看凭证/详情]: human-readable detail. Approve/reject stay on the
    card while the expense is still pending AND the user is the Owner."""
    await _answer(update, "")
    if not has_read_permission(role):
        await _answer(update, t("common.no_permission", locale))
        return
    if not expense_id_raw.isdigit():
        await _answer(update, t("common.invalid", locale))
        return
    api = context.bot_data["api_client"]
    try:
        expense = await api.get_expense(int(expense_id_raw))
    except PasayApiError as exc:
        await _answer(update, f"⚠️ {H.escape(str(exc.detail) or '')}")
        return
    location = await _expense_location(update, context, expense)
    text = cards.expense_detail_card(expense, locale, location=location)
    still_pending = (
        role == Role.OWNER and (expense.status or "").lower() == "pending"
    )
    kb = expense_detail_keyboard(expense.id, still_pending=still_pending, locale=locale)
    await edit_message_text_or_send(
        update.get_bot(),
        chat_id=update.effective_chat.id,
        message_id=update.callback_query.message.message_id,
        text=H.truncate(text),
        parse_mode=HTML,
        reply_markup=kb,
    )


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
    _remember_default_method(update, context, payload)
    if income is None:
        await _edit(update, H.escape(t("common.timeout", locale)), home_keyboard(locale))
        return
    if income.status == "reversed":
        text = cards.reversed_card(income, locale)
        await _edit(update, text)
        return
    if income.status == "pending":
        await _render_pending_card(update, context, income, payload, role, locale)
        return
    if payload.get("flow") == "nl":
        # Entry B exact-payment success: period + remaining balance instead of
        # the legacy success card (no income_id / raw state on screen).
        text = cards.rent_match_success_card(
            payload.get("property_name", ""),
            payload.get("unit_number", ""),
            payload.get("period", ""),
            income.amount,
            payload.get("remaining_balance") or 0,
            locale,
        )
    else:
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
    _remember_default_method(update, context, payload)
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



# --- V1.2 operations center (待办中心) -------------------------------------

def _ops_allowed(role) -> bool:
    """View/act on the operations center; the backend re-validates per task."""
    return has_permission(role, PERMISSION_OPERATIONS)


async def _handle_ops_nav(update, context, entity, role, locale):
    if not _ops_allowed(role):
        await _answer(update, t("common.no_permission", locale))
        return
    cq = update.callback_query
    if entity == OPS_OVERVIEW:
        await show_operations_center(
            context, update.effective_chat.id, locale, message_id=cq.message.message_id
        )
    else:
        await show_operations_section(
            context, update.effective_chat.id, cq.message.message_id, entity, locale
        )
    await _answer(update, "")


async def _handle_task_complete(update, context, ref, role, locale):
    if not _ops_allowed(role):
        await _answer(update, t("common.no_permission", locale))
        return
    if not ref.isdigit():
        await _answer(update, t("common.invalid", locale))
        return
    task_id = int(ref)
    api = context.bot_data["api_client"]
    try:
        task = await api.complete_operational_task(task_id)
    except PasayApiPermissionError:
        await _answer(update, t("ops.no_permission", locale))
        return
    except PasayApiError as exc:
        await _answer(update, f"⚠️ {H.escape(exc.detail)}")
        return
    text = t(
        "ops.completed_card", locale,
        title=H.escape(task.title or t("ops.task", locale)),
    )
    await _edit(update, text, ops_back_keyboard(locale))
    await _answer(update, t("ops.completed_toast", locale))


async def _handle_task_snooze(update, context, ref, role, locale):
    if not _ops_allowed(role):
        await _answer(update, t("common.no_permission", locale))
        return
    if not ref.isdigit():
        await _answer(update, t("common.invalid", locale))
        return
    text = H.escape(t("ops.snooze_title", locale))
    await _edit(update, text, snooze_preset_keyboard(int(ref), locale))


async def _handle_task_snooze_pick(update, context, entity, ref, role, locale):
    if not _ops_allowed(role):
        await _answer(update, t("common.no_permission", locale))
        return
    if not ref.isdigit():
        await _answer(update, t("common.invalid", locale))
        return
    task_id = int(ref)
    api = context.bot_data["api_client"]
    cq = update.callback_query

    if entity == "custom":
        store = context.bot_data["store"]
        user_id = update.effective_user.id if update.effective_user else None
        chat_id = update.effective_chat.id if update.effective_chat else user_id
        if user_id is not None:
            store.save_conversation(
                chat_id, user_id, "ops_snooze_custom",
                {"task_id": task_id, "message_id": cq.message.message_id},
            )
        await cq.edit_message_text(
            H.escape(t("ops.snooze_ask", locale)),
            parse_mode=HTML,
            reply_markup=edit_input_keyboard(locale),
        )
        return

    preset = SNOOZE_PRESET_MAP.get(entity)
    if preset is None:
        await _answer(update, t("common.invalid", locale))
        return
    try:
        task = await api.snooze_operational_task(task_id, preset=preset)
    except PasayApiPermissionError:
        await _answer(update, t("ops.no_permission", locale))
        return
    except PasayApiError as exc:
        await _answer(update, f"⚠️ {H.escape(exc.detail)}")
        return
    until = str(task.snoozed_until or "")[:16].replace("T", " ")
    text = t(
        "ops.snoozed_card", locale,
        title=H.escape(task.title or t("ops.task", locale)),
        until=H.escape(until),
    )
    await _edit(update, text, ops_back_keyboard(locale))
    await _answer(update, t("ops.snoozed_toast", locale))


async def _handle_task_detail(update, context, ref, role, locale):
    if not _ops_allowed(role):
        await _answer(update, t("common.no_permission", locale))
        return
    if not ref.isdigit():
        await _answer(update, t("common.invalid", locale))
        return
    api = context.bot_data["api_client"]
    try:
        task, properties = await asyncio.gather(
            api.get_operational_task(int(ref)), api.get_properties()
        )
    except PasayApiPermissionError:
        await _answer(update, t("ops.no_permission", locale))
        return
    except PasayApiError as exc:
        await _answer(update, f"⚠️ {H.escape(exc.detail)}")
        return
    text = cards.operational_task_detail_card(task, properties, locale)
    await _edit(update, text, task_action_keyboard(task.id, locale))


# --- 🤖 运营助手 (C1.1) callbacks ---------------------------------------------

async def _handle_copilot_nav(update, context, role, locale):
    """Dashboard [🤖 运营助手] button -> fast deterministic TODAY."""
    if not has_read_permission(role):
        await _answer(update, t("common.no_permission", locale))
        return
    await _answer(update, "")
    await pages.show_copilot(context, update.effective_chat.id, locale,
                             message_id=update.callback_query.message.message_id)


async def _handle_copilot_why(update, context, entity, role, locale):
    """Per-item [为什么?] -> on-demand LLM explanation (deterministic fallback)."""
    if not has_read_permission(role):
        await _answer(update, t("common.no_permission", locale))
        return
    if not entity.isdigit():
        await _answer(update, t("common.invalid", locale))
        return
    await _answer(update, "")
    await pages.show_copilot_why(
        context, update.effective_chat.id,
        update.callback_query.message.message_id, int(entity), locale,
        can_suggest=(role == Role.OWNER),
    )


async def _handle_copilot_ask(update, context, role, locale):
    """[问运营助手] -> prompt the user for a question (Q&A flow)."""
    if not has_read_permission(role):
        await _answer(update, t("common.no_permission", locale))
        return
    await _answer(update, t("copilot.ask_prompt"))
    # Capture the user's free-text question in the conversation state machine,
    # then answer via /copilot/ask when the next text message arrives.
    store = context.bot_data["store"]
    await asyncio.to_thread(
        store.save_conversation,
        update.effective_chat.id, update.effective_user.id, "copilot_ask", {},
    )
    await context.bot.send_message(
        update.effective_chat.id,
        H.escape(t("copilot.ask_prompt", locale)),
        parse_mode=HTML,
        reply_markup=expired_keyboard(locale),
    )


# --- 🤖 运营助手 · C2 confirmed-action copilot (v1.2.2) -----------------------
# Owner flow (zh): suggestion tap -> POST /recommend -> confirmation card ->
# [✅ 确认安排] -> POST /confirm + /execute -> success card. Every mutation
# needs the explicit [确认安排] tap; the secretary's English task notification
# is delivered by the backend outbox, never rendered here.

_COPILOT_DUE_PRESET_MAP = {
    "today": "today_afternoon",
    "tomorrow": "tomorrow_morning",
    "3d": "3d",
}

_COPILOT_STALE_CODES = frozenset(
    {
        "business_stale",
        "target_missing",
        "target_out_of_scope",
        "target_type_unknown",
        "action_target_illegal",
    }
)


def _copilot_allowed(role) -> bool:
    """The C2 confirm/execute surface is the OWNER flow (zh)."""
    return role == Role.OWNER


def _copilot_split_item_ref(item_ref: str) -> tuple[str, Optional[int]]:
    """'lease:3' -> ('lease', 3); anything else -> ('', None)."""
    source_type, _, raw = (item_ref or "").partition(":")
    if not raw.isdigit():
        return "", None
    return source_type, int(raw)


def _copilot_due_iso(preset_code: str) -> str:
    """Manila wall-clock due for the followup due picker (今天 17:00 /
    明天 09:00 / 3 天后), returned as UTC ISO for POST /recommend."""
    now = datetime.now(cards.MANILA_TZ)
    if preset_code == "today":
        target = now.replace(hour=17, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
    elif preset_code == "tomorrow":
        target = (now + timedelta(days=1)).replace(
            hour=9, minute=0, second=0, microsecond=0
        )
    else:  # "3d"
        target = now + timedelta(days=3)
    return target.astimezone(timezone.utc).isoformat()


def _copilot_conv(update, context) -> Optional[dict]:
    store = context.bot_data["store"]
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else chat_id
    conv = store.get_conversation(chat_id, user_id)
    if conv is None or conv["state"] != "copilot_confirm":
        return None
    return conv["payload"] or {}


def _copilot_failure_text(exc: Exception, locale: str) -> tuple[str, object]:
    """HTTP 409 error_code / detail -> human strings (contract §5). Never
    surfaces a raw error code, JSON, or internal enum."""
    code = str(getattr(exc, "error_code", None) or "")
    detail = str(getattr(exc, "detail", "") or "")
    d = detail.lower()
    if "notif" in d or "retry" in d:
        return cards.copilot_notify_retry_card(locale), copilot_back_today_keyboard(locale)
    if "already" in d or "executed" in d:
        return cards.copilot_replayed_card(locale), copilot_back_today_keyboard(locale)
    if "expired" in d or code == "proposal_expired":
        return H.escape(t("common.expired", locale)), home_keyboard(locale)
    if code in _COPILOT_STALE_CODES or "stale" in code or "target" in d:
        return cards.copilot_stale_card(locale), copilot_stale_keyboard(locale)
    # Generic fail-closed rejection: human backend message, no raw code.
    return H.escape(detail or t("common.error", locale, detail="")), copilot_back_today_keyboard(locale)


async def _render_copilot_failure(update, context, exc: Exception, locale: str):
    text, keyboard = _copilot_failure_text(exc, locale)
    await _edit(update, text, keyboard)


async def _render_copilot_confirm_card(update, context, rec: CopilotRecommend, src: dict, locale: str):
    """Render the confirmation card and persist the recommend params so the
    [✏️ 修改] pickers can re-recommend with overrides."""
    store = context.bot_data["store"]
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else chat_id
    payload = dict(src)
    payload["proposal_id"] = rec.proposal_id
    payload["assignee_name"] = (rec.card.assignee_name if rec.card else None) or ""
    store.save_conversation(chat_id, user_id, "copilot_confirm", payload)
    text = cards.copilot_suggest_card(rec, locale)
    await _edit(update, text, copilot_confirm_keyboard(rec.proposal_id, locale))


async def _re_recommend(update, context, payload: dict, overrides: dict, locale: str):
    """Re-run POST /copilot/recommend with the stored source params +
    overrides, then render the fresh confirmation card. The backend resolves
    the final values (idempotent replay when the logical request is
    unchanged); nothing executes here."""
    api = context.bot_data["api_client"]
    intent = payload.get("intent")
    try:
        if intent == "snooze":
            rec = await api.copilot_recommend(
                intent="snooze",
                task_ref=payload.get("task_ref"),
                preset=overrides.get("preset", payload.get("preset")),
                note=payload.get("note"),
            )
        elif intent == "assign":
            rec = await api.copilot_recommend(
                intent="assign",
                task_ref=payload.get("task_ref"),
                assignee_user_id=overrides.get("assignee_user_id"),
                note=payload.get("note"),
            )
        else:
            rec = await api.copilot_recommend(
                intent="followup",
                source_type=payload.get("source_type"),
                source_id=payload.get("source_id"),
                reason_code=payload.get("reason_code"),
                assignee_user_id=overrides.get("assignee_user_id"),
                due_at=overrides.get("due_at"),
                note=payload.get("note"),
            )
    except PasayApiError as exc:
        await _render_copilot_failure(update, context, exc, locale)
        return
    src = dict(payload)
    src.update(overrides)
    await _render_copilot_confirm_card(update, context, rec, src, locale)


async def _handle_copilot_suggest(update, context, entity, ref, nonce, ts, role, locale):
    """Suggestion tap -> POST /copilot/recommend -> confirmation card. The card
    is NOT executed until [✅ 确认安排] is tapped."""
    if not _copilot_allowed(role):
        await _answer(update, t("common.no_permission", locale))
        return
    if entity == "dismiss":
        # [暂不处理] on the WHY card: no-op dismiss (never mutates).
        await _answer(update, "")
        return
    settings = context.bot_data["settings"]
    if _expired(ts, settings):
        await _answer(update, t("common.expired", locale))
        await _edit(update, t("common.expired", locale), expired_keyboard(locale))
        return
    if entity not in ("follow", "snooze") or not ref.isdigit():
        await _answer(update, t("common.invalid", locale))
        return
    api = context.bot_data["api_client"]
    try:
        today = await api.copilot_today()
    except PasayApiError as exc:
        await _edit(update, _load_error(exc.detail, locale), error_keyboard("home", locale))
        return
    index = int(ref)
    items = today.top_items
    if index < 1 or index > len(items):
        await _edit(update, H.escape("⚠️ 该事项已变化，请重新进入运营助手"), home_keyboard(locale))
        return
    item = items[index - 1]
    if not (item.suggested_action or "").strip():
        await _answer(update, t("common.invalid", locale))
        return
    source_type, source_id = _copilot_split_item_ref(item.item_ref)
    if source_id is None:
        await _answer(update, t("common.invalid", locale))
        return
    note = item.suggested_action
    try:
        if entity == "follow":
            rec = await api.copilot_recommend(
                intent="followup",
                source_type=source_type,
                source_id=source_id,
                reason_code=None,
                note=note,
            )
            src = {
                "intent": "followup",
                "source_type": source_type,
                "source_id": source_id,
                "task_ref": None,
                "reason_code": None,
                "preset": None,
                "note": note,
            }
        else:  # snooze — only task items can be snoozed
            if source_type != "task":
                await _answer(update, t("common.invalid", locale))
                return
            rec = await api.copilot_recommend(
                intent="snooze",
                task_ref=source_id,
                preset="tomorrow_morning",
                note=note,
            )
            src = {
                "intent": "snooze",
                "source_type": None,
                "source_id": None,
                "task_ref": source_id,
                "reason_code": None,
                "preset": "tomorrow_morning",
                "note": note,
            }
    except PasayApiError as exc:
        await _render_copilot_failure(update, context, exc, locale)
        return
    await _answer(update, "")
    await _render_copilot_confirm_card(update, context, rec, src, locale)


async def _handle_copilot_confirm(update, context, entity, ref, nonce, ts, role, locale):
    """[✅ 确认安排] -> POST /confirm + /execute (the ONLY mutation path)."""
    if not _copilot_allowed(role):
        await _answer(update, t("common.no_permission", locale))
        return
    settings = context.bot_data["settings"]
    if _expired(ts, settings):
        await _answer(update, t("common.expired", locale))
        await _edit(update, t("common.expired", locale), expired_keyboard(locale))
        return
    if not entity.isdigit():
        await _answer(update, t("common.invalid", locale))
        return
    proposal_id = int(entity)
    guard = context.bot_data["idempotency"]
    api = context.bot_data["api_client"]
    key = f"ik:cp:exec:{proposal_id}:{nonce or '0'}"
    status = guard.acquire(key, kind="copilot", resource=str(proposal_id))
    if status == "done":
        await _edit(update, cards.copilot_replayed_card(locale), copilot_back_today_keyboard(locale))
        await _answer(update, t("copilot.executed_already", locale))
        return
    if status == "in_flight":
        await _answer(update, t("common.processing", locale))
        return
    payload = _copilot_conv(update, context) or {}
    assignee_name = str(payload.get("assignee_name") or "")
    try:
        try:
            await api.copilot_confirm(proposal_id)
        except PasayApiConflictError as exc:
            if "executed" not in str(getattr(exc, "detail", "")).lower():
                raise
            # Stale-card replay of an already-EXECUTED proposal: /execute
            # returns the idempotent replay result.
        result: CopilotExecute = await api.copilot_execute(proposal_id)
    except PasayApiConflictError as exc:
        guard.fail(key, resource=str(proposal_id))
        await _render_copilot_failure(update, context, exc, locale)
        return
    except PasayApiError as exc:
        guard.fail(key, resource=str(proposal_id))
        await _render_copilot_failure(update, context, exc, locale)
        return
    guard.settle(key, asdict(result), resource=str(proposal_id))
    if result.replay or "already" in (result.detail or "").lower():
        await _edit(update, cards.copilot_replayed_card(locale), copilot_back_today_keyboard(locale))
        await _answer(update, t("copilot.executed_already", locale))
        return
    text = cards.copilot_success_card(result, assignee_name=assignee_name, locale=locale)
    await _edit(update, text, copilot_success_keyboard(result.task_id, locale))
    await _answer(update, "")


async def _handle_copilot_decline(update, context, entity, ref, nonce, ts, role, locale):
    """[暂不处理] -> POST /proposals/{id}/cancel (no execution)."""
    if not _copilot_allowed(role):
        await _answer(update, t("common.no_permission", locale))
        return
    settings = context.bot_data["settings"]
    if _expired(ts, settings):
        await _answer(update, t("common.expired", locale))
        await _edit(update, t("common.expired", locale), expired_keyboard(locale))
        return
    if not entity.isdigit():
        await _answer(update, t("common.invalid", locale))
        return
    proposal_id = int(entity)
    guard = context.bot_data["idempotency"]
    api = context.bot_data["api_client"]
    key = f"ik:cp:cancel:{proposal_id}:{nonce or '0'}"
    status = guard.acquire(key, kind="copilot", resource=str(proposal_id))
    if status == "done":
        await _edit(update, H.escape(t("copilot.cancelled_card", locale)), copilot_back_today_keyboard(locale))
        await _answer(update, "")
        return
    if status == "in_flight":
        await _answer(update, t("common.processing", locale))
        return
    try:
        await api.copilot_cancel(proposal_id)
    except PasayApiError as exc:
        guard.fail(key, resource=str(proposal_id))
        await _render_copilot_failure(update, context, exc, locale)
        return
    guard.settle(key, {"proposal_id": proposal_id}, resource=str(proposal_id))
    await _edit(update, H.escape(t("copilot.cancelled_card", locale)), copilot_back_today_keyboard(locale))
    await _answer(update, "")


async def _handle_copilot_edit(update, context, entity, ref, nonce, ts, role, locale):
    """[✏️ 修改] -> inline pick for who/due; picks re-recommend (still PENDING,
    still needs [✅ 确认安排] to execute)."""
    if not _copilot_allowed(role):
        await _answer(update, t("common.no_permission", locale))
        return
    settings = context.bot_data["settings"]
    if _expired(ts, settings):
        await _answer(update, t("common.expired", locale))
        await _edit(update, t("common.expired", locale), expired_keyboard(locale))
        return
    if not ref.isdigit():
        await _answer(update, t("common.invalid", locale))
        return
    proposal_id = int(ref)
    payload = _copilot_conv(update, context)
    if payload is None:
        await _answer(update, t("common.expired", locale))
        await _edit(update, t("common.expired", locale), expired_keyboard(locale))
        return
    await _answer(update, "")
    if entity == "menu":
        await _edit(
            update, H.escape(t("copilot.edit_title", locale)),
            copilot_edit_menu_keyboard(proposal_id, payload.get("intent") == "snooze", locale),
        )
    elif entity == "back":
        await _re_recommend(update, context, payload, {}, locale)
    elif entity == "who":
        if payload.get("intent") == "snooze":
            await _answer(update, t("common.invalid", locale))
            return
        await _edit(update, H.escape(t("copilot.ask_who", locale)), copilot_who_keyboard(proposal_id, locale))
    elif entity == "due":
        await _edit(update, H.escape(t("copilot.ask_due", locale)), copilot_due_keyboard(proposal_id, locale))
    else:
        await _answer(update, t("common.invalid", locale))


async def _handle_copilot_snooze_pick(update, context, entity, ref, nonce, ts, role, locale):
    """Due-pick choice -> re-recommend with the preset (snooze) or a
    Manila-resolved due_at (followup) -> fresh confirmation card."""
    if not _copilot_allowed(role):
        await _answer(update, t("common.no_permission", locale))
        return
    settings = context.bot_data["settings"]
    if _expired(ts, settings):
        await _answer(update, t("common.expired", locale))
        await _edit(update, t("common.expired", locale), expired_keyboard(locale))
        return
    if not ref.isdigit():
        await _answer(update, t("common.invalid", locale))
        return
    payload = _copilot_conv(update, context)
    if payload is None:
        await _answer(update, t("common.expired", locale))
        await _edit(update, t("common.expired", locale), expired_keyboard(locale))
        return
    await _answer(update, "")
    preset = _COPILOT_DUE_PRESET_MAP.get(entity)
    if preset is None:
        await _answer(update, t("common.invalid", locale))
        return
    if payload.get("intent") == "snooze":
        await _re_recommend(update, context, payload, {"preset": preset}, locale)
    else:
        await _re_recommend(update, context, payload, {"due_at": _copilot_due_iso(entity)}, locale)


async def _handle_copilot_assignee_pick(update, context, entity, ref, nonce, ts, role, locale):
    """Who-pick choice -> re-recommend with the assignee override."""
    if not _copilot_allowed(role):
        await _answer(update, t("common.no_permission", locale))
        return
    settings = context.bot_data["settings"]
    if _expired(ts, settings):
        await _answer(update, t("common.expired", locale))
        await _edit(update, t("common.expired", locale), expired_keyboard(locale))
        return
    if not ref.isdigit():
        await _answer(update, t("common.invalid", locale))
        return
    payload = _copilot_conv(update, context)
    if payload is None:
        await _answer(update, t("common.expired", locale))
        await _edit(update, t("common.expired", locale), expired_keyboard(locale))
        return
    await _answer(update, "")
    api = context.bot_data["api_client"]
    if entity == "me":
        try:
            me = await api.get_me()
        except PasayApiError as exc:
            await _render_copilot_failure(update, context, exc, locale)
            return
        assignee_user_id = int(me.get("id") or 0)
    elif entity == "sec":
        assignee_user_id = None  # backend resolves the default secretary
    else:
        await _answer(update, t("common.invalid", locale))
        return
    await _re_recommend(update, context, payload, {"assignee_user_id": assignee_user_id}, locale)


async def _handle_copilot_recommend_back(update, context, role, locale):
    """[返回今日重点] / [刷新最新状态] -> deterministic TODAY card."""
    await _handle_copilot_nav(update, context, role, locale)
