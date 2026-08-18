"""Deterministic HTML cards. All message text must be built here (or via
html helpers) — no ad-hoc f-string message assembly in handlers."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

from pasay_bot.api_client import (
    CopilotExecute,
    CopilotRecommend,
    CopilotToday,
    CopilotTodayItem,
    Expense,
    FinancialSummary,
    Income,
    Lease,
    OverdueRent,
    Property,
    RentMatchCandidate,
    Unit,
)
from pasay_bot.render import html as H
from pasay_bot.render.i18n import t

DIVIDER = "━" * 12
PAGE_SIZE_PROPERTIES = 5
PAGE_SIZE_OVERDUE = 5
MANILA_TZ = ZoneInfo("Asia/Manila")


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


def _pct(value) -> str:
    """Render a percentage number without the peso sign and without a
    trailing '.00' (e.g. '34.00' -> '34', '33.5' -> '33.5')."""
    try:
        d = _dec(value)
    except Exception:  # noqa: BLE001 - never crash a card on a bad rate
        return "0"
    d = d.quantize(Decimal("0.1"))
    return format(d, "f").rstrip("0").rstrip(".") or "0"


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


def _rent_status_line(
    locale: str,
    paid: bool,
    outstanding=None,
    overdue_days: int = 0,
    overdue_months: int = 0,
    partial: bool = False,
) -> str:
    """One human status line: paid, or unpaid + owed + overdue (days, else
    periods). Internal enum/DB values are never rendered."""
    if paid:
        return H.escape(t("rent_status.paid_full", locale))
    if partial:
        return H.escape(t("rent_status.partial", locale))
    parts = [H.escape(t("rent_status.unpaid", locale))]
    if outstanding is not None and _dec(outstanding) > 0:
        parts.append(
            H.escape(t("rent_status.owes", locale, amount=H.money(outstanding)))
        )
    if overdue_days > 0:
        parts.append(
            H.escape(t("rent_status.overdue_days", locale, days=overdue_days))
        )
    elif overdue_months > 0:
        parts.append(
            H.escape(t("rent_status.overdue_periods", locale, count=overdue_months))
        )
    return " · ".join(parts)


def rent_status_card(
    *,
    locale: str = "zh",
    unit_number: str = "",
    property_name: str = "",
    tenant_name: str = "",
    monthly_rent=None,
    paid: bool = False,
    outstanding=None,
    overdue_days: int = 0,
    overdue_months: int = 0,
    month: str = "",
    paid_amount=None,
    due_amount=None,
    remaining=None,
) -> str:
    """Single unit / tenant rent status answer (V1.3 Slice 2, Entry C).

    Answers "1608 交了没有 / John 交了吗" style queries: unit + tenant +
    monthly rent + this month's paid/unpaid state (with owed amount and
    overdue days/periods when unpaid). Read-only text; no buttons."""
    unit_label = (
        f"{H.escape(property_name)} · Unit {H.escape(unit_number)}"
        if property_name
        else f"Unit {H.escape(unit_number)}"
    )
    lines = [f"🏢 <b>{unit_label}</b>"]
    if tenant_name:
        lines.append(f"{H.escape(t('overdue.tenant', locale))}：{H.escape(tenant_name)}")
    if monthly_rent is not None:
        lines.append(
            f"{H.escape(t('unit.monthly_rent', locale))}：{H.money(monthly_rent)}"
        )
    period = period_label(month, locale) if month else str(month)
    partial = (
        not paid
        and _dec(paid_amount or 0) > 0
        and _dec(remaining or 0) > 0
    )
    status_line = _rent_status_line(
        locale, paid, outstanding, overdue_days, overdue_months, partial
    )
    lines.append(
        f"📅 {period}：{status_line}"
    )
    if due_amount is not None:
        lines.append(
            H.escape(t("rent_status.due_line", locale, amount=H.money(due_amount)))
        )
    if paid_amount is not None:
        lines.append(
            H.escape(t("rent_status.paid_line", locale, amount=H.money(paid_amount)))
        )
    if remaining is not None and _dec(remaining) > 0:
        lines.append(
            H.escape(t("rent_status.remain_line", locale, amount=H.money(remaining)))
        )
    return "\n".join(lines)


def rent_status_selector_card(candidates: list[dict], locale: str = "zh") -> str:
    """Multi-match selector heading (V1.3 Slice 2, Entry D).

    The candidate rows are inline buttons (property · unit · tenant) built by
    ``rent_status_candidates_keyboard``, so the text carries only the shared
    heading — the same information as the old text-only candidates list, with
    every row now a tappable choice. Never auto-selects, never writes."""
    return f"👥 <b>{H.escape(t('rent_status.multiple', locale))}</b>"


def rent_status_card_for_candidate(candidate: dict, locale: str = "zh") -> str:
    """Render the exact single-hit status card for one selector candidate.

    Shared by the single-match NL answer and the read-only selector tap so a
    chosen candidate is byte-identical to asking about it directly."""
    return rent_status_card(
        locale=locale,
        unit_number=str(candidate.get("unit_number") or ""),
        property_name=str(candidate.get("property_name") or ""),
        tenant_name=str(candidate.get("tenant_name") or ""),
        monthly_rent=candidate.get("monthly_rent"),
        paid=bool(candidate.get("paid")),
        outstanding=candidate.get("outstanding"),
        overdue_days=int(candidate.get("overdue_days") or 0),
        overdue_months=int(candidate.get("overdue_months") or 0),
        month=str(candidate.get("month") or ""),
        paid_amount=candidate.get("paid_amount"),
        due_amount=candidate.get("due_amount"),
        remaining=candidate.get("remaining") or candidate.get("outstanding"),
    )


# --- P1-PASAY-NIGHTLY-PRODUCT-HARDENING-008 C: rent payment history ---------

def rent_history_card(
    *,
    locale: str = "zh",
    unit_number: str = "",
    property_name: str = "",
    tenant_name: str = "",
    count: int = 0,
    cumulative=None,
    latest_date: str = "",
    month: str = "",
) -> str:
    """'1608 交了几次 / 累计交了多少 / 最近什么时候交的' answer card.

    Counts CONFIRMED payment events (partial payments each count once),
    cumulative = sum of confirmed amounts, latest = newest confirmed
    received_date. Pending/reversed rows are excluded by the caller's
    financial semantics. ``month`` narrows the answer to one rent period.
    Read-only text; no buttons."""
    unit_label = (
        f"{H.escape(property_name)} · Unit {H.escape(unit_number)}"
        if property_name
        else f"Unit {H.escape(unit_number)}"
    )
    title_key = "rent_history.title_month" if month else "rent_history.title"
    if month:
        title = t(title_key, locale, month=H.escape(month))
    else:
        title = t(title_key, locale)
    lines = [f"<b>{H.escape(title)}</b>", unit_label]
    if tenant_name:
        lines.append(f"{H.escape(t('overdue.tenant', locale))}：{H.escape(tenant_name)}")
    if count <= 0:
        lines.append(H.escape(t("rent_history.none", locale)))
        return "\n".join(lines)
    total_line = (
        f"{H.escape(t('rent_history.count', locale, count=count))} · "
        f"{H.escape(t('rent_history.total', locale, amount=H.money(cumulative)))}"
    )
    lines.append(total_line)
    if latest_date:
        lines.append(
            H.escape(t("rent_history.latest", locale, date=H.format_date(latest_date)))
        )
    return "\n".join(lines)


def rent_history_card_for_candidate(candidate: dict, locale: str = "zh") -> str:
    """Single-hit history card for one selector candidate (byte-identical to
    the direct single-match answer)."""
    return rent_history_card(
        locale=locale,
        unit_number=str(candidate.get("unit_number") or ""),
        property_name=str(candidate.get("property_name") or ""),
        tenant_name=str(candidate.get("tenant_name") or ""),
        count=int(candidate.get("count") or 0),
        cumulative=candidate.get("cumulative"),
        latest_date=str(candidate.get("latest_date") or ""),
        month=str(candidate.get("month") or ""),
    )


def rent_history_selector_card(candidates: list[dict], locale: str = "zh") -> str:
    """Multi-match heading for payment-history questions; the rows are inline
    buttons built by ``rent_history_candidates_keyboard`` (no auto-select)."""
    return f"👥 <b>{H.escape(t('rent_status.multiple', locale))}</b>"


def unpaid_list_card(
    rows: list[OverdueRent],
    month: str = "",
    locale: str = "zh",
    property_by_unit: Optional[dict[int, str]] = None,
    max_rows: int = 20,
) -> str:
    """Who hasn't paid this month (reuses the overdue row rendering). Single
    page; the card truncates at Telegram's 4096-UTF-16 limit and notes any
    rows hidden beyond ``max_rows``."""
    if not rows:
        return f"🎉 <b>{H.escape(t('rent.collect_all_paid', locale))}</b>"
    rows = sorted(rows, key=lambda r: (-r.overdue_days, r.unit))
    period = period_label(month, locale) if month else str(month)
    blocks = [
        f"⚠️ <b>{H.escape(t('rent_status.unpaid_title', locale, period=period, count=len(rows)))}</b>"
    ]
    prop_names = property_by_unit or {}
    for row in rows[:max_rows]:
        blocks.append(overdue_block(row, prop_names.get(row.unit_id, ""), locale))
    if len(rows) > max_rows:
        blocks.append(
            H.escape(t("rent_status.more", locale, count=len(rows) - max_rows))
        )
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
    payment_state: Optional[str] = None,
) -> str:
    """Unit page card. ``payment_state`` is one of 'paid' / 'unpaid' / None
    (vacant or no active lease) and drives the state line (B5)."""
    lease_or_unit_rent = lease.monthly_rent if lease else unit.monthly_rent
    lines = [
        f"🏢 <b>{H.escape(property_name)} · Unit {H.escape(unit.unit_number)}</b>",
        f"📍 {H.escape(address)}",
        f"{H.escape(t('unit.tenant', locale))}："
        f"{H.escape(tenant_name) if tenant_name else H.escape(t('unit.no_tenant', locale))}",
        f"{H.escape(t('unit.monthly_rent', locale))}：{H.money(lease_or_unit_rent)}",
        f"{H.escape(t('unit.status', locale))}：{unit_status_label(unit.status, locale)}",
    ]
    if payment_state == "paid":
        lines.append(H.escape(t("unit.payment_paid", locale)))
    elif payment_state == "unpaid":
        lines.append(H.escape(t("unit.payment_unpaid", locale)))
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


def period_label(period: str, locale: str = "zh") -> str:
    """'2026-08' -> '8月租金' / 'Aug rent' (human only)."""
    year, _, mm = str(period).partition("-")
    if not mm.isdigit() or not 1 <= int(mm) <= 12:
        return str(period)
    if locale == "en":
        names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                 "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        return f"{names[int(mm) - 1]} rent"
    return f"{int(mm)}月租金"


def rent_match_card(
    candidate: RentMatchCandidate,
    received_date: str,
    locale: str = "zh",
    can_confirm: bool = True,
) -> str:
    """Entry B confirm card: exact payment found, action-at-source."""
    partial = (
        _dec(candidate.paid_amount or 0) > 0
        or _dec(candidate.amount) < _dec(candidate.due_amount)
    )
    if partial:
        paid_total = _dec(candidate.paid_amount) + _dec(candidate.amount)
        lines = [
            f"<b>{H.escape(t('rent.match_partial_title', locale))}</b>",
            f"{H.escape(candidate.property_name)} {H.escape(candidate.unit_number)}"
            f" · {period_label(candidate.period, locale)}",
            f"{H.escape(t('rent.match_partial_due', locale))}：{H.money(candidate.due_amount)}",
            f"{H.escape(t('rent.match_partial_received', locale))}：{H.money(candidate.amount)}",
            f"{H.escape(t('rent.match_partial_paid_total', locale))}：{H.money(paid_total)}",
            f"{H.escape(t('rent.match_partial_remaining', locale))}：{H.money(candidate.remaining_balance)}",
            "",
            H.escape(t("rent.match_unique", locale)),
            H.escape(t("rent.match_no_duplicate", locale)),
        ]
        if not can_confirm:
            lines.append(H.escape(t("rent.owner_only_confirm", locale)))
        return "\n".join(lines)
    lines = [
        f"💵 <b>{H.escape(t('rent.match_found_title', locale))}</b>",
        f"{H.escape(candidate.property_name)} {H.escape(candidate.unit_number)}"
        f" · {period_label(candidate.period, locale)}",
        f"{H.escape(t('rent.match_expected', locale))}：{H.money(candidate.amount)}",
        f"{H.escape(t('rent.match_received', locale))}：{H.money(candidate.amount)}",
        "",
        H.escape(t("rent.match_amount_ok", locale)),
        H.escape(t("rent.match_unique", locale)),
        H.escape(t("rent.match_no_duplicate", locale)),
    ]
    if not can_confirm:
        lines.append(H.escape(t("rent.owner_only_confirm", locale)))
    return "\n".join(lines)


def rent_match_partial_success_card(
    unit_number: str,
    amount,
    due_amount,
    paid_amount,
    remaining_balance,
    locale: str = "zh",
) -> str:
    """SLICE2-RENT-005 payment-recorded card: due / cumulative paid / remaining.
    A settled month adds the paid-in-full line. No internal ids or state."""
    paid_total = _dec(paid_amount) + _dec(amount)
    text = t(
        "rent.match_partial_success",
        locale,
        unit=H.escape(unit_number),
        amount=H.money(amount),
        due=H.money(due_amount),
        paid=H.money(paid_total),
        remaining=H.money(remaining_balance),
    )
    if _dec(remaining_balance) <= 0:
        text += "\n" + H.escape(t("rent.match_paid_off", locale))
    return text


def rent_overpayment_card(candidate: RentMatchCandidate, locale: str = "zh") -> str:
    """Overpayment guard (SLICE2-RENT-005): explain, never book, never show
    raw amounts as ids/enums."""
    return "\n".join(
        [
            f"<b>{H.escape(t('rent.overpayment_title', locale))}</b>",
            t(
                "rent.overpayment_text",
                locale,
                property=H.escape(candidate.property_name),
                unit=H.escape(candidate.unit_number),
                month=period_label(candidate.period, locale),
                due=H.money(candidate.due_amount),
                paid=H.money(candidate.paid_amount),
                remaining=H.money(candidate.remaining_balance),
                amount=H.money(candidate.amount),
            ),
        ]
    )


def rent_invalid_amount_card(locale: str = "zh") -> str:
    return H.escape(t("rent.invalid_amount", locale))


def rent_match_success_card(
    property_name: str,
    unit_number: str,
    period: str,
    amount,
    balance,
    locale: str = "zh",
) -> str:
    return t(
        "rent.match_success",
        locale,
        property=H.escape(property_name),
        unit=H.escape(unit_number),
        month=period_label(period, locale),
        amount=H.money(amount),
        balance=H.money(balance),
    )


def rent_already_booked_card(candidate: RentMatchCandidate, locale: str = "zh") -> str:
    return t(
        "rent.already_booked",
        locale,
        property=H.escape(candidate.property_name),
        unit=H.escape(candidate.unit_number),
        month=period_label(candidate.period, locale),
        amount=H.money(candidate.amount),
    )


def rent_match_pending_card(
    candidate: RentMatchCandidate,
    received_date: str,
    locale: str = "zh",
) -> str:
    return t(
        "rent.match_pending",
        locale,
        property=H.escape(candidate.property_name),
        unit=H.escape(candidate.unit_number),
        month=period_label(candidate.period, locale),
        amount=H.money(candidate.amount),
        date=H.format_date(received_date),
    )


def secretary_registered_card(
    candidate: RentMatchCandidate, locale: str = "zh",
) -> str:
    """Owner confirmation card after the Secretary registers a rent payment
    (V1.3 Slice 2). Human text only: no income_id / internal state."""
    partial = (
        _dec(candidate.paid_amount or 0) > 0
        or _dec(candidate.amount) < _dec(candidate.due_amount)
    )
    if partial:
        paid_total = _dec(candidate.paid_amount) + _dec(candidate.amount)
        lines = [
            f"📋 <b>{H.escape(t('rent.secretary_registered_title', locale))}</b>",
            H.escape(t("rent.match_partial_title", locale)),
            "",
            f"{H.escape(candidate.property_name)} {H.escape(candidate.unit_number)}"
            f" · {period_label(candidate.period, locale)}",
            f"{H.escape(t('rent.match_partial_due', locale))}：{H.money(candidate.due_amount)}",
            f"{H.escape(t('rent.match_partial_received', locale))}：{H.money(candidate.amount)}",
            f"{H.escape(t('rent.match_partial_paid_total', locale))}：{H.money(paid_total)}",
            f"{H.escape(t('rent.match_partial_remaining', locale))}：{H.money(candidate.remaining_balance)}",
            f"{H.escape(t('rent.registered_by', locale))}：Secretary",
            "",
            H.escape(t("rent.match_unique", locale)),
        ]
        return "\n".join(lines)
    lines = [
        f"📋 <b>{H.escape(t('rent.secretary_registered_title', locale))}</b>",
        "",
        f"{H.escape(candidate.property_name)} {H.escape(candidate.unit_number)}"
        f" · {period_label(candidate.period, locale)}",
        f"{H.escape(t('rent.match_expected', locale))}：{H.money(candidate.amount)}",
        f"{H.escape(t('rent.match_received', locale))}：{H.money(candidate.amount)}",
        f"{H.escape(t('rent.registered_by', locale))}：Secretary",
        "",
        H.escape(t("rent.match_amount_ok", locale)),
        H.escape(t("rent.match_unique", locale)),
    ]
    return "\n".join(lines)


def secretary_matched_reply(candidate: RentMatchCandidate, locale: str = "en") -> str:
    """English confirmation to the Secretary after the pending income lands
    (V1.3 Slice 2): one sentence, no re-entry, no internal identifiers."""
    return t(
        "rent.secretary_matched",
        locale,
        property=H.escape(candidate.property_name),
        unit=H.escape(candidate.unit_number),
        month=period_label(candidate.period, locale),
        amount=H.money(candidate.amount),
    )


def secretary_terminal_card(payload: dict, income: Income, locale: str = "zh") -> str:
    """Terminal state of the Owner's confirmation card after confirm: no
    confirm button remains; balance + registrar keep the entry human."""
    due = _dec(payload.get("due_amount") or 0)
    if due > 0 and _dec(income.amount) < due:
        text = rent_match_partial_success_card(
            payload.get("unit_number", ""),
            income.amount,
            due,
            payload.get("paid_amount") or 0,
            payload.get("remaining_balance") or 0,
            locale,
        )
        return text + "\n" + (
            H.escape(t("rent.registered_by", locale))
            + "：" + H.escape(payload.get("registrar", "Secretary"))
        )
    return t(
        "rent.secretary_terminal",
        locale,
        property=H.escape(payload.get("property_name", "")),
        unit=H.escape(payload.get("unit_number", "")),
        month=period_label(payload.get("period", ""), locale),
        amount=H.money(income.amount),
        balance=H.money(payload.get("remaining_balance") or 0),
        registrar=H.escape(payload.get("registrar", "Secretary")),
    )


def secretary_already_waiting_card(candidate: RentMatchCandidate, locale: str = "en") -> str:
    """Secretary duplicate report while a pending income awaits the Owner:
    friendly English, never a second pending row."""
    return t(
        "rent.already_waiting_owner",
        locale,
        property=H.escape(candidate.property_name),
        unit=H.escape(candidate.unit_number),
        month=period_label(candidate.period, locale),
        amount=H.money(candidate.amount),
    )


def secretary_already_confirmed_card(candidate: RentMatchCandidate, locale: str = "en") -> str:
    """Secretary duplicate report after the Owner confirmed: friendly English,
    never a new pending row."""
    return t(
        "rent.already_recorded_confirmed",
        locale,
        property=H.escape(candidate.property_name),
        unit=H.escape(candidate.unit_number),
        month=period_label(candidate.period, locale),
        amount=H.money(candidate.amount),
    )


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


# --- V1.1 UX cards (dashboard / collect / pending / payment detail) ---

def dashboard_card(
    date_label: str,
    *,
    expected,
    collected,
    outstanding,
    overdue_count: int = 0,
    expiring_count: int = 0,
    task_count: int = 0,
    vacant_count: int = 0,
    locale: str = "zh",
) -> str:
    """Today's management center (B1). Zero/empty values are hidden; a fully
    clear day shows the positive empty-state line."""
    blocks = [
        f"<b>{H.escape(t('dashboard.title', locale))}</b>",
        f"📅 {H.escape(date_label)}",
        f"{H.escape(t('dashboard.rent_title', locale))}",
        f"{H.escape(t('finance.expected', locale))}：{H.money(expected)}",
        f"{H.escape(t('finance.collected', locale))}：{H.money(collected)}",
        f"{H.escape(t('finance.outstanding', locale))}：<b>{H.money(outstanding)}</b>",
    ]
    attention: list[str] = []
    if overdue_count > 0:
        attention.append(H.escape(t("dashboard.overdue_count", locale, count=overdue_count)))
    if expiring_count > 0:
        attention.append(H.escape(t("dashboard.lease_expiring", locale, count=expiring_count)))
    if task_count > 0:
        attention.append(H.escape(t("dashboard.tasks_count", locale, count=task_count)))
    if attention:
        blocks.append(H.escape(t("dashboard.tasks_title", locale)))
        blocks.append(" · ".join(attention))
    if vacant_count > 0:
        blocks.append(H.escape(t("dashboard.vacant", locale, count=vacant_count)))
    if not attention:
        blocks.append(H.escape(t("dashboard.tasks_none", locale)))
    return "\n".join(blocks)


def rent_collect_list(rows: list[dict], locale: str = "zh") -> str:
    """All collectible units (B4 / P0-RENT-COLLECTION-UX-003). ``rows``:
    unit_number, property_name, tenant, receivable, received, outstanding,
    overdue_days. Every unit shows Unit / Tenant / 应收 / 已收 / 尚欠 /
    到期状态; units without outstanding are filtered out before this
    renderer is called."""
    if not rows:
        return (
            f"{H.escape(t('rent.collect_title', locale))}\n\n"
            f"{H.escape(t('rent.collect_all_paid', locale))}"
        )
    blocks = [f"<b>{H.escape(t('rent.collect_title', locale))}</b>"]
    for r in rows:
        where = " · ".join(
            x for x in (r.get("property_name"), r.get("unit_number")) if x
        )
        tenant = r.get("tenant") or ""
        head = H.escape(where) + (f" · {H.escape(tenant)}" if tenant else "")
        amounts = (
            f"{H.escape(t('rent.receivable', locale))} {H.money(r.get('receivable'))}"
            f" · {H.escape(t('rent.received', locale))} {H.money(r.get('received'))}"
            f" · {H.escape(t('rent.outstanding', locale))} {H.money(r.get('outstanding'))}"
        )
        block = f"{head}\n{amounts}"
        if int(r.get("overdue_days") or 0) > 0:
            block += "\n" + H.escape(
                t("rent.collect_overdue", locale, days=int(r["overdue_days"]))
            )
        blocks.append(block)
    return "\n\n".join(blocks)


def pending_overview_card(sections: dict, locale: str = "zh") -> str:
    """One aggregated to-do page (B2/B3): overdue / pending-confirm /
    expiring leases / open tasks. Empty sections are omitted."""
    blocks = [f"<b>{H.escape(t('pending.title', locale))}</b>"]
    if not any(sections.values()):
        return "\n".join(
            [
                f"<b>{H.escape(t('pending.title', locale))}</b>",
                H.escape(t("pending.empty", locale)),
            ]
        )
    overdue = sections.get("overdue") or []
    if overdue:
        items = [
            f"🔴 {H.escape(r.get('unit', ''))} · {H.money(r.get('total_outstanding'))}"
            f" · {H.escape(t('overdue.days', locale))} {r.get('overdue_days')}"
            f"{H.escape(t('overdue.days_unit', locale))}"
            for r in overdue
        ]
        blocks.append(H.escape(t("pending.section_overdue", locale, count=len(overdue))))
        blocks.append("\n".join(items))
    confirm = sections.get("confirm") or []
    if confirm:
        items = [
            f"#{r['id']} · {H.escape(r.get('where', ''))} · {H.money(r.get('amount'))}"
            for r in confirm
        ]
        blocks.append(H.escape(t("pending.section_confirm", locale, count=len(confirm))))
        blocks.append("\n".join(items))
        blocks.append(H.escape(t("pending.hint", locale)))
    expiring = sections.get("expiring") or []
    if expiring:
        items = [
            f"📋 {H.escape(r.get('unit', ''))} · {H.escape(r.get('tenant', ''))}"
            f" · {H.format_date(r.get('end_date'))}"
            for r in expiring
        ]
        blocks.append(H.escape(t("pending.section_expiring", locale, count=len(expiring))))
        blocks.append("\n".join(items))
    tasks = sections.get("tasks") or []
    if tasks:
        items = [
            f"🛠 {H.escape(r.get('title', ''))}"
            + (f" · {H.format_date(r.get('due_date'))}" if r.get("due_date") else "")
            for r in tasks
        ]
        blocks.append(H.escape(t("pending.section_tasks", locale, count=len(tasks))))
        blocks.append("\n".join(items))
    return "\n\n".join(blocks)


def payment_detail_card(income: Income, locale: str = "zh") -> str:
    """Paid unit's payment record (B5: [💰 查看付款])."""
    return t(
        "rent.payment_detail_title",
        locale,
    ) + f"\n#{income.id} · {H.money(income.amount)}\n{H.format_date(income.received_date)} · {H.escape(income.payment_method or '-')}"



# --- V1.2 operations center (待办中心) -------------------------------------

def _ops_property_name(task, properties) -> str:
    if task.property_id:
        prop = next((p for p in properties if p.id == task.property_id), None)
        if prop:
            return prop.name
    return "—"


def _ops_amount(task) -> Optional[str]:
    details = task.details or {}
    amount = details.get("amount") or details.get("total_outstanding")
    if amount is None:
        return None
    return str(amount)


def _ops_due(task) -> str:
    due = str(task.due_at or "")[:16].replace("T", " ")
    return due or "—"


def operational_task_line(task, properties, locale: str = "zh") -> str:
    """One task row: 房产 · 事项 · 金额(如有) · 到期 · 状态."""
    prop = H.escape(_ops_property_name(task, properties))
    title = H.escape(task.title or f"#{task.id}")
    parts = [f"🏢 {prop}", title]
    amount = _ops_amount(task)
    if amount is not None:
        parts.append(f"{H.escape(t('ops.amount', locale))}：{H.money(amount)}")
    parts.append(f"{H.escape(t('ops.due', locale))}：{H.escape(_ops_due(task))}")
    parts.append(f"{H.escape(t('ops.status', locale))}：{_ops_status_label(task, locale)}")
    return " · ".join(parts)


def _ops_status_label(task, locale: str) -> str:
    status = (task.status or "PENDING").upper()
    if status == "COMPLETED":
        return H.escape(t("ops.status_completed", locale))
    if status == "CANCELLED":
        return H.escape(t("ops.status_cancelled", locale))
    if getattr(task, "snoozed_until", None):
        return "⏰ " + H.escape(t("ops.snoozed_toast", locale))
    return H.escape(t("ops.status_pending", locale))


def operations_overview_card(summary: dict, locale: str = "zh") -> str:
    lines = [f"<b>{H.escape(t('ops.title', locale))}</b>"]
    lines.append(DIVIDER)
    lines.append(f"🔴 {H.escape(t('ops.section_overdue', locale))}：{int(summary.get('overdue', 0))}")
    lines.append(f"🟠 {H.escape(t('ops.section_today', locale))}：{int(summary.get('due_today', 0))}")
    lines.append(f"🟡 {H.escape(t('ops.section_next7', locale))}：{int(summary.get('due_7_days', 0))}")
    lines.append(f"📅 {H.escape(t('ops.section_all', locale))}：{int(summary.get('pending_total', 0))}")
    return "\n".join(lines)


def operations_section_card(
    title: str, tasks: list, properties, locale: str = "zh", empty_key: str = "ops.empty"
) -> str:
    lines = [f"<b>{H.escape(title)}</b>"]
    if not tasks:
        lines.append(H.escape(t(empty_key, locale)))
        return "\n".join(lines)
    for task in tasks[:15]:
        lines.append(operational_task_line(task, properties, locale))
    if len(tasks) > 15:
        lines.append(H.escape(f"… 共 {len(tasks)} 项"))
    return "\n\n".join(lines)


def operational_task_detail_card(task, properties, locale: str = "zh") -> str:
    details = task.details or {}
    lines = [f"<b>{H.escape(t('ops.task_detail_title', locale))}</b>"]
    lines.append(DIVIDER)
    lines.append(f"{H.escape(t('ops.task', locale))}：{H.escape(task.title or f'#{task.id}')}")
    lines.append(f"{H.escape(t('ops.property', locale))}：{H.escape(_ops_property_name(task, properties))}")
    tenant_name = details.get("tenant_name")
    if tenant_name:
        lines.append(f"{H.escape(t('ops.tenant', locale))}：{H.escape(tenant_name)}")
    amount = _ops_amount(task)
    if amount is not None:
        lines.append(f"{H.escape(t('ops.amount', locale))}：{H.money(amount)}")
    lines.append(f"{H.escape(t('ops.due', locale))}：{H.escape(_ops_due(task))}")
    lines.append(f"{H.escape(t('ops.status', locale))}：{_ops_status_label(task, locale)}")
    if task.description:
        lines.append(H.escape(task.description))
    return "\n".join(lines)


# --- V1.3 Slice 1: expense approval + unified to-do -------------------------

def _expense_status_label(status: str, locale: str = "zh") -> str:
    key = {
        "pending": "expense.status_pending",
        "approved": "expense.status_approved",
        "rejected": "expense.status_rejected",
        "paid": "expense.status_paid",
        "reversed": "expense.status_reversed",
        # 003B: a pending payment claim / partial payment are NOT paid.
        "payment_claimed": "expense.status_awaiting_verification",
        "partially_paid": "expense.status_partially_paid",
    }.get((status or "").lower())
    # Unknown statuses never reach the user as raw enum text; a neutral dash
    # keeps the card human (SLICE1-UX-003).
    return H.escape(t(key, locale)) if key else "—"


def _expense_location(expense: Expense, location: str = "") -> str:
    # No technical "Unit {id}" fallback on the degraded path (SLICE1-UX-003):
    # when the property/unit lookup fails the location line is simply hidden.
    return H.escape(location) if location else ""


def _expense_purpose_text(expense) -> str | None:
    """Existing-data purpose for one expense record: category -> description ->
    payee/vendor, with placeholder sentinels dropped (`??`, None, null, bare
    dash). An incomplete record (e.g. E7/E8 with a `??` category) still
    resolves to truthful existing facts (here: the 'Repair' payee) before the
    neutral unspecified-purpose label (P1-PASAY-NIGHTLY-...-008 A3)."""
    for field in (
        getattr(expense, "category", None),
        getattr(expense, "description", None),
        getattr(expense, "payee", None),
    ):
        text = _clean_free_text(field)
        if text:
            return text
    return None


# ZERO-LEARNING-004 §5: business-category words that are NOT vendor names.
# Legacy imports stored the purpose in the payee column (e.g. E7/E8:
# category='??', payee='Repair') — such a payee must never be presented as a
# real vendor. Only a distinct vendor name (e.g. 'Fix-It Co') renders.
_PAYEE_PURPOSE_SENTINELS = frozenset({
    "repair", "maintenance", "water", "electricity", "rent", "cleaning",
    "other", "维修", "水费", "电费", "物业费", "网费", "清洁", "其他",
})


def _expense_display_payee(expense) -> str | None:
    """Real payee for display; None means 'Not recorded / 未登记'.

    A payee that is missing, the '-' unknown-vendor sentinel, identical to
    the purpose, or a business-category word is NOT a real vendor and never
    renders as the payee (the purpose fallback must never be re-labelled as a
    vendor)."""
    raw = getattr(expense, "payee", None)
    text = _clean_free_text(raw)
    if not text:
        return None
    lowered = text.lower()
    if lowered in _PAYEE_PURPOSE_SENTINELS:
        return None
    purpose = _expense_purpose_text(expense)
    if purpose and lowered == " ".join(str(purpose).lower().split()):
        return None
    return text


def expense_approval_card(
    expense: Expense, locale: str = "zh", location: str = "",
) -> str:
    """Owner-facing approval card (zh default): location/unit, amount, payee,
    purpose and date. Raw statuses and expense ids never appear."""
    lines = [f"<b>{H.escape(t('expense.title', locale))}</b>"]
    loc = _expense_location(expense, location)
    if loc:
        lines.append(loc)
    lines.append(f"<b>{H.money(expense.amount)}</b>")
    lines.append(
        f"{H.escape(t('expense.payee', locale))}：{H.escape(expense.payee or '-')}"
    )
    purpose = _expense_purpose_text(expense)
    if purpose:
        lines.append(f"{H.escape(t('expense.purpose', locale))}：{H.escape(purpose)}")
    else:
        # Explicit unspecified-purpose state instead of a silently blank line
        # (A3): no placeholder text is ever rendered as a purpose.
        lines.append(
            f"{H.escape(t('expense.purpose', locale))}："
            f"{H.escape(t('expense.purpose_unspecified', locale))}"
        )
    lines.append(
        f"{H.escape(t('expense.date', locale))}：{H.format_date(expense.expense_date)}"
        + (
            f" · {H.escape(t('expense.due_date', locale))}：{H.format_date(expense.due_date)}"
            if expense.due_date else ""
        )
    )
    lines.append(
        H.escape(t("expense.receipt_present", locale))
        if expense.receipt_attachment_id
        else H.escape(t("expense.receipt_missing", locale))
    )
    return "\n".join(lines)


def expense_result_card(expense: Expense, locale: str = "zh", location: str = "") -> str:
    """Message-mutation result card: the tapped decision + the human next step.
    ``location`` is an optional Property · Unit label (V2 group feedback).
    No raw status enum, no expense_id."""
    status = (expense.status or "").lower()
    if status == "approved":
        title = t("expense.approved_card", locale)
        next_step = H.escape(t("expense.approved_next", locale))
    elif status == "rejected":
        title = t("expense.rejected_card", locale)
        next_step = H.escape(t("expense.rejected_next", locale))
    elif status == "paid":
        title = t("expense.paid_card", locale)
        next_step = H.escape(t("expense.rejected_next", locale))
    elif status == "reversed":
        title = t("expense.reversed_card", locale)
        next_step = H.escape(t("expense.rejected_next", locale))
    elif status == "payment_claimed":
        # 003B / E16: reported-but-unverified must read "verification pending".
        title = t("expense.payment_reported", locale)
        next_step = H.escape(t("expense.awaiting_verification_next", locale))
    elif status == "partially_paid":
        remaining = getattr(expense, "remaining", None) or 0
        title = t("expense.partial_paid_card", locale, remaining=H.money(remaining))
        next_step = H.escape(t("expense.partial_paid_next", locale))
    else:
        return expense_approval_card(expense, locale)
    purpose = _expense_purpose_text(expense) or t("expense.purpose_unspecified", locale)
    where = " · ".join(x for x in (location, purpose) if x)
    lines = [title, f"{H.escape(where)}  ·  <b>{H.money(expense.amount)}</b>", next_step]
    return "\n".join(x for x in lines if x)


def expense_detail_card(
    expense: Expense, locale: str = "zh", location: str = "",
    waiting_days: int = 0,
) -> str:
    """Human-readable expense detail (ZERO-LEARNING-004 §5): what this money
    is, how much, how long it has waited, and the state — NOT an ERP field
    form. One compact block:

        💸 E7 · DEV-BAY-1680

        Repair · ₱7,000
        Approved Aug 15 · Waiting 2d
        Payee: Not recorded
        Receipt: None

    The payee line only renders when a REAL vendor exists (``Fix-It Co``);
    otherwise ``Not recorded / 未登记`` — the purpose is never re-labelled as
    the payee. Raw attachment ids stay internal."""
    expense_id = getattr(expense, "id", None)
    id_part = f"E{int(expense_id)}" if expense_id is not None else ""
    title_parts = [x for x in (id_part, location) if x]
    lines = [f"💸 <b>{H.escape(' · '.join(title_parts))}</b>", ""]
    purpose = _expense_purpose_text(expense) or t("expense.purpose_unspecified", locale)
    lines.append(
        f"{H.escape(purpose)} · <b>{H.money(expense.amount)}</b>"
    )
    if getattr(expense, "approved_at", None):
        approved = str(expense.approved_at)[:10]
        waiting = (
            f" · {H.escape(_bi_value(locale, f'Waiting {waiting_days}d', f'等待 {waiting_days} 天'))}"
            if waiting_days
            else ""
        )
        lines.append(
            H.escape(_bi_value(locale, f"Approved {approved}", f"批准于 {approved}")) + waiting
        )
    payee = _expense_display_payee(expense)
    if payee:
        lines.append(
            H.escape(_bi_value(locale, f"Payee: {payee}", f"收款方：{payee}"))
        )
    else:
        lines.append(
            H.escape(_bi_value(locale, "Payee: Not recorded", "收款方：未登记"))
        )
    lines.append(
        H.escape(_bi_value(
            locale,
            "Receipt: Yes" if expense.receipt_attachment_id else "Receipt: None",
            "凭证：有" if expense.receipt_attachment_id else "凭证：无",
        ))
    )
    status = (expense.status or "").lower()
    if status == "approved":
        lines.append(H.escape(_bi_value(locale, "Waiting for payment", "等待付款")))
    elif status == "paid":
        lines.append(H.escape(_bi_value(locale, "Paid", "已付款")))
    elif status == "payment_claimed":
        # 003B / E16: a reported-but-unverified payment is NEVER shown as paid.
        lines.append(H.escape(_bi_value(
            locale, "Payment reported · verification pending", "已上报付款 · 待核验")))
    elif status == "partially_paid":
        verified = getattr(expense, "verified_paid", None) or 0
        remaining = getattr(expense, "remaining", None) or 0
        lines.append(H.escape(_bi_value(
            locale,
            f"Partially paid · {H.money(verified)} verified · {H.money(remaining)} remaining",
            f"部分付款 · 已核验 {H.money(verified)} · 剩余 {H.money(remaining)}",
        )))
    elif status == "pending":
        lines.append(H.escape(_bi_value(locale, "Pending approval", "待批准")))
    elif status in ("rejected", "reversed"):
        lines.append(H.escape(_bi_value(locale, "Closed", "已关闭")))
    return "\n".join(lines)


# --- BOT-V1-USABLE-001 P0-2: expense create flow ---------------------------

def expense_pay_confirm_card(
    expense: Expense,
    locale: str = "zh",
    location: str = "",
    *,
    similar: list | None = None,
) -> str:
    """Owner payment-confirmation card for an APPROVED (unpaid) expense
    (PASAY-V2-EXPENSE-PAYABLE-TASK-006 §4/§5/§7).

    Carries the stable plain-text identity ``E{id}`` (never the ``#E{id}``
    hashtag) plus unit/purpose/amount/date. A
    receipt is OPTIONAL (shown as a hint, never a blocker). When ``similar``
    holds possible-duplicate PAID rows, an advisory bilingual warning lists the
    existing IDs — it never deletes or rejects the current expense."""
    id_part = f"E{expense.id}"
    blocks = [f"💸 <b>{H.escape(t('expense.pay_confirm_title', locale))}</b>"]
    loc = _expense_location(expense, location)
    purpose = _expense_purpose_text(expense) or t("expense.purpose_unspecified", locale)
    header_bits = [id_part] + ([loc] if loc else []) + [H.escape(purpose)]
    blocks.append(" · ".join(b for b in header_bits if b))
    blocks.append(f"<b>{H.money(expense.amount)}</b>")
    if expense.description:
        blocks.append(H.escape(expense.description))
    blocks.append(
        f"{H.escape(t('expense.date', locale))}：{H.format_date(expense.expense_date)}"
    )
    if similar:
        dup = ["⚠️ " + H.escape(t("expense.pay_duplicate_title", locale))]
        dup.append(
            H.escape(
                t(
                    "expense.pay_duplicate_body",
                    locale,
                    unit=H.escape(str(similar[0].get("unit") or "")),
                )
            )
        )
        existing = " · ".join(
            f"E{int(r['expense_id'])}" for r in similar if r.get("expense_id")
        )
        if existing:
            dup.append(H.escape(t("expense.pay_view_existing", locale) + ": " + existing))
        blocks.append("\n".join(dup))
    blocks.append(H.escape(t("expense.pay_confirm_hint", locale)))
    return "\n\n".join(blocks)


def expense_pay_result_card(expense: Expense, locale: str = "zh", *, already: bool = False) -> str:
    """Payment result card: ``PAID`` the first time, an idempotent ``Already
    paid`` on a repeated confirmation (PASAY-V2-EXPENSE-PAYABLE-TASK-006 §6)."""
    if already:
        title = t("expense.pay_result_already", locale)
        note = H.escape(t("expense.pay_result_already_note", locale))
    else:
        title = t("expense.pay_result_paid", locale)
        note = H.escape(t("expense.pay_result_done", locale))
    purpose = _expense_purpose_text(expense) or t("expense.purpose_unspecified", locale)
    where = " · ".join(x for x in (f"E{expense.id}", purpose) if x)
    lines = [H.escape(title), f"{H.escape(where)} · <b>{H.money(expense.amount)}</b>", note]
    return "\n".join(x for x in lines if x)


# --- BOT-V1-USABLE-001 P0-2: expense create flow ---------------------------

def expense_confirm_card(
    *,
    unit_number: str,
    property_name: str,
    category: str,
    amount,
    expense_date: str,
    locale: str = "zh",
) -> str:
    """Secretary/Owner expense confirmation card (P0-2 spec)."""
    lines = [f"💸 <b>{H.escape(t('expense.confirm_title', locale))}</b>"]
    where = " · ".join(x for x in (property_name, unit_number) if x)
    if where:
        lines.append(f"{H.escape(t('rent.unit', locale))}：{H.escape(where)}")
    lines.append(f"{H.escape(t('expense.purpose', locale))}：{H.escape(category)}")
    lines.append(f"{H.escape(t('rent.amount', locale))}：<b>{H.money(amount)}</b>")
    lines.append(f"{H.escape(t('expense.date', locale))}：{H.escape(expense_date)}")
    return "\n".join(lines)


def expense_submitted_card(
    *,
    unit_number: str,
    property_name: str,
    category: str,
    amount,
    locale: str = "zh",
) -> str:
    where = " · ".join(x for x in (property_name, unit_number) if x)
    lines = [
        f"<b>{H.escape(t('expense.submitted_title', locale))}</b>",
        f"{H.escape(where)}" if where else "",
        f"{H.escape(category)} · <b>{H.money(amount)}</b>",
        H.escape(t("expense.submitted_next", locale)),
    ]
    return "\n".join(x for x in lines if x)


def expense_paid_card(expense: Expense, locale: str = "zh", location: str = "") -> str:
    """PASAY-V2-FOUNDATION-001: Owner confirmed payment of an approved
    expense -> PAID/COMPLETED. Bilingual in groups; receipt optional and
    never blocks completion. No raw enums, no ids."""
    title = t("expense.payment_confirmed_title", locale)
    lines = [title]
    purpose = _expense_purpose_text(expense) or t("expense.purpose_unspecified", locale)
    where = " · ".join(x for x in (location, purpose) if x)
    lines.append(f"{H.escape(where)}  ·  <b>{H.money(expense.amount)}</b>")
    lines.append(H.escape(t("expense.payment_completed", locale)))
    lines.append(H.escape(t("expense.receipt_optional", locale)))
    return "\n".join(x for x in lines if x)


# --- BOT-V1-USABLE-001 P0-3: deterministic NL query answers ----------------

def income_summary_card(
    month: str, *, collected, expected, outstanding, locale: str = "zh",
) -> str:
    """'这个月收了多少钱' -> real income data (direct answer, no menu)."""
    year, _, mm = str(month).partition("-")
    if locale == "en":
        label = t("query.income_month", locale, month=f"{year}-{mm}")
    else:
        label = t("query.income_month", locale, month=f"{int(mm)}月")
    return "\n".join(
        [
            f"💰 <b>{H.escape(label)}</b>",
            f"{H.escape(t('query.income_collected', locale))}：<b>{H.money(collected)}</b>",
            f"{H.escape(t('query.income_expected', locale))}：{H.money(expected)}",
            f"{H.escape(t('query.income_outstanding', locale))}：{H.money(outstanding)}",
        ]
    )


def expense_summary_card(
    month: str, *, total_expense, net_income, locale: str = "zh",
) -> str:
    year, _, mm = str(month).partition("-")
    if locale == "en":
        label = t("query.expense_month", locale, month=f"{year}-{mm}")
    else:
        label = t("query.expense_month", locale, month=f"{int(mm)}月")
    return "\n".join(
        [
            f"💸 <b>{H.escape(label)}</b>",
            f"{H.escape(t('query.expense_total', locale))}：<b>{H.money(total_expense)}</b>",
            f"{H.escape(t('query.net_income', locale))}：{H.money(net_income)}",
        ]
    )


def unit_expense_history_card(
    unit_number: str, rows: list[dict], locale: str = "zh",
) -> str:
    """'1680最近有什么支出' -> recent expenses of one unit (read-only). A
    placeholder category (`??`) never renders; rows without a truthful purpose
    show only amount/status/date (A3)."""
    if not rows:
        return H.escape(t("query.expense_none", locale))
    blocks = [f"💸 <b>{H.escape(t('query.expense_recent', locale))}</b> · {H.escape(unit_number)}"]
    for row in rows[:5]:
        purpose = _clean_free_text(row.get("category")) or _clean_free_text(
            row.get("description")
        )
        bits = []
        if purpose:
            bits.append(H.escape(purpose))
        bits.append(f"<b>{H.money(row.get('amount'))}</b>")
        status = H.escape(str(row.get("status_label") or ""))
        if status:
            bits.append(status)
        date_part = H.format_date(row.get("expense_date"))
        if date_part:
            bits.append(H.escape(date_part))
        blocks.append(" · ".join(bits))
    return "\n\n".join(blocks)


def unit_info_card(
    *,
    unit_number: str,
    property_name: str,
    tenant_name: str = "",
    monthly_rent=None,
    end_date=None,
    locale: str = "zh",
) -> str:
    lines = [f"🏢 <b>{H.escape(t('query.unit_info_title', locale))}</b>"]
    where = " · ".join(x for x in (property_name, unit_number) if x)
    if where:
        lines.append(H.escape(where))
    if tenant_name:
        lines.append(f"{H.escape(t('query.unit_tenant', locale))}：{H.escape(tenant_name)}")
    if monthly_rent is not None:
        lines.append(f"{H.escape(t('query.unit_rent', locale))}：{H.money(monthly_rent)}")
    if end_date:
        lines.append(
            f"{H.escape(t('query.unit_lease_end', locale))}：{H.format_date(end_date)}"
        )
    return "\n".join(lines)


def contracts_card(rows: list[dict], days: int, locale: str = "zh") -> str:
    """'有哪些合同快到期' -> real lease end dates within N days."""
    if not rows:
        return H.escape(t("query.contracts_none", locale))
    blocks = [
        f"📋 <b>{H.escape(t('query.contracts_title', locale))}</b>",
        H.escape(t("query.contracts_window", locale, days=days)),
    ]
    for row in rows[:10]:
        blocks.append(
            f"• {H.escape(row.get('unit') or '')} · {H.escape(row.get('tenant') or '')}"
            f" · {H.format_date(row.get('end_date'))}"
        )
    return "\n".join(blocks)


def unit_timeline_card(timeline: dict, unit_number: str, locale: str = "zh") -> str:
    """AI-OPS-FOUNDATION-001 §15: the unit's digital file (time-ordered
    timeline of rent/payments, expenses, repairs, evidence, lease events)."""
    unit = timeline.get("unit") or {}
    events = timeline.get("events") or []
    lines = [
        f"<b>{H.escape(t('query.timeline_title', locale, unit=unit_number))}</b>",
        (
            f"{H.escape(t('query.rent', locale))}: <b>{H.money(unit.get('monthly_rent'))}</b>"
            f" · {H.escape(unit.get('status') or '')}"
        ),
    ]
    if not events:
        lines.append(H.escape(t("query.timeline_empty", locale)))
        return "\n".join(lines)
    _KIND_EMOJI = {
        "lease": "📋", "rent": "💰", "expense": "💸",
        "task": "🛠", "evidence": "📎",
    }
    for ev in events:
        at = str(ev.get("at") or "")[:10]
        lines.append(
            f"{_KIND_EMOJI.get(ev.get('kind'), '·')} {at} · {H.escape(ev.get('label') or '')}"
            + (f"\n   {H.escape(ev.get('detail') or '')}" if ev.get("detail") else "")
        )
    return "\n".join(lines)


def home_summary_card(
    *,
    expected=None,
    collected,
    outstanding=None,
    total_arrears=None,
    overdue_count: int = 0,
    expiring_count: int = 0,
    vacant_count: int = 0,
    payable_count: int = 0,
    maintenance_open: int = 0,
    today_count: int = 0,
    locale: str = "zh",
) -> str:
    """CONVERGENCE-003 §2.2 + PASAY-AI-EMPLOYEE-FOUNDATION-007A §C: Home =
    the ONE global Operations Overview (God View), titled ``运营总览`` for the
    Owner (zh) / ``Pasay Operations`` (en) — never "Pasay Property".

    Ten deterministic numbers: this month expected / collected / outstanding,
    historical total arrears, overdue units, expiring contracts, vacant units,
    expenses awaiting payment, open maintenance, and items needing action
    today. No first-level menu buttons live on the card (the fixed Reply
    Keyboard carries navigation); only the two situational actions (⚠️ Today /
    🔄 Refresh) ride on the keyboard.
    """
    title = _home_title(locale)
    lines = [f"<b>{H.escape(title)}</b>"]
    if expected is not None:
        lines.append(H.escape(_bi_value(locale, f"Expected {H.money(expected)}", f"本月应收 {H.money(expected)}")))
    lines.append(H.escape(_bi_value(locale, f"Collected {H.money(collected)}", f"本月已收 {H.money(collected)}")))
    if outstanding is not None:
        lines.append(H.escape(_bi_value(locale, f"This month outstanding {H.money(outstanding)}", f"本月未收 {H.money(outstanding)}")))
    if total_arrears is not None:
        lines.append(H.escape(_bi_value(locale, f"Total arrears {H.money(total_arrears)}", f"历史累计欠租 {H.money(total_arrears)}")))
    lines.append(H.escape(_bi_value(locale, f"Overdue rents {overdue_count}", f"逾期租金 {overdue_count}")))
    if expiring_count:
        lines.append(H.escape(_bi_value(locale, f"Leases expiring {expiring_count}", f"合同到期 {expiring_count}")))
    if vacant_count:
        lines.append(H.escape(_bi_value(locale, f"Vacant {vacant_count}", f"空置 {vacant_count}")))
    if payable_count:
        lines.append(H.escape(_bi_value(locale, f"Expenses to pay {payable_count}", f"待付款 {payable_count}")))
    if maintenance_open:
        lines.append(H.escape(_bi_value(locale, f"Maintenance {maintenance_open}", f"未完成维修 {maintenance_open}")))
    if today_count:
        lines.append(H.escape(_bi_value(locale, f"Today's actions {today_count}", f"今日待办 {today_count}")))
    return "\n".join(lines)


def _todo_expense_purpose(row: dict, locale: str) -> str:
    """Purpose for one to-do expense row: category -> payee, with placeholder
    sentinels dropped (`??`, None, null, ...); explicit unspecified label as
    the final fallback (never a raw placeholder, EXPENSE-UX-FIX-001 Bug 2)."""
    for field in (row.get("category"), row.get("payee")):
        text = _clean_free_text(field)
        if text:
            return text
    return t("expense.purpose_unspecified", locale)


def todo_overview_card(sections: dict, locale: str = "zh", title_key: str = "todo.title") -> str:
    """Unified to-do page (V1.3): only what the current user must act on.
    Rows are human-readable; action buttons ride below each row.

    ``title_key`` lets the Owner queue render as "需要您处理 / Needs You"
    (AI-OPS-FOUNDATION-001 §5) while the Secretary keeps the plain to-do
    title."""
    blocks = [f"<b>{H.escape(t(title_key, locale))}</b>"]
    if not any(sections.values()):
        return "\n".join(
            [
                f"<b>{H.escape(t(title_key, locale))}</b>",
                H.escape(t("todo.empty", locale)),
            ]
        )
    expenses = sections.get("expenses") or []
    if expenses:
        # EXPENSE-UX-FIX-001: expense IDs render as plain `E{id}` (never the
        # `#E{id}` Telegram hashtag) and the purpose uses the real-field
        # fallback (category -> payee, sentinels dropped), so a legacy `??`
        # category can never reach the to-do page.
        items = [
            f"{'💳' if (r.get('status') or '').lower() != 'approved' else '💸'} "
            f"{'E' + str(r['id']) if r.get('id') is not None else ''} · "
            f"{H.escape(_todo_expense_purpose(r, locale))} · <b>{H.money(r.get('amount'))}</b>"
            + (f"\n{H.escape(r.get('location', ''))}" if r.get("location") else "")
            + (
                f"\n📋 {H.escape(t('expense.status_approved', locale))}"
                if (r.get("status") or "").lower() == "approved"
                else ""
            )
            for r in expenses
        ]
        blocks.append(H.escape(t("todo.section_expenses", locale, count=len(expenses))))
        blocks.append("\n".join(items))
    confirm = sections.get("confirm") or []
    if confirm:
        items = [
            f"⏳ {H.escape(r.get('where', ''))} · {H.money(r.get('amount'))}"
            for r in confirm
        ]
        blocks.append(H.escape(t("todo.section_confirm", locale, count=len(confirm))))
        blocks.append("\n".join(items))
    overdue = sections.get("overdue") or []
    if overdue:
        items = [
            f"🔴 {H.escape(r.get('unit', ''))} · {H.money(r.get('total_outstanding'))}"
            f" · {H.escape(t('overdue.days', locale))} {r.get('overdue_days')}"
            f"{H.escape(t('overdue.days_unit', locale))}"
            for r in overdue
        ]
        blocks.append(H.escape(t("todo.section_overdue", locale, count=len(overdue))))
        blocks.append("\n".join(items))
    contracts = sections.get("contracts") or []
    if contracts:
        items = [
            f"📋 {H.escape(r.get('unit', ''))} · {H.escape(r.get('tenant', ''))}"
            f" · {H.format_date(r.get('end_date'))}"
            for r in contracts
        ]
        blocks.append(H.escape(t("todo.section_contracts", locale, count=len(contracts))))
        blocks.append("\n".join(items))
    maintenance = sections.get("maintenance") or []
    if maintenance:
        items = [
            f"🔧 {H.escape(tk.title or t('ops.task', locale))}"
            + (f" · {H.escape(_ops_due(tk))}" if getattr(tk, "due_at", None) else "")
            for tk in maintenance
        ]
        blocks.append(H.escape(t("todo.section_maintenance", locale, count=len(maintenance))))
        blocks.append("\n".join(items))
    tasks = sections.get("tasks") or []
    if tasks:
        items = [
            f"🛠 {H.escape(tk.title or t('ops.task', locale))}"
            + (f" · {H.escape(_ops_due(tk))}" if getattr(tk, "due_at", None) else "")
            for tk in tasks
        ]
        blocks.append(H.escape(t("todo.section_tasks", locale, count=len(tasks))))
        blocks.append("\n".join(items))
    return "\n\n".join(blocks)


# --- V1.2.2 C1 read-only copilot (🤖 运营助手) ---------------------------------

# item_ref prefix -> emoji + zh/en cue. Matches the backend grounding refs
# (property:{id} / lease:{id} / task:{id} / expense:{id} / settlement:{id}).
_COPILOT_REF_EMOJI = {
    "lease": "🔴",      # rent / overdue
    "task": "🛠",       # maintenance / operational todo
    "expense": "💸",    # expense approval
    "settlement": "🤝", # commission settlement
    "property": "🏢",   # property-level
}


def _copilot_emoji(item: CopilotTodayItem) -> str:
    kind = (item.item_ref or "").split(":", 1)[0].lower()
    return _COPILOT_REF_EMOJI.get(kind, "📋")


def _copilot_one_line(text: str, max_chars: int = 160) -> str:
    """Single-line, HTML-escaped, length-capped human text (no refs/JSON)."""
    cleaned = " ".join((text or "").split())
    if len(cleaned) > max_chars:
        cleaned = cleaned[: max_chars - 1].rstrip() + "…"
    return H.escape(cleaned)


def copilot_today_card(today: CopilotToday, locale: str = "zh") -> str:
    """Read-only TODAY brief (C1.1 fast-first). ≤3 top items, each readable in
    seconds as a compact what/why line: ``<emoji> why``. No backend IDs, refs,
    JSON, or model terms in the user-visible text. The deterministic short
    summary appears once at the top; each item's button opens WHY."""
    blocks = [f"🤖 <b>{H.escape(t('copilot.today_title', locale))}</b>"]
    if today.summary:
        blocks.append(_copilot_one_line(today.summary))
    for item in today.top_items[:3]:
        emoji = _copilot_emoji(item)
        why = _copilot_one_line(item.reason_why_important)
        blocks.append(f"{emoji} {why}")
    if not today.top_items:
        blocks.append(H.escape(t("copilot.empty", locale)))
    return "\n\n".join(blocks)


def copilot_why_card(
    item_ref: str,
    explanation: str,
    recommendation: str,
    *,
    fallback: bool = False,
    suggested_action: str = "",
    locale: str = "zh",
) -> str:
    """WHY detail card (C1.1): grounded explanation + recommendation for one
    item. No refs / model / provider leak. ``fallback`` shows a subtle note that
    the answer is the deterministic reason (provider unavailable).
    ``suggested_action`` (C2) appends the owner-facing suggestion block that
    the per-item action buttons sit under."""
    blocks = [f"🤖 <b>{H.escape(t('copilot.why_title', locale))}</b>"]
    blocks.append(_copilot_one_line(explanation, max_chars=400))
    if recommendation:
        blocks.append(
            f"<b>{H.escape(t('copilot.action', locale))}</b>"
            f"{_copilot_one_line(recommendation)}"
        )
    if suggested_action:
        blocks.append(
            f"<b>{H.escape(t('copilot.suggest_title', locale))}</b>\n"
            f"{_copilot_one_line(suggested_action, max_chars=200)}"
        )
    if fallback:
        blocks.append(H.escape(t("copilot.fallback_note", locale)))
    return "\n\n".join(blocks)


def copilot_ask_card(answer: str, *, fallback: bool = False, locale: str = "zh") -> str:
    """PASAY-V2-FOUNDATION-001: direct answer, no 'Assistant answer' wrapper.
    The bot IS the work entry; product-layer packaging is removed."""
    blocks = [_copilot_one_line(answer, max_chars=900)]
    if fallback:
        blocks.append(H.escape(t("copilot.fallback_note", locale)))
    return "\n\n".join(blocks)


# --- V1.2.2 C2 confirmed-action copilot (render-safe, owner zh) --------------


def _format_due(value, locale: str = "zh") -> str:
    """Manila-local human due time: 今天 17:00 / 明天 09:00 / 8月13日 09:00."""
    if not value:
        return "-"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    local = dt.astimezone(MANILA_TZ)
    now = datetime.now(MANILA_TZ)
    if locale == "en":
        hm = local.strftime("%I:%M %p").lstrip("0")
        if local.date() == now.date():
            return f"Today {hm}"
        if local.date() == now.date() + timedelta(days=1):
            return f"Tomorrow {hm}"
        return f"{local.strftime('%b %d')} {hm}"
    hm = local.strftime("%H:%M")
    if local.date() == now.date():
        return f"今天 {hm}"
    if local.date() == now.date() + timedelta(days=1):
        return f"明天 {hm}"
    return f"{local.strftime('%m月%d日')} {hm}"


def _copilot_topic(card) -> str:
    """Human topic line for the confirmation card: the follow-up note, else the
    task title / tenant, else the render-safe target label (last resort)."""
    dc = card.display_context or {}
    raw = (
        card.note
        or dc.get("title")
        or dc.get("tenant")
        or card.target_label
        or ""
    )
    return _copilot_one_line(raw, max_chars=60)


def _copilot_property_line(card) -> str:
    """房产 line: property name + unit when available, else target label."""
    dc = card.display_context or {}
    parts = []
    if dc.get("property"):
        parts.append(str(dc["property"]))
    if dc.get("unit"):
        parts.append(f"Unit {dc['unit']}")
    if not parts:
        parts.append(card.target_label or "")
    return " · ".join(parts)


def copilot_suggest_card(rec: CopilotRecommend, locale: str = "zh") -> str:
    """C2 confirmation card (owner, zh): 房产 / 事项 / 负责人 / 截止 + secretary
    note. Built ONLY from ``rec.card`` (display_context + assignee_name +
    due_at + note) — never the proposal id, internal enums, JSON or raw
    status."""
    card = rec.card
    if card is None:
        return H.escape(t("copilot.confirm_title", locale))
    action = (card.action_type or "").lower()
    lines = [f"🤖 <b>{H.escape(t('copilot.confirm_title', locale))}</b>"]
    if action == "snooze_task":
        lines.append(f"{H.escape(t('copilot.role_topic', locale))}：{_copilot_topic(card)}")
        lines.append(f"{H.escape(t('copilot.role_due', locale))}：{H.escape(_format_due(card.due_at, locale))}")
    elif action == "assign_task":
        lines.append(f"{H.escape(t('copilot.role_topic', locale))}：{_copilot_topic(card)}")
        lines.append(f"{H.escape(t('copilot.role_owner', locale))}：{H.escape(card.assignee_name or '-')}")
    else:  # create_followup_task
        lines.append(f"{H.escape(t('copilot.role_property', locale))}：{H.escape(_copilot_property_line(card))}")
        lines.append(f"{H.escape(t('copilot.role_topic', locale))}：{_copilot_topic(card)}")
        lines.append(f"{H.escape(t('copilot.role_owner', locale))}：{H.escape(card.assignee_name or '-')}")
        lines.append(f"{H.escape(t('copilot.role_due', locale))}：{H.escape(_format_due(card.due_at, locale))}")
        lines.append(H.escape(t("copilot.hint_secretary_note", locale)))
    return "\n".join(lines)


def copilot_success_card(
    result: CopilotExecute, assignee_name: str = "", locale: str = "zh",
) -> str:
    """C2 success card: role-aware human text. Never shows proposal_id / raw
    status; ``assignee_name`` is the render-safe name from the recommend card
    (the execute result only carries the backend user id)."""
    action = (result.action_type or "").lower()
    due = _format_due(result.due_at, locale)
    assignee = H.escape(assignee_name or "-")
    if action == "snooze_task":
        return t("copilot.success_snooze", locale, due=H.escape(due))
    if action == "assign_task":
        return t("copilot.success_assign", locale, assignee=assignee)
    return t("copilot.success_follow", locale, assignee=assignee, due=H.escape(due))


def copilot_stale_card(locale: str = "zh") -> str:
    """Target changed / stale proposal: human warning, refresh button beside."""
    return H.escape(t("copilot.stale", locale))


def copilot_replayed_card(locale: str = "zh") -> str:
    """Duplicate callback / already executed: no second mutation."""
    return H.escape(t("copilot.executed_already", locale))


def copilot_notify_retry_card(locale: str = "zh") -> str:
    """Task created but the secretary notification is retrying."""
    return H.escape(t("copilot.notify_retry", locale))


# --- PASAY-V2-FOUNDATION-001: Quick Views / bilingual cards -----------------
# All V2 cards are deterministic (never call an LLM). Group mode renders
# every visible line English + 中文; private chats use the role language.
#
# TELEGRAM-OPS-REAL-WORLD-CLOSURE-005 §1: the Properties index carries ONE
# traffic-light per unit, and ONLY the three allowed colours. 🟢 normal,
# 🟡 needs attention (vacant / lease expiring), 🔴 needs action (rent overdue
# / open maintenance). Vacant is a non-urgent warning (🟡), never a green
# occupancy state — an operator should still notice it. When several states
# coexist the RENDERER must pick the highest severity once (see
# ``_property_traffic_light``); this map only scores a single status.
_V2_STATUS_EMOJI = {
    "overdue_rent": "🔴",
    "lease_expiring": "🟡",
    "paid": "🟢",
    "rent_paid": "🟢",
    "vacant": "🟡",
    "normal": "🟢",
    "overdue": "🔴",
}

# Severity order (large = more severe). Used to collapse a unit with several
# exception states down to its single worst traffic light (§1.2).
_PROPERTY_TRAFFIC_ORDER = {
    "overdue_rent": 3,
    "overdue": 3,
    "lease_expiring": 2,
    "vacant": 1,
    "paid": 0,
    "rent_paid": 0,
    "normal": 0,
}


def _property_traffic_light(status: str) -> str:
    """The ONE visible traffic light for a Property row (§1.1/§1.2).

    Only ``🟢`` / ``🟡`` / ``🔴`` are ever produced. The incoming ``status``
    field is a single label; if it does not map to a colour it defaults to
    🟢 (a healthy/unknown unit is not falsely alarmed)."""
    light = _V2_STATUS_EMOJI.get(str(status or "").lower())
    if light in ("🟢", "🟡", "🔴"):
        return light
    return "🟢"


def _short_unit_label(value: str, locale: str) -> str:
    """§1.4: strip any property prefix so the overview always shows the short
    room number (``DEV-BAY-1680`` -> ``1680``), matching the button labels."""
    s = str(value or "").strip()
    if "-" in s:
        s = s.split("-")[-1]
    return s or str(value or "")


def _bi_line(locale: str, en: str, zh: str) -> str:
    """One visible business line: English + 中文 in group mode; single
    language otherwise. Callers escape HTML before passing fragments in.

    CONVERGENCE-003 §6: when the zh fragment is identical to the en fragment
    it is rendered ONCE — a missing/English-identical translation never
    duplicates a line (e.g. a unit-only task row must not print twice)."""
    if locale == "bi":
        if zh == en or not zh:
            return en
        return f"{en}\n{zh}"
    return en if locale == "en" else zh


def _bi_header(locale: str, en: str, zh: str) -> str:
    """Compact one-line bilingual header, e.g. 'Pending / 未完成'."""
    if locale == "bi":
        if zh == en or not zh:
            return en
        return f"{en} / {zh}"
    return en if locale == "en" else zh


def _home_title(locale: str) -> str:
    """PASAY-AI-EMPLOYEE-FOUNDATION-007A §C: the Global Home title. Owner sees
    the Chinese single-language ``运营总览``; Secretary sees English
    ``Pasay Operations``. Never "Pasay Property" (avoid confusion with
    Properties)."""
    if locale == "en":
        return "Pasay Operations"
    return "运营总览"


def _bi_value(locale: str, en: str, zh: str) -> str:
    """Compact one-line bilingual VALUE, e.g. 'Outstanding ₱75,000 · 未付
    ₱75,000' (CONVERGENCE-003 §6.1) — one line, never two."""
    if locale == "bi":
        if zh == en or not zh:
            return en
        return f"{en} · {zh}"
    return en if locale == "en" else zh


def _v2_section(status_key: str, locale: str, emoji: str) -> str:
    header = _bi_header(locale, t(status_key, "en"), t(status_key, "zh"))
    return f"{emoji} <b>{H.escape(header)}</b>"


def _v2_rent_chip(status: str) -> str:
    """Rent chip: ``💰✅`` when the unit's rent is paid/current, ``💰⚠️`` when
    overdue. ``None`` (no active lease / vacant) -> no chip. Never renders a
    raw status enum."""
    s = str(status or "").lower()
    if s == "paid":
        return "💰✅"
    if s == "overdue_rent":
        return "💰⚠️"
    return ""


def _v2_lease_chip(status: str, days) -> str:
    """Lease chip: ``📄✅`` when the active lease is healthy, ``📄⚠️`` when it is
    expiring (within the 30-day window). Vacant / no lease -> no chip."""
    s = str(status or "").lower()
    if s == "paid":
        return "📄✅"
    if s == "lease_expiring":
        return "📄⚠️"
    return ""


def _v2_maintenance_chip(count) -> str:
    """Maintenance chip: ``🔧N`` for open maintenance count > 0, else ``🔧0``.
    Only rendered for leased/occupied rows (vacant rows keep the row sparse)."""
    try:
        n = int(count or 0)
    except (TypeError, ValueError):
        n = 0
    return f"🔧{n}"


def _v2_is_zero(value) -> bool:
    """True for missing/empty/zero amounts (handles Decimal, float and str)."""
    if value is None or value == "":
        return True
    try:
        return Decimal(str(value)) == 0
    except Exception:
        return False


def _v2_title(locale: str, key: str, emoji: str) -> str:
    return f"{emoji} <b>{H.escape(_bi_header(locale, t(key, 'en'), t(key, 'zh')))}</b>"


# --- TELEGRAM-OPS-UX-CONVERGENCE-001: rent detail + remind-owner cards ------

def rent_detail_card(
    *,
    unit_label: str,
    locale: str = "bi",
    tenant_name: str = "",
    outstanding=None,
    unpaid_periods: int = 0,
    overdue_days: int = 0,
    last_followup: str = "",
    vacant: bool = False,
    followup_status: str = "",
) -> str:
    """💰 Rent detail card for one overdue/collectible unit. Compact: title +
    tenant + outstanding + unpaid periods + overdue days + last follow-up. Only
    the fields that are real are shown; ``vacant`` shows the vacant line. The
    buttons (Follow up / Record payment / History) ride on the keyboard, never
    in the text.

    ``followup_status`` (§7), when provided, renders the real-world follow-up
    state as its own line (``🟡 已交秘书跟进`` / ``✅ 今日已催``) — a button tap
    never fabricates this; only the Secretary's real confirmation does.

    CONVERGENCE-003 §6: fields render as ONE bilingual line
    (``Outstanding ₱75,000 · 未付 ₱75,000``) — never two duplicated lines."""
    title = H.escape(t("v2.rent_detail_title", locale, unit=H.escape(unit_label)))
    lines = [f"<b>{title}</b>"]
    if followup_status:
        lines.append(f"{H.escape(followup_status)}")
    if vacant:
        lines.append(H.escape(t("v2.rent_vacant", locale)))
        return "\n".join(lines)
    if tenant_name:
        lines.append(
            H.escape(_bi_value(locale, f"Tenant: {tenant_name}", f"租客：{tenant_name}"))
        )
    if outstanding is not None:
        money = H.money(outstanding)
        lines.append(
            H.escape(_bi_value(locale, f"Outstanding {money}", f"未付 {money}"))
        )
    if unpaid_periods > 0:
        lines.append(
            H.escape(_bi_value(
                locale,
                f"Unpaid periods: {int(unpaid_periods)}",
                f"未付期数：{int(unpaid_periods)}",
            ))
        )
    if overdue_days >= 0:
        lines.append(
            H.escape(_bi_value(
                locale,
                f"Overdue {int(overdue_days)}d",
                f"逾期 {int(overdue_days)} 天",
            ))
        )
    if last_followup:
        lines.append(
            H.escape(_bi_value(locale, f"Last follow-up: {last_followup}", f"最近催租：{last_followup}"))
        )
    else:
        lines.append(H.escape(_bi_value(locale, "Last follow-up: none", "最近催租：无")))
    return "\n".join(lines)


def remind_owner_card(
    *,
    unit_label: str = "",
    purpose: str = "",
    amount=None,
    approved_date: str = "",
    waiting_days: int = 0,
    locale: str = "bi",
) -> str:
    """🔔 Payment Reminder DM (ZERO-LEARNING-004 §4): property/unit + purpose +
    amount + approved date + waiting duration + who requested the Owner's
    attention. Bilingual in groups / owner-language in the Owner's private
    chat. Only real fields are shown — placeholders never render."""
    header = H.escape(t("v2.remind_owner_title", locale))
    lines = [f"<b>{header}</b>", ""]
    if unit_label:
        lines.append(H.escape(unit_label))
    if purpose:
        lines.append(H.escape(purpose))
    if amount is not None:
        lines.append(f"<b>{H.money(amount)}</b>")
    if approved_date:
        lines.append(H.escape(t("v2.remind_owner_approved", locale, date=H.escape(approved_date))))
    lines.append(
        H.escape(t("v2.remind_owner_waiting", locale, days=int(waiting_days)))
    )
    lines.append(
        H.escape(_bi_value(locale, "Secretary requested your attention.", "秘书提醒您处理。"))
    )
    return "\n".join(lines)


def secretary_followup_card(
    *,
    unit_label: str,
    locale: str = "bi",
    tenant_name: str = "",
    tenant_phone: str = "",
    outstanding=None,
    unpaid_periods: int = 0,
    overdue_days: int = 0,
    last_followup: str = "",
    call_script: str = "",
    message_script: str = "",
    done: bool = False,
) -> str:
    """Secretary private-chat collection task card (§3/§13). Built ONLY from the
    real rent truth source (quick-rent row + unit/lease/tenant) — the amount,
    period count and overdue days are never re-derived by the renderer. When
    the tenant phone is present it is shown, and the call/message scripts (also
    built from structured truth, §14/§15) are attached so the Secretary never
    re-organizes language. When ``done`` the card shows the executed state and
    the buttons go away."""
    if done:
        lines = [
            f"<b>{H.escape(t('v2.sec_dm_contact_recorded', locale))}</b>",
        ]
        if unit_label:
            lines.append(H.escape(unit_label))
        lines.append(H.escape(t("v2.sec_dm_already_today", locale)))
        return "\n".join(lines)
    title = H.escape(t("v2.sec_dm_title", locale))
    lines = [f"🔔 <b>{title}</b>", ""]
    if unit_label:
        lines.append(H.escape(t("v2.sec_dm_unit", locale, unit=H.escape(unit_label))))
    if tenant_name:
        lines.append(H.escape(t("v2.sec_dm_tenant", locale, tenant=H.escape(tenant_name))))
    if tenant_phone:
        lines.append(H.escape(t("v2.sec_dm_phone", locale, phone=H.escape(tenant_phone))))
    if outstanding is not None:
        lines.append(H.escape(t("v2.sec_dm_outstanding", locale, amount=H.money(outstanding))))
    if unpaid_periods:
        lines.append(H.escape(t("v2.sec_dm_periods", locale, count=int(unpaid_periods))))
    if overdue_days:
        lines.append(H.escape(t("v2.sec_dm_overdue", locale, days=int(overdue_days))))
    if last_followup:
        lines.append(H.escape(t("v2.sec_dm_last", locale, date=H.escape(last_followup))))
    else:
        lines.append(H.escape(t("v2.sec_dm_last_none", locale)))
    lines.append("")
    lines.append(H.escape(t("v2.sec_dm_next_action", locale)))
    lines.append(H.escape(t("v2.sec_dm_body", locale)))
    if outstanding is not None and _dec(outstanding) > 0:
        lines.append(H.escape(t("v2.sec_dm_redirect_payment", locale)))
    if call_script:
        lines.append("")
        lines.append(H.escape(t("v2.sec_dm_call_script", locale, script=H.escape(call_script))))
    if message_script:
        lines.append("")
        lines.append(H.escape(t("v2.sec_dm_message_script", locale, script=H.escape(message_script))))
    return "\n".join(lines)


def followup_status_text(details: dict, locale: str, *, executed_daily: bool = False) -> str:
    """TELEGRAM-OPS-REAL-WORLD-CLOSURE-005 §7: one human state for a rent
    follow-up, used on the group Rent detail / Tasks row. The state mirrors
    reality, never the button click alone:
    🔴 需要催租 -> 🟡 已交秘书跟进 -> ✅ 今日已催 (only a real Secretary confirm)."""
    if executed_daily:
        return _bi_value(
            locale,
            t("v2.followup_status_executed", "en"),
            t("v2.followup_status_executed", "zh"),
        )
    if details and details.get("assigned_to"):
        return _bi_value(
            locale,
            t("v2.followup_status_assigned", "en"),
            t("v2.followup_status_assigned", "zh"),
        )
    return _bi_value(
        locale,
        t("v2.followup_status_pending", "en"),
        t("v2.followup_status_pending", "zh"),
    )


def quick_unit_view_card(
    *,
    unit_label: str,
    locale: str = "bi",
    vacant: bool = False,
    status: str = "normal",
    amount=None,
    days=None,
    open_maintenance: int = 0,
) -> str:
    """🏠 Property Quick View for one unit (frozen §3): occupancy + rent +
    lease + maintenance, compact, no tenant/deposit/contract expansion. The
    ``📄 Property Archive`` button and Rent/Maintenance entries ride on the
    keyboard; this card only summarises the unit state."""
    emoji = "⚪" if vacant else "🟢"
    lines = [f"🏠 <b>{emoji} {H.escape(unit_label)}</b>"]
    if vacant:
        lines.append(H.escape(t("v2.rent_vacant", locale)))
        return "\n".join(lines)
    s = str(status or "normal").lower()
    lines.append(_bi_line(locale, "Occupied", "已出租"))
    if s in ("paid", "rent_paid"):
        lines.append(_bi_line(locale, "Rent paid", "租金已收"))
    else:
        lines.append(H.escape(t("v2.rent_overdue", locale, days=int(days or 0))))
    if int(open_maintenance or 0):
        lines.append(
            _bi_line(
                locale,
                f"Repair {int(open_maintenance)} open",
                f"维修 {int(open_maintenance)} 项",
            )
        )
    if s == "lease_expiring":
        lines.append(_bi_line(locale, f"Lease expires in {int(days or 0)}d", f"租约还有 {int(days or 0)} 天到期"))
    else:
        lines.append(_bi_line(locale, "Lease OK", "租约正常"))
    return "\n".join(lines)


def property_archive_card(locale: str = "bi", *, link: str = "") -> str:
    """📄 Property Archive link card: the group is the index, the archive
    channel holds the full archive. When a configurable link exists it is
    surfaced as a tappable deep link; otherwise a short hint with no dead
    placeholder."""
    en = "Full property files live in the Property Archive channel."
    zh = "完整房产档案存放在 Property Archive 频道。"
    lines = [
        f"<b>{H.escape(t('v2.property_archive', locale))}</b>",
        _bi_line(locale, en, zh),
    ]
    if link:
        lines.append(f'<a href="{H.escape(link)}">{H.escape(link)}</a>')
    return "\n".join(lines)


def _v2_property_label(row: dict, locale: str) -> str:
    """ZERO-LEARNING-004 §1 + TELEGRAM-OPS-REAL-WORLD-CLOSURE-005 §1: one unit
    line with a SINGLE traffic-light + the exception expressed in WORDS:

      overdue rent:  ``🔴 1680 · Rent overdue 104d / 欠租104天 · 3期``
      vacant:        ``🟡 2308 · Vacant / 空置``
      expiring:      ``🟡 1203 · Lease expires in 18d / 合同18天到期``
      open repair:   any light + appends ``· Repair 1 / 待修1项`` (🟡/🔴
                     upgrade: an open repair is an action item -> 🔴)
      normal/paid:   ``🟢 1203 · OK``

    Exactly ONE light is shown (``_property_traffic_light``): the highest
    severity among the property's flags. ``unpaid_periods`` comes from the
    backend (the SAME truth source as the RENT_OVERDUE generator). The unit
    id is the SHORT room number (§1.4), never ``DEV-BAY-1680``. No
    ``💰⚠️`` / ``📄✅`` / ``🔧0`` / ``👁`` icon-password chips."""
    status = str(row.get("status") or "normal").lower()
    unit = _short_unit_label(
        row.get("unit_code") or row.get("property_code") or "", locale
    )
    light = _property_traffic_light(status)
    # §1.2: open repair is a current action item -> escalate the light to 🔴
    # unless an even-worse overdue state already dominates it.
    repair = int(row.get("open_maintenance") or 0)
    if repair and status not in ("overdue_rent", "overdue"):
        light = "🔴"
    en_parts = [f"{light} {unit}"]
    zh_parts = [f"{light} {unit}"]
    if status == "vacant":
        en_parts.append("Vacant")
        zh_parts.append("空置")
    elif status in ("overdue_rent", "overdue"):
        days = int(row.get("days") or 0)
        en_parts.append(f"Rent overdue {days}d")
        zh_parts.append(f"欠租{days}天")
        periods = row.get("unpaid_periods")
        if periods:
            en_parts.append(f"{int(periods)}期")
            zh_parts.append(f"{int(periods)}期")
    elif status == "lease_expiring":
        days = int(row.get("days") or 0)
        en_parts.append(f"Lease expires in {days}d")
        zh_parts.append(f"合同{days}天到期")
    else:  # normal / paid -> collapse to OK
        en_parts.append("OK")
        zh_parts.append("OK")
    if repair:
        en_parts.append(f"Repair {repair}")
        zh_parts.append(f"待修{repair}项")
    return _bi_value(locale, " · ".join(en_parts), " · ".join(zh_parts))


_TASK_TITLE_SENTINEL_RE = re.compile(
    r"(?:^|[\s·])(?:\?\?|\?|--|none|null|n\/a|na|unknown)(?=$|[\s·])",
    re.IGNORECASE,
)


def _clean_task_title(value) -> str:
    """Drop placeholder sentinel fragments (`??`, `None`, `null`, ...) from a
    task title. Legacy operational tasks created from a raw `??` expense
    category (e.g. `待付款支出 · ??`) must never reach a user-visible card
    (EXPENSE-UX-FIX-001 Bug 2). The real source fix lives in the backend task
    generator; this is a render-side guard for already-persisted rows."""
    text = " ".join(str(value or "").split())
    text = _TASK_TITLE_SENTINEL_RE.sub("", text)
    return text.strip(" ·")


def _v2_task_line(task: dict, locale: str, emoji: str) -> str:
    """One active-task row: unit · title · due/overdue · next action.

    CONVERGENCE-003 §5.3/§8: rows carry the REAL business context so two
    identical-looking reminders stay distinguishable and an overdue rent item
    never reads as ``due in 0d``:
    - payable/approval expenses: ``💸 E{id} · unit · purpose · ₱amount ·
      waiting Nd``
    - overdue rent: ``🔴 unit · ₱amount · N period(s) · overdue Nd`` + the
      next action line (the ACTION deadline, never the rent due date)."""
    unit = H.escape(str(task.get("property_code") or task.get("unit_code") or ""))
    title = H.escape(_clean_task_title(task.get("title")) or t("ops.task", locale))
    task_type = str(task.get("task_type") or "").upper()
    en_parts: list[str] = []
    zh_parts: list[str] = []
    # Stable business identity: expense tasks carry E{id}, rent tasks the
    # unit — a human can tell E7 apart from E8 without reading the title.
    expense_id = task.get("expense_id")
    if expense_id is not None:
        id_part = f"E{int(expense_id)}"
        en_parts.append(id_part)
        zh_parts.append(id_part)
    if unit:
        en_parts.append(unit)
        zh_parts.append(unit)
    if task_type in ("PAYMENT_PENDING", "APPROVAL_PENDING"):
        purpose = H.escape(_clean_free_text(task.get("purpose")) or "")
        amount = task.get("amount")
        if purpose:
            en_parts.append(purpose)
            zh_parts.append(purpose)
        if amount is not None:
            en_parts.append(H.money(amount))
            zh_parts.append(H.money(amount))
    elif task_type == "RENT_OVERDUE":
        amount = task.get("amount")
        periods = task.get("unpaid_periods")
        if amount is not None:
            en_parts.append(H.money(amount))
            zh_parts.append(H.money(amount))
        if periods:
            en_parts.append(f"{int(periods)} period(s)")
            zh_parts.append(f"{int(periods)}期")
        if task.get("followup_assigned"):
            en_parts.append(t("v2.followup_in_progress", "en"))
            zh_parts.append(t("v2.followup_in_progress", "zh"))
    else:
        en_parts.append(title)
        zh_parts.append(title)
    overdue_days = task.get("overdue_days")
    due_days = task.get("due_in_days")
    is_rent_action = task_type in ("RENT_OVERDUE", "FOLLOWUP")
    waiting = task.get("waiting_days")
    if waiting is None and task_type in ("PAYMENT_PENDING", "APPROVAL_PENDING") and overdue_days is not None:
        waiting = overdue_days  # an approved-but-unpaid expense "waits", it does not overduplicate
    if waiting is not None:
        en_parts.append(t("v2.waiting_days", "en", days=int(waiting)))
        zh_parts.append(t("v2.waiting_days", "zh", days=int(waiting)))
    elif overdue_days is not None:
        en_parts.append(t("v2.overdue_days", "en", days=overdue_days))
        zh_parts.append(t("v2.overdue_days", "zh", days=overdue_days))
    elif is_rent_action:
        # TELEGRAM-OPS-REAL-WORLD-CLOSURE-005 §12: an overdue-rent follow-up
        # must NEVER read ``due in 0d``. The action deadline is TODAY, not a
        # calendar due date, so when no real overdue count is available we say
        # the truthful "follow up today" instead of a misleading due-date.
        en_parts.append(t("v2.action_today", "en"))
        zh_parts.append(t("v2.action_today", "zh"))
    elif due_days is not None and int(due_days) > 0:
        en_parts.append(t("v2.due_in_days", "en", days=due_days))
        zh_parts.append(t("v2.due_in_days", "zh", days=due_days))
    elif task.get("due_at"):
        date_str = H.format_date(task.get("due_at"))
        en_parts.append(date_str)
        zh_parts.append(date_str)
    en_line = " · ".join(p for p in en_parts if p)
    zh_line = " · ".join(p for p in zh_parts if p)
    next_action = task.get("next_action")
    if next_action:
        en_line += "\n" + t("v2.next", "en", next=H.escape(str(next_action)))
        zh_line += "\n" + t("v2.next", "zh", next=H.escape(str(next_action)))
    return f"{emoji} {_bi_line(locale, en_line, zh_line)}"


_V2_EXPENSE_STATUS_EMOJI = {
    "pending": "⏳",
    "approved": "📋",
    "paid": "✅",
    "rejected": "❌",
    "reversed": "↩️",
}


def _v2_expense_status(status: str, locale: str) -> str:
    """One expense-record status chip: emoji + human label, bilingual in
    groups. Unknown statuses fall back to a neutral dot, never raw enums."""
    status = str(status or "").lower()
    emoji = _V2_EXPENSE_STATUS_EMOJI.get(status, "•")
    en = _expense_status_label(status, "en")
    zh = _expense_status_label(status, "zh")
    return f"{emoji} {_bi_header(locale, en, zh)}"


def _v2_mmdd(value) -> str:
    """YYYY-MM-DD -> MM-DD (e.g. 2026-08-15 -> 08-15); other input as-is."""
    s = str(value or "")
    if len(s) >= 10 and s[4] == "-" and s[:4].isdigit():
        return s[5:10]
    return s


def _v2_expense_record_line(row: dict, locale: str) -> str:
    """One expense record row: Expense ID · Unit · Purpose · Amount · MM-DD ·
    Status. Carries the stable plain-text ``E{id}`` (never the Telegram
    ``#E{id}``) so same-date/same-amount records stay distinguishable; if the
    id is absent the row still renders (unit-first)."""
    expense_id = row.get("expense_id")
    id_part = f"E{int(expense_id)}" if expense_id is not None else ""
    unit = H.escape(str(row.get("unit") or row.get("unit_code") or "-"))
    purpose = _v2_expense_purpose(row, locale)
    amount = H.money(row.get("amount"))
    date = H.escape(_v2_mmdd(row.get("expense_date") or row.get("date")))
    status = _v2_expense_status(row.get("status"), locale)
    parts = [x for x in (id_part, unit, purpose, f"<b>{amount}</b>", date, status) if x]
    return " · ".join(parts)


def _clean_free_text(value) -> str | None:
    """Trim text and drop placeholder/empty sentinels (`??`, None, null, empty,
    bare dash) so a real value is never replaced by a placeholder
    (PASAY-V2-EXPENSE-UX-AUDIT-005 §2)."""
    if not value:
        return None
    text = " ".join(str(value).split())
    if not text or text.lower() in {"none", "null", "??", "-"}:
        return None
    return text


def _v2_expense_purpose(row: dict, locale: str) -> str:
    """Purpose for one record row: purpose -> category -> description -> payee,
    else the locale-aware `Other / 其他` fallback. Raw `??`/None/null never
    render (P1-...-008 A3 adds payee so an incomplete record like E7/E8 still
    shows its truthful 'Repair' vendor before the neutral label)."""
    for field in (
        row.get("purpose"),
        row.get("category"),
        row.get("description"),
        row.get("payee"),
    ):
        text = _clean_free_text(field)
        if text:
            return H.escape(text)
    return H.escape(t("v2.expense_other", locale))


def _v2_is_zero(value) -> bool:
    """True for missing/empty/zero amounts (handles Decimal, float and str)."""
    if value is None or value == "":
        return True
    try:
        return Decimal(str(value)) == 0
    except Exception:
        return False


def properties_quick_card(data, locale: str = "bi") -> str:
    """🏠 Properties quick view: occupancy summary + ONE high-density line per
    unit, ordered by severity (§1.3: 🔴 first, then 🟡, then 🟢) so the Owner
    sees problem units first.

    §1.1/§1.2: each row shows exactly ONE traffic light (🟢/🟡/🔴) as the
    severity, with the concrete business fact in words. §1.4: the unit id is
    the SHORT room number. §1.5: the top summary is compact and bilingual in
    groups (``6 occupied · 1 vacant · 4 need action``). The title carries the
    unit count (``🏠 Properties · 7``)."""
    rows = data if isinstance(data, list) else ((data or {}).get("properties") or [])
    total = len(rows)
    header = _bi_header(
        locale, t("v2.properties_title", "en"), t("v2.properties_title", "zh")
    )
    blocks = [f"🏠 <b>{H.escape(header)}{(' · ' + str(total)) if total else ''}</b>"]
    if not rows:
        blocks.append(H.escape(t("v2.empty", locale)))
        return "\n".join(blocks)
    vacant = sum(1 for r in rows if str(r.get("status") or "normal").lower() == "vacant")
    occupied = total - vacant

    def _row_light(r) -> str:
        light = _property_traffic_light(str(r.get("status") or "normal").lower())
        if int(r.get("open_maintenance") or 0) and light != "🔴":
            light = "🔴"
        return light

    ordered = sorted(
        rows,
        key=lambda r: (_PROPERTY_TRAFFIC_ORDER.get(r.get("status", ""), 0),
                       str(r.get("unit_code") or r.get("property_code") or "")),
        reverse=True,
    )
    need_action = sum(1 for r in rows if _row_light(r) == "🔴")
    want_attention = sum(1 for r in rows if _row_light(r) == "🟡")
    blocks.append(_properties_summary_line(locale, total, occupied, vacant, need_action, want_attention))
    for row in ordered:
        blocks.append(_v2_property_label(row, locale))
    return "\n\n".join(blocks)


def _properties_summary_line(
    locale: str, total, occupied, vacant, need_action, want_attention,
) -> str:
    """§1.5: one compact bilingual occupancy+attention summary line. No long
    duplicated sentences, no whole-line English-then-Chinese repetition."""
    en_parts = [f"{t('properties.total', 'en')} {total}",
                f"{t('properties.occupied', 'en')} {occupied}"]
    zh_parts = [f"{t('properties.total', 'zh')} {total}",
                f"{t('properties.occupied', 'zh')} {occupied}"]
    if vacant:
        en_parts.append(f"{t('properties.vacant', 'en')} {vacant}")
        zh_parts.append(f"{t('properties.vacant', 'zh')} {vacant}")
    if need_action:
        en_parts.append(f"🔴 {t('properties.need_action', 'en')} {need_action}")
        zh_parts.append(f"🔴 {t('properties.need_action', 'zh')} {need_action}")
    elif want_attention:
        en_parts.append(f"🟡 {t('properties.want_attention', 'en')} {want_attention}")
        zh_parts.append(f"🟡 {t('properties.want_attention', 'zh')} {want_attention}")
    en_line = " · ".join(en_parts)
    zh_line = " · ".join(zh_parts)
    return f"📊 {_bi_value(locale, H.escape(en_line), H.escape(zh_line))}"


def _payable_expense_line(row: dict, locale: str) -> str:
    """One payable (APPROVED, unpaid) expense row with a stable visible
    identity: ``💸 E{id} · unit · purpose · amount · waiting Nd`` (plain text,
    never a Telegram ``#E{id}`` hashtag). The database expense id is the
    stable identity (PASAY-V2-EXPENSE-PAYABLE-TASK-006 §3), so same-day /
    same-amount expenses stay distinguishable. ZERO-LEARNING-004 §6: the row
    carries the SAME waiting-day fact as the task rows (single
    representation)."""
    expense_id = row.get("expense_id")
    id_part = f"E{int(expense_id)}" if expense_id is not None else ""
    unit = H.escape(str(row.get("unit") or ""))
    purpose = _v2_expense_purpose(row, locale)
    amount = H.money(row.get("amount"))
    waiting = row.get("waiting_days")
    parts = [x for x in (id_part, unit, purpose, f"<b>{amount}</b>") if x]
    if waiting is not None:
        parts.append(H.escape(t("v2.waiting_days", locale, days=int(waiting))))
    return "💸 " + " · ".join(parts)


def tasks_quick_card(data, locale: str = "bi") -> str:
    """✅ Tasks quick view: ONE representation per business item
    (ZERO-LEARNING-004 §6).

    Payable APPROVED expenses form the ``💸 To pay`` group (E{id} · unit ·
    purpose · amount · waiting Nd) and are then EXCLUDED from the Pending
    group — an expense never appears twice on the same first screen. Only the
    Pending / In-progress operational tasks that are NOT already covered by
    the To-pay group render below."""
    tasks = data if isinstance(data, list) else ((data or {}).get("tasks") or [])
    payable = [
        t_ for t_ in tasks if str(t_.get("kind") or "") == "payable_expense"
    ]
    payable_expense_ids = {
        p.get("expense_id") for p in payable if p.get("expense_id") is not None
    }

    def _covered_by_payable(t_) -> bool:
        # An APPROVAL_PENDING / PAYMENT_PENDING operational task that mirrors
        # a payable expense is the SAME business item — drop it from Pending.
        if str(t_.get("task_type") or "").upper() not in (
            "APPROVAL_PENDING", "PAYMENT_PENDING",
        ):
            return False
        return t_.get("expense_id") in payable_expense_ids

    operational = [t_ for t_ in tasks if t_ not in payable and not _covered_by_payable(t_)]
    pending = [
        t_ for t_ in operational
        if str(t_.get("status") or "").upper() == "PENDING"
    ]
    in_progress = [
        t_ for t_ in operational
        if str(t_.get("status") or "").upper() in ("IN_PROGRESS", "IN PROGRESS")
    ]
    other = [t_ for t_ in operational if t_ not in pending and t_ not in in_progress]
    # §13: a rent follow-up already ASSIGNED to the Secretary is NOT a red
    # pending item — it is a 🟡 in-progress item ("秘书跟进中"), the same
    # business action shown once, in its real state.
    assigned_followups = [
        t_ for t_ in pending
        if str(t_.get("task_type") or "").upper() in ("RENT_OVERDUE", "FOLLOWUP")
        and t_.get("followup_assigned")
    ]
    pending = [t_ for t_ in pending if t_ not in assigned_followups]
    blocks = [_v2_title(locale, "v2.tasks_title", "✅")]
    if not tasks:
        blocks.append(H.escape(t("v2.empty", locale)))
        return "\n".join(blocks)
    if payable:
        subtitle = _bi_value(locale, f"To pay {len(payable)}", f"待付款 {len(payable)}")
        blocks.append(f"💸 <b>{H.escape(subtitle)}</b>")
        blocks.extend(_payable_expense_line(p_, locale) for p_ in payable)
    if pending:
        blocks.append(_v2_section("v2.status.pending", locale, "🔴"))
        blocks.extend(_v2_task_line(t_, locale, "🔴") for t_ in pending)
    if assigned_followups or in_progress:
        blocks.append(_v2_section("v2.status.in_progress", locale, "🟡"))
        blocks.extend(_v2_task_line(t_, locale, "🟡") for t_ in in_progress)
        blocks.extend(_v2_task_line(t_, locale, "🟡") for t_ in assigned_followups)
    for t_ in other:
        blocks.append(_v2_task_line(t_, locale, "📋"))
    return "\n\n".join(blocks)


def rent_quick_card(data, locale: str = "bi") -> str:
    """💰 Rent quick view: current-month statistics (expected / collected /
    outstanding / collection rate / unpaid unit count) + overdue units.

    ``outstanding_rent`` = expected - valid collected in this month; partial
    payments reduce it and are never treated as fully paid. The overdue list
    below stays the per-period aggregate view."""
    data = data or {}
    overdue = data.get("overdue") or []
    outstanding = data.get("outstanding_total")
    blocks = [_v2_title(locale, "v2.rent_title", "💰")]
    expected = data.get("expected_rent_total")
    collected = data.get("collected_rent")
    cur_outstanding = data.get("outstanding_rent")
    rate = data.get("collection_rate")
    unpaid_count = data.get("unpaid_unit_count")
    if expected is not None:
        blocks.append(_rent_month_stats(locale, expected, collected, cur_outstanding, rate, unpaid_count))
    if not overdue:
        blocks.append(H.escape(t("v2.rent_no_overdue", locale)))
    else:
        blocks.append(_v2_section("v2.rent_overdue_section", locale, "🔴"))
        for row in overdue:
            unit = H.escape(str(row.get("unit") or row.get("unit_code") or ""))
            amount = H.money(row.get("amount"))
            days = row.get("overdue_days")
            if days is None:
                days = row.get("days")
            en = f"{unit} · {amount} · " + t("v2.overdue_days", "en", days=days or 0)
            zh = f"{unit} · {amount} · " + t("v2.overdue_days", "zh", days=days or 0)
            blocks.append("🔴 " + _bi_line(locale, en, zh))
    if outstanding is not None:
        # CONVERGENCE-003 §9: ``outstanding_total`` is the HISTORICAL arrears
        # sum (never the current-month number) — labelled distinctly from the
        # current-month ``outstanding_rent`` in the stats block above.
        blocks.append(
            H.escape(_bi_value(
                locale,
                f"Total overdue arrears {H.money(outstanding)}",
                f"历史累计欠租 {H.money(outstanding)}",
            ))
        )
    return "\n\n".join(blocks)


def _rent_month_stats(
    locale: str, expected, collected, outstanding, rate, unpaid_count,
) -> str:
    """One bilingual current-month rent stats block (expected/collected/
    outstanding/collection rate/unpaid unit count). ``None`` values are
    skipped, so a partial payload never renders `None/null`."""
    en_parts, zh_parts = [], []
    if expected is not None:
        en_parts.append(t("v2.rent_expected", "en", amount=H.money(expected)))
        zh_parts.append(t("v2.rent_expected", "zh", amount=H.money(expected)))
    if collected is not None:
        en_parts.append(t("v2.rent_collected", "en", amount=H.money(collected)))
        zh_parts.append(t("v2.rent_collected", "zh", amount=H.money(collected)))
    if outstanding is not None:
        # CONVERGENCE-003 §9: this month's gap is labelled "This month
        # outstanding · 本月未收" — never plain "Outstanding" (which is the
        # historical arrears total at the bottom of the card).
        en_parts.append(
            f"{H.escape(t('v2.rent_month_outstanding', 'en'))} {H.money(outstanding)}"
        )
        zh_parts.append(
            f"{H.escape(t('v2.rent_month_outstanding', 'zh'))} {H.money(outstanding)}"
        )
    if rate is not None:
        en_parts.append(t("v2.rent_collection_rate", "en", rate=_pct(rate)))
        zh_parts.append(t("v2.rent_collection_rate", "zh", rate=_pct(rate)))
    if unpaid_count is not None:
        en_parts.append(t("v2.rent_unpaid_units", "en", count=int(unpaid_count)))
        zh_parts.append(t("v2.rent_unpaid_units", "zh", count=int(unpaid_count)))
    en_line = " · ".join(p for p in en_parts if p)
    zh_line = " · ".join(p for p in zh_parts if p)
    if not en_line:
        return ""
    return f"📊 {_bi_line(locale, en_line, zh_line)}"


def expense_quick_card(data, locale: str = "bi") -> str:
    """💸 Expense quick view: month total, then the pending-payment queue
    (APPROVED, unpaid) built from the REAL expense fields, then this month's
    PAID records. No unresolved-task text block is generated, so an APPROVED
    expense appears exactly once per page and legacy `??` task titles can
    never leak in (EXPENSE-UX-FIX-001)."""
    data = data or {}
    month_total = data.get("month_total")
    if month_total is None:
        month_total = data.get("current_month_total")
    payable = data.get("payable") or []
    paid_records = data.get("paid_records")
    if paid_records is None:
        paid_records = [
            r for r in (data.get("records") or [])
            if str(r.get("status") or "").lower() == "paid"
        ]
    pending_count = data.get("pending_approval_count")
    pending_amount = data.get("pending_approval_amount")
    blocks = [_v2_title(locale, "v2.expense_title", "💸")]
    if month_total is not None:
        blocks.append(
            H.escape(t("v2.expense_month_total", locale, amount=H.money(month_total)))
        )
    if payable:
        blocks.append(_v2_expense_payable_header(locale, len(payable)))
        blocks.extend(_v2_expense_payable_line(row, locale) for row in payable)
    if paid_records:
        blocks.append(_v2_section("v2.expense_paid_section", locale, "✅"))
        blocks.extend(_v2_expense_record_line(row, locale) for row in paid_records)
    elif _v2_is_zero(month_total) and not payable:
        blocks.append(H.escape(t("v2.expense_records_empty", locale)))
    if pending_count:
        blocks.append(
            H.escape(
                t(
                    "v2.expense_pending_approval",
                    locale,
                    count=pending_count,
                    amount=H.money(pending_amount or 0),
                )
            )
        )
    return "\n\n".join(blocks)


def _v2_expense_payable_header(locale: str, count: int) -> str:
    """``📋 Pending payment / 待付款 · {count}`` — bilingual section header."""
    header = _bi_header(
        locale,
        t("v2.expense_pending_payment", "en"),
        t("v2.expense_pending_payment", "zh"),
    )
    return f"📋 <b>{H.escape(header)} · {count}</b>"


def _v2_expense_payable_line(row: dict, locale: str) -> str:
    """One APPROVED-unpaid (pending payment) expense row built from the REAL
    expense fields — Expense ID · Unit · Purpose · Amount · MM-DD ·
    ``📋 Approved/待付款``. Never derived from a task title, so a legacy `??`
    category can never leak into the UI (EXPENSE-UX-FIX-001 Bug 2)."""
    expense_id = row.get("expense_id")
    id_part = f"E{int(expense_id)}" if expense_id is not None else ""
    unit = H.escape(str(row.get("unit") or row.get("unit_code") or "-"))
    purpose = _v2_expense_purpose(row, locale)
    amount = H.money(row.get("amount"))
    date = H.escape(_v2_mmdd(row.get("expense_date") or row.get("date")))
    status = "📋 " + H.escape(
        _bi_header(
            locale,
            t("v2.expense_payable_status", "en"),
            t("v2.expense_payable_status", "zh"),
        )
    )
    parts = [x for x in (id_part, unit, purpose, f"<b>{amount}</b>", date, status) if x]
    return " · ".join(parts)


def _digest_section(header_key: str, locale: str, emoji: str) -> str:
    """One digest section header, e.g. ``🔴 现在处理 · 6`` (count appended by
    the caller as part of the header text when needed)."""
    return f"{emoji} <b>{H.escape(_bi_header(locale, t(header_key, 'en'), t(header_key, 'zh')))}</b>"


def _digest_act_line(item: dict, locale: str) -> str:
    """🔴 ACT-NOW line. Rent overdue reads ``催租 · ₱total · N期 · 逾期D天``
    from the REAL arrears truth; payable expense reads ``付款 · <purpose> ·
    ₱amount`` (PHASE 6: the action, not a bare category)."""
    kind = str(item.get("kind") or "").lower()
    unit = H.escape(str(item.get("unit") or item.get("unit_code") or ""))
    en: list[str] = []
    zh: list[str] = []
    if kind == "payable_expense" and item.get("expense_id") is not None:
        e_id = f"E{int(item['expense_id'])}"
        en.append(e_id)
        zh.append(e_id)
    if unit:
        en.append(unit)
        zh.append(unit)
    action_en = t("v2.digest_rent_action", "en") if kind == "rent_overdue" else t("v2.digest_pay_action", "en")
    action_zh = t("v2.digest_rent_action", "zh") if kind == "rent_overdue" else t("v2.digest_pay_action", "zh")
    en.append(action_en)
    zh.append(action_zh)
    if kind == "rent_overdue":
        if item.get("amount") is not None:
            en.append(H.money(item.get("amount")))
            zh.append(H.money(item.get("amount")))
        periods = item.get("unpaid_periods")
        if periods:
            en.append(f"{int(periods)} period(s)")
            zh.append(f"{int(periods)}期")
        if item.get("overdue_days") is not None:
            en.append(t("v2.overdue_days", "en", days=int(item["overdue_days"])))
            zh.append(t("v2.overdue_days", "zh", days=int(item["overdue_days"])))
    else:  # payable_expense
        purpose = H.escape(str(item.get("purpose") or ""))
        if purpose:
            en.append(purpose)
            zh.append(purpose)
        if item.get("amount") is not None:
            en.append(H.money(item.get("amount")))
            zh.append(H.money(item.get("amount")))
    en_line = " · ".join(p for p in en if p)
    zh_line = " · ".join(p for p in zh if p)
    return f"🔴 {_bi_line(locale, en_line, zh_line)}"


def _digest_upcoming_line(item: dict, locale: str) -> str:
    """🟡 UPCOMING line: ``1608 · 合同18天后到期`` (lease expiry is a watch item,
    never a red chase)."""
    unit = H.escape(str(item.get("unit") or item.get("unit_code") or ""))
    days = item.get("days_to_expiry")
    label_en = t("v2.digest_lease_action", "en", days=int(days or 0))
    label_zh = t("v2.digest_lease_action", "zh", days=int(days or 0))
    parts = [p for p in (unit, ) if p]
    en_line = " · ".join(parts + [label_en])
    zh_line = " · ".join(parts + [label_zh])
    return f"🟡 {_bi_line(locale, en_line, zh_line)}"


def _digest_done_line(item: dict, locale: str) -> str:
    """✅ DONE-TODAY line: only genuinely HUMAN completions. Rent follow-up reads
    ``1680 · 已联系租客``; a paid expense reads ``E4 · 已付款 · ₱amount``."""
    kind = str(item.get("kind") or "").lower()
    unit = H.escape(str(item.get("unit") or item.get("unit_code") or ""))
    expense_id = item.get("expense_id")
    en: list[str] = []
    zh: list[str] = []
    if expense_id is not None:
        e_id = f"E{int(expense_id)}"
        en.append(e_id)
        zh.append(e_id)
    if unit:
        en.append(unit)
        zh.append(unit)
    label_key = {
        "rent_followup": "v2.digest_followed_up",
        "expense_paid": "v2.digest_paid_action",
        "expense_approved": "v2.digest_approved_action",
        "maintenance": "v2.digest_maintenance_action",
    }.get(kind, "v2.digest_done_action")
    en.append(t(label_key, "en"))
    zh.append(t(label_key, "zh"))
    if kind == "expense_paid" and item.get("amount") is not None:
        en.append(H.money(item.get("amount")))
        zh.append(H.money(item.get("amount")))
    en_line = " · ".join(p for p in en if p)
    zh_line = " · ".join(p for p in zh if p)
    return f"✅ {_bi_line(locale, en_line, zh_line)}"


def _digest_section_block(data: dict, key: str, locale: str, emoji: str,
                          line_fn, row_key: str, max_items: int,
                          more_key: str = "v2.digest_more") -> str:
    """Build one digest section block. Defense-in-depth: rows are deduped by
    business_dedupe_key (PHASE 4 — one line per business object) and truncated
    to ``max_items`` with an overflow line (PHASE 11)."""
    rows = data.get(row_key) or []
    if not rows:
        return ""
    seen: set[str] = set()
    deduped: list[dict] = []
    for r in rows:
        bkey = r.get("business_dedupe_key") or (
            f"{r.get('kind')}:{r.get('expense_id') or r.get('unit')}"
        )
        if bkey in seen:
            continue
        seen.add(bkey)
        deduped.append(r)
    count = int((data.get("counts") or {}).get(row_key, len(deduped)))
    shown = deduped[:max_items]
    hidden = int((data.get("hidden") or {}).get(row_key, 0))
    header_en = t(key, "en")
    header_zh = t(key, "zh")
    header = _bi_header(locale, f"{header_en} · {count}", f"{header_zh} · {count}")
    blocks = [f"{emoji} <b>{H.escape(header)}</b>"]
    blocks.extend(line_fn(r, locale) for r in shown)
    if hidden or len(deduped) > max_items:
        overflow = hidden if hidden else len(deduped) - max_items
        blocks.append(H.escape(_bi_line(
            locale,
            t(more_key, "en", count=overflow),
            t(more_key, "zh", count=overflow),
        )))
    return "\n".join(blocks)


def active_tasks_digest_card(data, locale: str = "bi") -> str:
    """Daily Tasks Digest — three user-semantic sections.

    * 🔴 Act now — real current human actions (overdue rent / payable expense)
    * 🟡 Upcoming — near-term lease expiries
    * ✅ Done today — tasks a HUMAN completed today (system auto-completions
      never appear)

    One line per business object at most, hard mobile length caps per section,
    deterministic ordering, single-language per ``locale`` (zh / en / bi)."""
    data = data or {}
    title_en = t("v2.digest_title", "en")
    title_zh = t("v2.digest_title", "zh")
    blocks = [f"📋 <b>{H.escape(_bi_header(locale, title_en, title_zh))}</b>"]
    act = _digest_section_block(
        data, "v2.digest_act", locale, "🔴", _digest_act_line,
        "act_now", max_items=8, more_key="v2.digest_more",
    )
    upcoming = _digest_section_block(
        data, "v2.digest_upcoming", locale, "🟡", _digest_upcoming_line,
        "upcoming", max_items=5, more_key="v2.digest_more",
    )
    done = _digest_section_block(
        data, "v2.digest_done", locale, "✅", _digest_done_line,
        "done_today", max_items=3, more_key="v2.digest_done_more",
    )
    for block in (act, upcoming, done):
        if block:
            blocks.append(block)
    if not (act or upcoming or done):
        # Legacy payload fallback (older callers without the semantic keys).
        pending = data.get("pending") or []
        in_progress = data.get("in_progress") or []
        recently = data.get("recently_completed") or []
        if in_progress:
            blocks.append(_v2_section("v2.status.in_progress", locale, "🟡"))
            blocks.extend(_v2_task_line(t_, locale, "🟡") for t_ in in_progress)
        if pending:
            blocks.append(_v2_section("v2.status.pending", locale, "🔴"))
            blocks.extend(_v2_task_line(t_, locale, "🔴") for t_ in pending)
        if recently:
            blocks.append(_v2_section("v2.status.completed", locale, "✅"))
            blocks.extend(_v2_task_line(t_, locale, "✅") for t_ in recently)
        if not (pending or in_progress or recently):
            blocks.append(H.escape(t("v2.empty", locale)))
    return "\n\n".join(blocks)


def greeting_card(locale: str = "zh", reminder_count: int = 0) -> str:
    """V2 greeting: short, actionable, at most one reminder-count line.
    Never the full dashboard/portfolio summary."""
    blocks = ["👋 <b>Hello / 你好</b>"]
    blocks.append(H.escape(t("v2.greeting", locale)))
    if reminder_count > 0:
        blocks.append(H.escape(t("v2.greeting_reminders", locale, count=reminder_count)))
    return "\n".join(blocks)


def task_event_card(event: str, task: dict, locale: str = "zh") -> str:
    """Conversation-driven task feedback: created / updated / completed.
    Short, bilingual in groups, and always shows the next action when known."""
    status = str(task.get("status") or "").upper()
    if status == "PENDING":
        emoji = "🔴"
    elif status in ("IN_PROGRESS", "IN PROGRESS"):
        emoji = "🟡"
    else:
        emoji = "✅"
    event_low = str(event).lower()
    event_key = {
        "created": "v2.event.repair_reported",
        "updated": "v2.event.repair_in_progress",
        "completed": "v2.event.completed",
    }.get(event_low, "v2.event.updated")
    header = _bi_line(locale, t(event_key, "en"), t(event_key, "zh"))
    lines = [f"{emoji} <b>{H.escape(header)}</b>"]
    unit = H.escape(str(task.get("property_code") or task.get("unit_code") or ""))
    title = H.escape(str(task.get("title") or ""))
    where = f"{unit} · {title}" if unit else title
    if where:
        lines.append(where)
    if event_low == "created":
        lines.append(_bi_line(
            locale,
            t("v2.repair_waiting", "en"),
            t("v2.repair_waiting", "zh"),
        ))
        return "\n".join(lines)
    next_action = task.get("next_action")
    if next_action and event_low != "completed":
        lines.append(t("v2.next", locale, next=H.escape(str(next_action))))
    next_check = task.get("next_check_at")
    if next_check and event_low != "completed":
        lines.append(t("v2.check_at", locale, date=H.format_date(next_check)))
    return "\n".join(lines)
