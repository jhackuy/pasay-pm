"""Command handlers + page builders shared with the callback router.

V1.1 UX: the home page is a live dashboard ("today management center"),
secondary pages are edit-first, and the rent flow compresses to
home -> pick unpaid unit -> confirm with smart defaults.
"""
from __future__ import annotations

import asyncio
import calendar
import logging
import re
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional

from telegram import Chat, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from pasay_bot.api_client import PasayApiError
from pasay_bot.followup_truth import (
    compute_followup_snapshot,
)
from pasay_bot.keyboards import (
    ACTION_COPILOT_ASK,
    ACTION_COPILOT_NAV,
    ACTION_COPILOT_WHY,
    ACTION_EXPENSE_DETAIL,
    ACTION_EXPENSE_OPEN,
    ACTION_NAV,
    ACTION_REMIND_OWNER,
    OPS_OVERVIEW,
    OPS_SECTION_ALL,
    OPS_SECTION_NEXT7,
    OPS_SECTION_OVERDUE,
    OPS_SECTION_TODAY,
    collect_list_keyboard,
    copilot_why_keyboard,
    copilot_today_keyboard,
    dashboard_keyboard,
    encode,
    error_keyboard,
    home_keyboard,
    home_summary_keyboard,
    reply_keyboard,
    ops_overview_keyboard,
    ops_section_keyboard,
    overdue_page_keyboard,
    pending_page_keyboard,
    property_list_keyboard,
    property_pagination_keyboard,
    properties_quick_keyboard,
    rent_quick_keyboard,
    expense_remind_keyboard,
    tasks_quick_keyboard,
    todo_keyboard,
    unit_list_keyboard,
    unit_page_keyboard,
)
from pasay_bot.handlers.edit_utils import edit_message_text_idempotent
from pasay_bot.roles import Role
from pasay_bot.render import cards, html as H
from pasay_bot.render.cards import PAGE_SIZE_OVERDUE, PAGE_SIZE_PROPERTIES
from pasay_bot.render.i18n import bl, t
from pasay_bot.roles import (
    PERMISSION_OPERATIONS,
    PERMISSION_RENT_CONFIRM,
    Role,
    has_permission,
    has_read_permission,
    locale_for_chat,
    locale_for,
    role_for_telegram_id,
)

HTML = "HTML"

logger = logging.getLogger(__name__)

EXPIRING_LEASE_DAYS = 60
TASK_WINDOW_DAYS = 7
EXPIRING_CONTRACT_DAYS = 30  # BOT-V1-USABLE-001 P0 spec: 30-day contract window


def _bind_identity(update, context, user_id=None) -> bool:
    """Bind the Telegram effective_user to the API clients. Returns True when
    an identity was bound, False when the update has no effective_user (a
    channel_post or other anonymous update) — callers must then ignore it."""
    api = context.bot_data["api_client"]
    admin = context.bot_data.get("admin_api_client")
    # Clear first so even a malformed update cannot inherit the previous
    # sequential update's identity in the same asyncio task.
    api.clear_telegram_user()
    if admin is not None:
        admin.clear_telegram_user()
    if user_id is None:
        if update.effective_user is None:
            # AI-OPS-FOUNDATION-001 §12: a channel_post / anonymous update is
            # not a user action — ignore it, never crash the handler.
            return False
        user_id = update.effective_user.id
    api.bind_telegram_user(user_id)
    if admin is not None:
        admin.bind_telegram_user(user_id)
    return True


# --- SLICE3-UX-PERSISTENT-MENU-002: persistent menu initialization ----------
# Minimal in-process dedupe (bot_data resets on restart, the allowed scope):
# each chat gets the persistent Reply Keyboard at most once per process, so
# normal messages never re-mount it and never spam welcome/menu messages.


def _menu_init_chats(context) -> set:
    return context.bot_data.setdefault("menu_init_chats", set())


def _mark_menu_initialized(context, chat_id) -> None:
    _menu_init_chats(context).add(chat_id)


def _is_menu_initialized(context, chat_id) -> bool:
    return chat_id in _menu_init_chats(context)


async def _send_persistent_menu(context, chat_id, role, locale) -> bool:
    """Re-send the persistent Reply Keyboard once per chat (private chats only,
    enforced by callers) for identified users. Never depends on backend data
    and never prompts the user to type /start. Returns True when sent."""
    if role is None or not has_read_permission(role):
        return False
    if _is_menu_initialized(context, chat_id):
        return False
    _mark_menu_initialized(context, chat_id)
    await context.bot.send_message(
        chat_id,
        H.escape(t("menu.ready", locale)),
        parse_mode=HTML,
        reply_markup=reply_keyboard(role),
    )
    return True


def _current_month() -> str:
    return date.today().strftime("%Y-%m")


_PERIOD_IN_DESC = re.compile(r"(?<!\d)(\d{4})(?:[-/.])?(\d{1,2})(?!\d)")


def _income_period(inc) -> Optional[str]:
    """YYYY-MM rent period an income maps to.

    Mirrors the backend (``app/api/routers/reports.py:_income_period``): an
    explicit YYYY-MM in the description is authoritative; the received-date
    month is used ONLY when the description carries no period.
    """
    desc = getattr(inc, "description", None) or ""
    match = _PERIOD_IN_DESC.search(desc)
    if match is not None:
        year, month = int(match.group(1)), int(match.group(2))
        if 1 <= month <= 12:
            return f"{year:04d}-{month:02d}"
    if inc.received_date is not None:
        return inc.received_date.strftime("%Y-%m")
    return None


def _period_covered(incomes, lease_id: int, month: str) -> bool:
    """True when the lease has a confirmed income covering ``month``.

    Mirrors the backend coverage rule exactly: period matching is
    description-first (YYYY-MM), with a received-date-month fallback ONLY
    when the description has no period. A payment recorded in a different
    month never covers this month.
    """
    for inc in incomes:
        if inc.lease_id != lease_id or inc.status != "confirmed":
            continue
        if _income_period(inc) == month:
            return True
    return False


def _lease_accounting_start(lease) -> date:
    """Earliest month the lease accrues rent (mirrors backend)."""
    if lease.accounting_start_date is None:
        return lease.start_date
    return max(lease.start_date, lease.accounting_start_date)


def _lease_periods(lease) -> list[tuple[str, date]]:
    """(YYYY-MM, due_date) for every rent month from accounting start through
    end, mirroring the backend's ``reports._lease_periods``."""
    due_day = lease.due_day if lease.due_day is not None else lease.start_date.day
    periods: list[tuple[str, date]] = []
    accounting_start = _lease_accounting_start(lease)
    year, month = accounting_start.year, accounting_start.month
    end_year, end_month = lease.end_date.year, lease.end_date.month
    end_is_fully_covered = lease.end_date.day >= calendar.monthrange(end_year, end_month)[1]
    while (year, month) <= (end_year, end_month):
        if (year, month) == (end_year, end_month) and not end_is_fully_covered:
            # final month is partial (lease ends mid-month) -> skip
            break
        day = min(due_day, calendar.monthrange(year, month)[1])
        periods.append((f"{year:04d}-{month:02d}", date(year, month, day)))
        month += 1
        if month > 12:
            month = 1
            year += 1
    return periods


def _last_income_for_lease(incomes, lease_id: int):
    rows = [i for i in incomes if i.lease_id == lease_id]
    if not rows:
        return None
    return max(rows, key=lambda i: (i.received_date, i.id))


# --- commands ---

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """PASAY-V2-FOUNDATION-001: /start is a technical recovery command only —
    never part of the normal user path. It shows the short V2 greeting (not
    the full dashboard) and mounts the fixed Quick View keyboard."""
    _bind_identity(update, context)
    role = role_for_telegram_id(update.effective_user.id if update.effective_user else None)
    if not has_read_permission(role):
        await _refuse(update, context, role)
        return
    chat_id = update.effective_chat.id
    locale = locale_for_chat(
        update.effective_chat.type if update.effective_chat else None, role
    )
    await show_greeting(context, chat_id, role, locale)


async def handle_new_chat_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """PASAY-V2-FOUNDATION-001: keyboard self-healing when the bot is added
    to a group (and a friendly bilingual welcome when anyone joins).

    The V2 fixed menu is identical for every role (4 English Quick View
    buttons), so attaching the neutral fixed keyboard in a group can never
    leak an Owner/Secretary menu. Users are never told to type /start."""
    message = update.effective_message
    chat = update.effective_chat
    members = message.new_chat_members if message is not None else None
    if chat is None or not members or chat.type == Chat.PRIVATE:
        return
    context.bot_data["store"].remember_group(chat.id, title=(chat.title or ""))
    lines = [
        t("v2.welcome", "en"),
        t("v2.welcome", "zh"),
        t("v2.help", "en"),
        t("v2.help", "zh"),
    ]
    await context.bot.send_message(
        chat.id,
        H.escape("\n".join(lines)),
        parse_mode=HTML,
        reply_markup=None if _is_menu_initialized(context, chat.id) else reply_keyboard(Role.OWNER),
    )
    _mark_menu_initialized(context, chat.id)


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _bind_identity(update, context)
    role = role_for_telegram_id(update.effective_user.id if update.effective_user else None)
    locale = locale_for_chat(
        update.effective_chat.type if update.effective_chat else None, role
    )
    text = (
        f"📖 <b>{H.escape(t('help.title', locale))}</b>\n\n"
        f"{H.escape(t('help.text', locale))}"
    )
    await context.bot.send_message(
        update.effective_chat.id, text, parse_mode=HTML,
        reply_markup=reply_keyboard(role),
    )


async def cmd_properties(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _bind_identity(update, context)
    role = role_for_telegram_id(update.effective_user.id if update.effective_user else None)
    if not has_read_permission(role):
        await _refuse(update, context, role)
        return
    await show_properties(context, update.effective_chat.id, role, locale_for(role), page=1)


async def cmd_finance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _bind_identity(update, context)
    role = role_for_telegram_id(update.effective_user.id if update.effective_user else None)
    if not has_read_permission(role):
        await _refuse(update, context, role)
        return
    await show_finance(context, update.effective_chat.id, locale_for(role))


async def cmd_overdue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _bind_identity(update, context)
    role = role_for_telegram_id(update.effective_user.id if update.effective_user else None)
    if not has_read_permission(role):
        await _refuse(update, context, role)
        return
    await show_overdue(context, update.effective_chat.id, locale_for(role), page=1)


async def cmd_rent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _bind_identity(update, context)
    role = role_for_telegram_id(update.effective_user.id if update.effective_user else None)
    if not has_read_permission(role):
        await _refuse(update, context, role)
        return
    await show_rent(context, update.effective_chat.id, locale_for(role))


async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _bind_identity(update, context)
    """Unified to-do page (V1.3): everything the current user must act on."""
    role = role_for_telegram_id(update.effective_user.id if update.effective_user else None)
    locale = locale_for(role)
    if not has_read_permission(role):
        await _refuse(update, context, role)
        return
    await show_todo(context, update.effective_chat.id, role, locale)


async def cmd_ops(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _bind_identity(update, context)
    """/ops and /todo both open the unified to-do page (V1.3)."""
    role = role_for_telegram_id(update.effective_user.id if update.effective_user else None)
    locale = locale_for(role)
    if not has_permission(role, PERMISSION_OPERATIONS):
        await _refuse(update, context, role)
        return
    await show_todo(context, update.effective_chat.id, role, locale)


async def cmd_todo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _bind_identity(update, context)
    role = role_for_telegram_id(update.effective_user.id if update.effective_user else None)
    locale = locale_for(role)
    if not has_permission(role, PERMISSION_OPERATIONS):
        await _refuse(update, context, role)
        return
    await show_todo(context, update.effective_chat.id, role, locale)


async def cmd_copilot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _bind_identity(update, context)
    """🤖 运营助手 (C1 read-only TODAY brief)."""
    role = role_for_telegram_id(update.effective_user.id if update.effective_user else None)
    locale = locale_for(role)
    if not has_permission(role, PERMISSION_OPERATIONS):
        await _refuse(update, context, role)
        return
    await show_copilot(context, update.effective_chat.id, locale)


async def _refuse(update: Update, context: ContextTypes.DEFAULT_TYPE, role):
    await context.bot.send_message(
        update.effective_chat.id,
        H.escape(t("common.no_permission", locale_for(role))),
        parse_mode=HTML,
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _bind_identity(update, context)
    role = role_for_telegram_id(update.effective_user.id if update.effective_user else None)
    locale = locale_for(role)
    store = context.bot_data["store"]
    chat_id, user_id = update.effective_chat.id, update.effective_user.id
    conv = store.get_conversation(chat_id, user_id)
    if conv:
        store.delete_conversation(chat_id, user_id)
        await context.bot.send_message(
            chat_id, H.escape(t("rent.cancelled", locale)),
            parse_mode=HTML, reply_markup=home_keyboard(locale),
        )
        return
    # CONVERGENCE-003 §2.1: /cancel with no conversation lands on the ONE Home.
    await show_home(context, chat_id, role, locale)


# --- page builders ---

async def _send(context, chat_id, text, keyboard=None, reply_keyboard=None):
    await context.bot.send_message(
        chat_id, H.truncate(text), parse_mode=HTML,
        reply_markup=keyboard if keyboard is not None else reply_keyboard,
    )


async def _render(context, chat_id, message_id, text, keyboard=None, reply_keyboard=None):
    """edit-first: when a message_id is known we edit it, else send (B6)."""
    print(f"[TRACE] render start chat_id={chat_id} message_id={message_id} text_len={len(text) if text else 0}", flush=True)
    _ph_start = time.monotonic()
    try:
        if message_id:
            _tg_start = time.monotonic()
            await edit_message_text_idempotent(
                context.bot,
                chat_id=chat_id,
                message_id=message_id,
                text=H.truncate(text),
                parse_mode=HTML,
                reply_markup=keyboard,
            )
            _record_phase("telegram_edit_ms", (time.monotonic() - _tg_start) * 1000)
        else:
            _tg_start = time.monotonic()
            await _send(context, chat_id, text, keyboard, reply_keyboard=reply_keyboard)
            _record_phase("telegram_edit_ms", (time.monotonic() - _tg_start) * 1000)
        print(f"[TRACE] render OK chat_id={chat_id}", flush=True)
    except Exception as exc:  # noqa: BLE001 - trace must never swallow the reply failure
        print(f"[TRACE] render FAIL {type(exc).__name__} {exc!r} chat_id={chat_id}", flush=True)
        raise
    finally:
        _record_phase("render_ms", (time.monotonic() - _ph_start) * 1000)


def _record_phase(attr: str, ms: float) -> None:
    """Best-effort record of a phase into the active PhaseProbe (007A A)."""
    try:
        from pasay_bot.state.latency import current_phase

        probe = current_phase()
        if probe is not None:
            if attr == "render_ms":
                probe.add_render(ms)
            elif attr == "telegram_edit_ms":
                probe.add_telegram(ms)
    except Exception:  # noqa: BLE001 - profiling never breaks rendering
        pass


def _load_error(detail: str, locale: str) -> str:
    return f"⚠️ {H.escape(t('common.load_error', locale, detail=str(detail)))}"


async def show_dashboard(
    context, chat_id, locale: str, message_id=None, role=None, fallback_inline=False,
):
    """Today's management center (B1). Data is fetched in parallel; any section
    the backend cannot serve is hidden rather than fabricated."""
    api = context.bot_data["api_client"]
    month = _current_month()
    results = await asyncio.gather(
        api.get_financial_summary(month),
        api.get_overdue_rents(),
        api.get_units(),
        api.get_leases(),
        api.get_tasks(within_days=TASK_WINDOW_DAYS),
        return_exceptions=True,
    )
    fin, overdue_rows, units, leases, tasks = results
    if isinstance(fin, PasayApiError) or isinstance(fin, Exception):
        await _render(context, chat_id, message_id, _load_error("dashboard", locale),
                      error_keyboard("home", locale))
        return
    overdue_count = len(overdue_rows) if not isinstance(overdue_rows, Exception) else 0
    units_list = [] if isinstance(units, Exception) else units
    leases_list = [] if isinstance(leases, Exception) else leases
    tasks_list = [] if isinstance(tasks, Exception) else tasks
    today = date.today()
    expiring_count = sum(
        1
        for l in leases_list
        if l.status == "active"
        and l.end_date >= today
        and (l.end_date - today).days <= EXPIRING_LEASE_DAYS
    )
    vacant_count = sum(1 for u in units_list if u.status == "vacant")
    text = cards.dashboard_card(
        today.strftime("%Y-%m-%d"),
        expected=fin.expected_rent_total,
        collected=fin.collected_rent,
        outstanding=fin.outstanding_rent,
        overdue_count=overdue_count,
        expiring_count=expiring_count,
        task_count=len(tasks_list),
        vacant_count=vacant_count,
        locale=locale,
    )
    if message_id:
        # Telegram does not allow ReplyKeyboardMarkup on editMessageText; the
        # persistent keyboard stays visible from the last send, so the edited
        # message carries the minimal inline fallback instead.
        await _render(context, chat_id, message_id, text, dashboard_keyboard(locale))
    else:
        if fallback_inline:
            # ☰ 更多: the bottom nav is already visible; show the fallback
            # inline actions (收租/逾期/运营助手/首页) on this message.
            await _send(context, chat_id, text, keyboard=dashboard_keyboard(locale))
        else:
            await _send(context, chat_id, text,
                        reply_keyboard=reply_keyboard(role) if role else None)
            if role:
                # The persistent keyboard was just mounted on this message;
                # remember it so later normal messages don't re-send it.
                _mark_menu_initialized(context, chat_id)


async def show_home(context, chat_id, role, locale: str, message_id=None):
    """CONVERGENCE-003 §2.2 Home: the ONE Telegram Operations Overview.

    Ten deterministic numbers (expected / collected / outstanding this month,
    historical arrears, overdue units, expiring contracts, vacant units,
    expenses awaiting payment, open maintenance, needs-action today). No
    second navigation: the fixed Reply Keyboard is the navigation, and the
    card carries only the situational ⚠️ Today / 🔄 Refresh buttons."""
    api = context.bot_data["api_client"]
    month = _current_month()
    # PASAY-AI-EMPLOYEE-FOUNDATION-007A §A: one parallel gather (never serial
    # N+1); every read-model ack clears the spinner immediately, so the God
    # View renders as fast as the slowest single snapshot.
    expenses, incomes, overdue, leases, units, tasks, quick_exp, quick_rent, fin = await asyncio.gather(
        api.list_expenses(),
        api.list_incomes(),
        api.get_overdue_rents(),
        api.get_leases(),
        api.get_units(),
        api.get_operational_tasks(status="PENDING"),
        api.get_quick_expense(),
        api.get_quick_rent(),
        api.get_financial_summary(month),
        return_exceptions=True,
    )
    if isinstance(fin, Exception) or not fin:
        await _render(context, chat_id, message_id, _load_error("home", locale),
                      home_summary_keyboard(locale))
        if role and message_id is None:
            # SLICE3-UX-PERSISTENT-MENU-002: the persistent keyboard must not
            # depend on backend/dashboard success (private chat only).
            await _send_persistent_menu(context, chat_id, role, locale)
        return
    expenses_list = [] if isinstance(expenses, Exception) else expenses
    overdue_list = [] if isinstance(overdue, Exception) else overdue
    leases_list = [] if isinstance(leases, Exception) else leases
    units_list = [] if isinstance(units, Exception) else units
    tasks_list = [] if isinstance(tasks, Exception) else tasks
    quick_exp_data = {} if isinstance(quick_exp, Exception) else (quick_exp or {})
    quick_rent_data = {} if isinstance(quick_rent, Exception) else (quick_rent or {})

    today = date.today()
    expiring_contracts = sum(
        1
        for l in leases_list
        if l.status == "active"
        and l.end_date >= today
        and (l.end_date - today).days <= EXPIRING_CONTRACT_DAYS
    )
    vacant_count = sum(1 for u in units_list if u.status == "vacant")
    payable_count = len(quick_exp_data.get("payable") or [])
    total_arrears = quick_rent_data.get("outstanding_total")
    occupied_count = sum(1 for u in units_list if u.status == "occupied")
    text = cards.home_summary_card(
        expected=fin.expected_rent_total,
        collected=fin.collected_rent,
        outstanding=fin.outstanding_rent,
        total_arrears=total_arrears,
        overdue_count=len(overdue_list),
        expiring_count=expiring_contracts,
        vacant_count=vacant_count,
        payable_count=payable_count,
        today_count=len(tasks_list),
        property_total=len(units_list),
        occupied_count=occupied_count,
        locale=locale,
    )
    if message_id:
        # Telegram cannot attach a ReplyKeyboardMarkup via editMessageText;
        # the persistent menu stays visible from the last send, and the
        # message carries the situational action buttons instead.
        await _render(context, chat_id, message_id, text, home_summary_keyboard(locale))
    else:
        if role and _is_menu_initialized(context, chat_id):
            await _send(context, chat_id, text, keyboard=home_summary_keyboard(locale))
        else:
            await _send(
                context, chat_id, text,
                reply_keyboard=reply_keyboard(role) if role else None,
            )
            if role:
                # The persistent keyboard was just mounted on this message;
                # remember it so later normal messages don't re-send it.
                _mark_menu_initialized(context, chat_id)


async def show_greeting(context, chat_id, role, locale: str, message_id=None):
    """PASAY-V2-FOUNDATION-001: short greeting — 👋 + one action line + at
    most one reminder-count line. Never the full dashboard."""
    api = context.bot_data["api_client"]
    reminder_count = 0
    try:
        digest = await api.get_digest()
        reminder_count = len((digest or {}).get("pending") or []) + len(
            (digest or {}).get("in_progress") or []
        )
    except Exception as exc:  # noqa: BLE001 - greeting must never depend on backend
        logger.warning("greeting digest unavailable: %s", exc)
        reminder_count = 0
    text = cards.greeting_card(locale, reminder_count=reminder_count)
    keyboard = (
        reply_keyboard(role)
        if role and not _is_menu_initialized(context, chat_id)
        else None
    )
    await _render(
        context, chat_id, message_id, text, reply_keyboard=keyboard,
    )
    if role:
        _mark_menu_initialized(context, chat_id)


async def _quick_view(
    context, chat_id, role, locale: str, message_id,
    fetcher, card_fn, error_key: str,
):
    """Shared deterministic Quick View path: one backend read + one card.
    Never calls an LLM. The reply carries the persistent keyboard so the
    fixed menu is self-healing without extra messages."""
    api = context.bot_data["api_client"]
    try:
        data = await fetcher(api)
    except PasayApiError as exc:
        await _render(context, chat_id, message_id, _load_error(exc.detail, locale),
                      error_keyboard("home", locale))
        return
    except Exception as exc:  # noqa: BLE001 - user-visible fallback
        logger.warning("quick view %s failed: %s", error_key, exc)
        await _render(context, chat_id, message_id, _load_error(error_key, locale),
                      error_keyboard("home", locale))
        return
    text = card_fn(data, locale)
    await _render(
        context, chat_id, message_id, text,
        reply_keyboard=reply_keyboard(role) if role else None,
    )
    if role:
        _mark_menu_initialized(context, chat_id)


async def show_quick_properties(context, chat_id, role, locale: str, message_id=None):
    """🏠 Properties Quick View (deterministic, no LLM): high-density index
    with one line per unit and a per-unit ``👁`` entry + ``📄 Property Archive``
    inline button (TELEGRAM-OPS-UX-CONVERGENCE-001 §2/§3)."""
    api = context.bot_data["api_client"]
    try:
        data = await api.get_quick_properties()
    except PasayApiError as exc:
        await _render(context, chat_id, message_id, _load_error(exc.detail, locale),
                      error_keyboard("home", locale))
        return
    except Exception as exc:  # noqa: BLE001 - user-visible fallback
        logger.warning("quick view properties failed: %s", exc)
        await _render(context, chat_id, message_id, _load_error("properties", locale),
                      error_keyboard("home", locale))
        return
    rows = data if isinstance(data, list) else ((data or {}).get("properties") or [])
    text = cards.properties_quick_card(data, locale)
    if rows:
        await _render(
            context, chat_id, message_id, text,
            keyboard=properties_quick_keyboard(rows, locale),
        )
    else:
        await _render(
            context, chat_id, message_id, text,
            reply_keyboard=(reply_keyboard(role) if role else None),
        )
    if role:
        _mark_menu_initialized(context, chat_id)


async def show_today_digest(context, chat_id, role, locale: str, message_id=None):
    """PASAY-AI-EMPLOYEE-FOUNDATION-007A §D: ⚠️ Today = the SAME Daily Digest
    path as the scheduled job — one truth, one renderer.

    Calls ``GET /operations/digest`` (the same ``build_digest`` with business
    dedup + HUMAN/SYSTEM completion filter) and renders with the SAME
    ``cards.active_tasks_digest_card`` used by the scheduled private deliverer.
    Never a second per-page logic set: the Owner sees their own (zh) digest
    on demand, without waiting for the next scheduled delivery."""
    api = context.bot_data["api_client"]
    try:
        data = await api.get_digest()
    except PasayApiError as exc:
        await _render(context, chat_id, message_id, _load_error(exc.detail, locale),
                      error_keyboard("home", locale))
        return
    data = data or {}
    text = cards.active_tasks_digest_card(data, locale)
    # Same situational nav as Home: ⚠️ Today re-entry is the digest itself.
    await _render(
        context, chat_id, message_id, text,
        keyboard=home_summary_keyboard(locale),
    )


async def show_quick_tasks(context, chat_id, role, locale: str, message_id=None):
    """✅ Tasks Quick View (deterministic, no LLM).

    When payable APPROVED expenses are present, the card attaches per-row Pay
    buttons so the Owner can open the deterministic payment flow straight from
    the Quick View (PASAY-V2-EXPENSE-PAYABLE-TASK-006 §4). Otherwise the card
    carries the fixed Reply Keyboard, matching the other Quick Views."""
    api = context.bot_data["api_client"]
    print(f"[TRACE] quick_tasks api start role={role.value if role else None} chat_id={chat_id}", flush=True)
    try:
        # AI-OPS-FOUNDATION-001 §5: the Owner's Quick Tasks view is filtered
        # to their own Needs-You queue; the Secretary sees operational tasks.
        data = await api.get_quick_tasks(scope="owner" if role == Role.OWNER else None)
        print(f"[TRACE] quick_tasks api OK type={type(data).__name__} "
              f"len={len(data) if hasattr(data, '__len__') else '?'}", flush=True)
    except PasayApiError as exc:
        print(f"[TRACE] quick_tasks api PasayApiError {type(exc).__name__} detail={getattr(exc, 'detail', None)!r}", flush=True)
        await _render(context, chat_id, message_id, _load_error(exc.detail, locale),
                      error_keyboard("home", locale))
        return
    except Exception as exc:  # noqa: BLE001 - user-visible fallback
        print(f"[TRACE] quick_tasks api EXC {type(exc).__name__} {exc!r}", flush=True)
        logger.warning("quick view tasks failed: %s", exc)
        await _render(context, chat_id, message_id, _load_error("tasks", locale),
                      error_keyboard("home", locale))
        return
    text = cards.tasks_quick_card(data, locale)
    rows = data if isinstance(data, list) else ((data or {}).get("tasks") or [])
    has_payable = any(
        str(r.get("kind") or "") == "payable_expense" for r in rows
    )
    if has_payable:
        await _render(
            context, chat_id, message_id, text,
            keyboard=tasks_quick_keyboard(data, locale),
        )
    else:
        await _render(
            context, chat_id, message_id, text,
            reply_keyboard=(reply_keyboard(role) if role else None),
        )
    if role:
        _mark_menu_initialized(context, chat_id)


async def show_quick_rent(context, chat_id, role, locale: str, message_id=None):
    """💰 Rent Quick View (deterministic, no LLM). Keeps the high-density
    stats + overdue list and adds one ``Follow up`` inline button per overdue
    unit -> the Rent detail card (TELEGRAM-OPS-UX-CONVERGENCE-001 §7)."""
    api = context.bot_data["api_client"]
    try:
        data = await api.get_quick_rent()
    except PasayApiError as exc:
        await _render(context, chat_id, message_id, _load_error(exc.detail, locale),
                      error_keyboard("home", locale))
        return
    except Exception as exc:  # noqa: BLE001 - user-visible fallback
        logger.warning("quick view rent failed: %s", exc)
        await _render(context, chat_id, message_id, _load_error("rent", locale),
                      error_keyboard("home", locale))
        return
    data = data or {}
    overdue = data.get("overdue") or []
    text = cards.rent_quick_card(data, locale)
    if overdue:
        button_labels: dict[int, str] = {}
        try:
            units, leases, tasks = await asyncio.gather(
                api.get_units(),
                api.get_leases(),
                api.get_operational_tasks(),
            )
        except PasayApiError:
            units, leases, tasks = [], [], []
        unit_id_by_code = {}
        for unit in units:
            unit_num = str(getattr(unit, "unit_number", "") or "")
            unit_id = getattr(unit, "id", None)
            if unit_num and unit_id is not None:
                unit_id_by_code[unit_num] = unit_id
                unit_id_by_code.setdefault(unit_num.split("-")[-1], unit_id)
        store = context.bot_data.get("store")
        for i, row in enumerate(overdue, start=1):
            unit_code = str(row.get("unit") or row.get("unit_code") or "")
            amount = H.money(row.get("amount"))
            button_unit = unit_code.split("-")[-1] if unit_code else unit_code
            unit_id = unit_id_by_code.get(unit_code) or unit_id_by_code.get(unit_code.split("-")[-1])
            if unit_id is None:
                button_labels[i] = f"{button_unit} · {amount}"
                continue
            snapshot = compute_followup_snapshot(
                tasks, leases, store, unit_id, unit_code,
                last_followup_at=row.get("last_followup_at"),
            )
            if snapshot.followed_up_today:
                status_label = bl("v2.rent_followed_short", locale)
            elif snapshot.assigned:
                status_label = bl("v2.followup_status_assigned", locale)
            else:
                status_label = bl("v2.rent_followup_pending_short", locale)
            button_labels[i] = f"{button_unit} · {amount} · {status_label}"
        await _render(
            context, chat_id, message_id, text,
            keyboard=rent_quick_keyboard(overdue, locale, button_labels=button_labels),
        )
    else:
        await _render(
            context, chat_id, message_id, text,
            reply_keyboard=(reply_keyboard(role) if role else None),
        )
    if role:
        _mark_menu_initialized(context, chat_id)


async def show_quick_expense(context, chat_id, role, locale: str, message_id=None):
    """💸 Expense Quick View (deterministic, no LLM). High-density business
    list (Category/Purpose) with a ``🔔 Remind Owner`` entry per waiting-
    payment (APPROVED unpaid) row so the Secretary can push the payment
    forward (TELEGRAM-OPS-UX-CONVERGENCE-001 §6/§9)."""
    api = context.bot_data["api_client"]
    try:
        data = await api.get_quick_expense()
    except PasayApiError as exc:
        await _render(context, chat_id, message_id, _load_error(exc.detail, locale),
                      error_keyboard("home", locale))
        return
    except Exception as exc:  # noqa: BLE001 - user-visible fallback
        logger.warning("quick view expense failed: %s", exc)
        await _render(context, chat_id, message_id, _load_error("expense", locale),
                      error_keyboard("home", locale))
        return
    data = data or {}
    text = cards.expense_quick_card(data, locale)
    payable = data.get("payable") or []
    # CONVERGENCE-003 §4.2/§10: the LIST is for reading — one short
    # ``E{id} Open`` button per payable row (the actions live on the DETAIL
    # card). Never the long ``View details 查看详情 / Remind Owner 提醒老板``
    # labels that got truncated to ``Remin...`` on narrow phones.
    kb_rows: list[list[InlineKeyboardButton]] = []
    for row in payable:
        expense_id = row.get("expense_id")
        if expense_id is None:
            continue
        kb_rows.append(
            [
                InlineKeyboardButton(
                    f"E{int(expense_id)} · Open",
                    callback_data=encode(ACTION_EXPENSE_OPEN, str(int(expense_id))),
                )
            ]
        )
    if kb_rows:
        kb_rows.append(
            [
                InlineKeyboardButton(
                    t("common.home", locale), callback_data=encode(ACTION_NAV, "home")
                )
            ]
        )
        await _render(context, chat_id, message_id, text, keyboard=InlineKeyboardMarkup(kb_rows))
    else:
        await _render(
            context, chat_id, message_id, text,
            reply_keyboard=(reply_keyboard(role) if role else None),
        )
    if role:
        _mark_menu_initialized(context, chat_id)


async def handle_media_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Media receipt ack (PASAY-AI-EMPLOYEE-FOUNDATION-007 §6.2).

    A photo/document may NEVER be blind-published to the archive. If the media
    already has a semantic caption or the active business context implies a
    category, it is archived and indexed as an ArchiveAsset. Otherwise the bot
    asks ONE clarifying question (``1608 水表`` / ``1608 房租手机``) and only
    archives AFTER the semantic caption arrives — never a floating file."""
    user = update.effective_user
    role = role_for_telegram_id(user.id if user else None)
    chat_id = update.effective_chat.id if update.effective_chat else (user.id if user else None)
    if chat_id is None or not has_read_permission(role):
        return
    _bind_identity(update, context)
    if update.effective_chat and update.effective_chat.type in ("group", "supergroup"):
        context.bot_data["store"].remember_group(
            chat_id, title=(update.effective_chat.title or "")
        )
    locale = locale_for_chat(
        update.effective_chat.type if update.effective_chat else None, role
    )
    msg = update.effective_message
    caption = (getattr(msg, "caption", None) or "").strip()
    # Semantic signal: a caption, or an active business context that pins a
    # category (repair/expense evidence). If neither -> ask, never blind-publish.
    semantic = bool(caption) or _media_has_context(update, context)
    archived = False
    if semantic:
        try:
            archived = await _archive_media(update, context)
        except Exception as exc:  # noqa: BLE001 - archiving never breaks the ack
            logger.warning("media archive failed: %s", exc)
            archived = False
        if archived:
            await context.bot.send_message(
                chat_id,
                H.escape(t("v2.media_archived", locale)),
                parse_mode=HTML,
                reply_markup=reply_keyboard(role),
            )
        else:
            await context.bot.send_message(
                chat_id,
                H.escape(t("v2.media_received", locale)),
                parse_mode=HTML,
                reply_markup=reply_keyboard(role),
            )
    else:
        # §6.2: no semantic caption -> one clarifying question, NO publication.
        # Store the pending media so the follow-up reply ("1608 水表") archives
        # it with real business semantics.
        store = context.bot_data["store"]
        file_id, media_type, mime_type, filename, size = _media_file_info(msg, msg)
        store.save_conversation(
            chat_id, user.id,
            "archive_caption",
            {
                "media_pending": True,
                "message_id": getattr(msg, "message_id", None),
                "file_id": file_id,
                "media_type": media_type,
                "mime_type": mime_type,
                "filename": filename,
                "size_bytes": size,
            },
        )
        await context.bot.send_message(
            chat_id,
            H.escape(t("v2.media_ask_caption", locale)),
            parse_mode=HTML,
            reply_markup=reply_keyboard(role),
        )
    _mark_menu_initialized(context, chat_id)


def _media_has_context(update, context) -> bool:
    """A media message is 'semantic' when an active business context (repair /
    expense) pins the file to a real entity + category — only then forward."""
    try:
        user = update.effective_user
        if user is None:
            return False
        ctx = context.bot_data["store"].get_v2_context(
            update.effective_chat.id, user.id
        )
        if not ctx:
            return False
        payload = dict(ctx["payload"] or {})
        return bool(payload.get("task_ref") or payload.get("expense_ref") or payload.get("intent"))
    except Exception:  # noqa: BLE001 - context is best-effort
        return False


async def _archive_media(update: Update, context) -> bool:
    """Forward one photo/document/video to the archive channel and index it.

    Returns True when the media was archived AND indexed. Falls back to
    False when the archive chat is not configured (graceful no-op)."""
    settings = context.bot_data["settings"]
    archive_chat = (settings.archive_chat_id or "").strip()
    if not archive_chat:
        return False
    msg = update.effective_message
    if msg is None:
        return False
    try:
        forwarded = await context.bot.forward_message(
            chat_id=archive_chat,
            from_chat_id=msg.chat_id,
            message_id=msg.message_id,
        )
    except Exception as exc:  # noqa: BLE001 - fail-open, keep old ack path
        logger.warning("forward to archive failed: %s", exc)
        return False
    file_id, media_type, mime_type, filename, size = _media_file_info(forwarded, msg)
    if not file_id:
        return False
    store = context.bot_data["store"]
    user_id = update.effective_user.id if update.effective_user else None
    ctx = store.get_v2_context(msg.chat_id, user_id) if user_id else None
    payload = dict(ctx["payload"]) if ctx else {}
    entity_type, entity_id, unit_id, category = _evidence_links(payload, msg)
    try:
        api = context.bot_data["api_client"]
        evidence = await api.create_evidence(
            external_file_id=file_id,
            external_message_id=forwarded.message_id,
            media_type=media_type,
            mime_type=mime_type,
            filename=filename,
            size_bytes=size,
            category=category,
            unit_id=unit_id,
            entity_type=entity_type,
            entity_id=entity_id,
        )
        if not evidence or not (evidence.get("id")):
            # The backend did not persist an evidence row -> never claim success.
            logger.warning("evidence index returned no row; media not marked archived")
            return False
    except Exception as exc:  # noqa: BLE001 - index failure must never look like success
        logger.warning("evidence index failed: %s", exc)
        return False
    return True


def _media_file_info(forwarded, original) -> tuple:
    """Extract (file_id, media_type, mime_type, filename, size) from the
    message. Real Telegram forwards keep the ORIGINAL file_id, so the source
    message's info is authoritative; the forwarded object is the fallback."""
    src = original if (
        getattr(original, "photo", None) or getattr(original, "document", None)
        or getattr(original, "video", None)
    ) else forwarded
    if getattr(src, "photo", None):
        largest = src.photo[-1]
        return (
            largest.file_id, "photo", "image/jpeg",
            f"photo_{largest.file_id[:20]}.jpg",
            getattr(largest, "file_size", None),
        )
    if getattr(src, "document", None):
        doc = src.document
        return (
            doc.file_id, "document", getattr(doc, "mime_type", None),
            getattr(doc, "file_name", None), getattr(doc, "file_size", None),
        )
    if getattr(src, "video", None):
        vid = src.video
        return (
            vid.file_id, "video", getattr(vid, "mime_type", None) or "video/mp4",
            f"video_{vid.file_id[:20]}.mp4", getattr(vid, "file_size", None),
        )
    return (None, None, None, None, None)


def _evidence_links(payload: dict, msg) -> tuple:
    """Resolve (entity_type, entity_id, unit_id, category) from the active
    conversation context — the media attaches to the current repair/expense
    event instead of being a floating file."""
    task_ref = payload.get("task_ref")
    expense_ref = payload.get("expense_ref")
    unit_token = payload.get("unit_token")
    unit_id = payload.get("unit_id")
    intent = payload.get("intent") or ""
    if task_ref:
        # A repair task: before/after photos and receipts are repair evidence.
        category = (
            "receipt" if ("receipt" in (payload.get("_media_hint") or ""))
            else "after_repair"
        )
        return "task", int(task_ref), int(unit_id) if unit_id else None, category
    if expense_ref:
        return "expense", int(expense_ref), int(unit_id) if unit_id else None, "receipt"
    if intent:
        return None, None, int(unit_id) if unit_id else None, "other"
    return None, None, None, "other"


async def show_expense(context, chat_id, locale: str, message_id=None):
    """💸 支出 entry page: natural-language first, no form, no /help."""
    text = (
        f"<b>{H.escape(t('expense.create_title', locale))}</b>\n\n"
        f"{H.escape(t('expense.page_hint', locale))}"
    )
    await _render(context, chat_id, message_id, text, home_keyboard(locale))


async def show_contracts_page(context, chat_id, locale: str, message_id=None,
                              days: int = EXPIRING_CONTRACT_DAYS):
    """Home [合同到期] -> real contract list within ``days`` (read-only)."""
    api = context.bot_data["api_client"]
    try:
        leases, units, tenants = await asyncio.gather(
            api.get_leases(), api.get_units(), api.get_tenants(),
        )
    except PasayApiError as exc:
        await _render(context, chat_id, message_id, _load_error(exc.detail, locale),
                      home_summary_keyboard(locale))
        return
    by_unit = {u.id: u.unit_number for u in units}
    by_tenant = {tn.id: tn.full_name for tn in tenants}
    today = date.today()
    rows = []
    for lease in leases:
        if lease.status != "active" or not lease.end_date:
            continue
        remaining = (lease.end_date - today).days
        if 0 <= remaining <= days:
            rows.append(
                {
                    "unit": by_unit.get(lease.unit_id, ""),
                    "tenant": by_tenant.get(lease.tenant_id, ""),
                    "end_date": lease.end_date,
                }
            )
    rows.sort(key=lambda r: r["end_date"])
    await _render(
        context, chat_id, message_id,
        cards.contracts_card(rows, days, locale),
        home_summary_keyboard(locale),
    )


def _is_maintenance_task(task) -> bool:
    task_type = str(getattr(task, "task_type", "") or "").upper()
    title = str(getattr(task, "title", "") or "")
    return (
        "MAINTENANCE" in task_type
        or "维修" in title
        or "maintenance" in title.lower()
    )


async def show_menu(context, chat_id, locale: str, message_id=None, role=None):
    """The menu IS the dashboard now (backward-compatible name)."""
    await show_dashboard(context, chat_id, locale, message_id=message_id, role=role)


async def build_properties_page(api, page: int, locale: str):
    try:
        properties, units = await asyncio.gather(api.get_properties(), api.get_units())
    except PasayApiError as exc:
        return _load_error(exc.detail, locale), error_keyboard("properties", locale)
    stats = cards._stats_by_property(units)
    total_pages = H.total_pages(len(properties), PAGE_SIZE_PROPERTIES)
    page = min(max(page, 1), total_pages)
    text = cards.properties_overview(properties, stats, page, PAGE_SIZE_PROPERTIES, locale)
    if not properties:
        keyboard = home_keyboard(locale)
    elif total_pages > 1:
        keyboard = property_pagination_keyboard(page, total_pages, locale)
    else:
        keyboard = home_keyboard(locale)
    return text, keyboard


async def show_properties(context, chat_id, role: Role | None, locale: str, page: int = 1,
                          message_id=None):
    api = context.bot_data["api_client"]
    text, keyboard = await build_properties_page(api, page, locale)
    await _render(context, chat_id, message_id, text, keyboard)


async def build_finance_page(api, locale: str):
    try:
        fin, overdue = await asyncio.gather(
            api.get_financial_summary(_current_month()), api.get_overdue_rents()
        )
    except PasayApiError as exc:
        return _load_error(exc.detail, locale), error_keyboard("finance", locale)
    overdue_total = sum(r.total_outstanding for r in overdue)
    return cards.finance_card(fin, overdue_total, locale), home_keyboard(locale)


async def show_finance(context, chat_id, locale: str, message_id=None):
    api = context.bot_data["api_client"]
    text, keyboard = await build_finance_page(api, locale)
    await _render(context, chat_id, message_id, text, keyboard)


async def build_overdue_page(api, page: int, locale: str):
    try:
        rows = await api.get_overdue_rents()
    except PasayApiError as exc:
        return _load_error(exc.detail, locale), error_keyboard("overdue", locale)
    rows = sorted(rows, key=lambda r: (-r.overdue_days, -r.total_outstanding))
    prop_by_unit: dict[int, str] = {}
    try:
        units, properties = await asyncio.gather(api.get_units(), api.get_properties())
        by_pid = {p.id: p.name for p in properties}
        prop_by_unit = {u.id: by_pid[u.property_id] for u in units if u.property_id in by_pid}
    except PasayApiError:
        pass  # property names are a nice-to-have on the overdue page
    total_pages = H.total_pages(len(rows), PAGE_SIZE_OVERDUE)
    page = min(max(page, 1), total_pages)
    text = cards.overdue_list(rows, page, PAGE_SIZE_OVERDUE, locale, prop_by_unit)
    if not rows:
        keyboard = home_keyboard(locale)
    else:
        page_rows = rows[(page - 1) * PAGE_SIZE_OVERDUE: page * PAGE_SIZE_OVERDUE]
        keyboard = overdue_page_keyboard(page_rows, page, total_pages, locale)
    return text, keyboard


async def show_overdue(context, chat_id, locale: str, page: int = 1, message_id=None):
    api = context.bot_data["api_client"]
    text, keyboard = await build_overdue_page(api, page, locale)
    await _render(context, chat_id, message_id, text, keyboard)


# --- rent: collect list with smart defaults (B4) ---

async def build_rent_collect_list(api, locale: str):
    """All currently collectible units (the backend overdue report is the
    authority): every unit with due-and-uncovered rent periods is listed with
    Unit / Tenant / 应收 / 已收 / 尚欠 / 到期状态. Never picks a single unit
    and never hides a unit because an unrelated income was recorded in the
    current month (P0-RENT-COLLECTION-UX-003)."""
    try:
        units, leases, incomes, properties, overdue = await asyncio.gather(
            api.get_units(),
            api.get_leases(),
            api.list_incomes(),
            api.get_properties(),
            api.get_overdue_rents(),
        )
    except PasayApiError as exc:
        return _load_error(exc.detail, locale), error_keyboard("rent", locale)
    lease_by_id = {l.id: l for l in leases if l.status == "active"}
    unit_by_id = {u.id: u for u in units}
    prop_by_id = {p.id: p.name for p in properties}
    today = date.today()
    rows = []
    for ovd in sorted(overdue, key=lambda r: (-r.overdue_days, r.unit)):
        lease = lease_by_id.get(ovd.lease_id)
        if lease is None:
            continue
        due_periods = [
            (month, due) for month, due in _lease_periods(lease) if due <= today
        ]
        receivable = lease.monthly_rent * len(due_periods)
        outstanding = ovd.total_outstanding
        received = max(receivable - outstanding, Decimal("0"))
        unit = unit_by_id.get(ovd.unit_id)
        rows.append(
            {
                "unit_id": ovd.unit_id,
                "lease_id": ovd.lease_id,
                "unit_number": ovd.unit,
                "property_name": prop_by_id.get(unit.property_id, "") if unit else "",
                "tenant": ovd.tenant,
                "monthly_rent": lease.monthly_rent,
                "receivable": receivable,
                "received": received,
                "outstanding": outstanding,
                "overdue_days": ovd.overdue_days if ovd else 0,
            }
        )
    text = cards.rent_collect_list(rows, locale)
    keyboard = collect_list_keyboard(rows, locale)
    return text, keyboard


async def show_rent(context, chat_id, locale: str, message_id=None):
    api = context.bot_data["api_client"]
    text, keyboard = await build_rent_collect_list(api, locale)
    await _render(context, chat_id, message_id, text, keyboard)


# --- pending: aggregated to-do (B2/B3) ---

async def show_pending(context, chat_id, role, locale: str, message_id=None):
    """One aggregated to-do page: overdue, pending confirm, expiring leases,
    open tasks. All data is real API data; missing sections are hidden."""
    api = context.bot_data["api_client"]
    try:
        overdue = await api.get_overdue_rents()
        incomes = await api.list_incomes()
    except PasayApiError as exc:
        await _render(context, chat_id, message_id, _load_error(exc.detail, locale),
                      error_keyboard("pending", locale))
        return
    try:
        leases, units, properties, tenants, tasks = await asyncio.gather(
            api.get_leases(), api.get_units(), api.get_properties(),
            api.get_tenants(), api.get_tasks(within_days=TASK_WINDOW_DAYS),
        )
    except PasayApiError:
        leases, units, properties, tenants, tasks = [], [], [], [], []
    by_lease = {l.id: l for l in leases}
    by_unit = {u.id: u for u in units}
    by_prop = {p.id: p.name for p in properties}
    by_tenant = {tn.id: tn.full_name for tn in tenants}
    today = date.today()

    overdue_rows = [
        {
            "unit_id": r.unit_id,
            "lease_id": r.lease_id,
            "unit": r.unit,
            "tenant": r.tenant,
            "total_outstanding": r.total_outstanding,
            "overdue_days": r.overdue_days,
        }
        for r in sorted(overdue, key=lambda x: (-x.overdue_days, -x.total_outstanding))
    ]
    pending_incomes = sorted(
        (i for i in incomes if i.status == "pending"),
        key=lambda i: (i.received_date, i.id),
    )
    confirm_rows = []
    for inc in pending_incomes:
        lease = by_lease.get(inc.lease_id) if inc.lease_id else None
        unit = by_unit.get(lease.unit_id) if lease else None
        where = " · ".join(
            x for x in (by_prop.get(unit.property_id, "") if unit else "",
                        unit.unit_number if unit else "") if x
        )
        confirm_rows.append({"id": inc.id, "amount": inc.amount, "where": where})
    expiring_rows = [
        {
            "unit": (by_unit.get(l.unit_id).unit_number if by_unit.get(l.unit_id) else ""),
            "tenant": by_tenant.get(l.tenant_id, ""),
            "end_date": l.end_date,
        }
        for l in leases
        if l.status == "active"
        and today <= l.end_date
        and (l.end_date - today).days <= EXPIRING_LEASE_DAYS
    ]
    expiring_rows.sort(key=lambda r: r["end_date"])
    task_rows = [
        {"title": tk.title, "due_date": tk.due_date}
        for tk in sorted(tasks, key=lambda x: (x.due_date is None, x.due_date or date.max))
    ]
    sections = {
        "overdue": overdue_rows,
        "confirm": confirm_rows,
        "expiring": expiring_rows,
        "tasks": task_rows,
    }
    text = cards.pending_overview_card(sections, locale)
    confirm_entries = [
        (r["id"], f"✅ #{r['id']} {H.money(r['amount'])}") for r in confirm_rows
    ]
    keyboard = pending_page_keyboard(
        overdue_rows, confirm_entries, locale,
        can_confirm=has_permission(role, PERMISSION_RENT_CONFIRM),
    )
    await _render(context, chat_id, message_id, text, keyboard)


# --- V1.3 unified to-do page (待办 / Tasks) ---

def _expense_location(expense, units, properties) -> str:
    """Property · Unit label for an expense card; empty when the expense has
    no unit (expense_id stays internal)."""
    if not getattr(expense, "unit_id", None):
        return ""
    unit = next((u for u in units if u.id == expense.unit_id), None)
    if unit is None:
        return ""
    prop = next((p for p in properties if p.id == unit.property_id), None)
    return " · ".join(x for x in ((prop.name if prop else ""), unit.unit_number) if x)


async def show_todo(context, chat_id, role, locale: str, message_id=None):
    """Unified to-do page (V1.3): only what the current user must act on.
    Owner sees the "需要您处理 / Needs You" queue — expense approvals,
    payments, pending income confirmations and escalated/decision tasks ONLY;
    Secretary sees the operational tasks the backend scoped to them (overdue
    rent, maintenance, contracts, follow-ups). Every row carries its action
    button (action-at-source)."""
    api = context.bot_data["api_client"]
    owner_view = role == Role.OWNER
    expenses, incomes, overdue, tasks = await asyncio.gather(
        api.list_expenses(),
        api.list_incomes(),
        api.get_overdue_rents(),
        # AI-OPS-FOUNDATION-001 §5: the Owner queue is backend-filtered to
        # owner-actionable tasks (approvals / payments / decisions /
        # escalations); routine operational work never reaches it.
        api.get_operational_tasks(
            status="PENDING", scope="owner" if owner_view else None,
        ),
        return_exceptions=True,
    )
    if isinstance(expenses, Exception):
        expenses = []
    if isinstance(incomes, Exception):
        incomes = []
    if isinstance(overdue, Exception):
        overdue = []
    if isinstance(tasks, Exception):
        tasks = []

    units, properties, leases = [], [], []
    try:
        units, properties = await asyncio.gather(api.get_units(), api.get_properties())
    except PasayApiError:
        pass
    try:
        leases = await api.get_leases()
    except PasayApiError:
        pass

    owner_view = role == Role.OWNER
    expense_rows = []
    if owner_view:
        # PASAY-V2-EXPENSE-PAYABLE-TASK-006: both PENDING (needs approval) and
        # APPROVED (needs payment) expenses are Owner-actionable Tasks; PAID /
        # REJECTED / REVERSED are not actionable. Each row carries its status so
        # the keyboard can offer Approve/Reject or Pay accordingly.
        actionable_expenses = sorted(
            (e for e in expenses if (e.status or "").lower() in ("pending", "approved")),
            key=lambda e: (e.due_date or e.expense_date, e.id),
        )
        expense_rows = [
            {
                "id": e.id,
                "status": (e.status or "").lower(),
                "category": e.category,
                "payee": e.payee,
                "amount": e.amount,
                "location": _expense_location(e, units, properties),
                "has_receipt": bool(e.receipt_attachment_id),
            }
            for e in actionable_expenses
        ]

    confirm_rows = []
    if owner_view:
        by_lease = {l.id: l for l in leases}
        by_unit = {u.id: u for u in units}
        by_prop = {p.id: p.name for p in properties}
        pending_incomes = sorted(
            (i for i in incomes if i.status == "pending"),
            key=lambda i: (i.received_date, i.id),
        )
        for inc in pending_incomes:
            lease = by_lease.get(inc.lease_id) if inc.lease_id else None
            unit = by_unit.get(lease.unit_id) if lease else None
            where = " · ".join(
                x for x in (
                    by_prop.get(unit.property_id, "") if unit else "",
                    unit.unit_number if unit else "",
                ) if x
            )
            confirm_rows.append({"id": inc.id, "amount": inc.amount, "where": where})

    # AI-OPS-FOUNDATION-001 §5: overdue rent, contract expiry and maintenance
    # are routine Secretary work — they never enter the Owner queue. The
    # Owner still sees them via the informational pages (/overdue, quick
    # views), just not as Owner Tasks.
    overdue_rows = []
    if not owner_view:
        overdue_rows = [
            {
                "unit_id": r.unit_id,
                "lease_id": r.lease_id,
                "unit": r.unit,
                "tenant": r.tenant,
                "total_outstanding": r.total_outstanding,
                "overdue_days": r.overdue_days,
            }
            for r in sorted(overdue, key=lambda x: (-x.overdue_days, -x.total_outstanding))
        ]

    # BOT-V1-USABLE-001 P0-4: 30-day contract expiry + open maintenance are
    # first-class todo sections (maintenance rows are NOT duplicated into the
    # generic task list).
    today = date.today()
    by_lease = {l.id: l for l in leases}
    by_unit = {u.id: u for u in units}
    by_prop = {p.id: p.name for p in properties}
    try:
        tenants_list = await api.get_tenants()
    except PasayApiError:
        tenants_list = []
    by_tenant = {tn.id: tn.full_name for tn in tenants_list}
    contract_rows = []
    for lease in leases:
        if lease.status != "active" or not lease.end_date:
            continue
        remaining = (lease.end_date - today).days
        if 0 <= remaining <= EXPIRING_CONTRACT_DAYS:
            contract_rows.append(
                {
                    "unit": by_unit.get(lease.unit_id).unit_number
                    if by_unit.get(lease.unit_id) else "",
                    "tenant": by_tenant.get(lease.tenant_id, ""),
                    "end_date": lease.end_date,
                }
            )
    contract_rows.sort(key=lambda r: r["end_date"])
    maintenance_rows = [tk for tk in tasks if _is_maintenance_task(tk)]
    task_rows = sorted(
        (tk for tk in tasks if not _is_maintenance_task(tk)),
        key=lambda x: (x.due_at is None, x.due_at or ""),
    )
    if owner_view:
        # Expense approvals/payments are already represented by the 💳 expense
        # rows with their action buttons; keep the Owner queue free of the
        # duplicate task representation.
        task_rows = [
            tk for tk in task_rows
            if tk.task_type not in ("APPROVAL_PENDING", "PAYMENT_PENDING")
        ]
    sections = {
        "expenses": expense_rows,
        "confirm": confirm_rows,
        "overdue": overdue_rows,
        "contracts": contract_rows if not owner_view else [],
        "maintenance": maintenance_rows if not owner_view else [],
        "tasks": task_rows,
    }
    text = cards.todo_overview_card(
        sections, locale,
        title_key="todo.title_owner" if owner_view else "todo.title",
    )
    keyboard = todo_keyboard(sections, owner_view=owner_view, locale=locale)
    # The persistent bottom keyboard stays visible from /start; this message
    # carries the per-row action buttons (action-at-source).
    await _render(context, chat_id, message_id, text, keyboard)


# --- unit page (state-driven, B5) ---

async def build_unit_page(api, unit_id: int, can_rent: bool, locale: str,
                          back_entity: str = "home"):
    """Unit page used by the rent flow, overdue detail and payment view.

    PASAY-AI-EMPLOYEE-FOUNDATION-007A §A8: fast-path — only the few fields this
    card really needs are fetched, in one parallel gather (no unused round-trip
    to the overdue report, no N+1 serial calls)."""
    try:
        unit, properties, leases, tenants, incomes = await asyncio.gather(
            api.get_unit(unit_id),
            api.get_properties(),
            api.get_leases(),
            api.get_tenants(),
            api.list_incomes(),
        )
    except PasayApiError as exc:
        return _load_error(exc.detail, locale), error_keyboard(back_entity, locale)
    prop = next((p for p in properties if p.id == unit.property_id), None)
    prop_name = prop.name if prop else "?"
    address = prop.address if prop else ""
    active = next(
        (l for l in leases if l.unit_id == unit.id and l.status == "active"), None
    )
    tenant_name = None
    payment_state = None
    action = None
    action_ref = ""
    if active:
        tenant = next((tn for tn in tenants if tn.id == active.tenant_id), None)
        tenant_name = tenant.full_name if tenant else None
        month = _current_month()
        if _period_covered(incomes, active.id, month):
            payment_state = "paid"
            last = _last_income_for_lease(incomes, active.id)
            if last is not None:
                action = "view"
                action_ref = str(last.id)
        else:
            payment_state = "unpaid"
            reversed_any = any(
                i.lease_id == active.id and i.status == "reversed" for i in incomes
            )
            action = "reopen" if reversed_any else "collect"
    text = cards.unit_card(
        unit, prop_name, address, active, tenant_name, locale, payment_state
    )
    keyboard = unit_page_keyboard(
        unit_id, action, locale, back_entity=back_entity, ref=action_ref
    )
    return text, keyboard


async def show_unit_page(context, chat_id, message_id, unit_id: int, can_rent: bool,
                         locale: str, back_entity: str = "home"):
    api = context.bot_data["api_client"]
    text, keyboard = await build_unit_page(api, unit_id, can_rent, locale,
                                           back_entity=back_entity)
    await edit_message_text_idempotent(
        context.bot,
        chat_id=chat_id,
        message_id=message_id,
        text=H.truncate(text),
        parse_mode=HTML,
        reply_markup=keyboard,
    )


async def build_rent_property_list(api, locale: str):
    """Legacy property-first entry (kept for old cards; the collect list is
    the primary path)."""
    try:
        properties = await api.get_properties()
    except PasayApiError as exc:
        return _load_error(exc.detail, locale), error_keyboard("rent", locale)
    text = f"{H.escape(t('rent.select_property', locale))}："
    return text, property_list_keyboard(properties, locale)


async def show_rent_units(context, chat_id, message_id, property_id: int, locale: str):
    api = context.bot_data["api_client"]
    try:
        properties, units = await asyncio.gather(api.get_properties(), api.get_units())
    except PasayApiError as exc:
        await edit_message_text_idempotent(
            context.bot,
            chat_id=chat_id, message_id=message_id, text=_load_error(exc.detail, locale),
            parse_mode=HTML, reply_markup=error_keyboard("rent", locale),
        )
        return
    prop = next((p for p in properties if p.id == property_id), None)
    prop_name = prop.name if prop else t("ops.property", locale)
    items = sorted(
        (u for u in units if u.property_id == property_id), key=lambda u: u.unit_number
    )
    text = f"{H.escape(t('rent.select_unit', locale, property=prop_name))}："
    await edit_message_text_idempotent(
        context.bot,
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        parse_mode=HTML,
        reply_markup=unit_list_keyboard(items, locale),
    )



# --- V1.2 operations center (待办中心) -------------------------------------

def _ops_due_datetime(task):
    raw = task.due_at
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _ops_sections(tasks: list) -> dict[str, list]:
    """Split PENDING tasks into overdue / today / next7 / all (client-side
    presentation; the backend is the data source of truth)."""
    now = datetime.now()
    start_today = datetime.combine(now.date(), datetime.min.time())
    end_today = start_today + timedelta(days=1)
    end_7 = start_today + timedelta(days=7)
    overdue, today, next7, snoozed = [], [], [], []
    for task in tasks:
        if getattr(task, "snoozed_until", None):
            try:
                if datetime.fromisoformat(str(task.snoozed_until).replace("Z", "+00:00")).replace(
                    tzinfo=None
                ) > now:
                    snoozed.append(task)
                    continue
            except ValueError:
                pass
        due = _ops_due_datetime(task)
        if due is None:
            continue
        due_naive = due.replace(tzinfo=None)
        if due_naive < start_today:
            overdue.append(task)
        elif due_naive < end_today:
            today.append(task)
        if start_today <= due_naive < end_7:
            next7.append(task)
    ordered = sorted
    return {
        "overdue": ordered(overdue, key=lambda x: str(x.due_at or "")),
        "today": ordered(today, key=lambda x: str(x.due_at or "")),
        "next7": ordered(next7, key=lambda x: str(x.due_at or "")),
        "all": ordered(tasks, key=lambda x: str(x.due_at or "")),
    }


async def show_operations_center(context, chat_id: int, locale: str, message_id=None, role=None):
    """待办中心 overview with four section buttons + counts.

    AI-OPS-FOUNDATION-001 §5: the Owner's center counts only their Needs-You
    queue (approvals / payments / decisions / escalations); the Secretary's
    center counts their operational workload."""
    api = context.bot_data["api_client"]
    from pasay_bot.roles import Role
    scope = "owner" if role == Role.OWNER else None
    try:
        summary = await api.get_operations_summary(scope=scope)
    except PasayApiError as exc:
        await _render(context, chat_id, message_id, _load_error(exc.detail, locale),
                      error_keyboard("home", locale))
        return
    text = cards.operations_overview_card(summary, locale)
    await _render(context, chat_id, message_id, text, ops_overview_keyboard(summary, locale))


async def show_copilot(context, chat_id: int, locale: str, message_id=None):
    """🤖 运营助手 — C1.1 deterministic-first TODAY brief (fast, no LLM). Calls
    /today (deterministic path), renders instantly, no free-text input needed."""
    api = context.bot_data["api_client"]
    try:
        today = await api.copilot_today()
    except PasayApiError as exc:
        await _render(context, chat_id, message_id, _load_error(exc.detail, locale),
                      error_keyboard("home", locale))
        return
    text = cards.copilot_today_card(today, locale)
    kb = copilot_today_keyboard(len(today.top_items), locale)
    await _render(context, chat_id, message_id, text, kb)


async def show_copilot_why(context, chat_id: int, message_id: int, item_index: int,
                           locale: str, can_suggest: bool = False):
    """[为什么?] → POST /copilot/why (on-demand LLM, deterministic fallback).
    ``can_suggest`` (C2, owner) adds per-item suggestion action rows for
    actionable items — tapping one leads to the confirm/execute flow."""
    api = context.bot_data["api_client"]
    # Re-fetch the deterministic TODAY to resolve item_ref by 1-based index
    # (avoids encoding backend refs in callback_data).
    try:
        today = await api.copilot_today()
    except PasayApiError as exc:
        await edit_message_text_idempotent(
            context.bot, chat_id=chat_id, message_id=message_id,
            text=_load_error(exc.detail, locale), parse_mode=HTML,
            reply_markup=error_keyboard("home", locale),
        )
        return
    items = today.top_items
    if item_index < 1 or item_index > len(items):
        await _render(context, chat_id, message_id,
                      H.escape("⚠️ 该事项已变化，请重新进入运营助手"), home_keyboard(locale))
        return
    item = items[item_index - 1]
    item_ref = item.item_ref
    try:
        why = await api.copilot_why(item_ref)
    except PasayApiError as exc:
        await _render(context, chat_id, message_id,
                      _load_error(exc.detail, locale), error_keyboard("home", locale))
        return
    text = cards.copilot_why_card(
        why.item_ref,
        why.explanation,
        why.recommendation,
        fallback=why.fallback,
        suggested_action=item.suggested_action if can_suggest else "",
        locale=locale,
    )
    await edit_message_text_idempotent(
        context.bot, chat_id=chat_id, message_id=message_id, text=H.truncate(text),
        parse_mode=HTML,
        reply_markup=copilot_why_keyboard(item_index, item, locale, can_suggest=can_suggest),
    )


async def ask_copilot(context, chat_id: int, locale: str, question: str):
    """[问运营助手] → POST /copilot/ask (on-demand LLM, friendly fallback)."""
    api = context.bot_data["api_client"]
    try:
        ask = await api.copilot_ask(question)
    except PasayApiError as exc:
        await _send(context, chat_id,
                    f"⚠️ {H.escape(t('common.load_error', locale, detail=str(exc.detail))[:120])}",
                    home_keyboard(locale))
        return
    text = cards.copilot_ask_card(ask.answer, fallback=ask.fallback, locale=locale)
    await _send(context, chat_id, H.truncate(text), home_keyboard(locale))


async def show_operations_section(context, chat_id: int, message_id: int, section: str,
                                  locale: str):
    api = context.bot_data["api_client"]
    try:
        tasks, properties = await asyncio.gather(
            api.get_operational_tasks(status="PENDING"),
            api.get_properties(),
        )
    except PasayApiError as exc:
        await _render(context, chat_id, message_id, _load_error(exc.detail, locale),
                      error_keyboard("home", locale))
        return
    sections = _ops_sections(tasks)
    if section == OPS_SECTION_OVERDUE:
        key, rows, title = "ops.section_overdue", sections["overdue"], t("ops.section_overdue", locale)
    elif section == OPS_SECTION_TODAY:
        key, rows, title = "ops.section_today", sections["today"], t("ops.section_today", locale)
    elif section == OPS_SECTION_NEXT7:
        key, rows, title = "ops.section_next7", sections["next7"], t("ops.section_next7", locale)
    else:  # OPS_SECTION_ALL
        key, rows, title = "ops.section_all", sections["all"], t("ops.section_all", locale)
    text = cards.operations_section_card(
        title, rows, properties, locale, empty_key=key + "_empty"
    )
    await edit_message_text_idempotent(
        context.bot,
        chat_id=chat_id,
        message_id=message_id,
        text=H.truncate(text),
        parse_mode=HTML,
        reply_markup=ops_section_keyboard(rows, locale),
    )
