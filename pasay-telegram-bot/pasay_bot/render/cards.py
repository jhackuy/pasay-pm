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
    tenant_id = task.tenant_id
    if tenant_id:
        lines.append(f"{H.escape(t('ops.tenant', locale))}：#<code>{tenant_id}</code>")
    amount = _ops_amount(task)
    if amount is not None:
        lines.append(f"{H.escape(t('ops.amount', locale))}：{H.money(amount)}")
    lines.append(f"{H.escape(t('ops.due', locale))}：{H.escape(_ops_due(task))}")
    lines.append(f"{H.escape(t('ops.status', locale))}：{_ops_status_label(task, locale)}")
    lines.append(f"类型：<code>{H.escape(task.task_type or '')}</code>")
    if task.description:
        lines.append(H.escape(task.description))
    return "\n".join(lines)
