"""Command handlers + page builders shared with the callback router."""
from __future__ import annotations

from datetime import date

from telegram import Update
from telegram.ext import ContextTypes

from pasay_bot.api_client import PasayApiError
from pasay_bot.keyboards import (
    menu_keyboard,
    overdue_page_keyboard,
    pending_list_keyboard,
    property_list_keyboard,
    property_pagination_keyboard,
    unit_list_keyboard,
    unit_page_keyboard,
)
from pasay_bot.render import cards, html as H
from pasay_bot.render.cards import PAGE_SIZE_OVERDUE, PAGE_SIZE_PROPERTIES
from pasay_bot.render.i18n import t
from pasay_bot.roles import (
    PERMISSION_RENT_CONFIRM,
    Role,
    has_permission,
    has_read_permission,
    locale_for,
    role_for_telegram_id,
)

HTML = "HTML"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    role = role_for_telegram_id(update.effective_user.id if update.effective_user else None)
    if not has_read_permission(role):
        await _refuse(update, context, role)
        return
    await show_menu(context, update.effective_chat.id, locale_for(role))


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
        update.effective_chat.id, text, parse_mode=HTML, reply_markup=menu_keyboard(locale)
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
    """OWNER-only pending-income confirmation list (F5)."""
    role = role_for_telegram_id(update.effective_user.id if update.effective_user else None)
    locale = locale_for(role)
    if not has_read_permission(role):
        await _refuse(update, context, role)
        return
    await show_pending(context, update.effective_chat.id, role, locale)


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
        await context.bot.send_message(chat_id, H.escape(t("rent.cancelled", locale)))
        return
    await show_menu(context, chat_id, locale)


async def show_menu(context, chat_id, locale: str):
    text = (
        f"🏠 <b>{H.escape(t('menu.title', locale))}</b>\n\n"
        f"{H.escape(t('menu.hint', locale))}"
    )
    await context.bot.send_message(chat_id, text, parse_mode=HTML, reply_markup=menu_keyboard(locale))


async def _send(context, chat_id, text, keyboard=None):
    await context.bot.send_message(
        chat_id, H.truncate(text), parse_mode=HTML, reply_markup=keyboard
    )


def _load_error(detail: str, locale: str) -> str:
    return f"⚠️ {H.escape(t('common.load_error', locale, detail=str(detail)))}"


async def build_properties_page(api, page: int, locale: str):
    try:
        properties = await api.get_properties()
        units = await api.get_units()
    except PasayApiError as exc:
        return _load_error(exc.detail, locale), None
    stats = cards._stats_by_property(units)
    total_pages = H.total_pages(len(properties), PAGE_SIZE_PROPERTIES)
    page = min(max(page, 1), total_pages)
    text = cards.properties_overview(properties, stats, page, PAGE_SIZE_PROPERTIES, locale)
    keyboard = (
        property_pagination_keyboard(page, total_pages, locale)
        if total_pages > 1
        else None
    )
    return text, keyboard


async def show_properties(context, chat_id, role: Role | None, locale: str, page: int = 1):
    api = context.bot_data["api_client"]
    text, keyboard = await build_properties_page(api, page, locale)
    await _send(context, chat_id, text, keyboard)


async def build_finance_page(api, locale: str):
    try:
        fin = await api.get_financial_summary(date.today().strftime("%Y-%m"))
        overdue = await api.get_overdue_rents()
    except PasayApiError as exc:
        return _load_error(exc.detail, locale), None
    overdue_total = sum(r.total_outstanding for r in overdue)
    return cards.finance_card(fin, overdue_total, locale), None


async def show_finance(context, chat_id, locale: str):
    api = context.bot_data["api_client"]
    text, keyboard = await build_finance_page(api, locale)
    await _send(context, chat_id, text, keyboard)


async def build_overdue_page(api, page: int, locale: str):
    try:
        rows = await api.get_overdue_rents()
    except PasayApiError as exc:
        return _load_error(exc.detail, locale), None
    rows = sorted(rows, key=lambda r: (-r.overdue_days, -r.total_outstanding))
    prop_by_unit: dict[int, str] = {}
    try:
        units = await api.get_units()
        properties = await api.get_properties()
        by_pid = {p.id: p.name for p in properties}
        prop_by_unit = {u.id: by_pid[u.property_id] for u in units if u.property_id in by_pid}
    except PasayApiError:
        pass  # property names are a nice-to-have on the overdue page
    total_pages = H.total_pages(len(rows), PAGE_SIZE_OVERDUE)
    page = min(max(page, 1), total_pages)
    text = cards.overdue_list(rows, page, PAGE_SIZE_OVERDUE, locale, prop_by_unit)
    page_rows = rows[(page - 1) * PAGE_SIZE_OVERDUE: page * PAGE_SIZE_OVERDUE]
    keyboard = overdue_page_keyboard(page_rows, page, total_pages, locale) if rows else None
    return text, keyboard


async def show_overdue(context, chat_id, locale: str, page: int = 1):
    api = context.bot_data["api_client"]
    text, keyboard = await build_overdue_page(api, page, locale)
    await _send(context, chat_id, text, keyboard)


async def build_rent_property_list(api, locale: str):
    try:
        properties = await api.get_properties()
    except PasayApiError as exc:
        return _load_error(exc.detail, locale), None
    text = f"{H.escape(t('rent.select_property', locale))}："
    return text, property_list_keyboard(properties, locale)


async def show_rent(context, chat_id, locale: str):
    api = context.bot_data["api_client"]
    text, keyboard = await build_rent_property_list(api, locale)
    await _send(context, chat_id, text, keyboard)


async def show_pending(context, chat_id, role, locale: str):
    """Render the pending-income list; confirm buttons only for confirm roles."""
    api = context.bot_data["api_client"]
    try:
        incomes = await api.list_incomes()
        leases = await api.get_leases()
        units = await api.get_units()
        properties = await api.get_properties()
        tenants = await api.get_tenants()
    except PasayApiError as exc:
        await _send(context, chat_id, _load_error(exc.detail, locale))
        return
    pending = sorted(
        (i for i in incomes if i.status == "pending"),
        key=lambda i: (i.received_date, i.id),
    )
    by_lease = {l.id: l for l in leases}
    by_unit = {u.id: u for u in units}
    by_prop = {p.id: p for p in properties}
    by_tenant = {t.id: t for t in tenants}
    rows = []
    for inc in pending:
        lease = by_lease.get(inc.lease_id) if inc.lease_id else None
        unit = by_unit.get(lease.unit_id) if lease else None
        prop = by_prop.get(unit.property_id) if unit else None
        tenant = by_tenant.get(lease.tenant_id) if lease else None
        rows.append(
            {
                "id": inc.id,
                "amount": inc.amount,
                "received_date": inc.received_date,
                "method": inc.payment_method,
                "property_name": prop.name if prop else "",
                "unit_number": unit.unit_number if unit else "",
                "tenant_name": tenant.full_name if tenant else None,
            }
        )
    text = cards.pending_list_card(rows, locale)
    keyboard = None
    if rows and has_permission(role, PERMISSION_RENT_CONFIRM):
        keyboard = pending_list_keyboard(
            [(r["id"], f"✅ #{r['id']} {H.money(r['amount'])}") for r in rows],
            locale,
        )
    await _send(context, chat_id, text, keyboard)


async def build_unit_page(api, unit_id: int, can_rent: bool, locale: str):
    """Unit page used by both the rent flow and the overdue detail view."""
    try:
        unit = await api.get_unit(unit_id)
        properties = await api.get_properties()
        leases = await api.get_leases()
        tenants = await api.get_tenants()
    except PasayApiError as exc:
        return _load_error(exc.detail, locale), None
    prop = next((p for p in properties if p.id == unit.property_id), None)
    prop_name = prop.name if prop else "?"
    address = prop.address if prop else ""
    active = next(
        (l for l in leases if l.unit_id == unit.id and l.status == "active"), None
    )
    tenant_name = None
    if active:
        tenant = next((tn for tn in tenants if tn.id == active.tenant_id), None)
        tenant_name = tenant.full_name if tenant else None
    text = cards.unit_card(unit, prop_name, address, active, tenant_name, locale)
    keyboard = unit_page_keyboard(unit_id, can_rent, locale)
    return text, keyboard


async def show_unit_page(context, chat_id, message_id, unit_id: int, can_rent: bool, locale: str):
    api = context.bot_data["api_client"]
    text, keyboard = await build_unit_page(api, unit_id, can_rent, locale)
    await context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=H.truncate(text),
        parse_mode=HTML,
        reply_markup=keyboard,
    )


async def show_rent_units(context, chat_id, message_id, property_id: int, locale: str):
    api = context.bot_data["api_client"]
    try:
        properties = await api.get_properties()
        units = await api.get_units()
    except PasayApiError as exc:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=_load_error(exc.detail, locale),
            parse_mode=HTML,
        )
        return
    prop = next((p for p in properties if p.id == property_id), None)
    prop_name = prop.name if prop else f"#{property_id}"
    items = sorted(
        (u for u in units if u.property_id == property_id), key=lambda u: u.unit_number
    )
    text = f"{H.escape(t('rent.select_unit', locale, property=prop_name))}："
    await context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        parse_mode=HTML,
        reply_markup=unit_list_keyboard(items, locale),
    )
