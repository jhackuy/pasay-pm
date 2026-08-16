"""Deterministic HTML cards. All message text must be built here (or via
html helpers) — no ad-hoc f-string message assembly in handlers."""
from __future__ import annotations

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
    else:
        return expense_approval_card(expense, locale)
    purpose = _expense_purpose_text(expense) or t("expense.purpose_unspecified", locale)
    where = " · ".join(x for x in (location, purpose) if x)
    lines = [title, f"{H.escape(where)}  ·  <b>{H.money(expense.amount)}</b>", next_step]
    return "\n".join(x for x in lines if x)


def expense_detail_card(
    expense: Expense, locale: str = "zh", location: str = "",
) -> str:
    """Human-readable expense detail. Receipt presence is shown as a label;
    raw attachment ids stay internal."""
    lines = [f"<b>{H.escape(t('expense.detail_title', locale))}</b>"]
    loc = _expense_location(expense, location)
    if loc:
        lines.append(loc)
    purpose = _expense_purpose_text(expense)
    lines.append(
        f"{H.escape(t('expense.purpose', locale))}："
        f"{H.escape(purpose or t('expense.purpose_unspecified', locale))}"
    )
    if expense.description:
        lines.append(H.escape(expense.description))
    lines.append(f"{H.escape(t('expense.payee', locale))}：{H.escape(expense.payee or '-')}")
    lines.append(f"{H.escape(t('rent.amount', locale))}：<b>{H.money(expense.amount)}</b>")
    lines.append(
        f"{H.escape(t('expense.date', locale))}：{H.format_date(expense.expense_date)}"
    )
    if expense.due_date:
        lines.append(
            f"{H.escape(t('expense.due_date', locale))}：{H.format_date(expense.due_date)}"
        )
    lines.append(
        f"{H.escape(t('ops.status', locale))}：{_expense_status_label(expense.status, locale)}"
    )
    lines.append(
        H.escape(t("expense.receipt_present", locale))
        if expense.receipt_attachment_id
        else H.escape(t("expense.receipt_missing", locale))
    )
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

    Carries the stable identity ``#E{id}`` plus unit/purpose/amount/date. A
    receipt is OPTIONAL (shown as a hint, never a blocker). When ``similar``
    holds possible-duplicate PAID rows, an advisory bilingual warning lists the
    existing IDs — it never deletes or rejects the current expense."""
    id_part = f"#E{expense.id}"
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
            f"#E{int(r['expense_id'])}" for r in similar if r.get("expense_id")
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
    where = " · ".join(x for x in (f"#E{expense.id}", purpose) if x)
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
    collected,
    unpaid_count: int,
    pending_approvals: int,
    expiring_contracts: int,
    maintenance_open: int,
    locale: str = "zh",
) -> str:
    """P0 spec home: operational summary only (no second navigation)."""
    lines = ["<b>Pasay Property</b>"]
    lines.append(
        f"{H.escape(t('home.collected', locale))}：<b>{H.money(collected)}</b>"
    )
    lines.append(H.escape(t("home.unpaid_count", locale, count=unpaid_count)))
    lines.append(H.escape(t("home.pending_approvals", locale, count=pending_approvals)))
    lines.append(H.escape(t("home.expiring_contracts", locale, count=expiring_contracts)))
    lines.append(H.escape(t("home.maintenance_open", locale, count=maintenance_open)))
    return "\n".join(lines)


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
        items = [
            f"{'💳' if (r.get('status') or '').lower() != 'approved' else '💸'} "
            f"{'#E' + str(r['id']) if r.get('id') is not None else ''} · "
            f"{H.escape(r.get('category', ''))} · <b>{H.money(r.get('amount'))}</b>"
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

_V2_STATUS_EMOJI = {
    "overdue_rent": "🔴",
    "lease_expiring": "🟡",
    "paid": "🟢",
    "rent_paid": "🟢",
    "vacant": "⚪",
    "normal": "🔵",
}


def _bi_line(locale: str, en: str, zh: str) -> str:
    """One visible business line: English + 中文 in group mode; single
    language otherwise. Callers escape HTML before passing fragments in."""
    if locale == "bi":
        return f"{en}\n{zh}"
    return en if locale == "en" else zh


def _bi_header(locale: str, en: str, zh: str) -> str:
    """Compact one-line bilingual header, e.g. 'Pending / 未完成'."""
    if locale == "bi":
        return f"{en} / {zh}"
    return en if locale == "en" else zh


def _v2_section(status_key: str, locale: str, emoji: str) -> str:
    header = _bi_header(locale, t(status_key, "en"), t(status_key, "zh"))
    return f"{emoji} <b>{H.escape(header)}</b>"


def _v2_property_label(row: dict, locale: str) -> tuple[str, str]:
    """(emoji, rendered line) for one property quick-view row."""
    status = str(row.get("status") or "normal").lower()
    unit = H.escape(str(row.get("unit_code") or row.get("property_code") or ""))
    amount = H.money(row.get("amount")) if row.get("amount") is not None else ""
    days = row.get("days")
    if days is None:
        days = row.get("due_days")
    if status == "overdue_rent":
        en = t("v2.status.overdue_rent", "en", amount=amount)
        zh = t("v2.status.overdue_rent", "zh", amount=amount)
    elif status == "lease_expiring":
        en = t("v2.status.lease_expiring", "en", days=days or 0)
        zh = t("v2.status.lease_expiring", "zh", days=days or 0)
    elif status in ("paid", "rent_paid"):
        en, zh = t("v2.status.rent_paid", "en"), t("v2.status.rent_paid", "zh")
    elif status == "vacant":
        en, zh = t("v2.status.vacant", "en"), t("v2.status.vacant", "zh")
    else:
        en, zh = t("v2.status.normal", "en"), t("v2.status.normal", "zh")
    emoji = _V2_STATUS_EMOJI.get(status, "🔵")
    line = _bi_line(locale, f"{unit} · {en}" if unit else en,
                    f"{unit} · {zh}" if unit else zh)
    return emoji, line


def _v2_task_line(task: dict, locale: str, emoji: str) -> str:
    """One active-task row: unit · title · due/overdue · next action."""
    unit = H.escape(str(task.get("property_code") or task.get("unit_code") or ""))
    title = H.escape(str(task.get("title") or t("ops.task", locale)))
    en_parts: list[str] = []
    zh_parts: list[str] = []
    if unit:
        en_parts.append(unit)
        zh_parts.append(unit)
    en_parts.append(title)
    zh_parts.append(title)
    overdue_days = task.get("overdue_days")
    due_days = task.get("due_in_days")
    if overdue_days is not None:
        en_parts.append(t("v2.overdue_days", "en", days=overdue_days))
        zh_parts.append(t("v2.overdue_days", "zh", days=overdue_days))
    elif due_days is not None:
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
    Status. Carries the stable ``#E{id}`` so same-date/same-amount records stay
    distinguishable; if the id is absent the row still renders (unit-first)."""
    expense_id = row.get("expense_id")
    id_part = f"#E{int(expense_id)}" if expense_id is not None else ""
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


def _v2_title(locale: str, key: str, emoji: str) -> str:
    return f"{emoji} <b>{H.escape(_bi_header(locale, t(key, 'en'), t(key, 'zh')))}</b>"


def properties_quick_card(data, locale: str = "bi") -> str:
    """🏠 Properties quick view: occupancy summary + one line per unit.

    Deterministic summary (no LLM): total / occupied (rented) / vacant /
    occupancy rate derived from each unit row's status. ``vacant`` marks an
    empty unit; every other status (overdue_rent / lease_expiring / normal /
    paid) implies an active lease, i.e. occupied/rented. Rent delinquency is
    NOT counted as a separate property stat — it stays a per-unit status only."""
    rows = data if isinstance(data, list) else ((data or {}).get("properties") or [])
    blocks = [_v2_title(locale, "v2.properties_title", "🏠")]
    if not rows:
        blocks.append(H.escape(t("v2.empty", locale)))
        return "\n".join(blocks)
    total = len(rows)
    vacant = sum(1 for r in rows if str(r.get("status") or "normal").lower() == "vacant")
    occupied = total - vacant
    rate = (occupied / total * 100) if total else 0
    blocks.append(_properties_summary_line(locale, total, occupied, vacant, rate))
    for row in rows:
        emoji, line = _v2_property_label(row, locale)
        blocks.append(f"{emoji} {line}")
    return "\n\n".join(blocks)


def _properties_summary_line(locale: str, total: int, occupied: int, vacant: int, rate) -> str:
    """One compact occupancy summary line, bilingual in groups."""
    en = (
        f"{H.escape(t('properties.total', 'en'))} {total} · "
        f"{H.escape(t('properties.occupied', 'en'))} {occupied} · "
        f"{H.escape(t('properties.vacant', 'en'))} {vacant} · "
        f"{H.escape(t('properties.occupancy_rate', 'en'))} {rate:.0f}%"
    )
    zh = (
        f"{H.escape(t('properties.total', 'zh'))} {total} · "
        f"{H.escape(t('properties.occupied', 'zh'))} {occupied} · "
        f"{H.escape(t('properties.vacant', 'zh'))} {vacant} · "
        f"{H.escape(t('properties.occupancy_rate', 'zh'))} {rate:.0f}%"
    )
    return f"📊 {_bi_line(locale, en, zh)}"


def _payable_expense_line(row: dict, locale: str) -> str:
    """One payable (APPROVED, unpaid) expense row with a stable visible
    identity: ``💸 #E{id} · unit · purpose · amount · Approved``. The database
    expense id is the stable identity (PASAY-V2-EXPENSE-PAYABLE-TASK-006 §3),
    so same-day/same-amount expenses stay distinguishable."""
    expense_id = row.get("expense_id")
    id_part = f"#E{int(expense_id)}" if expense_id is not None else ""
    unit = H.escape(str(row.get("unit") or ""))
    purpose = _v2_expense_purpose(row, locale)
    amount = H.money(row.get("amount"))
    status = H.escape(_expense_status_label(row.get("status"), locale))
    parts = [x for x in (id_part, unit, purpose, f"<b>{amount}</b>", status) if x]
    return "💸 " + " · ".join(parts)


def tasks_quick_card(data, locale: str = "bi") -> str:
    """✅ Tasks quick view: active tasks grouped by status, plus the Owner's
    payable APPROVED expenses (PASAY-V2-EXPENSE-PAYABLE-TASK-006).

    Payable expenses use ``#E{id}`` as the stable identity so same-day /
    same-amount records stay distinguishable. When payable rows exist the card
    never shows the empty state."""
    tasks = data if isinstance(data, list) else ((data or {}).get("tasks") or [])
    payable = [
        t_ for t_ in tasks if str(t_.get("kind") or "") == "payable_expense"
    ]
    operational = [t_ for t_ in tasks if t_ not in payable]
    pending = [
        t_ for t_ in operational
        if str(t_.get("status") or "").upper() == "PENDING"
    ]
    in_progress = [
        t_ for t_ in operational
        if str(t_.get("status") or "").upper() in ("IN_PROGRESS", "IN PROGRESS")
    ]
    other = [t_ for t_ in operational if t_ not in pending and t_ not in in_progress]
    blocks = [_v2_title(locale, "v2.tasks_title", "✅")]
    if not tasks:
        blocks.append(H.escape(t("v2.empty", locale)))
        return "\n".join(blocks)
    if payable:
        subtitle = t("v2.payable_expenses", locale, count=len(payable))
        blocks.append(f"💸 <b>{H.escape(subtitle)}</b>")
        blocks.extend(_payable_expense_line(p_, locale) for p_ in payable)
    if pending:
        blocks.append(_v2_section("v2.status.pending", locale, "🔴"))
        blocks.extend(_v2_task_line(t_, locale, "🔴") for t_ in pending)
    if in_progress:
        blocks.append(_v2_section("v2.status.in_progress", locale, "🟡"))
        blocks.extend(_v2_task_line(t_, locale, "🟡") for t_ in in_progress)
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
        blocks.append(
            H.escape(t("v2.outstanding_total", locale, amount=H.money(outstanding)))
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
        en_parts.append(t("v2.rent_outstanding", "en", amount=H.money(outstanding)))
        zh_parts.append(t("v2.rent_outstanding", "zh", amount=H.money(outstanding)))
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
    """💸 Expense quick view: month total + this month's records + pending
    approval + unresolved. Records are the default view (PAID included);
    the unresolved line stays as auxiliary status only."""
    data = data or {}
    month_total = data.get("month_total")
    if month_total is None:
        month_total = data.get("current_month_total")
    pending_count = data.get("pending_approval_count")
    pending_amount = data.get("pending_approval_amount")
    unresolved = data.get("unresolved_expense_tasks") or []
    records = data.get("records") or []
    blocks = [_v2_title(locale, "v2.expense_title", "💸")]
    if month_total is not None:
        blocks.append(
            H.escape(t("v2.expense_month_total", locale, amount=H.money(month_total)))
        )
    if records:
        blocks.append(_v2_section("v2.expense_records_section", locale, "📋"))
        blocks.extend(_v2_expense_record_line(row, locale) for row in records)
    elif _v2_is_zero(month_total):
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
    if unresolved:
        blocks.append(
            H.escape(t("v2.expense_unresolved", locale, count=len(unresolved)))
        )
        for task in unresolved[:5]:
            blocks.append(_v2_task_line(task, locale, "💸"))
    else:
        blocks.append(H.escape(t("v2.expense_no_unresolved", locale)))
    return "\n\n".join(blocks)


def active_tasks_digest_card(data, locale: str = "bi") -> str:
    """Daily Active Tasks Digest: pending / in progress / recently completed."""
    data = data or {}
    pending = data.get("pending") or []
    in_progress = data.get("in_progress") or []
    recently = data.get("recently_completed") or []
    blocks = [_v2_title(locale, "v2.digest_title", "📋")]
    if pending:
        blocks.append(_v2_section("v2.status.pending", locale, "🔴"))
        blocks.extend(_v2_task_line(t_, locale, "🔴") for t_ in pending)
    if in_progress:
        blocks.append(_v2_section("v2.status.in_progress", locale, "🟡"))
        blocks.extend(_v2_task_line(t_, locale, "🟡") for t_ in in_progress)
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
