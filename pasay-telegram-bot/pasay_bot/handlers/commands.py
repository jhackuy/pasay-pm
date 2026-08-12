"""Command handlers + page builders shared with the callback router.

V1.1 UX: the home page is a live dashboard ("today management center"),
secondary pages are edit-first, and the rent flow compresses to
home -> pick unpaid unit -> confirm with smart defaults.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

from telegram import Update
from telegram.ext import ContextTypes

from pasay_bot.api_client import PasayApiError
from pasay_bot.keyboards import (
    ACTION_COPILOT_ASK,
    ACTION_COPILOT_NAV,
    ACTION_COPILOT_WHY,
    OPS_OVERVIEW,
    OPS_SECTION_ALL,
    OPS_SECTION_NEXT7,
    OPS_SECTION_OVERDUE,
    OPS_SECTION_TODAY,
    collect_list_keyboard,
    copilot_why_keyboard,
    copilot_today_keyboard,
    dashboard_keyboard,
    error_keyboard,
    home_keyboard,
    reply_keyboard,
    ops_overview_keyboard,
    ops_section_keyboard,
    overdue_page_keyboard,
    pending_page_keyboard,
    property_list_keyboard,
    property_pagination_keyboard,
    todo_keyboard,
    unit_list_keyboard,
    unit_page_keyboard,
)
from pasay_bot.handlers.edit_utils import edit_message_text_idempotent
from pasay_bot.roles import Role
from pasay_bot.render import cards, html as H
from pasay_bot.render.cards import PAGE_SIZE_OVERDUE, PAGE_SIZE_PROPERTIES
from pasay_bot.render.i18n import t
from pasay_bot.roles import (
    PERMISSION_OPERATIONS,
    PERMISSION_RENT_CONFIRM,
    Role,
    has_permission,
    has_read_permission,
    locale_for,
    role_for_telegram_id,
)

HTML = "HTML"

EXPIRING_LEASE_DAYS = 60
TASK_WINDOW_DAYS = 7


def _bind_identity(update, context):
    api = context.bot_data["api_client"]
    admin = context.bot_data.get("admin_api_client")
    # Clear first so even a malformed update cannot inherit the previous
    # sequential update's identity in the same asyncio task.
    api.clear_telegram_user()
    if admin is not None:
        admin.clear_telegram_user()
    if update.effective_user is None:
        raise ValueError("Telegram update has no effective_user")
    api.bind_telegram_user(update.effective_user.id)
    if admin is not None:
        admin.bind_telegram_user(update.effective_user.id)


def _current_month() -> str:
    return date.today().strftime("%Y-%m")


def _period_covered(incomes, lease_id: int, month: str) -> bool:
    """True when the lease has a confirmed income covering ``month`` (matched
    via the backend's 'rent YYYY-MM' description, falling back to the
    received-date month). Mirrors the backend's coverage rule."""
    for inc in incomes:
        if inc.lease_id != lease_id or inc.status != "confirmed":
            continue
        if month in (inc.description or ""):
            return True
        if inc.received_date and inc.received_date.strftime("%Y-%m") == month:
            return True
    return False


def _last_income_for_lease(incomes, lease_id: int):
    rows = [i for i in incomes if i.lease_id == lease_id]
    if not rows:
        return None
    return max(rows, key=lambda i: (i.received_date, i.id))


# --- commands ---

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _bind_identity(update, context)
    role = role_for_telegram_id(update.effective_user.id if update.effective_user else None)
    if not has_read_permission(role):
        await _refuse(update, context, role)
        return
    await show_dashboard(context, update.effective_chat.id, locale_for(role), role=role)


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _bind_identity(update, context)
    role = role_for_telegram_id(update.effective_user.id if update.effective_user else None)
    locale = locale_for(role)
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
    await show_dashboard(context, chat_id, locale, role=role)


# --- page builders ---

async def _send(context, chat_id, text, keyboard=None, reply_keyboard=None):
    await context.bot.send_message(
        chat_id, H.truncate(text), parse_mode=HTML,
        reply_markup=keyboard if keyboard is not None else reply_keyboard,
    )


async def _render(context, chat_id, message_id, text, keyboard=None, reply_keyboard=None):
    """edit-first: when a message_id is known we edit it, else send (B6)."""
    if message_id:
        await edit_message_text_idempotent(
            context.bot,
            chat_id=chat_id,
            message_id=message_id,
            text=H.truncate(text),
            parse_mode=HTML,
            reply_markup=keyboard,
        )
    else:
        await _send(context, chat_id, text, keyboard, reply_keyboard=reply_keyboard)


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
    """Unpaid units only: paid units and vacant units are hidden; overdue
    units sort first. Each row carries the current-month receivable."""
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
    month = _current_month()
    lease_by_unit = {l.unit_id: l for l in leases if l.status == "active"}
    prop_by_id = {p.id: p.name for p in properties}
    overdue_by_lease = {r.lease_id: r for r in overdue}
    rows = []
    for u in units:
        lease = lease_by_unit.get(u.id)
        if lease is None:
            continue  # vacant / no active lease -> no collect button (B5)
        if _period_covered(incomes, lease.id, month):
            continue  # paid this month -> hidden (B5)
        ovd = overdue_by_lease.get(lease.id)
        rows.append(
            {
                "unit_id": u.id,
                "unit_number": u.unit_number,
                "property_name": prop_by_id.get(u.property_id, ""),
                "amount": lease.monthly_rent,
                "overdue_days": ovd.overdue_days if ovd else 0,
                "lease_id": lease.id,
            }
        )
    rows.sort(key=lambda r: (-r["overdue_days"], r["unit_number"]))
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
    Owner sees expense approvals, pending income confirmations, overdue rent
    and their tasks; Secretary sees the tasks the backend scoped to them.
    Every row carries its action button (action-at-source)."""
    api = context.bot_data["api_client"]
    expenses, incomes, overdue, tasks = await asyncio.gather(
        api.list_expenses(),
        api.list_incomes(),
        api.get_overdue_rents(),
        api.get_operational_tasks(status="PENDING"),
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
        pending_expenses = sorted(
            (e for e in expenses if (e.status or "").lower() == "pending"),
            key=lambda e: (e.due_date or e.expense_date, e.id),
        )
        expense_rows = [
            {
                "id": e.id,
                "category": e.category,
                "payee": e.payee,
                "amount": e.amount,
                "location": _expense_location(e, units, properties),
                "has_receipt": bool(e.receipt_attachment_id),
            }
            for e in pending_expenses
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

    overdue_rows = []
    if owner_view:
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

    task_rows = sorted(tasks, key=lambda x: (x.due_at is None, x.due_at or ""))
    sections = {
        "expenses": expense_rows,
        "confirm": confirm_rows,
        "overdue": overdue_rows,
        "tasks": task_rows,
    }
    text = cards.todo_overview_card(sections, locale)
    keyboard = todo_keyboard(sections, owner_view=owner_view, locale=locale)
    # The persistent bottom keyboard stays visible from /start; this message
    # carries the per-row action buttons (action-at-source).
    await _render(context, chat_id, message_id, text, keyboard)


# --- unit page (state-driven, B5) ---

async def build_unit_page(api, unit_id: int, can_rent: bool, locale: str,
                          back_entity: str = "home"):
    """Unit page used by the rent flow, overdue detail and payment view."""
    try:
        unit, properties, leases, tenants, incomes, overdue = await asyncio.gather(
            api.get_unit(unit_id),
            api.get_properties(),
            api.get_leases(),
            api.get_tenants(),
            api.list_incomes(),
            api.get_overdue_rents(),
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
    prop_name = prop.name if prop else f"#{property_id}"
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


async def show_operations_center(context, chat_id: int, locale: str, message_id=None):
    """待办中心 overview with four section buttons + counts."""
    api = context.bot_data["api_client"]
    try:
        summary = await api.get_operations_summary()
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
