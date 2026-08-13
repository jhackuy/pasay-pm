"""Deterministic NL query answers (BOT-V1-USABLE-001 P0-3).

Income/expense summaries, unit/tenant info and contract-expiry questions are
answered DIRECTLY from the existing read endpoints — no menu detour, no LLM,
no writes. Rent-status queries keep their dedicated handler in nl_bridge.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from telegram.ext import ContextTypes

from pasay_bot.api_client import PasayApiError
from pasay_bot.handlers.expense_flow import unit_matches
from pasay_bot.render import cards
from pasay_bot.render import html as H
from pasay_bot.render.i18n import t

HTML = "HTML"

_INCOME_QUERY_CN = ("收了多少钱", "收入多少", "收了多少", "收入", "收了多少钱", "收了")
_EXPENSE_QUERY_CN = ("花了多少钱", "花了多少", "支出多少", "支出记录", "花销", "花了", "支出")
_UNIT_INFO_CN = ("是谁租的", "谁在住", "谁租的", "租给谁", "租金多少", "月租多少", "房租多少", "合同什么时候到期", "租约什么时候到期", "租约到期", "合同到什么时候")
_CONTRACTS_CN = ("合同快到期", "合同到期", "租约快到期", "租约到期", "快到期", "要到期", "合同还有")
_QUERY_SUFFIX = re.compile(r"(吗|么|？|\?)\s*$")
_UNIT_TOKEN = re.compile(
    r"(?<![A-Za-z0-9-])("
    r"(?:[A-Za-z]+-)+[A-Za-z]*\d{1,4}[A-Za-z]?"
    r"|[A-Za-z]?\d{1,4}[A-Za-z]?"
    r")(?![A-Za-z0-9-])"
)


@dataclass(frozen=True)
class QueryIntent:
    kind: str  # income_summary | expense_summary | unit_expenses | unit_info | contracts
    unit_token: str = ""
    month: str = ""
    days: int = 30


def _current_month() -> str:
    return date.today().strftime("%Y-%m")


def _parse_month(text: str) -> str:
    lowered = (text or "").strip()
    if lowered in ("这个月", "本月", "this month", "this"):
        return _current_month()
    if lowered in ("上个月", "last month"):
        y, m = _current_month().split("-")
        m = int(m) - 1
        if m == 0:
            m, y = 12, int(y) - 1
        return f"{y:04d}-{m:02d}"
    match = re.search(r"(?:(\d{4})年?)?\s*(\d{1,2})\s*月", text or "")
    if match:
        year = int(match.group(1)) if match.group(1) else date.today().year
        month = int(match.group(2))
        if 1 <= month <= 12:
            return f"{year:04d}-{month:02d}"
    match = re.search(r"(\d{4})-(\d{1,2})", text or "")
    if match:
        return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}"
    return ""


def _parse_days(text: str) -> int:
    match = re.search(r"(\d{1,3})\s*天", text or "")
    if match:
        days = int(match.group(1))
        return max(1, min(days, 365))
    return 30


def detect_query(text: str) -> Optional[QueryIntent]:
    """Deterministic detector for the P0-3 query families (read-only)."""
    raw = (text or "").strip()
    if not raw or _QUERY_SUFFIX.search(raw):
        return None
    unit_m = _UNIT_TOKEN.search(raw)
    unit_token = unit_m.group(1) if unit_m else ""
    month = _parse_month(raw)

    if any(w in raw for w in _CONTRACTS_CN):
        return QueryIntent(kind="contracts", days=_parse_days(raw))
    if any(w in raw for w in _UNIT_INFO_CN):
        return QueryIntent(kind="unit_info", unit_token=unit_token)
    if any(w in raw for w in _INCOME_QUERY_CN):
        return QueryIntent(kind="income_summary", month=month or _current_month())
    if any(w in raw for w in _EXPENSE_QUERY_CN):
        return QueryIntent(
            kind="unit_expenses" if unit_token else "expense_summary",
            unit_token=unit_token,
            month=month or _current_month(),
        )
    return None


async def handle_query(context, chat_id, query: QueryIntent, locale: str):
    """Dispatch a detected query to its direct data answer."""
    text = await build_query_answer(context, query, locale)
    await context.bot.send_message(
        chat_id, H.truncate(text), parse_mode=HTML,
    )


async def build_query_answer(context, query: QueryIntent, locale: str) -> str:
    """Return the direct data answer text for a query (no send side effects),
    so callbacks can edit a card in place. Raises PasayApiError on failure."""
    api = context.bot_data["api_client"]
    if query.kind == "income_summary":
        return await _answer_income_summary(context, query.month, locale)
    if query.kind == "expense_summary":
        return await _answer_expense_summary(context, query.month, locale)
    if query.kind == "unit_expenses":
        return await _answer_unit_expenses(context, query.unit_token, locale)
    if query.kind == "unit_info":
        return await _answer_unit_info(context, query.unit_token, locale)
    if query.kind == "contracts":
        return await _answer_contracts(context, query.days, locale)
    return t("ai.unknown", locale)


async def _answer_income_summary(context, month, locale) -> str:
    api = context.bot_data["api_client"]
    fin = await api.get_financial_summary(month)
    return cards.income_summary_card(
        month,
        collected=fin.collected_rent,
        expected=fin.expected_rent_total,
        outstanding=fin.outstanding_rent,
        locale=locale,
    )


async def _answer_expense_summary(context, month, locale) -> str:
    api = context.bot_data["api_client"]
    fin = await api.get_financial_summary(month)
    return cards.expense_summary_card(
        month,
        total_expense=fin.total_expense,
        net_income=fin.net_income,
        locale=locale,
    )


async def _answer_unit_expenses(context, unit_token, locale) -> str:
    api = context.bot_data["api_client"]
    units = await api.get_units()
    unit = next((u for u in units if unit_matches(unit_token, u.unit_number)), None)
    if unit is None:
        return t("rent_status.no_unit", locale, unit=unit_token)
    expenses = await api.list_expenses()
    rows = []
    for e in sorted(expenses, key=lambda x: (x.expense_date, x.id), reverse=True):
        if e.unit_id != unit.id:
            continue
        status_key = {
            "pending": "expense.status_pending",
            "approved": "expense.status_approved",
            "rejected": "expense.status_rejected",
            "paid": "expense.status_paid",
            "reversed": "expense.status_reversed",
        }.get((e.status or "").lower())
        rows.append(
            {
                "category": e.category,
                "amount": e.amount,
                "expense_date": e.expense_date,
                "status_label": t(status_key, locale) if status_key else "",
            }
        )
    return cards.unit_expense_history_card(unit.unit_number, rows, locale)


async def _answer_unit_info(context, unit_token, locale) -> str:
    api = context.bot_data["api_client"]
    units, leases, tenants, properties = (
        await _gather(api, "units", "leases", "tenants", "properties")
    )
    unit = next((u for u in units if unit_matches(unit_token, u.unit_number)), None)
    if unit is None:
        return t("rent_status.no_unit", locale, unit=unit_token)
    prop = next((p for p in properties if p.id == unit.property_id), None)
    lease = next(
        (l for l in leases if l.unit_id == unit.id and l.status == "active"), None
    )
    if lease is None:
        return t("query.unit_no_active_lease", locale)
    tenant = next((tn for tn in tenants if tn.id == lease.tenant_id), None)
    return cards.unit_info_card(
        unit_number=unit.unit_number,
        property_name=prop.name if prop else "",
        tenant_name=tenant.full_name if tenant else "",
        monthly_rent=lease.monthly_rent,
        end_date=lease.end_date,
        locale=locale,
    )


async def _answer_contracts(context, days, locale) -> str:
    api = context.bot_data["api_client"]
    leases, units, tenants = await _gather(api, "leases", "units", "tenants")
    today = date.today()
    by_unit = {u.id: u.unit_number for u in units}
    by_tenant = {tn.id: tn.full_name for tn in tenants}
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
    return cards.contracts_card(rows, days, locale)


async def _gather(api, *names: str):
    import asyncio

    methods = {
        "units": api.get_units,
        "leases": api.get_leases,
        "tenants": api.get_tenants,
        "properties": api.get_properties,
    }
    results = await asyncio.gather(*(methods[n]() for n in names))
    return tuple(results)
