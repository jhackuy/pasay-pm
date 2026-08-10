"""Deterministic HTML cards. All message text must be built here (or via
html helpers) — no ad-hoc f-string message assembly in handlers."""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from pasay_bot.api_client import (
    FinancialSummary,
    Income,
    Lease,
    OverdueRent,
    Property,
    Unit,
)
from pasay_bot.render import html as H
from pasay_bot.render.i18n import t

DIVIDER = "━" * 12
PAGE_SIZE_PROPERTIES = 5
PAGE_SIZE_OVERDUE = 5


def _stats_by_property(units: list[Unit]) -> dict[int, dict[str, int]]:
    stats: dict[int, dict[str, int]] = {}
    for u in units:
        s = stats.setdefault(u.property_id, {"occupied": 0, "vacant": 0, "total": 0})
        s["total"] += 1
        if u.status == "occupied":
            s["occupied"] += 1
        elif u.status == "vacant":
            s["vacant"] += 1
    return stats


def property_card(prop: Property, stats: dict, locale: str = "zh") -> str:
    occupied = int(stats.get("occupied", 0))
    vacant = int(stats.get("vacant", 0))
    total_units = int(stats.get("total", prop.total_units or 0))
    rate = H.percent(occupied, total_units) if total_units else "0.0"
    lines = [
        f"🏢 <b>{H.escape(prop.name)}</b>",
        f"📍 {H.escape(prop.address)}",
        f"🚪 {H.escape(t('properties.units', locale))}：{total_units}",
        f"🟢 {H.escape(t('properties.occupied', locale))}：{occupied}",
        f"⚪ {H.escape(t('properties.vacant', locale))}：{vacant}",
        f"{H.escape(t('properties.occupancy_rate', locale))}：{rate}%",
    ]
    return "\n".join(lines)


def properties_overview(
    properties: list[Property],
    stats_by_property: Optional[dict[int, dict[str, int]]] = None,
    page: int = 1,
    page_size: int = PAGE_SIZE_PROPERTIES,
    locale: str = "zh",
) -> str:
    if not properties:
        return (
            f"🏘 <b>{H.escape(t('properties.title', locale))}</b>\n\n"
            f"{H.escape(t('properties.empty', locale))}"
        )
    stats = stats_by_property or {}
    total_pages = H.total_pages(len(properties), page_size)
    page = min(max(page, 1), total_pages)
    items = properties[(page - 1) * page_size: page * page_size]

    blocks = [f"🏘 <b>{H.escape(t('properties.title', locale))}</b>"]
    for p in items:
        blocks.append(property_card(p, stats.get(p.id, {}), locale))

    total_units = sum(int(s.get("total", 0)) for s in stats.values())
    occupied = sum(int(s.get("occupied", 0)) for s in stats.values())
    vacant = sum(int(s.get("vacant", 0)) for s in stats.values())
    rate = H.percent(occupied, total_units) if total_units else "0.0"
    blocks.append(DIVIDER)
    blocks.append(
        f"📊 {H.escape(t('properties.total', locale))}：{len(properties)} "
        f"{H.escape(t('properties.unit_classifier', locale))}"
    )
    blocks.append(f"🟢 {H.escape(t('properties.occupied', locale))}：{occupied}")
    blocks.append(f"⚪ {H.escape(t('properties.vacant', locale))}：{vacant}")
    blocks.append(f"{H.escape(t('properties.occupancy_rate', locale))}：{rate}%")
    blocks.append(H.pagination_footer(page, page_size, len(properties), locale))
    return "\n\n".join(blocks)


def finance_card(fin: FinancialSummary, overdue_total, locale: str = "zh") -> str:
    year = str(fin.month)[:4]
    mm = str(fin.month)[5:7]
    if locale == "en":
        title = f"{H.format_month(fin.month, 'en')} Finance"
    else:
        month_label = str(int(mm)) if mm.isdigit() else mm
        title = t("finance.title", locale, year=H.escape(year), month=H.escape(month_label))
    blocks = [f"💰 <b>{title}</b>"]
    blocks.append(H.escape(t("finance.rent", locale)))
    blocks.append(f"{H.escape(t('finance.expected', locale))}：{H.money(fin.expected_rent_total)}")
    blocks.append(f"{H.escape(t('finance.collected', locale))}：{H.money(fin.collected_rent)}")
    blocks.append(
        f"{H.escape(t('finance.outstanding', locale))}："
        f"<b>{H.money(fin.outstanding_rent)}</b>"
    )
    blocks.append(
        f"{H.escape(t('finance.collection_rate', locale))}："
        f"{H.percent(fin.collected_rent, fin.expected_rent_total)}%"
    )
    blocks.append(H.escape(t("finance.income_expense", locale)))
    blocks.append(f"{H.escape(t('finance.total_income', locale))}：{H.money(fin.total_income)}")
    blocks.append(f"{H.escape(t('finance.total_expense', locale))}：{H.money(fin.total_expense)}")
    blocks.append(f"{H.escape(t('finance.net_income', locale))}：<b>{H.money(fin.net_income)}</b>")
    if _dec(overdue_total) > 0:
        blocks.append(f"{H.escape(t('finance.overdue_warning', locale))}：{H.money(overdue_total)}")
    return "\n".join(blocks)


def _dec(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value or 0))


def overdue_emoji(days: int) -> str:
    """Overdue entries are action items; all render the 🔴 severity marker."""
    return "🔴"


def overdue_block(row: OverdueRent, property_name: str = "", locale: str = "zh") -> str:
    unit_label = (
        f"{H.escape(property_name)} · Unit {H.escape(row.unit)}"
        if property_name
        else f"Unit {H.escape(row.unit)}"
    )
    lines = [
        f"{overdue_emoji(row.overdue_days)} <b>{unit_label}</b>",
        f"{H.escape(t('overdue.tenant', locale))}：{H.escape(row.tenant)}",
        f"{H.escape(t('overdue.amount_due', locale))}：{H.money(row.total_outstanding)}",
        f"{H.escape(t('overdue.due_date', locale))}：{H.format_date(row.oldest_due_date)}",
        f"{H.escape(t('overdue.days', locale))}：{row.overdue_days}{H.escape(t('overdue.days_unit', locale))}",
    ]
    return "\n".join(lines)


def overdue_list(
    rows: list[OverdueRent],
    page: int = 1,
    page_size: int = PAGE_SIZE_OVERDUE,
    locale: str = "zh",
    property_by_unit: Optional[dict[int, str]] = None,
) -> str:
    if not rows:
        return (
            f"⚠️ <b>{H.escape(t('overdue.title', locale, count=0))}</b>\n\n"
            f"{H.escape(t('overdue.empty', locale))}"
        )
    # Sort: overdue days desc -> amount desc (design §8).
    rows = sorted(rows, key=lambda r: (-r.overdue_days, -r.total_outstanding))
    total_pages = H.total_pages(len(rows), page_size)
    page = min(max(page, 1), total_pages)
    items = rows[(page - 1) * page_size: page * page_size]
    prop_names = property_by_unit or {}
    blocks = [f"⚠️ <b>{H.escape(t('overdue.title', locale, count=len(rows)))}</b>"]
    for row in items:
        blocks.append(overdue_block(row, prop_names.get(row.unit_id, ""), locale))
    blocks.append(H.pagination_footer(page, page_size, len(rows), locale))
    return "\n\n".join(blocks)


def unit_status_label(status: str, locale: str = "zh") -> str:
    if status == "occupied":
        return t("unit.status_occupied", locale)
    if status == "vacant":
        return t("unit.status_vacant", locale)
    return t("unit.status_unknown", locale)


def unit_card(
    unit: Unit,
    property_name: str,
    address: str,
    lease: Optional[Lease],
    tenant_name: Optional[str],
    locale: str = "zh",
) -> str:
    lease_or_unit_rent = lease.monthly_rent if lease else unit.monthly_rent
    lines = [
        f"🏢 <b>{H.escape(property_name)} · Unit {H.escape(unit.unit_number)}</b>",
        f"📍 {H.escape(address)}",
        f"{H.escape(t('unit.tenant', locale))}："
        f"{H.escape(tenant_name) if tenant_name else H.escape(t('unit.no_tenant', locale))}",
        f"{H.escape(t('unit.monthly_rent', locale))}：{H.money(lease_or_unit_rent)}",
        f"{H.escape(t('unit.status', locale))}：{unit_status_label(unit.status, locale)}",
    ]
    return "\n".join(lines)


def rent_confirm_card(
    property_name: str,
    unit_number: str,
    amount,
    date_str: str,
    method: str,
    locale: str = "zh",
) -> str:
    lines = [
        f"💵 <b>{H.escape(t('rent.confirm_title', locale))}</b>",
        f"{H.escape(t('rent.property', locale))}：{H.escape(property_name)}",
        f"{H.escape(t('rent.unit', locale))}：{H.escape(unit_number)}",
        f"{H.escape(t('rent.amount', locale))}：<b>{H.money(amount)}</b>",
        f"{H.escape(t('rent.date', locale))}：{H.escape(date_str)}",
        f"{H.escape(t('rent.method', locale))}：{H.escape(method)}",
    ]
    return "\n".join(lines)


def rent_success_card(
    income: Income, property_name: str, unit_number: str, locale: str = "zh"
) -> str:
    return t(
        "rent.success",
        locale,
        property=H.escape(property_name),
        unit=H.escape(unit_number),
        amount=H.money(income.amount),
        date=H.format_date(income.received_date),
        method=H.escape(income.payment_method or "-"),
        income_id=income.id,
    )


def pending_recorded_card(
    income: Income, property_name: str, unit_number: str, locale: str = "zh"
) -> str:
    return t(
        "rent.pending_recorded",
        locale,
        property=H.escape(property_name),
        unit=H.escape(unit_number),
        amount=H.money(income.amount),
        date=H.format_date(income.received_date),
        method=H.escape(income.payment_method or "-"),
        income_id=income.id,
    )


def reversed_card(income: Income, locale: str = "zh") -> str:
    return t(
        "rent.reversed",
        locale,
        income_id=income.id,
        amount=H.money(-income.amount),
        date=H.format_date(income.received_date),
    )


def pending_list_card(rows: list[dict], locale: str = "zh") -> str:
    """Pending-income list for the /pending command (F5).

    ``rows`` items: id, amount, received_date, method, property_name,
    unit_number, tenant_name.
    """
    if not rows:
        return (
            f"📋 <b>{H.escape(t('pending.title', locale))}</b>\n\n"
            f"{H.escape(t('pending.empty', locale))}"
        )
    blocks = [f"📋 <b>{H.escape(t('pending.title', locale))}</b>"]
    for r in rows:
        where = " · ".join(
            x for x in (r.get("property_name"), r.get("unit_number")) if x
        )
        tenant = H.escape(r["tenant_name"]) if r.get("tenant_name") else "-"
        blocks.append(
            f"#{r['id']} · {H.escape(where)}\n"
            f"{H.escape(t('overdue.tenant', locale))}：{tenant}\n"
            f"{H.money(r['amount'])} · {H.format_date(r['received_date'])}"
            f" · {H.escape(r.get('method') or '-')}"
        )
    blocks.append(H.escape(t("pending.hint", locale)))
    return "\n\n".join(blocks)
