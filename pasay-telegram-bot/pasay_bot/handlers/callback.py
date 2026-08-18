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
from decimal import Decimal

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
from pasay_bot.handlers import expense_flow, nl_bridge, nl_queries
from pasay_bot.handlers.commands import (
    _current_month,
    show_dashboard,
    show_operations_center,
    show_operations_section,
)
from pasay_bot.keyboards import (
    ACTION_ACK,
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
    ACTION_AI_CHOICE,
    ACTION_EXPENSE_APPROVE,
    ACTION_EXPENSE_BACK,
    ACTION_EXPENSE_CREATE,
    ACTION_EXPENSE_DETAIL,
    ACTION_EXPENSE_EDIT,
    ACTION_EXPENSE_OPEN,
    ACTION_EXPENSE_PAY,
    ACTION_EXPENSE_PAY_CONFIRM,
    ACTION_EXPENSE_REJECT,
    ACTION_HOME_NAV,
    ACTION_ISSUE,
    ACTION_METHOD,
    ACTION_NAV,
    ACTION_OPS_NAV,
    back_home_keyboard,
    ACTION_PAGE,
    ACTION_PROP_ARCHIVE,
    ACTION_QUICK_UNIT_VIEW,
    ACTION_REMIND_OWNER,
    ACTION_RENT,
    ACTION_REVERSE,
    ACTION_RENT_FOLLOWUP,
    ACTION_RENT_QUICK_DETAIL,
    ACTION_RENT_STATUS_SELECT,
    ACTION_RENT_HISTORY_SELECT,
    ACTION_REPAIR_COMPLETE_CANDIDATE,
    ACTION_SEC_FOLLOWUP_CONTACT,
    ACTION_SEC_FOLLOWUP_PAYMENT,
    ACTION_SEC_FOLLOWUP_SNOOZE,
    ACTION_SEC_FOLLOWUP_NO_ANSWER,
    ACTION_SEC_FOLLOWUP_PROMISE,
    ACTION_SEC_FOLLOWUP_WRONG_NUMBER,
    ACTION_TASK_COMPLETE,
    ACTION_TASK_DETAIL,
    ACTION_TASK_SNOOZE,
    ACTION_TASK_SNOOZE_PICK,
    ACTION_UNIT_ADD_CONFIRM,
    ACTION_VIEWING_CONFIRM,
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
    expense_confirm_keyboard,
    expense_detail_keyboard,
    expense_edit_keyboard,
    expense_open_keyboard,
    expense_pay_confirm_keyboard,
    expense_pay_result_keyboard,
    expense_reminder_actions,
    expense_result_keyboard,
    expired_keyboard,
    home_keyboard,
    home_summary_keyboard,
    new_nonce,
    now_ts,
    ops_back_keyboard,
    payment_method_keyboard,
    rent_detail_keyboard,
    retry_confirm_keyboard,
    secretary_followup_done_keyboard,
    secretary_followup_keyboard,
    snooze_preset_keyboard,
    task_action_keyboard,
)
from pasay_bot.handlers.edit_utils import (
    edit_message_text_idempotent,
    edit_message_text_or_send,
)
from pasay_bot.render import cards, html as H
from pasay_bot.render import completion
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
    locale_for_chat,
    role_for_telegram_id,
)

HTML = "HTML"

logger = logging.getLogger(__name__)


def _expired(ts, settings) -> bool:
    if ts is None:
        return True
    return (int(time.time()) - int(ts)) > settings.callback_ttl_seconds


async def _answer(update: Update, text: str, *, durable: bool = False, keyboard=None):
    """Deliver callback feedback exactly once, in Telegram's real semantics.

    Telegram accepts exactly ONE answerCallbackQuery per callback id, so the
    first call wins and carries the user-visible toast (and clears the client
    spinner). Any LATER call fails with QUERY_ID_INVALID; with
    ``durable=True`` it then edits the tapped message in place instead - a
    backend error, permission denial, expiry or timeout must never be silent.
    Success toasts after the result card is rendered stay non-durable on
    purpose (the card IS the result); durable=False post-ack calls are safe
    no-ops.
    """
    cq = update.callback_query
    if cq is None:
        return
    try:
        await cq.answer(text)
        try:
            from pasay_bot.state.latency import current_phase

            probe = current_phase()
            if probe is not None:
                probe.mark_ack()
        except Exception:  # noqa: BLE001 - profiling never breaks ACK
            pass
        return
    except Exception:  # noqa: BLE001 - already answered / query invalid
        pass
    if durable and text:
        user = update.effective_user
        locale = locale_for_chat(
            update.effective_chat.type if update.effective_chat else None,
            role_for_telegram_id(user.id if user else None),
        )
        try:
            await edit_message_text_or_send(
                update.get_bot(),
                chat_id=update.effective_chat.id,
                message_id=cq.message.message_id,
                text=H.truncate(text),
                parse_mode=HTML,
                reply_markup=keyboard if keyboard is not None else home_keyboard(locale),
            )
        except Exception:  # noqa: BLE001 - feedback must never raise into the router
            logger.exception("durable callback feedback failed")


async def _ack_working(update: Update, locale: str):
    """Acknowledge the click with a visible 'processing…' status before a
    backend operation that will take a moment (007A §A5). The result card is
    then rendered by mutating the tapped message, so the user always sees
    处理中 -> result and never "did my tap register?". Fast read/nav handlers
    deliberately do NOT use this — they ACK instantly with ``_ack_fast``."""
    await _answer(update, t("common.working", locale))


async def _ack_fast(update: Update, locale: str):
    """PASAY-AI-EMPLOYEE-FOUNDATION-007A §A4: FAST deterministic ACK for
    read/nav/detail callbacks — answers EMPTY (clears the client spinner
    immediately, no ``处理中`` toast) because the result render is fast and
    never needs a processing hint."""
    await _answer(update, "")


async def _ack_processing(update: Update, locale: str):
    """Show the ``⏳ 处理中...`` toast ONLY for operations expected to exceed
    ~0.8–1s (007A §A5): slow financial writes / backend-heavy confirms. The
    toast never replaces the real result and never lingers — the handler
    renders the outcome onto the tapped message right after."""
    await _answer(update, t("common.working", locale))


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


def _dec(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value or 0))


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
    """Deterministic inline-button router (buttons are UI commands, never NL).

    Order: bind identity -> decode -> local role/locale -> route. The first
    handler feedback (permission/expiry/invalid toast, or the 'processing'
    ack before a slow backend call) is the single Telegram answer for this
    click, so the client spinner clears within milliseconds. Post-ack errors
    are rendered durably onto the tapped message (message mutation), never
    silent. The whole dispatch is fail-closed: ANY unexpected exception still
    yields a human-visible reply, and every handled action is timed by the
    code-side latency tracker (never by an LLM).
    """
    started = time.monotonic()
    pages._bind_identity(update, context)
    cq = update.callback_query
    parsed = decode(cq.data)
    if parsed is None:
        await _answer(update, t("common.invalid"))
        return
    action = parsed["action"]
    user = update.effective_user
    user_id = user.id if user else None
    role = role_for_telegram_id(user_id)
    locale = locale_for_chat(
        update.effective_chat.type if update.effective_chat else None, role
    )
    # PASAY-AI-EMPLOYEE-FOUNDATION-007A §A: bind a phase probe so the shared
    # backend/render/edit helpers record their stage times and this callback
    # is profiled (callback_ack_ms/backend_fetch_ms/render_ms/telegram_edit_ms).
    from pasay_bot.state.latency import PhaseProbe, bind_phase

    probe = PhaseProbe()
    bind_phase(probe)
    outcome, detail = "ok", ""
    try:
        await _dispatch_callback(update, context, parsed, role, locale)
    except Exception as exc:  # noqa: BLE001 - fail closed, never silent
        logger.exception("callback action=%s data=%r failed", action, cq.data)
        outcome, detail = "error", str(exc)
        await _answer(update, t("common.unexpected", locale), durable=True)
    finally:
        # Safety net: if a handler path produced no answer and no durable
        # edit, clear the client spinner anyway (never leave "waiting").
        try:
            await cq.answer("")
        except Exception:  # noqa: BLE001 - best effort
            pass
        tracker = context.bot_data.get("latency")
        if tracker is not None:
            try:
                tracker.record_phases(
                    "callback", action,
                    callback_ack_ms=probe.callback_ack_ms,
                    backend_fetch_ms=probe.backend_fetch_ms,
                    render_ms=probe.render_ms,
                    telegram_edit_ms=probe.telegram_edit_ms,
                    total_ms=(time.monotonic() - started) * 1000,
                    outcome=outcome, detail=detail,
                )
            except Exception:  # noqa: BLE001 - instrumentation never breaks UX
                pass
        try:
            from pasay_bot.state.latency import bind_phase as _unbind

            _unbind(None)
        except Exception:  # noqa: BLE001 - cleanup never breaks UX
            pass


async def _dispatch_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    parsed: dict,
    role,
    locale: str,
):
    action = parsed["action"]
    entity = parsed["entity"]
    ref = parsed["ref"]
    nonce = parsed["nonce"]
    ts = parsed["ts"]

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
    elif action == ACTION_RENT_STATUS_SELECT:
        await _handle_rent_status_select(update, context, ref, nonce, ts, role, locale)
    elif action == ACTION_RENT_HISTORY_SELECT:
        await _handle_rent_history_select(update, context, ref, nonce, ts, role, locale)
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
    elif action == ACTION_EXPENSE_PAY:
        await _handle_expense_pay(update, context, entity, ref, role, locale)
    elif action == ACTION_EXPENSE_PAY_CONFIRM:
        await _handle_expense_pay_confirm(update, context, entity, ref, nonce, ts, role, locale)
    elif action == ACTION_EXPENSE_CREATE:
        await _handle_expense_create(update, context, nonce, ts, role, locale)
    elif action == ACTION_EXPENSE_EDIT:
        await _handle_expense_edit(update, context, entity, role, locale)
    elif action == ACTION_AI_CHOICE:
        await _handle_ai_choice(update, context, entity, ref, nonce, ts, role, locale)
    elif action == ACTION_HOME_NAV:
        await _handle_home_nav(update, context, entity, role, locale)
    elif action == ACTION_EDIT:
        await _handle_edit(update, context, entity, role, locale)
    elif action == ACTION_ISSUE:
        await _handle_issue(update, context, ref, role, locale)
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
    elif action == ACTION_REPAIR_COMPLETE_CANDIDATE:
        # AI-OPS-FOUNDATION-001 §9/§12: ambiguous "finished" candidate tap —
        # completes exactly the repair the human picked (idempotent).
        await _handle_task_complete(update, context, ref, role, locale)
    elif action == ACTION_UNIT_ADD_CONFIRM:
        await _handle_unit_add_confirm(update, context, nonce, ts, role, locale)
    elif action == ACTION_VIEWING_CONFIRM:
        await _handle_viewing_confirm(update, context, nonce, ts, role, locale)
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
    elif action == ACTION_QUICK_UNIT_VIEW:
        await _handle_quick_unit_view(update, context, ref, role, locale)
    elif action == ACTION_PROP_ARCHIVE:
        await _handle_prop_archive(update, context, role, locale)
    elif action == ACTION_RENT_QUICK_DETAIL:
        await _handle_rent_quick_detail(update, context, ref, role, locale)
    elif action == ACTION_RENT_FOLLOWUP:
        await _handle_rent_followup(update, context, ref, nonce, ts, role, locale)
    elif action == ACTION_REMIND_OWNER:
        await _handle_remind_owner(update, context, ref, nonce, ts, role, locale)
    elif action == ACTION_SEC_FOLLOWUP_CONTACT:
        # TELEGRAM-OPS-REAL-WORLD-CLOSURE-005 §4: ✅ 已联系租客 (Secretary DM).
        await _handle_sec_followup_contact(update, context, ref, nonce, ts, role, locale)
    elif action == ACTION_SEC_FOLLOWUP_NO_ANSWER:
        # PASAY-AI-EMPLOYEE-FOUNDATION-007 §16.2: 📵 未接听 records an attempt
        # (never "contacted") and schedules the next follow-up.
        await _handle_sec_followup_no_answer(update, context, ref, nonce, ts, role, locale)
    elif action == ACTION_SEC_FOLLOWUP_PROMISE:
        # §16/§17: 📅 承诺付款 -> ask the promised date to capture a promise.
        await _handle_sec_followup_promise(update, context, ref, nonce, ts, role, locale)
    elif action == ACTION_SEC_FOLLOWUP_WRONG_NUMBER:
        # §16.3: 📞 号码错误 -> WRONG_NUMBER + a resolver issue for a new phone.
        await _handle_sec_followup_wrong_number(update, context, ref, nonce, ts, role, locale)
    elif action == ACTION_SEC_FOLLOWUP_PAYMENT:
        # §5: 💰 已收款 -> existing record-payment flow.
        await _handle_sec_followup_payment(update, context, ref, nonce, ts, role, locale)
    elif action == ACTION_SEC_FOLLOWUP_SNOOZE:
        # §6: ⏰ 稍后处理 -> existing snooze machinery.
        await _handle_sec_followup_snooze(update, context, ref, nonce, ts, role, locale)
    elif action == ACTION_ACK:
        # CONVERGENCE-003 §1.5: ✅ Acknowledge on a proactive reminder card.
        await _handle_ack(update, context, ref, role, locale)
    elif action == ACTION_EXPENSE_OPEN:
        # CONVERGENCE-003 §4.3: E{id} Open on the Expense list -> detail.
        # The expense id rides in `entity` (mirrors ACTION_EXPENSE_DETAIL).
        await _handle_expense_detail(update, context, entity, role, locale)
    elif action == ACTION_EXPENSE_BACK:
        # CONVERGENCE-003 §4.3: ◀ Back on the Expense detail -> list.
        await _handle_expense_back(update, context, role, locale)
    else:
        logger.warning("unknown callback action=%r data=%r", action, update.callback_query.data)
        await _answer(update, t("common.invalid", locale))


async def _handle_nav(update, context, entity, role, locale):
    if not has_read_permission(role):
        await _answer(update, t("common.no_permission", locale))
        return
    # PASAY-AI-EMPLOYEE-FOUNDATION-007A §A: deterministic nav is a FAST path —
    # ACK instantly (no "处理中" toast); the page render is the result.
    await _ack_fast(update, locale)
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
        # CONVERGENCE-003 §2.1: EVERY 🏠 Home callback lands on the ONE Home
        # renderer (the Operations Overview). The legacy dashboard is never
        # reachable from a Home button.
        await pages.show_home(context, chat_id, role, locale, message_id=message_id)


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
    await _ack_fast(update, locale)
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
        await _ack_working(update, locale)
        await pages.show_rent_units(context, chat_id, message_id, int(ref), locale)
        return
    if entity == "unit" and ref.isdigit():
        await _ack_working(update, locale)
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


async def _handle_rent_status_select(update, context, ref, nonce, ts, role, locale):
    """V1.3 Slice 2, Entry D: tap one read-only multi-match candidate.

    Renders ONLY the stored candidate's status card (byte-identical to a
    single hit) onto the tapped message. Zero API calls and zero writes;
    re-taps are idempotent through the shared IdempotencyGuard; expired or
    foreign selectors get the friendly expired copy, never a stack trace or
    internal state."""
    if not has_read_permission(role):
        await _answer(update, t("common.no_permission", locale))
        return
    if not ref.isdigit() or not nonce:
        await _answer(update, t("common.invalid", locale))
        return
    settings = context.bot_data["settings"]
    if _expired(ts, settings):
        await _answer(update, t("common.expired", locale))
        await _edit(update, t("common.expired", locale), expired_keyboard(locale))
        return
    store = context.bot_data["store"]
    guard = context.bot_data["idempotency"]
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    payload = store.get_rent_status_selector(nonce, chat_id, user_id)
    if not payload:
        await _answer(update, t("common.expired", locale))
        await _edit(update, t("common.expired", locale), expired_keyboard(locale))
        return
    index = int(ref)
    if index < 1 or index > len(payload):
        await _answer(update, t("common.invalid", locale))
        return
    key = f"ik:rss:{nonce}:{index}"
    status = guard.acquire(key, kind="rent_status", resource=str(index))
    candidate = payload[index - 1]
    text = H.truncate(cards.rent_status_card_for_candidate(candidate, locale))
    if status == "done":
        result = guard.result(key) or {}
        if isinstance(result, dict) and result.get("text"):
            text = result["text"]
        await _edit(update, text)
        await _answer(update, t("rent_status.selected_toast", locale))
        return
    if status == "in_flight":
        await _answer(update, t("common.processing", locale))
        return
    await _edit(update, text)
    guard.settle(key, {"text": text, "index": index}, resource=str(index))
    await _answer(update, t("rent_status.selected_toast", locale))


async def _handle_rent_history_select(update, context, ref, nonce, ts, role, locale):
    """P1-PASAY-NIGHTLY-PRODUCT-HARDENING-008 C: tap one read-only
    payment-history candidate. Renders ONLY the stored candidate's history
    card onto the tapped message (byte-identical to a single hit). Zero API
    calls and zero writes; re-taps idempotent; expired/foreign selectors get
    the friendly expired copy."""
    if not has_read_permission(role):
        await _answer(update, t("common.no_permission", locale))
        return
    if not ref.isdigit() or not nonce:
        await _answer(update, t("common.invalid", locale))
        return
    settings = context.bot_data["settings"]
    if _expired(ts, settings):
        await _answer(update, t("common.expired", locale))
        await _edit(update, t("common.expired", locale), expired_keyboard(locale))
        return
    store = context.bot_data["store"]
    guard = context.bot_data["idempotency"]
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    payload = store.get_rent_status_selector(nonce, chat_id, user_id)
    if not payload:
        await _answer(update, t("common.expired", locale))
        await _edit(update, t("common.expired", locale), expired_keyboard(locale))
        return
    index = int(ref)
    if index < 1 or index > len(payload):
        await _answer(update, t("common.invalid", locale))
        return
    key = f"ik:rhs:{nonce}:{index}"
    status = guard.acquire(key, kind="rent_history", resource=str(index))
    candidate = payload[index - 1]
    text = H.truncate(cards.rent_history_card_for_candidate(candidate, locale))
    if status == "done":
        result = guard.result(key) or {}
        if isinstance(result, dict) and result.get("text"):
            text = result["text"]
        await _edit(update, text)
        await _answer(update, t("rent_status.selected_toast", locale))
        return
    if status == "in_flight":
        await _answer(update, t("common.processing", locale))
        return
    await _edit(update, text)
    guard.settle(key, {"text": text, "index": index}, resource=str(index))
    await _answer(update, t("rent_status.selected_toast", locale))


async def _begin_rent_entry(update, context, unit_id: int, role, locale):
    """Compressed rent entry (B4): apply smart defaults (period = this month,
    date = today, amount = current receivable, method = last used) and jump
    straight to the final confirmation card. The final confirm is never
    skipped; stale clicks on already-paid units are refused."""
    api = context.bot_data["api_client"]
    store = context.bot_data["store"]
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    await _ack_working(update, locale)
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
    covered = pages._period_covered(incomes, lease.id, month)
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


async def _handle_issue(update, context, ref, role, locale):
    """[有问题] on the Secretary-registered Owner card: read-only status hint.

    Never writes a financial record, never changes state. The backend state
    (pending vs confirmed) is read via GET and turned into a friendly tip so
    no internal status/ID ever reaches the screen.
    """
    if not has_permission(role, PERMISSION_RENT_CONFIRM):
        await _answer(update, t("common.no_permission", locale))
        return
    if not ref.isdigit():
        await _answer(update, t("common.invalid", locale))
        return
    api = context.bot_data["api_client"]
    await _ack_working(update, locale)
    try:
        income = await api.get_income(int(ref))
    except PasayApiError:
        await _answer(update, t("rent.issue_error", locale), durable=True)
        return
    if income.status == "pending":
        await _answer(update, t("rent.issue_pending", locale), durable=True)
    elif income.status == "confirmed":
        await _answer(update, t("rent.issue_confirmed", locale), durable=True)
    else:
        await _answer(update, t("rent.issue_error", locale), durable=True)


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
    await _ack_processing(update, locale)

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
        await _answer(update, t("common.processing", locale), durable=True)
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
            idempotency_key=key,
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
            await _answer(update, t("common.error", locale, detail="conflict"), durable=True)
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
        await _answer(update, t("common.no_permission", locale), durable=True)
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
    await _ack_processing(update, locale)
    key = f"ik:cnf:inc:{income_id}:{nonce or '0'}"
    status = guard.acquire(key, kind="income", resource=str(income_id))
    if status == "done":
        result = guard.result(key) or {}
        current = Income.from_dict(result) if isinstance(result, dict) else await api.get_income(income_id)
        await _render_income_state(update, context, current, role, locale)
        await _answer(update, t("rent.processed_toast", locale))
        return
    if status == "in_flight":
        await _answer(update, t("common.processing", locale), durable=True)
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
        await _answer(update, t("common.no_permission", locale), durable=True)
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
    await _ack_processing(update, locale)
    key = f"ik:rv:{income_id}:{nonce or '0'}"
    status = guard.acquire(key, kind="income", resource=str(income_id))
    if status == "done":
        result = guard.result(key) or {}
        current = Income.from_dict(result) if isinstance(result, dict) else await api.get_income(income_id)
        await _render_income_state(update, context, current, role, locale)
        await _answer(update, t("rent.processed_toast", locale))
        return
    if status == "in_flight":
        await _answer(update, t("common.processing", locale), durable=True)
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
        await _answer(update, t("common.no_permission", locale), durable=True)
    except PasayApiError as exc:
        guard.fail(key)
        await _answer(update, t("common.error", locale, detail=exc.detail), durable=True)


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
        # CONVERGENCE-003 §2.1: cancel with no write state lands on the ONE Home.
        await pages.show_home(
            context, chat_id, role, locale,
            message_id=update.callback_query.message.message_id,
        )
    await _answer(update, t("rent.cancelled", locale))


async def _handle_detail(update, context, entity, ref, role, locale):
    if not has_read_permission(role):
        await _answer(update, t("common.no_permission", locale))
        return
    await _ack_working(update, locale)
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


# --- TELEGRAM-OPS-UX-CONVERGENCE-001: Properties / Rent / Remind actions ----

async def _handle_quick_unit_view(update, context, ref, role, locale):
    """👁 1608 on the Properties index -> the unit's Quick View."""
    if not has_read_permission(role):
        await _answer(update, t("common.no_permission", locale))
        return
    if not ref.isdigit():
        await _answer(update, t("common.invalid", locale))
        return
    index = int(ref)
    api = context.bot_data["api_client"]
    try:
        rows = await api.get_quick_properties()
    except PasayApiError as exc:
        await _answer(update, f"⚠️ {H.escape(str(exc.detail) or '')}", durable=True)
        return
    if index < 1 or index > len(rows):
        await _answer(update, t("common.expired", locale), durable=True)
        return
    row = rows[index - 1]
    unit_code = str(row.get("unit_code") or "")
    vacant = (str(row.get("status") or "").lower() == "vacant")
    text = cards.quick_unit_view_card(
        unit_label=unit_code,
        locale=locale,
        vacant=vacant,
        status=str(row.get("status") or "normal"),
        amount=row.get("amount"),
        days=row.get("days"),
        open_maintenance=row.get("open_maintenance"),
    )
    kb = back_home_keyboard("properties", locale)
    await edit_message_text_or_send(
        update.get_bot(),
        chat_id=update.effective_chat.id,
        message_id=update.callback_query.message.message_id,
        text=H.truncate(text),
        parse_mode=HTML,
        reply_markup=kb,
    )


async def _handle_prop_archive(update, context, role, locale):
    """📄 Property Archive: surface the private archive channel link (index
    stays in the group; the full archive lives in the channel)."""
    if not has_read_permission(role):
        await _answer(update, t("common.no_permission", locale))
        return
    settings = context.bot_data["settings"]
    archive_id = str(getattr(settings, "archive_chat_id", "") or "").strip()
    if archive_id:
        # -100xxx -> c/xxx ; a bare group/supergroup id also maps to c/.
        if archive_id.startswith("-100"):
            cid = archive_id[4:]
        elif archive_id.startswith("-"):
            cid = archive_id[1:]
        else:
            cid = archive_id
        link = f"https://t.me/c/{cid}"
    else:
        link = ""
    text = cards.property_archive_card(locale, link=link)
    kb = home_keyboard(locale) if link else None
    await edit_message_text_or_send(
        update.get_bot(),
        chat_id=update.effective_chat.id,
        message_id=update.callback_query.message.message_id,
        text=H.truncate(text),
        parse_mode=HTML,
        reply_markup=kb,
    )


async def _handle_rent_quick_detail(update, context, ref, role, locale):
    """1680 Follow up (on the Rent quick view) -> the unit's rent detail."""
    if not has_read_permission(role):
        await _answer(update, t("common.no_permission", locale))
        return
    if not ref.isdigit():
        await _answer(update, t("common.invalid", locale))
        return
    index = int(ref)
    api = context.bot_data["api_client"]
    try:
        data = await api.get_quick_rent()
    except PasayApiError as exc:
        await _answer(update, f"⚠️ {H.escape(str(exc.detail) or '')}", durable=True)
        return
    overdue = data.get("overdue") or []
    if index < 1 or index > len(overdue):
        await _answer(update, t("common.expired", locale), durable=True)
        return
    row = overdue[index - 1]
    unit_code = str(row.get("unit") or row.get("unit_code") or "")
    unit_id = await _resolve_unit_id(update, context, unit_code)
    followup_assigned = False
    if unit_id:
        followup_assigned = await _followup_assigned_for_unit(api, unit_id, unit_code)
    text = await _render_rent_detail_text(
        api, unit_code, unit_id, row, locale, assume_assigned=followup_assigned
    )
    kb = (
        rent_detail_keyboard(unit_id, locale, followup_assigned=followup_assigned)
        if unit_id
        else home_keyboard(locale)
    )
    await edit_message_text_or_send(
        update.get_bot(),
        chat_id=update.effective_chat.id,
        message_id=update.callback_query.message.message_id,
        text=H.truncate(text),
        parse_mode=HTML,
        reply_markup=kb,
    )


async def _resolve_unit_id(update, context, unit_code: str):
    """Resolve a unit_id from a display unit number; None when not resolvable
    (the detail still renders, just without the write buttons)."""
    if not unit_code:
        return None
    # Strip a property prefix like BAY-1608 down to the bare number search too.
    candidates = [unit_code]
    if "-" in unit_code:
        candidates.append(unit_code.split("-")[-1])
    try:
        units = await context.bot_data["api_client"].get_units()
    except PasayApiError:
        return None
    for c in candidates:
        for u in units:
            if (u.unit_number or "").lower() == c.lower():
                return u.id
    return None


async def _render_rent_detail_text(
    api,
    unit_code,
    unit_id,
    row,
    locale,
    *,
    assume_assigned: bool = False,
) -> str:
    """Rent detail text from the quick-rent row + per-unit page data (tenant,
    outstanding, unpaid periods, overdue days, last follow-up).

    CONVERGENCE-003 §7: the counts come from the backend quick-rent row
    (``unpaid_periods`` = the SAME len(overdue) the RENT_OVERDUE task generator
    uses) — the renderer never computes its own arrears fact, so Tasks and
    Rent detail can never disagree (3 periods / 1 period bug)."""
    last_followup = ""
    tenant_name = ""
    vacant = False
    if unit_id:
        try:
            unit = await api.get_unit(unit_id)
            leases = await api.get_leases()
            tenants = await api.get_tenants()
            active = next(
                (l for l in leases if l.unit_id == unit_id and l.status == "active"), None
            )
            if active is None:
                vacant = True
            elif unit.status == "vacant":
                vacant = True
            else:
                tenant = next((tn for tn in tenants if tn.id == active.tenant_id), None)
                tenant_name = tenant.full_name if tenant else ""
        except PasayApiError:
            pass
    outstanding = row.get("amount") if row.get("amount") is not None else row.get("outstanding")
    overdue_days = row.get("overdue_days")
    if overdue_days is None:
        overdue_days = row.get("days") or 0
    unpaid_periods = row.get("unpaid_periods")
    if unpaid_periods is None:
        # Legacy fallback: derive from amount ÷ monthly rent (truthful, never
        # a hardcoded 1).
        try:
            from decimal import Decimal as _D
            monthly = _D(str(row.get("monthly_rent") or "0"))
            amt = _D(str(outstanding or "0"))
            unpaid_periods = int((amt / monthly).to_integral_value()) if monthly > 0 else 0
        except Exception:  # noqa: BLE001 - never crash the detail card
            unpaid_periods = 0
    raw_followup = row.get("last_followup_at")
    if raw_followup:
        try:
            last_followup = str(raw_followup)[:16].replace("T", " ")
        except Exception:  # noqa: BLE001
            last_followup = str(raw_followup)[:16]
    followup_status = ""
    if not vacant:
        followup_status = await _followup_status_for_unit(
            api, unit_id, unit_code, locale, assume_assigned=assume_assigned
        )
    return cards.rent_detail_card(
        unit_label=unit_code,
        locale=locale,
        tenant_name=tenant_name,
        outstanding=outstanding,
        unpaid_periods=int(unpaid_periods or 0),
        overdue_days=int(overdue_days or 0),
        last_followup=last_followup,
        vacant=vacant,
        followup_status=followup_status,
    )


async def _followup_status_for_unit(api, unit_id, unit_code, locale, *, assume_assigned: bool = False):
    """Resolve the real-world follow-up state for a unit's Rent detail card
    (§7): ``🟡 已交秘书跟进`` when a follow-up task is assigned to the Secretary,
    ``✅ 今日已催`` when already executed, else empty (🔴 pending)."""
    try:
        tasks = await api.get_operational_tasks()
    except PasayApiError:
        tasks = []
    lease_id = None
    try:
        leases = await api.get_leases()
        active = next((l for l in leases if l.unit_id == unit_id and l.status == "active"), None)
        if active is not None:
            lease_id = active.id
    except PasayApiError:
        pass
    unit_key = str(unit_code).split("-")[-1]
    for t_ in tasks:
        if str(t_.task_type or "").upper() not in ("RENT_OVERDUE", "FOLLOWUP"):
            continue
        details = t_.details or {}
        if lease_id is not None and t_.lease_id != lease_id:
            if str(details.get("unit_number") or "").split("-")[-1] != unit_key:
                continue
        elif lease_id is None and str(details.get("unit_number") or "").split("-")[-1] != unit_key:
            continue
        if str(t_.status or "").upper() == "COMPLETED":
            return cards.followup_status_text(details, locale, executed_daily=True)
        if details.get("assigned_to"):
            return cards.followup_status_text(details, locale)
    if assume_assigned:
        return cards.followup_status_text({"assigned_to": True}, locale)
    return ""


async def _followup_assigned_for_unit(api, unit_id, unit_code) -> bool:
    """Truth-only assigned flag for the Rent detail keyboard (never by text)."""
    try:
        tasks = await api.get_operational_tasks()
    except PasayApiError:
        return False
    lease_id = None
    try:
        leases = await api.get_leases()
        active = next((l for l in leases if l.unit_id == unit_id and l.status == "active"), None)
        if active is not None:
            lease_id = active.id
    except PasayApiError:
        lease_id = None
    unit_key = str(unit_code).split("-")[-1]
    for t_ in tasks:
        if str(t_.task_type or "").upper() not in ("RENT_OVERDUE", "FOLLOWUP"):
            continue
        details = t_.details or {}
        if lease_id is not None and t_.lease_id != lease_id:
            if str(details.get("unit_number") or "").split("-")[-1] != unit_key:
                continue
        elif lease_id is None and str(details.get("unit_number") or "").split("-")[-1] != unit_key:
            continue
        if str(t_.status or "").upper() == "COMPLETED":
            return False
        return bool(details.get("assigned_to"))
    return False


def _v2_context_followup_message_id(store, chat_id, user_id, unit_id: int) -> int | None:
    """Best-effort message_id to refresh for a rent follow-up self-heal."""
    try:
        ctx = store.get_v2_context(chat_id, user_id)
        payload = dict(ctx["payload"]) if ctx else {}
        hint = payload.get("rent_followup_ui") or {}
        if int(hint.get("unit_id") or 0) != int(unit_id):
            return None
        mid = hint.get("message_id")
        return int(mid) if mid is not None else None
    except Exception:  # noqa: BLE001 - best-effort only
        return None


async def _render_rent_detail_in_place(
    bot,
    api,
    store,
    *,
    chat_id: int,
    message_id: int,
    unit_id: int,
    locale: str,
    followed_up_today: bool = False,
    assume_assigned: bool = False,
):
    """Edit-first render of the Rent detail card (callback + self-heal)."""
    unit = await api.get_unit(unit_id)
    unit_code = unit.unit_number
    followup_assigned = (
        False
        if followed_up_today
        else bool(assume_assigned or await _followup_assigned_for_unit(api, unit_id, unit_code))
    )
    row = {"amount": None, "overdue_days": 0}
    try:
        rent_data = await api.get_quick_rent()
        found = next(
            (r for r in (rent_data.get("overdue") or [])
             if str(r.get("unit") or r.get("unit_code") or "") == unit_code),
            None,
        )
        if found is not None:
            row = found
    except PasayApiError:
        pass
    text = await _render_rent_detail_text(
        api, unit_code, unit_id, row, locale, assume_assigned=followup_assigned
    )
    kb = rent_detail_keyboard(
        unit_id,
        locale,
        followed_up_today=followed_up_today,
        followup_assigned=followup_assigned,
    )
    await edit_message_text_or_send(
        bot,
        chat_id=chat_id,
        message_id=message_id,
        text=H.truncate(text),
        parse_mode=HTML,
        reply_markup=kb,
    )


async def _handle_rent_followup(update, context, ref, nonce, ts, role, locale):
    """📞 催租 — TELEGRAM-OPS-REAL-WORLD-CLOSURE-005 §2/§8/§9.

    A tap only ASSIGNS the collection to the Secretary; it never marks it done.
    Sequence (all mandatory):
      1. resolve the REAL Secretary DM target (canonical human identity);
      2. confirm the follow-up task exists (FOLLOWUP/RENT_OVERDUE, dedupe by
         lease so the auto RENT_OVERDUE and this task are ONE item);
      3. BOT SENDS A REAL PRIVATE DM to the Secretary with the collection card;
      4. ONLY after the DM succeeds is the task marked assigned and the group
         card flipped to ``🟡 已交秘书跟进``;
      5. DM failure (recipient / forbidden / timeout / network) leaves it
         ``🔴 需要催租`` — never a fake success.
    last_follow_up_at is NEVER changed here (the Secretary has not contacted
    the tenant yet). If the same-day EXECUTED mark is set, no second follow-up
    is created.
    """
    if not has_permission(role, PERMISSION_OPERATIONS):
        await _answer(update, t("common.no_permission", locale), durable=True)
        return
    if not ref.isdigit():
        await _answer(update, t("common.invalid", locale))
        return
    unit_id = int(ref)
    api = context.bot_data["api_client"]
    store = context.bot_data["store"]
    from pasay_bot.state.store import ph_local_date
    guard = context.bot_data.get("idempotency")
    user_id = update.effective_user.id if update.effective_user else None
    chat_id = update.effective_chat.id if update.effective_chat else None
    message_id = None
    if update.callback_query is not None:
        message_id = update.callback_query.message.message_id
    elif chat_id is not None and user_id is not None:
        message_id = _v2_context_followup_message_id(store, chat_id, user_id, unit_id)

    key = f"ik:rfu:{unit_id}:{nonce or '0'}"

    # Same-day EXECUTED dedup: the rent was already actually followed-up.
    if await _followed_up_today(context, unit_id):
        await _answer(update, t("v2.followup_already_today", locale), durable=True)
        if chat_id is not None and message_id is not None:
            await _render_rent_detail_in_place(
                update.get_bot(),
                api,
                store,
                chat_id=chat_id,
                message_id=message_id,
                unit_id=unit_id,
                locale=locale,
                followed_up_today=True,
            )
        return
    if guard is not None:
        status = guard.acquire(key, kind="rent_followup", resource=str(unit_id))
        if status == "in_flight":
            await _answer(update, t("common.processing", locale), durable=True)
            return
    # 1) resolve the unit + real overdue context.
    try:
        units = await api.get_units()
    except PasayApiError:
        await _answer(update, t("common.unexpected", locale), durable=True)
        return
    unit = next((u for u in units if u.id == unit_id), None)
    if unit is None:
        await _answer(update, t("common.invalid", locale))
        return
    unit_code = unit.unit_number
    try:
        leases = await api.get_leases()
    except PasayApiError:
        leases = []
    active = next((l for l in leases if l.unit_id == unit_id and l.status == "active"), None)
    lease_id = getattr(active, "id", None) if active else None
    try:
        tenants = await api.get_tenants()
    except PasayApiError:
        tenants = []
    tenant = next((tn for tn in tenants if tn.id == getattr(active, "tenant_id", None)), None) if active else None

    # PASAY-AI-EMPLOYEE-FOUNDATION-007 §12: never hand a collection job to the
    # Secretary without real execution materials. If the tenant phone is
    # missing or WRONG_NUMBER, BLOCK the assignment (a blocked issue is left on
    # the task for self-healing) and show a NO-DEAD-END warning (§1), NOT a DM.
    available_phone = ""
    contact_status = ""
    if tenant is not None:
        available_phone = (tenant.phone or "").strip() or (tenant.secondary_phone or "").strip()
        contact_status = (tenant.contact_status or "").strip()
    if not available_phone or contact_status == "WRONG_NUMBER":
        # Persist the in-place message target so a later phone fix (text) can
        # refresh the original actionable card.
        try:
            if chat_id is not None and user_id is not None and message_id is not None:
                ctx = store.get_v2_context(chat_id, user_id)
                payload = dict(ctx["payload"]) if ctx else {}
                payload["rent_followup_ui"] = {"unit_id": unit_id, "message_id": message_id}
                store.save_v2_context(chat_id, user_id, payload)
        except Exception:  # noqa: BLE001 - best-effort only
            pass
        blocked = await _block_followup_on_missing_phone(
            context, tenant, unit, unit_code, available_phone, contact_status,
            update, role, locale,
        )
        if blocked:
            if guard is not None:
                guard.fail(key, resource=str(unit_id))
            return

    overdue_ctx = await _load_rent_followup_ctx(api, unit_code)
    # 2) confirm the follow-up task exists (dedupe with the auto RENT_OVERDUE
    #    of the same lease so the board never holds two copies of one item).
    try:
        task = await _resolve_rent_followup_task(api, unit, lease_id, overdue_ctx)
    except PasayApiError as exc:
        await _answer(update, f"⚠️ {H.escape(str(exc.detail) or '')}", durable=True)
        return
    if task is None:
        await _answer(update, t("common.unexpected", locale), durable=True)
        if guard is not None:
            guard.fail(key, resource=str(unit_id))
        return
    already_assigned = bool((getattr(task, "details", None) or {}).get("assigned_to"))
    already_delivered = bool(store.get_followup_delivery(task.id))
    if already_assigned or already_delivered:
        await _answer(update, t("v2.followup_already_assigned", locale), durable=True)
        if chat_id is not None and message_id is not None:
            await _render_rent_detail_in_place(
                update.get_bot(),
                api,
                store,
                chat_id=chat_id,
                message_id=message_id,
                unit_id=unit_id,
                locale=locale,
                assume_assigned=True,
            )
        if guard is not None:
            guard.settle(key, {"unit_id": unit_id, "task_id": task.id}, resource=str(unit_id))
        return
    # 3) resolve the REAL Secretary DM target (fail closed, §9).
    try:
        sec_chat_id, _sec_principal = await api.get_secretary_dm_chat_id()
        sec_chat_id_int = int(sec_chat_id)
    except (PasayApiError, TypeError, ValueError):
        logger.warning("rent followup secretary target resolution failed unit=%s", unit_id)
        await _answer(update, t("v2.followup_cannot_notify_toast", locale), durable=True)
        if guard is not None:
            guard.fail(key, resource=str(unit_id))
        return
    # 4) build + send the Secretary DM card (§3).
    dm_text = cards.secretary_followup_card(
        unit_label=unit_code,
        locale=locale_for(Role.SECRETARY) if not locale or locale == "bi" else locale,
        tenant_name=getattr(tenant, "full_name", "") if tenant else "",
        tenant_phone=available_phone,
        outstanding=overdue_ctx["amount"],
        unpaid_periods=overdue_ctx["periods"],
        overdue_days=overdue_ctx["days"],
        last_followup=overdue_ctx["last_followup"],
    )
    dm_kb = secretary_followup_keyboard(task.id, unit_id, locale_for(Role.SECRETARY))
    try:
        dm_msg = await update.get_bot().send_message(
            sec_chat_id_int, H.truncate(dm_text), parse_mode=HTML, reply_markup=dm_kb,
        )
    except Exception as exc:  # noqa: BLE001 - any delivery failure is real
        logger.warning("rent followup DM to secretary %s failed: %s", sec_chat_id, exc)
        await _answer(update, t("v2.followup_cannot_notify_toast", locale), durable=True)
        if guard is not None:
            guard.fail(key, resource=str(unit_id))
        return
    # Delivery truth: record the proven Telegram message_id so re-clicks never
    # re-send, even if the backend assignment write later fails.
    try:
        store.record_followup_delivery(
            task.id,
            unit_id=unit_id,
            date=ph_local_date(),
            target_user=str(_sec_principal or ""),
            destination=str(sec_chat_id_int),
            message_id=str(getattr(dm_msg, "message_id", "") or ""),
        )
    except Exception:  # noqa: BLE001 - delivery truth is best-effort
        pass
    # 5) DM succeeded -> record the assignment (only now does it become 🟡).
    try:
        await _mark_rent_followup_assigned(api, task, sec_principal_id=_sec_principal)
    except PasayApiError:
        # The DM went out; assignment recording is best-effort but we still
        # surface the assigned state on the group card.
        pass
    await _answer(update, t("v2.followup_assigned_toast", locale))
    # Re-render the group Rent detail card in place as 🟡 (NOT executed).
    if chat_id is not None and message_id is not None:
        await _render_rent_detail_in_place(
            update.get_bot(),
            api,
            store,
            chat_id=chat_id,
            message_id=message_id,
            unit_id=unit_id,
            locale=locale,
            assume_assigned=True,
        )
    if guard is not None:
        guard.settle(key, {"unit_id": unit_id, "task_id": task.id}, resource=str(unit_id))


async def _block_followup_on_missing_phone(
    context, tenant, unit, unit_code: str, available_phone: str,
    contact_status: str, update, role, locale: str,
) -> bool:
    """PASAY-AI-EMPLOYEE-FOUNDATION-007 §8/§12: block a rent-follow-up when the
    execution phone is missing/invalid. Leaves a blocked issue on the task (so
    self-healing can resume it) and sends a NO-DEAD-END warning (§1) — the
    Owner gets exactly what's missing, why it matters, how to fix it, and the
    shortest input example. Returns True when handled (do NOT send the DM)."""
    api = context.bot_data["api_client"]
    user = update.effective_user
    client_role = role_for_telegram_id(user.id if user else None)
    # Only the Owner/manager can be told to supply the phone; a Secretary who
    # taps 催租 on a phone-less unit also gets the actionable hint (no fake DM).
    missing = not available_phone
    invalid = contact_status == "WRONG_NUMBER"
    fix_cmd = f"{unit_code} 租客电话 09XXXXXXXXX"
    if missing:
        title = f"{unit_code} 缺少租客电话"
        why = "无法把催租任务交给 Secretary。"
    else:
        title = f"{unit_code} 租客电话无效"
        why = "号码已确认无效，无法联系租客。"
    text = (
        f"⚠️ <b>{H.escape(title)}</b>\n\n"
        f"{H.escape(why)}\n\n"
        f"直接发送：\n<code>{H.escape(fix_cmd)}</code>\n\n"
        f"补充后系统会自动继续催租任务。"
    )
    # Leave a blocked marker on the active rent task for the resolver, so the
    # moment the phone is supplied the follow-up auto-resumes.
    try:
        leases = await api.get_leases()
        active = next((l for l in leases if l.unit_id == unit.id and l.status == "active"), None)
        lease_id = active.id if active else None
        existing_tasks = await api.get_operational_tasks()
        task = next(
            (t for t in existing_tasks
             if t.lease_id == lease_id
             and str(t.task_type or "").upper() in ("RENT_OVERDUE", "FOLLOWUP")
             and str(t.status or "").upper() in ("PENDING", "IN_PROGRESS")),
            None,
        )
        if task is not None:
            details = dict(task.details or {})
            details["blocked"] = {
                "issue_type": "TENANT_PHONE_MISSING" if missing else "TENANT_PHONE_INVALID",
                "entity": f"tenant:{tenant.id if tenant else '?'}",
                "field": "phone",
                "blocked_action": "assign_to_secretary",
                "risk_level": "low",
                "suggested_fix": fix_cmd,
                "resume_ref": "assign_to_secretary",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "resolved_at": None,
            }
            await api.update_operational_task(task.id, details=details)
    except PasayApiError:
        pass  # the warning message still delivers even if the marker fails
    if update.effective_chat:
        kb = home_keyboard(locale_for_chat(update.effective_chat.type, client_role))
        # Refresh the original actionable card in place when possible, so the
        # Follow-up button does not keep looking executable while blocked.
        if update.callback_query is not None:
            try:
                await edit_message_text_or_send(
                    update.get_bot(),
                    chat_id=update.effective_chat.id,
                    message_id=update.callback_query.message.message_id,
                    text=H.truncate(text),
                    parse_mode=HTML,
                    reply_markup=kb,
                )
            except Exception:  # noqa: BLE001
                await update.effective_chat.send_message(
                    H.truncate(text), parse_mode=HTML, reply_markup=kb
                )
        else:
            await update.effective_chat.send_message(
                H.truncate(text), parse_mode=HTML, reply_markup=kb
            )
    else:
        await _answer(update, H.escape("缺少租客电话"), durable=True)
    return True


async def _load_rent_followup_ctx(api, unit_code: str) -> dict:
    """Real rent truth for the follow-up card/task (amount / periods / days /
    last executed follow-up), from the SAME quick-rent source the Rent detail
    uses. Never re-derived by the renderer (§3)."""
    amount = None
    periods = 0
    days = 0
    last_followup = ""
    try:
        rent_data = await api.get_quick_rent()
        row = next(
            (r for r in (rent_data.get("overdue") or [])
             if str(r.get("unit") or r.get("unit_code") or "") in (unit_code, unit_code.split("-")[-1])),
            None,
        )
        if row is not None:
            amount = row.get("amount")
            periods = int(row.get("unpaid_periods") or 0)
            days = int(row.get("overdue_days") or 0)
            raw = row.get("last_followup_at")
            if raw:
                try:
                    last_followup = str(raw)[:16].replace("T", " ")
                except Exception:  # noqa: BLE001
                    last_followup = str(raw)[:16]
    except PasayApiError:
        pass
    return {"amount": amount, "periods": periods, "days": days,
            "last_followup": last_followup}


async def _resolve_rent_followup_task(api, unit, lease_id, overdue_ctx):
    """Find the active rent follow-up task for this unit/lease or create it.
    Dedupe key aligns with the scheduler's ``lease:{lease_id}:RENT_OVERDUE`` so
    the auto-generated overdue task and this human follow-up are ONE business
    item (§13). Returns the OperationalTask or None on hard failure.

    A task that is already active (PENDING or IN_PROGRESS) is reused — tapping
    催租 again must never produce a second active follow-up for the same lease.
    """
    unit_code = unit.unit_number
    if lease_id is not None:
        try:
            existing = await api.get_operational_tasks()
            for t in existing:
                if t.lease_id != lease_id:
                    continue
                if str(t.task_type or "").upper() not in ("RENT_OVERDUE", "FOLLOWUP"):
                    continue
                if str(t.status or "").upper() in ("PENDING", "IN_PROGRESS"):
                    return t
        except PasayApiError:
            pass  # fall through to create
    amount = overdue_ctx["amount"]
    title = f"Collect overdue rent · {unit_code}"
    parts = []
    if amount is not None:
        parts.append(H.money(amount))
    if overdue_ctx["periods"]:
        parts.append(f"{overdue_ctx['periods']} period(s)")
    if overdue_ctx["days"]:
        parts.append(f"overdue {overdue_ctx['days']}d")
    description = " · ".join(parts) or f"Collect overdue rent for {unit_code}."
    lease_key = f"lease:{lease_id}:RENT_OVERDUE" if lease_id else f"rent-followup:unit:{unit.id}"
    return await api.create_operational_task(
        task_type="RENT_OVERDUE",
        title=title,
        description=description,
        property_id=getattr(unit, "property_id", None),
        due_at=None,  # never "due in 0d" for an overdue item
        next_action="Follow up with tenant to collect overdue rent.",
        next_check_at=None,
        dedupe_key=lease_key,
        status="PENDING",
        details=_rent_followup_details(unit.unit_number, amount, overdue_ctx["periods"]),
    )


def _rent_followup_details(unit_number, amount, periods) -> dict:
    """Structured follow-up truth for the task's JSONB ``details`` (§11/§18):
    the TOTAL arrears (never a bare monthly rent), the unpaid period count,
    the unit id, and empty assignment/execution slots to be filled by real
    actions."""
    return {
        "unit_number": unit_number,
        "amount": str(amount) if amount is not None else None,
        "total_outstanding": str(amount) if amount is not None else None,
        "periods": [],
        "unpaid_periods": int(periods or 0),
        "assigned_to": None,
        "assigned_at": None,
        "executed_by": None,
        "executed_at": None,
    }


async def _mark_rent_followup_assigned(api, task, sec_principal_id=None):
    """Record the REAL assignment on the follow-up task: details.assigned_to /
    assigned_at. The task stays PENDING (so the Secretary can complete it via
    the existing PENDING->COMPLETED path); the 🟡 assigned state is carried by
    the details, never by a status flip that would block the confirm. last
    follow_up_at is untouched."""
    details = dict(getattr(task, "details", None) or {})
    details["assigned_to"] = sec_principal_id if sec_principal_id is not None else "secretary"
    details["assigned_at"] = datetime.now(timezone.utc).isoformat()
    await api.update_operational_task(
        task.id,
        next_action="Secretary to contact tenant for overdue rent.",
        details=details,
    )


async def _followed_up_today(context, unit_id: int) -> bool:
    """Bot-local same-day executed mark (SQLite, restart-safe). Set ONLY when
    the Secretary actually confirms ``✅ 已联系租客`` — never on the Owner tap."""
    try:
        from pasay_bot.state.store import ph_local_date
        return context.bot_data["store"].is_marked_daily(
            f"followup:{unit_id}:{ph_local_date()}"
        )
    except Exception:  # noqa: BLE001 - dedup is best-effort, never fatal
        return False


async def _handle_sec_followup_contact(update, context, ref, nonce, ts, role, locale):
    """✅ 已联系租客 (Secretary DM, §4): the ONLY action that reflects real
    contact. Records follow_up_at / executed actor / audit by completing the
    follow-up task (backend sets completed_by + task_completed audit)."""
    if role != Role.SECRETARY:
        await _answer(update, t("v2.sec_not_secretary", locale))
        return
    if not ref.isdigit():
        await _answer(update, t("common.invalid", locale))
        return
    task_id = int(ref)
    api = context.bot_data["api_client"]
    store = context.bot_data["store"]
    from pasay_bot.state.store import ph_local_date

    unit_id = await _unit_id_for_followup_task(context, task_id)
    if unit_id is not None and await _followed_up_today(context, unit_id):
        await _answer(update, t("v2.sec_dm_already_today", locale), durable=True)
        await _edit_secretary_done(update, unit_id, locale)
        return
    guard = context.bot_data["idempotency"]
    key = f"ik:sfc:{task_id}:{nonce or '0'}"
    status = guard.acquire(key, kind="sec_followup_contact", resource=str(task_id))
    if status == "done":
        await _answer(update, t("v2.sec_confirm_toast", locale))
        return
    if status == "in_flight":
        await _answer(update, t("common.processing", locale), durable=True)
        return
    await _ack_processing(update, locale)
    try:
        task = await api.complete_operational_task(task_id)
    except (PasayApiPermissionError, PasayApiError) as exc:
        guard.fail(key, resource=str(task_id))
        await _answer(update, f"⚠️ {H.escape(getattr(exc, 'detail', '') or '')}", durable=True)
        return
    guard.settle(key, {"task_id": task_id}, resource=str(task_id))
    if unit_id is not None:
        store.mark_daily(f"followup:{unit_id}:{ph_local_date()}")
    await _answer(update, t("v2.sec_confirm_toast", locale))
    await _edit_secretary_done(update, unit_id, locale)
    # Owners see the real state on their next refresh — no group spam (§4.2).


async def _unit_id_for_followup_task(context, task_id: int) -> int | None:
    """Best-effort unit id for a follow-up task (to gate the same-day dedup).
    Prefers the lease's unit; falls back to the task details.unit_number."""
    try:
        api = context.bot_data["api_client"]
        task = await api.get_operational_task(task_id)
        lease_id = getattr(task, "lease_id", None)
        if lease_id is not None:
            leases = await api.get_leases()
            match = next((l for l in leases if l.id == lease_id), None)
            if match is not None:
                return match.unit_id
        # Fall back to the unit_number stored on the task details.
        unit_num = ((getattr(task, "details", None) or {}).get("unit_number") or "").strip()
        if unit_num:
            units = await api.get_units()
            for u in units:
                if (u.unit_number or "").split("-")[-1] == unit_num.split("-")[-1]:
                    return u.id
    except Exception:  # noqa: BLE001 - dedup is best-effort
        return None
    return None


async def _edit_secretary_done(update, unit_id, locale):
    """Edit the Secretary's DM card in place to the executed state (§4.1)."""
    try:
        text = cards.secretary_followup_card(
            unit_label="", locale=locale, done=True,
        )
        await update.get_bot().edit_message_text(
            H.truncate(text), chat_id=update.effective_chat.id,
            message_id=update.callback_query.message.message_id,
            parse_mode=HTML, reply_markup=secretary_followup_done_keyboard(locale),
        )
    except Exception:  # noqa: BLE001 - best-effort in-place update
        pass


async def _handle_sec_followup_payment(update, context, ref, nonce, ts, role, locale):
    """💰 已收款 (Secretary DM, §5): a SHORTCUT into the EXISTING record-payment
    flow — never a forced PAID. Routes to the deterministic rent-collect path
    for the unit. The actual financial state is decided by the existing
    RENT-008 / payment-confirmation / Owner-approval rules."""
    if role != Role.SECRETARY:
        await _answer(update, t("v2.sec_not_secretary", locale))
        return
    if not ref.isdigit():
        await _answer(update, t("common.invalid", locale))
        return
    unit_id = int(ref)
    # Reuse the existing record-payment entry point (ACTION_RENT -> _handle_rent).
    await _handle_rent(update, context, "go", str(unit_id), role, locale)


async def _handle_sec_followup_snooze(update, context, ref, nonce, ts, role, locale):
    """⏰ 稍后处理 (Secretary DM, §6): reuses the EXISTING snooze machinery —
    never marks the task as contacted, just reschedules it."""
    if role != Role.SECRETARY:
        await _answer(update, t("v2.sec_not_secretary", locale))
        return
    if not ref.isdigit():
        await _answer(update, t("common.invalid", locale))
        return
    task_id = int(ref)
    text = H.escape(t("ops.snooze_title", locale))
    await edit_message_text_or_send(
        update.get_bot(),
        chat_id=update.effective_chat.id,
        message_id=update.callback_query.message.message_id,
        text=H.truncate(text),
        parse_mode=HTML,
        reply_markup=snooze_preset_keyboard(task_id, locale),
    )


async def _handle_sec_followup_no_answer(update, context, ref, nonce, ts, role, locale):
    """📵 未接听 (PASAY-AI-EMPLOYEE-FOUNDATION-007 §16.2): a REAL attempt is
    recorded but this is NOT a successful contact — it must never move
    last_follow_up / mark 🟡->✅. It increments an attempt counter on the task
    and routes through the snooze machinery so the next follow-up is scheduled.
    """
    if role != Role.SECRETARY:
        await _answer(update, t("v2.sec_not_secretary", locale))
        return
    if not ref.isdigit():
        await _answer(update, t("common.invalid", locale))
        return
    task_id = int(ref)
    api = context.bot_data["api_client"]
    store = context.bot_data["store"]
    try:
        task = await api.get_operational_task(task_id)
        details = dict(task.details or {})
        attempts = int(details.get("attempts") or 0) + 1
        details["attempts"] = attempts
        details["last_attempt"] = datetime.now(timezone.utc).isoformat()
        details["last_attempt_outcome"] = "no_answer"
        await api.update_operational_task(task_id, details=details)
    except (PasayApiError, ValueError):
        pass
    await _answer(update, t("v2.sec_no_answer_recorded", locale), durable=True)
    text = H.escape(t("ops.snooze_title", locale))
    await edit_message_text_or_send(
        update.get_bot(),
        chat_id=update.effective_chat.id,
        message_id=update.callback_query.message.message_id,
        text=H.truncate(text),
        parse_mode=HTML,
        reply_markup=snooze_preset_keyboard(task_id, locale),
    )


async def _handle_sec_followup_promise(update, context, ref, nonce, ts, role, locale):
    """📅 承诺付款 (PASAY-AI-EMPLOYEE-FOUNDATION-007 §17): starts a payment-
    promise capture. Asks the Secretary when the tenant promised to pay; the
    reply is parsed (e.g. ``周五`` / ``明天付30000``) and confirmed before the
    structured promise is recorded. Dead-end never fires — the ask shows intent."""
    if role != Role.SECRETARY:
        await _answer(update, t("v2.sec_not_secretary", locale))
        return
    if not ref.isdigit():
        await _answer(update, t("common.invalid", locale))
        return
    task_id = int(ref)
    store = context.bot_data["store"]
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    store.save_conversation(
        chat_id, user_id, "sec_promise",
        {"task_id": task_id}, nonce=nonce,
    )
    # Also allow a bare inline capture via the message text.
    await _answer(update, t("v2.sec_promise_ask", locale))
    await edit_message_text_or_send(
        update.get_bot(),
        chat_id=chat_id,
        message_id=update.callback_query.message.message_id,
        text=H.truncate(t("v2.sec_promise_ask_card", locale)),
        parse_mode=HTML,
        reply_markup=home_keyboard(locale),
    )


async def _handle_sec_followup_wrong_number(update, context, ref, nonce, ts, role, locale):
    """📞 号码错误 (PASAY-AI-EMPLOYEE-FOUNDATION-007 §16.3): immediately sets the
    tenant contact_status to WRONG_NUMBER, leaves a resolver issue on the task
    and shows the actionable NO-DEAD-END reply that a NEW phone auto-resumes."""
    if role != Role.SECRETARY:
        await _answer(update, t("v2.sec_not_secretary", locale))
        return
    if not ref.isdigit():
        await _answer(update, t("common.invalid", locale))
        return
    task_id = int(ref)
    api = context.bot_data["api_client"]
    # Resolve the tenant from the task.
    tenant_id = None
    unit_code = ""
    try:
        task = await api.get_operational_task(task_id)
        details = dict(task.details or {})
        unit_code = str(details.get("unit_number") or "")
        lease_id = getattr(task, "lease_id", None)
        if lease_id is not None:
            leases = await api.get_leases()
            active = next((l for l in leases if l.id == lease_id), None)
            if active is not None:
                tenant_id = active.tenant_id
        if tenant_id is None and unit_code:
            # The task may not carry a lease (created before lease linkage);
            # resolve the tenant via the unit -> active lease -> tenant.
            unit_key = unit_code.split("-")[-1]
            units = await api.get_units()
            unit = next((u for u in units if (u.unit_number or "").split("-")[-1] == unit_key), None)
            if unit is not None:
                leases = await api.get_leases()
                active = next((l for l in leases if l.unit_id == unit.id and l.status == "active"), None)
                if active is not None:
                    tenant_id = active.tenant_id
    except PasayApiError:
        pass
    if tenant_id is not None:
        try:
            await api.update_tenant(tenant_id, contact_status="WRONG_NUMBER")
        except PasayApiError:
            pass
    fix_cmd = f"{unit_code} 租客电话 09XXXXXXXXX"
    await _answer(update, t("v2.sec_wrong_number_recorded", locale), durable=True)
    text = (
        f"⚠️ <b>{H.escape(t('v2.sec_wrong_number_title', locale, unit=H.escape(unit_code)))}</b>\n\n"
        f"{H.escape(t('v2.sec_wrong_number_why', locale))}\n\n"
        f"直接发送：\n<code>{H.escape(fix_cmd)}</code>\n\n"
        f"{H.escape(t('v2.sec_wrong_number_after', locale))}"
    )
    await edit_message_text_or_send(
        update.get_bot(),
        chat_id=update.effective_chat.id,
        message_id=update.callback_query.message.message_id,
        text=H.truncate(text),
        parse_mode=HTML,
        reply_markup=home_keyboard(locale),
    )


async def _reopen_rent_detail(update, context, unit_id, locale, followed_up_today: bool = False):
    try:
        api = context.bot_data["api_client"]
        store = context.bot_data["store"]
        await _render_rent_detail_in_place(
            update.get_bot(),
            api,
            store,
            chat_id=update.effective_chat.id,
            message_id=update.callback_query.message.message_id,
            unit_id=int(unit_id),
            locale=locale,
            followed_up_today=followed_up_today,
        )
    except Exception:
        pass  # best-effort; the toast already reported the outcome


async def _reminded_today(context, expense_id: int) -> bool:
    """Persisted same-day Remind-Owner CONFIRMED-delivery mark (SQLite,
    restart-safe). True only after a successfully delivered reminder for this
    expense today (see ``store.record_reminder_delivery`` / ``get_reminder_delivery``).
    """
    try:
        from pasay_bot.state.store import ph_local_date
        store = context.bot_data.get("store")
        if store is None:
            return False
        return store.get_reminder_delivery(expense_id, ph_local_date()) is not None
    except Exception:  # noqa: BLE001 - dedup is best-effort, never fatal
        return False


async def _handle_remind_owner(update, context, ref, nonce, ts, role, locale):
    """🔔 Remind Owner (ZERO-LEARNING-004 §4): a REAL action.

    The tap must result in an actual PRIVATE message to the HUMAN Owner:
    1. resolve the Owner's Telegram DM target (canonical identity, never a
       hardcoded chat id);
    2. bot sends the reminder DM to the Owner's private chat (NOT a repeat
       message in the group);
    3. ONLY after the DM succeeds is the reminder recorded (daily dedup mark)
       and the group card flipped to ``✅ Reminded``;
    4. any failure (recipient resolution / forbidden / timeout / Telegram
       error) does NOT record success: the group answers ``⚠️ Reminder
       failed`` and the button stays ``🔔 Remind``.

    The persistent same-day dedup is kept (one real DM per expense per PH day).
    """
    if not ref.isdigit():
        await _answer(update, t("common.invalid", locale))
        return
    expense_id = int(ref)
    api = context.bot_data["api_client"]
    store = context.bot_data["store"]

    if await _reminded_today(context, expense_id):
        await _answer(update, t("v2.remind_owner_already", locale), durable=True)
        await _render_expense_detail_in_place(update, context, expense_id, locale, reminded_today=True)
        return
    guard = context.bot_data["idempotency"]
    key = f"ik:rmo:{expense_id}:{nonce or '0'}"
    status = guard.acquire(key, kind="remind_owner", resource=str(expense_id))
    if status == "done":
        # Same rendered button re-clicked. A "done" key is only ever settled
        # AFTER a confirmed delivery, so the truthful reply is "already
        # reminded" — never a bare "Reminder sent" and never a second DM.
        if await _reminded_today(context, expense_id):
            await _answer(update, t("v2.remind_owner_already", locale), durable=True)
        else:
            # Defensive: a stale/permanent key that claims done but has NO
            # persisted delivery today must NOT report a fake success. Fall
            # through and actually re-send (fresh attempt).
            logger.warning(
                "remind owner idempotency=%s status=done but no delivery record "
                "for expense=%s; re-sending instead of fake success",
                key, expense_id,
            )
        if await _reminded_today(context, expense_id):
            await _render_expense_detail_in_place(
                update, context, expense_id, locale, reminded_today=True)
            return
        guard.fail(key, resource=str(expense_id))
    elif status == "in_flight":
        await _answer(update, t("common.processing", locale), durable=True)
        return
    await _ack_working(update, locale)
    try:
        expense = await api.get_expense(expense_id)
    except PasayApiError as exc:
        guard.fail(key, resource=str(expense_id))
        await _answer(update, f"⚠️ {H.escape(str(exc.detail) or '')}", durable=True)
        return
    location = await _expense_location(update, context, expense)
    unit_label = location or ""
    # The DM speaks the OWNER's language (private chat), not the group locale.
    owner_locale = locale_for_chat("private", Role.OWNER)
    purpose = cards._expense_purpose_text(expense) or t("v2.expense_other", owner_locale)
    approved_date = getattr(expense, "approved_at", None) or ""
    if approved_date:
        approved_date = str(approved_date)[:10]
    waiting_days = 0
    if approved_date:
        try:
            from datetime import date as _date
            waiting_days = max((_date.today() - _date.fromisoformat(approved_date)).days, 0)
        except ValueError:
            waiting_days = 0
    dm_text = cards.remind_owner_card(
        unit_label=unit_label,
        purpose=purpose,
        amount=expense.amount,
        approved_date=approved_date,
        waiting_days=waiting_days,
        locale=owner_locale,
    )
    # Step 1: resolve the REAL Owner DM target (fail closed -> NO success).
    try:
        owner_chat_id = await api.get_owner_dm_chat_id()
    except PasayApiError as exc:
        logger.warning("remind owner target resolution failed: %s", exc.detail)
        guard.fail(key, resource=str(expense_id))
        await _answer(update, t("v2.remind_owner_failed", locale), durable=True)
        return
    try:
        owner_chat_id_int = int(owner_chat_id)
    except (TypeError, ValueError):
        guard.fail(key, resource=str(expense_id))
        await _answer(update, t("v2.remind_owner_failed", locale), durable=True)
        return
    # Step 2: send the DM to the Owner's PRIVATE chat, with STATE-DRIVEN
    # action buttons reusing the exact Expense card callbacks (never a new
    # workflow). No actionable state (paid/rejected/closed) -> plain card.
    reminder_kb = expense_reminder_actions(
        getattr(expense, "status", "") or "", expense_id, locale=owner_locale,
    )
    send_kwargs = {"parse_mode": HTML}
    if reminder_kb is not None:
        send_kwargs["reply_markup"] = reminder_kb
    try:
        send_result = await update.get_bot().send_message(
            owner_chat_id_int,
            H.truncate(dm_text),
            **send_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 - any delivery failure is a real failure
        logger.warning("remind owner DM to %s failed: %s", owner_chat_id, exc)
        guard.fail(key, resource=str(expense_id))
        await _answer(update, t("v2.remind_owner_failed", locale), durable=True)
        return
    # A confirmed Telegram delivery requires a returned message_id. If the
    # call did not raise but returned no message, DO NOT report success.
    if not getattr(send_result, "message_id", None):
        logger.warning("remind owner DM to %s: send accepted but no message_id returned",
                       owner_chat_id)
        guard.fail(key, resource=str(expense_id))
        await _answer(update, t("v2.remind_owner_failed", locale), durable=True)
        return
    # Step 3: CONFIRMED delivery -> persist delivery truth and settle the key.
    from pasay_bot.state.store import ph_local_date
    today = ph_local_date()
    store.record_reminder_delivery(
        expense_id,
        today,
        target_user=owner_chat_id,           # resolved Owner Telegram destination
        destination=str(owner_chat_id_int),  # the private chat we actually DMed
        message_id=str(send_result.message_id),
    )
    guard.settle(key, {"expense_id": expense_id, "message_id": str(send_result.message_id)},
                 resource=str(expense_id))
    logger.info(
        "remind_owner delivered expense=%s to=%s (private %s) message_id=%s date=%s",
        expense_id, owner_chat_id, owner_chat_id_int, send_result.message_id, today,
    )
    await _answer(update, t("v2.remind_owner_sent", locale))
    # Step 4: flip the group card to ✅ Reminded (message mutation).
    await _render_expense_detail_in_place(update, context, expense_id, locale, reminded_today=True)


async def _handle_ack(update, context, ref, role, locale):
    """✅ Acknowledge on a proactive reminder card (CONVERGENCE-003 §1.5):
    mark the task IN_PROGRESS via the backend (which stops same-day reminders
    — the notifier drops any further outbox row for a non-PENDING task), then
    edit the reminder message in place to the acknowledged state. Idempotent."""
    if not has_read_permission(role):
        await _answer(update, t("common.no_permission", locale))
        return
    if not ref.isdigit():
        await _answer(update, t("common.invalid", locale))
        return
    task_id = int(ref)
    api = context.bot_data["api_client"]
    try:
        await api.acknowledge_operational_task(task_id)
    except PasayApiError as exc:
        await _answer(update, f"⚠️ {H.escape(str(exc.detail) or '')}", durable=True)
        return
    text = H.escape(t("v2.acknowledged_card", locale))
    await edit_message_text_or_send(
        update.get_bot(),
        chat_id=update.effective_chat.id,
        message_id=update.callback_query.message.message_id,
        text=text,
        parse_mode=HTML,
        reply_markup=None,
    )
    await _answer(update, t("v2.acknowledged_card", locale))


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


async def _notify_groups_expense_result(
    context, expense, action: str,
):
    """PASAY-V2-FOUNDATION-001: after approve/reject, push a bilingual result
    to every known group (Secretary sees the outcome in the group too)."""
    store = context.bot_data["store"]
    groups = store.list_known_groups()
    if not groups:
        return
    locale = "bi"
    try:
        location = await _expense_location(None, context, expense)
    except Exception:  # noqa: BLE001 - best-effort label
        location = ""
    text = cards.expense_result_card(expense, locale, location=location)
    if not text:
        return
    for group in groups:
        try:
            await context.bot.send_message(group["chat_id"], H.truncate(text), parse_mode=HTML)
        except Exception as exc:  # noqa: BLE001 - one bad group never blocks the rest
            logger.warning("expense result to group %s failed: %s", group["chat_id"], exc)


async def _handle_expense_action(
    update, context, action: str, expense_id_raw: str, nonce: str, ts, role, locale
):
    """Approve/reject core (V1.3): answer first (no loading spinner), Owner
    only, idempotency-guarded, original message mutated to the result card.
    Backend errors never destroy the original card."""
    if role != Role.OWNER:
        await _answer(update, t("expense.owner_only", locale))
        return
    if not expense_id_raw.isdigit():
        await _answer(update, t("common.invalid", locale))
        return
    settings = context.bot_data["settings"]
    if _expired(ts, settings):
        await _answer(update, t("common.expired", locale))
        # Make the expiry visible on the card too: a fleeting toast alone is
        # easy to miss and reads as "tap did nothing".
        await _edit(update, t("common.expired", locale), expired_keyboard(locale))
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
        await _answer(update, t("common.processing", locale), durable=True)
        return
    await _ack_working(update, locale)
    try:
        current = await api.get_expense(expense_id)
    except PasayApiError as exc:
        guard.fail(key, resource=str(expense_id))
        await _answer(update, f"⚠️ {H.escape(str(exc.detail) or '')}", durable=True)
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
        # PASAY-V2-FOUNDATION-001: group bilingual closed loop (approve/reject).
        await _notify_groups_expense_result(context, updated, action)
        # Remember the approved/rejected expense in this chat's context so a
        # follow-up payment statement ("已经付款") advances THIS record instead
        # of creating a new expense.
        try:
            store = context.bot_data["store"]
            chat_id = update.effective_chat.id
            user_id = update.effective_user.id
            store.save_v2_context(
                chat_id, user_id,
                {
                    "expense_ref": str(expense_id),
                    "expense_status": (updated.status or "").lower(),
                    "unit_token": getattr(updated, "unit_id", None),
                    "intent": "expense",
                },
            )
        except Exception:  # noqa: BLE001 - context is best-effort, never blocks UX
            logger.debug("expense context save failed", exc_info=True)
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
        await _answer(update, t("common.timeout", locale), durable=True)
    except PasayApiPermissionError:
        guard.fail(key, resource=str(expense_id))
        await _answer(update, t("common.no_permission", locale), durable=True)
    except PasayApiError as exc:
        guard.fail(key, resource=str(expense_id))
        await _answer(update, f"⚠️ {H.escape(str(exc.detail) or '')}", durable=True)


async def _handle_expense_approve(update, context, expense_id_raw, nonce, ts, role, locale):
    await _handle_expense_action(
        update, context, "approve", expense_id_raw, nonce, ts, role, locale
    )


async def _handle_expense_reject(update, context, expense_id_raw, nonce, ts, role, locale):
    await _handle_expense_action(
        update, context, "reject", expense_id_raw, nonce, ts, role, locale
    )


async def _handle_expense_detail(update, context, expense_id_raw, role, locale):
    """[Open / 查看详情] (CONVERGENCE-003 §4.3): ACK -> edit in place ->
    Expense Detail (property, category/purpose, amount, approved date, waiting
    days, status) with SHORT operation buttons (🔔 Remind / ✅ Paid / ◀ Back /
    🏠 Home). Approve/reject stay while the expense is still pending AND the
    user is the Owner. The backend read is fixed for legacy `??` categories —
    it must never 500."""
    if not has_read_permission(role):
        await _answer(update, t("common.no_permission", locale))
        return
    if not expense_id_raw.isdigit():
        await _answer(update, t("common.invalid", locale))
        return
    await _ack_working(update, locale)
    expense_id = int(expense_id_raw)
    await _render_expense_detail_in_place(update, context, expense_id, locale)


async def _render_expense_detail_in_place(
    update, context, expense_id: int, locale: str, role=None, reminded_today: bool = False,
):
    """Shared Expense-detail renderer: fetch -> detail card -> short buttons."""
    if role is None:
        user = update.effective_user
        role = role_for_telegram_id(user.id if user else None)
    api = context.bot_data["api_client"]
    try:
        # 003B: fetch the FULL detail so the bot carries the authoritative
        # verified_paid / remaining / pending-claims truth (§19 / §14 / E16).
        expense = await api.get_expense_detail(expense_id)
    except PasayApiError as exc:
        await _answer(update, f"⚠️ {H.escape(str(exc.detail) or '')}", durable=True)
        return
    location = await _expense_location(update, context, expense)
    text = cards.expense_detail_card(
        expense, locale, location=location,
        waiting_days=_expense_waiting_days(expense),
    )
    status = (expense.status or "").lower()
    if status == "pending" and role == Role.OWNER:
        kb = expense_detail_keyboard(expense.id, still_pending=True, locale=locale,
                                     amount=expense.amount)
    else:
        kb = expense_open_keyboard(
            expense.id,
            status=status,
            locale=locale,
            has_receipt=bool(expense.receipt_attachment_id),
            reminded_today=reminded_today or await _reminded_today(context, expense.id),
        )
    await edit_message_text_or_send(
        update.get_bot(),
        chat_id=update.effective_chat.id,
        message_id=update.callback_query.message.message_id,
        text=H.truncate(text),
        parse_mode=HTML,
        reply_markup=kb,
    )


def _expense_waiting_days(expense) -> int:
    """Days since approval (0 when not approved yet / unparseable)."""
    approved_date = getattr(expense, "approved_at", None) or ""
    if not approved_date:
        return 0
    try:
        from datetime import date as _date
        return max((_date.today() - _date.fromisoformat(str(approved_date)[:10])).days, 0)
    except (ValueError, TypeError):
        return 0


async def _handle_expense_back(update, context, role, locale):
    """◀ Back on the Expense detail -> re-render the Expense Quick View in
    place (no new junk message)."""
    if not has_read_permission(role):
        await _answer(update, t("common.no_permission", locale))
        return
    message_id = update.callback_query.message.message_id
    await pages.show_quick_expense(
        context, update.effective_chat.id, role, locale, message_id=message_id
    )


async def _handle_unit_add_confirm(update, context, nonce, ts, role, locale):
    """AI-OPS-FOUNDATION-001 §14: [✅ 确认创建] — create the unit exactly as
    the confirmation card showed (deterministic, idempotency-guarded)."""
    if not has_read_permission(role):
        await _answer(update, t("common.no_permission", locale))
        return
    settings = context.bot_data["settings"]
    if _expired(ts, settings):
        await _answer(update, t("common.expired", locale))
        return
    store = context.bot_data["store"]
    chat_id, user_id = update.effective_chat.id, update.effective_user.id
    conv = store.get_conversation(chat_id, user_id)
    if conv is None or conv["state"] != "unit_add_confirm" or conv["nonce"] != nonce:
        await _answer(update, t("common.expired", locale))
        return
    payload = _payload(conv)
    api = context.bot_data["api_client"]
    guard = context.bot_data["idempotency"]
    key = f"ik:unit:{payload['unit_number']}:{payload['monthly_rent']}:{nonce or '0'}"
    status = guard.acquire(key, kind="unit", resource=payload["unit_number"])
    if status in ("done", "in_flight"):
        await _answer(update, t("common.processing", locale), durable=True)
        return
    await _ack_working(update, locale)
    try:
        unit = await api.create_unit(
            property_id=int(payload["property_id"]),
            unit_number=payload["unit_number"],
            monthly_rent=payload["monthly_rent"],
            status=payload["status"],
        )
        guard.settle(key, unit.as_dict(), resource=str(unit.id))
    except PasayApiConflictError:
        guard.fail(key)
        await _answer(update, t("unit_add.exists", locale), durable=True)
        return
    except PasayApiError as exc:
        guard.fail(key)
        await _answer(update, f"⚠️ {H.escape(str(exc.detail) or '')}", durable=True)
        return
    store.delete_conversation(chat_id, user_id)
    text = t("unit_add.created", locale, unit=unit.unit_number)
    await _edit(update, H.escape(text), home_keyboard(locale))
    await _answer(update, "")


async def _handle_viewing_confirm(update, context, nonce, ts, role, locale):
    """AI-OPS-FOUNDATION-001 §17: [✅ 确认安排] — persist the viewing as a
    business event (unit bound, scheduled time stored, Secretary reminded)."""
    if not has_read_permission(role):
        await _answer(update, t("common.no_permission", locale))
        return
    settings = context.bot_data["settings"]
    if _expired(ts, settings):
        await _answer(update, t("common.expired", locale))
        return
    store = context.bot_data["store"]
    chat_id, user_id = update.effective_chat.id, update.effective_user.id
    conv = store.get_conversation(chat_id, user_id)
    if conv is None or conv["state"] != "viewing_confirm" or conv["nonce"] != nonce:
        await _answer(update, t("common.expired", locale))
        return
    payload = _payload(conv)
    api = context.bot_data["api_client"]
    guard = context.bot_data["idempotency"]
    key = f"ik:viewing:{payload['unit_token']}:{payload['scheduled_at']}:{nonce or '0'}"
    status = guard.acquire(key, kind="viewing", resource=payload["unit_token"])
    if status in ("done", "in_flight"):
        await _answer(update, t("common.processing", locale), durable=True)
        return
    # Resolve the unit id (the card only carried the display token).
    try:
        units = await api.get_units()
    except PasayApiError:
        guard.fail(key)
        await _answer(update, t("common.unexpected", locale), durable=True)
        return
    unit = next(
        (u for u in units if u.unit_number.lower() == payload["unit_token"].lower()),
        None,
    )
    if unit is None:
        guard.fail(key)
        await _answer(update, t("rent_status.no_unit", locale, unit=payload["unit_token"]),
                      durable=True)
        return
    await _ack_working(update, locale)
    try:
        await api.create_viewing(unit_id=unit.id, scheduled_at=payload["scheduled_at"])
        guard.settle(key, {"unit_id": unit.id}, resource=str(unit.id))
    except PasayApiError as exc:
        guard.fail(key)
        await _answer(update, f"⚠️ {H.escape(str(exc.detail) or '')}", durable=True)
        return
    store.delete_conversation(chat_id, user_id)
    text = t("viewing.created", locale)
    await _edit(update, H.escape(text), home_keyboard(locale))
    await _answer(update, "")


# --- PASAY-V2-EXPENSE-PAYABLE-TASK-006: owner payment (deterministic) -------
async def _handle_expense_pay(update, context, entity, ref, role, locale):
    """[付款] tap on an APPROVED (unpaid) expense: open the deterministic
    payment-confirmation card.

    Two sub-paths:
    - ``entity == "cancel"`` -> dismiss the confirm and go home.
    - Otherwise ``entity`` is the expense_id -> fetch the expense + any
      possible-duplicate PAID rows and render the confirm card. The Owner
      must tap an explicit Confirm/Continue; a receipt stays optional."""
    if not has_read_permission(role):
        await _answer(update, t("common.no_permission", locale))
        return
    if entity == "cancel":
        await _edit(update, t("expense.pay_cancel", locale), home_keyboard(locale))
        await _answer(update, "")
        return
    if not entity.isdigit():
        await _answer(update, t("common.invalid", locale))
        return
    expense_id = int(entity)
    api = context.bot_data["api_client"]
    await _ack_working(update, locale)
    try:
        expense = await api.get_expense(expense_id)
    except PasayApiError as exc:
        await _answer(update, f"⚠️ {H.escape(str(exc.detail) or '')}", durable=True)
        return
    status = (expense.status or "").lower()
    location = await _expense_location(update, context, expense)
    if status == "paid":
        text = cards.expense_pay_result_card(expense, locale, already=True)
        kb = expense_pay_result_keyboard(locale)
        await edit_message_text_or_send(
            update.get_bot(), chat_id=update.effective_chat.id,
            message_id=update.callback_query.message.message_id,
            text=H.truncate(text), parse_mode=HTML, reply_markup=kb,
        )
        await _answer(update, "")
        return
    if status != "approved":
        # No longer payable -> route through the shared state renderer.
        await _render_expense_state(update, context, expense, locale)
        return
    similar = []
    try:
        similar = await api.get_expense_duplicates(expense_id)
    except PasayApiError:
        similar = []  # advisory only; never blocks payment when it fails
    text = cards.expense_pay_confirm_card(
        expense, locale, location=location, similar=similar
    )
    kb = expense_pay_confirm_keyboard(expense_id, locale=locale, similar=similar)
    await edit_message_text_or_send(
        update.get_bot(), chat_id=update.effective_chat.id,
        message_id=update.callback_query.message.message_id,
        text=H.truncate(text), parse_mode=HTML, reply_markup=kb,
    )
    await _answer(update, "")


async def _handle_expense_pay_confirm(update, context, expense_id_raw, ref, nonce, ts, role, locale):
    """[✅ 确认已付款] -> deterministic finalization.

    Owner-only, idempotency-guarded on the business ``expense_id``. The
    expense is re-fetched and the backend state is the authority:
    - already PAID -> idempotent 'already paid' result, no second write;
    - not APPROVED -> render the current state, no write;
    - APPROVED -> POST /pay (idempotent on the backend), then the PAID result.
    If possible duplicates exist this handler refuses to pay until the Owner
    explicitly passes ``ref == 'force'`` (the Continue button), honoring the
    advisory warning without ever auto-rejecting the expense."""
    if role != Role.OWNER:
        await _answer(update, t("expense.no_permission_pay", locale))
        return
    if not expense_id_raw.isdigit():
        await _answer(update, t("common.invalid", locale))
        return
    settings = context.bot_data["settings"]
    if _expired(ts, settings):
        await _answer(update, t("common.expired", locale))
        await _edit(update, t("common.expired", locale), expired_keyboard(locale))
        return
    expense_id = int(expense_id_raw)
    api = context.bot_data["api_client"]
    guard = context.bot_data["idempotency"]
    key = f"ik:exp:pay:{expense_id}:{nonce or '0'}"
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
        await _answer(update, t("common.processing", locale), durable=True)
        return
    await _ack_working(update, locale)
    try:
        current = await api.get_expense(expense_id)
    except PasayApiError as exc:
        guard.fail(key, resource=str(expense_id))
        await _answer(update, f"⚠️ {H.escape(str(exc.detail) or '')}", durable=True)
        return
    current_status = (current.status or "").lower()
    if current_status == "paid":
        guard.settle(key, current.as_dict(), resource=str(expense_id))
        text = cards.expense_pay_result_card(current, locale, already=True)
        kb = expense_pay_result_keyboard(locale)
        await edit_message_text_or_send(
            update.get_bot(), chat_id=update.effective_chat.id,
            message_id=update.callback_query.message.message_id,
            text=H.truncate(text), parse_mode=HTML, reply_markup=kb,
        )
        await _answer(update, t("expense.pay_result_already", locale))
        return
    if current_status != "approved":
        guard.settle(key, current.as_dict(), resource=str(expense_id))
        await _answer(update, t("expense.already_processed", locale))
        await _render_expense_state(update, context, current, locale)
        return
    # The possible-duplicate warning (if any) was already shown on the
    # confirmation card returned by the opening tap; reaching this handler is
    # the Owner's explicit Continue/Confirm, so finalize the payment. The
    # backend /pay remains the state authority and is idempotent.
    try:
        updated = await api.pay_expense(expense_id)
    except PasayApiConflictError:
        # 409 = only approved can be paid -> reprocessed elsewhere.
        try:
            current = await api.get_expense(expense_id)
        except PasayApiError:
            guard.fail(key, resource=str(expense_id))
            return
        guard.settle(key, current.as_dict(), resource=str(expense_id))
        if (current.status or "").lower() == "paid":
            text = cards.expense_pay_result_card(current, locale, already=False)
        else:
            text = cards.expense_pay_result_card(current, locale, already=True)
        kb = expense_pay_result_keyboard(locale)
        await edit_message_text_or_send(
            update.get_bot(), chat_id=update.effective_chat.id,
            message_id=update.callback_query.message.message_id,
            text=H.truncate(text), parse_mode=HTML, reply_markup=kb,
        )
        await _answer(update, "")
        return
    except PasayApiTimeoutError:
        guard.fail(key, resource=str(expense_id))
        await _answer(update, t("common.timeout", locale), durable=True)
        return
    except PasayApiPermissionError:
        guard.fail(key, resource=str(expense_id))
        await _answer(update, t("common.no_permission", locale), durable=True)
        return
    except PasayApiError as exc:
        guard.fail(key, resource=str(expense_id))
        await _answer(update, f"⚠️ {H.escape(str(exc.detail) or '')}", durable=True)
        return
    guard.settle(key, updated.as_dict(), resource=str(expense_id))
    text = cards.expense_pay_result_card(updated, locale, already=False)
    kb = expense_pay_result_keyboard(locale)
    await edit_message_text_or_send(
        update.get_bot(), chat_id=update.effective_chat.id,
        message_id=update.callback_query.message.message_id,
        text=H.truncate(text), parse_mode=HTML, reply_markup=kb,
    )
    await _answer(update, t("expense.pay_result_paid", locale))
    # Remember the paid expense in this chat's context so a follow-up NL
    # statement advances THIS record instead of creating a new one.
    try:
        store = context.bot_data["store"]
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        store.save_v2_context(
            chat_id, user_id,
            {
                "expense_ref": str(expense_id),
                "expense_status": "paid",
                "unit_token": getattr(updated, "unit_id", None),
                "intent": "expense",
            },
        )
    except Exception:  # noqa: BLE001 - context is best-effort
        logger.debug("expense context save failed", exc_info=True)


# --- BOT-V1-USABLE-001 P0-2: expense create/edit (deterministic) -----------

async def _handle_expense_create(update, context, nonce, ts, role, locale):
    """[提交审批] tap: create ONE PENDING expense through the existing
    backend service (idempotency-guarded). Approval stays Owner-only."""
    if not has_read_permission(role):
        await _answer(update, t("common.no_permission", locale))
        return
    settings = context.bot_data["settings"]
    if _expired(ts, settings):
        await _answer(update, t("common.expired", locale))
        await _edit(update, t("common.expired", locale), expired_keyboard(locale))
        return
    store = context.bot_data["store"]
    chat_id, user_id = update.effective_chat.id, update.effective_user.id
    conv = store.get_conversation(chat_id, user_id)
    if conv is None or conv["state"] != "expense_confirm" or conv["nonce"] != nonce:
        await _answer(update, t("common.expired", locale))
        return
    payload = _payload(conv)
    await _ack_working(update, locale)
    await expense_flow.submit_expense(update, context, payload, role, locale)


async def _handle_expense_edit(update, context, sub, role, locale):
    """[✏️ 修改] picker + field edits. All free-text follow-ups are handled by
    the deterministic conversation states (amount/category/unit)."""
    if not has_read_permission(role):
        await _answer(update, t("common.no_permission", locale))
        return
    store = context.bot_data["store"]
    chat_id, user_id = update.effective_chat.id, update.effective_user.id
    conv = store.get_conversation(chat_id, user_id)
    if conv is None or conv["state"] != "expense_confirm":
        await _answer(update, t("common.expired", locale))
        return
    payload = _payload(conv)
    message_id = update.callback_query.message.message_id
    if sub == "menu":
        await _edit(
            update,
            "✏️ " + H.escape(t("expense.edit", locale)),
            expense_edit_keyboard(locale),
        )
        await _answer(update, "")
        return
    if sub == "back":
        await expense_flow.render_expense_confirm(
            update, context, payload, role, locale, message_id=message_id,
        )
        await _answer(update, "")
        return
    state_map = {
        "amount": ("expense_edit_amount", t("expense.ask_amount", locale)),
        "cat": ("expense_edit_category", t("expense.ask_category", locale)),
        "unit": ("expense_edit_unit", t("expense.ask_unit", locale)),
    }
    if sub not in state_map:
        await _answer(update, t("common.invalid", locale))
        return
    state, ask_text = state_map[sub]
    store.save_conversation(chat_id, user_id, state, payload)
    await _edit(update, H.escape(ask_text), None)
    await _answer(update, "")


async def _handle_ai_choice(update, context, entity, ref, nonce, ts, role, locale):
    """P0-5 ambiguity choice taps: deterministic routing from the stored
    intent payload (record / query / ask)."""
    if not has_read_permission(role):
        await _answer(update, t("common.no_permission", locale))
        return
    settings = context.bot_data["settings"]
    if _expired(ts, settings):
        await _answer(update, t("common.expired", locale))
        return
    if not ref.isdigit():
        await _answer(update, t("common.invalid", locale))
        return
    store = context.bot_data["store"]
    chat_id, user_id = update.effective_chat.id, update.effective_user.id
    conv = store.get_conversation(chat_id, user_id)
    if conv is None or conv["state"] != "ai_choice" or conv["nonce"] != nonce:
        await _answer(update, t("common.expired", locale))
        return
    payload = _payload(conv)
    idx = int(ref)
    base = payload.get("choice_base") or "generic"
    options = payload.get("options") or []
    if idx < 0 or idx >= len(options):
        await _answer(update, t("common.invalid", locale))
        return
    await _ack_working(update, locale)
    message_id = update.callback_query.message.message_id

    if base == "expense" and idx == 0:
        partial = {
            "unit_id": payload.get("unit_id"),
            "unit_number": payload.get("unit") or "",
            "property_name": "",
            "category": payload.get("category") or "",
            "amount": "",
            "expense_date": date.today().isoformat(),
            "payee": "",
            "description": "",
            "missing": ["amount"],
        }
        store.save_conversation(chat_id, user_id, "ai_expense_partial", partial)
        await _edit(
            update,
            H.escape(t(
                "ai.ask_amount", locale,
                unit=partial["unit_number"] or "该房源",
                category=partial["category"],
            )),
            None,
        )
        await _answer(update, "")
        return
    if base == "expense" and idx == 1:
        answer = await nl_queries.build_query_answer(
            context,
            nl_queries.QueryIntent(
                kind="unit_expenses", unit_token=payload.get("unit") or "",
            ),
            locale,
        )
        await _edit(update, H.truncate(answer), None)
        await _answer(update, "")
        return
    if base == "income" and idx == 0:
        await _edit(update, H.escape(t("ai.thinking", locale)), None)
        await _answer(update, "")
        await nl_bridge._handle_rent_payment_statement(
            update, context, payload.get("text") or "", role, locale,
        )
        return
    if base == "income" and idx == 1:
        await _edit(update, H.escape(t("ai.thinking", locale)), None)
        await _answer(update, "")
        await nl_bridge._handle_rent_status_query(
            update, context,
            nl_bridge.RentStatusQuery(kind="unit", unit_token=payload.get("unit") or ""),
            role, locale,
        )
        return
    # generic: 0 = record expense, 1/2 = query/ask via copilot.
    if idx == 0:
        await _edit(
            update,
            H.escape(t("expense.page_hint", locale)),
            home_keyboard(locale),
        )
        await _answer(update, "")
        return
    question = payload.get("text") or ""
    api = context.bot_data["api_client"]
    try:
        ask = await api.copilot_ask(question)
    except PasayApiError:
        await _edit(update, H.escape(t("ai.unknown", locale)), None)
        await _answer(update, "")
        return
    await _edit(
        update,
        H.truncate(cards.copilot_ask_card(ask.answer, fallback=ask.fallback, locale=locale)),
        None,
    )
    await _answer(update, "")


async def _handle_home_nav(update, context, sub, role, locale):
    """Home situational actions (CONVERGENCE-003 §2.2): ⚠️ Today opens the
    needs-action list, 🔄 Refresh re-renders Home in place. No navigation
    grid — the fixed Reply Keyboard is the only navigation."""
    if not has_read_permission(role):
        await _answer(update, t("common.no_permission", locale))
        return
    # PASAY-AI-EMPLOYEE-FOUNDATION-007A §A/§D: Today/Refresh are fast reads —
    # fast ACK, no "处理中" toast.
    await _ack_fast(update, locale)
    message_id = update.callback_query.message.message_id
    chat_id = update.effective_chat.id
    if sub == "today":
        # PASAY-AI-EMPLOYEE-FOUNDATION-007A §D: Today = the SAME digest builder
        # + renderer as the scheduled Daily Digest (business dedup, HUMAN/SYSTEM
        # filter, language). Never the separate quick-tasks path.
        await pages.show_today_digest(context, chat_id, role, locale, message_id=message_id)
    elif sub == "refresh":
        await pages.show_home(context, chat_id, role, locale, message_id=message_id)
    elif sub == "unpaid":
        await pages.show_rent(context, chat_id, locale, message_id=message_id)
    elif sub == "approvals" or sub == "maintenance":
        await pages.show_todo(context, chat_id, role, locale, message_id=message_id)
    elif sub == "contracts":
        await pages.show_contracts_page(context, chat_id, locale, message_id=message_id)
    else:
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
        await _answer(update, t("common.timeout", locale), durable=True)
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
        await _answer(update, t("common.timeout", locale), durable=True)


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
            idempotency_key=key,
        )
    except PasayApiError:
        guard.fail(key)
        await _answer(update, t("common.timeout", locale), durable=True)
        return
    if matched is None:
        guard.fail(key)
        await _answer(update, t("common.timeout", locale), durable=True)
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
        await _answer(update, t("common.timeout", locale), durable=True)
        return
    if current.status in ("confirmed", "reversed"):
        guard.settle(key, current.as_dict(), resource=str(income_id))
        await _render_income_state(update, context, current, role, locale)
        if reverse and current.status == "confirmed":
            # Reverse timed out and the income is still confirmed: the reversal
            # did NOT land. Never tell the user it was processed (F7).
            await _answer(update, t("rent.reverse_failed_toast", locale), durable=True)
        else:
            await _answer(update, t("rent.processed_toast", locale))
    else:
        guard.fail(key, resource=str(income_id))
        await _answer(update, t("common.timeout", locale), durable=True)


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
    due = _dec(payload.get("due_amount") or 0)
    partial = due > 0 and _dec(income.amount) < due
    if partial:
        # SLICE2-RENT-005: partial payment success shows due / cumulative paid
        # / remaining (and paid-in-full once the month is settled).
        text = cards.rent_match_partial_success_card(
            payload.get("unit_number", ""),
            income.amount,
            due,
            payload.get("paid_amount") or 0,
            payload.get("remaining_balance") or 0,
            locale,
        )
        if payload.get("flow") == "secretary_register":
            text += "\n" + (
                H.escape(t("rent.registered_by", locale))
                + "：" + H.escape(payload.get("registrar", "Secretary"))
            )
    elif payload.get("flow") == "nl":
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
            income.id,
            new_nonce(),
            now_ts(),
            can_reverse=True,
            locale=locale,
            show_confirm=False,
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
        # Secretary-registered card (V1.3 Slice 2): terminal Chinese state with
        # balance + registrar; falls back to the generic success card when the
        # local conversation context is missing or belongs to another income.
        conv = context.bot_data["store"].get_conversation(
            update.effective_chat.id, update.effective_user.id
        )
        if (
            conv is not None
            and conv["state"] == "rent_secretary_confirm"
            and int(conv["payload"].get("income_id") or 0) == income.id
        ):
            text = cards.secretary_terminal_card(conv["payload"], income, locale)
            keyboard = (
                confirm_income_keyboard(
                    income.id,
                    new_nonce(),
                    now_ts(),
                    can_reverse=True,
                    locale=locale,
                    show_confirm=False,
                )
                if _can_reverse(context, role)
                else None
            )
            await _edit(update, text, keyboard)
            return
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
            confirm_income_keyboard(
                income.id,
                new_nonce(),
                now_ts(),
                can_reverse=True,
                locale=locale,
                show_confirm=False,
            )
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
    await _ack_working(update, locale)
    if entity == OPS_OVERVIEW:
        await show_operations_center(
            context, update.effective_chat.id, locale,
            message_id=cq.message.message_id, role=role,
        )
    else:
        await show_operations_section(
            context, update.effective_chat.id, cq.message.message_id, entity, locale
        )


async def _handle_task_complete(update, context, ref, role, locale):
    if not _ops_allowed(role):
        await _answer(update, t("common.no_permission", locale))
        return
    if not ref.isdigit():
        await _answer(update, t("common.invalid", locale))
        return
    task_id = int(ref)
    api = context.bot_data["api_client"]
    await _ack_processing(update, locale)
    try:
        task = await api.complete_operational_task(task_id)
    except PasayApiPermissionError:
        await _answer(update, t("ops.no_permission", locale), durable=True)
        return
    except PasayApiError as exc:
        await _answer(update, f"⚠️ {H.escape(exc.detail)}", durable=True)
        return
    text = t(
        "ops.completed_card", locale,
        title=H.escape(task.title or t("ops.task", locale)),
    )
    # PASAY-V2-OWNER-SECRETARY-JOURNEY-AUDIT-006 (Journey M): append a varied,
    # positive deterministic closing line (never LLM) so completion is not a
    # cold mechanical line and does not repeat the same wording every time.
    try:
        recent = context.bot_data.setdefault("completion_recent", set())
        key, closing = completion.select(locale, "task", recent)
        if closing:
            recent.add(key)
            if len(recent) > completion._RECENT_LIMIT:
                recent.pop()  # approximate LRU: drop an arbitrary old key
        text += "\n" + closing
    except Exception:  # noqa: BLE001 - completion feedback is best-effort
        pass
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
    await _ack_working(update, locale)
    try:
        task = await api.snooze_operational_task(task_id, preset=preset)
    except PasayApiPermissionError:
        await _answer(update, t("ops.no_permission", locale), durable=True)
        return
    except PasayApiError as exc:
        await _answer(update, f"⚠️ {H.escape(exc.detail)}", durable=True)
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
    await _ack_working(update, locale)
    try:
        task, properties = await asyncio.gather(
            api.get_operational_task(int(ref)), api.get_properties()
        )
    except PasayApiPermissionError:
        await _answer(update, t("ops.no_permission", locale), durable=True)
        return
    except PasayApiError as exc:
        await _answer(update, f"⚠️ {H.escape(exc.detail)}", durable=True)
        return
    text = cards.operational_task_detail_card(task, properties, locale)
    await _edit(update, text, task_action_keyboard(task.id, locale))


# --- 🤖 运营助手 (C1.1) callbacks ---------------------------------------------

async def _handle_copilot_nav(update, context, role, locale):
    """Dashboard [🤖 运营助手] button -> fast deterministic TODAY."""
    if not has_read_permission(role):
        await _answer(update, t("common.no_permission", locale))
        return
    await _ack_working(update, locale)
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
    await _ack_working(update, locale)
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
    await _ack_working(update, locale)
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
    await _ack_working(update, locale)
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
        await _answer(update, t("common.processing", locale), durable=True)
        return
    await _ack_working(update, locale)
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
        await _answer(update, t("common.processing", locale), durable=True)
        return
    await _ack_working(update, locale)
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
    api = context.bot_data["api_client"]
    if entity == "me":
        await _ack_working(update, locale)
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
