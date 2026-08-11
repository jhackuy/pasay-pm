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
    OPS_OVERVIEW,
    OPS_SECTION_ALL,
    OPS_SECTION_NEXT7,
    OPS_SECTION_OVERDUE,
    OPS_SECTION_TODAY,
    collect_list_keyboard,
    dashboard_keyboard,
    error_keyboard,
    home_keyboard,
    ops_overview_keyboard,
    ops_section_keyboard,
    overdue_page_keyboard,
    pending_page_keyboard,
    property_list_keyboard,
    property_pagination_keyboard,
    unit_list_keyboard,
    unit_page_keyboard,
)
from pasay_bot.handlers.edit_utils import edit_message_text_idempotent
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
    role = role_for_telegram_id(update.effective_user.id if update.effective_user else None)
    if not has_read_permission(role):
        await _refuse(update, context, role)
        return
    await show_dashboard(context, update.effective_chat.id, locale_for(role))


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    role = role_for_telegram_id(update.effective_user.id if update.effective_user else None)
    locale = locale_for(role)
    text = (
        f"📖 <b>{H.escape(t('help.title', locale))}</b>\n\n"
        f"{H.escape(t('help.text', locale))}"
    )
    await context.bot.send_message(
        update.effective_chat.id, text, parse_mode=HTML, reply_markup=dashboard_keyboard(locale)
    )


async def cmd_properties(update: Update, context: ContextTypes.DEFAULT_TYPE):
    role = role_for_telegram_id(update.effective_user.id if update.effective_user else None)
    if not has_read_permission(role):
        await _refuse(update, context, role)
        return
    await show_properties(context, update.effective_chat.id, role, locale_for(role), page=1)


async def cmd_finance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    role = role_for_telegram_id(update.effective_user.id if update.effective_user else None)
    if not has_read_permission(role):
        await _refuse(update, context, role)
        return
    await show_finance(context, update.effective_chat.id, locale_for(role))


async def cmd_overdue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    role = role_for_telegram_id(update.effective_user.id if update.effective_user else None)
    if not has_read_permission(role):
        await _refuse(update, context, role)
        return
    await show_overdue(context, update.effective_chat.id, locale_for(role), page=1)


async def cmd_rent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    role = role_for_telegram_id(update.effective_user.id if update.effective_user else None)
    if not has_read_permission(role):
        await _refuse(update, context, role)
        return
    await show_rent(context, update.effective_chat.id, locale_for(role))


async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Aggregated to-do page (B2): overdue, pending confirm, expiring leases, tasks."""
    role = role_for_telegram_id(update.effective_user.id if update.effective_user else None)
    locale = locale_for(role)
    if not has_read_permission(role):
        await _refuse(update, context, role)
        return
    await show_pending(context, update.effective_chat.id, role, locale)


async def cmd_ops(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """V1.2 待办中心 (/ops, /todo)."""
    role = role_for_telegram_id(update.effective_user.id if update.effective_user else None)
    locale = locale_for(role)
    if not has_permission(role, PERMISSION_OPERATIONS):
        await _refuse(update, context, role)
        return
    await show_operations_center(context, update.effective_chat.id, locale)


async def cmd_copilot(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    await show_dashboard(context, chat_id, locale)


# --- page builders ---

async def _send(context, chat_id, text, keyboard=None):
    await context.bot.send_message(
        chat_id, H.truncate(text), parse_mode=HTML, reply_markup=keyboard
    )


async def _render(context, chat_id, message_id, text, keyboard=None):
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
        await _send(context, chat_id, text, keyboard)


def _load_error(detail: str, locale: str) -> str:
    return f"⚠️ {H.escape(t('common.load_error', locale, detail=str(detail)))}"


async def show_dashboard(context, chat_id, locale: str, message_id=None):
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
    await _render(context, chat_id, message_id, text, dashboard_keyboard(locale))


async def show_menu(context, chat_id, locale: str, message_id=None):
    """The menu IS the dashboard now (backward-compatible name)."""
    await show_dashboard(context, chat_id, locale, message_id=message_id)


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
    """🤖 运营助手 — C1 read-only TODAY brief. No free-text input needed; the
    bot posts an empty body and renders the deterministic grounded brief."""
    api = context.bot_data["api_client"]
    try:
        today = await api.copilot_today()
    except PasayApiError as exc:
        # 503 provider-unavailable / timeout -> clear retryable error, never fabricate.
        await _render(context, chat_id, message_id, _load_error(exc.detail, locale),
                      error_keyboard("home", locale))
        return
    text = cards.copilot_today_card(today, locale)
    await _render(context, chat_id, message_id, text, home_keyboard(locale))


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
