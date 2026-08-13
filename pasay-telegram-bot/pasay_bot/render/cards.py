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
) -> str:
    """One human status line: paid, or unpaid + owed + overdue (days, else
    periods). Internal enum/DB values are never rendered."""
    if paid:
        return H.escape(t("rent_status.paid", locale))
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
    lines.append(
        f"📅 {period}：{_rent_status_line(locale, paid, outstanding, overdue_days, overdue_months)}"
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
    )


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
    """Unpaid-unit collect list (B4). ``rows``: unit_id, unit_number,
    property_name, amount, overdue_days (0 when not overdue). Paid/vacant
    units are filtered out before this renderer is called."""
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
        marker = ""
        if int(r.get("overdue_days") or 0) > 0:
            marker = " " + H.escape(
                t("rent.collect_overdue", locale, days=int(r["overdue_days"]))
            )
        blocks.append(
            f"{H.escape(where)}\n"
            f"{H.escape(t('rent.amount', locale))}：<b>{H.money(r.get('amount'))}</b>{marker}"
        )
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
    purpose = " · ".join(
        x for x in (expense.category, expense.description or "") if x
    )
    if purpose:
        lines.append(f"{H.escape(t('expense.purpose', locale))}：{H.escape(purpose)}")
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


def expense_result_card(expense: Expense, locale: str = "zh") -> str:
    """Message-mutation result card: the tapped decision + the human next step.
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
    lines = [title, f"{H.escape(expense.category or '')} · {H.money(expense.amount)}", next_step]
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
    lines.append(f"{H.escape(t('expense.purpose', locale))}：{H.escape(expense.category)}")
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


def todo_overview_card(sections: dict, locale: str = "zh") -> str:
    """Unified to-do page (V1.3): only what the current user must act on.
    Rows are human-readable; action buttons ride below each row."""
    blocks = [f"<b>{H.escape(t('todo.title', locale))}</b>"]
    if not any(sections.values()):
        return "\n".join(
            [
                f"<b>{H.escape(t('todo.title', locale))}</b>",
                H.escape(t("todo.empty", locale)),
            ]
        )
    expenses = sections.get("expenses") or []
    if expenses:
        items = [
            f"💳 {H.escape(r.get('category', ''))} · {H.escape(r.get('payee', ''))}"
            f" · <b>{H.money(r.get('amount'))}</b>"
            + (f"\n{H.escape(r.get('location', ''))}" if r.get("location") else "")
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
    """Q&A answer card (C1.1). Human text only."""
    blocks = [f"🤖 <b>{H.escape(t('copilot.ask_title', locale))}</b>"]
    blocks.append(_copilot_one_line(answer, max_chars=900))
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
