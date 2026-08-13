"""Deterministic keyword/command parsing (no LLM this phase).

The Hermes NLU adapter is intentionally NOT wired up in this phase. Recognized
Chinese/English phrases route to the same deterministic pages as the buttons;
anything else gets a "use the buttons" reply.

V1.3 Slice 2 (Entry B): rent-payment statements ("1608租金收到了",
"John的70000到了", "昨天收到1608房租") are recognized BEFORE the button
routes and resolved by the backend matcher into an action-at-source confirm
card. The matcher is read-only; confirmation reuses the existing Income
create + Owner-only confirm chain.

V1.3 Slice 2 (Entry C): read-only rent status queries ("这个月谁还没交",
"1608 交了没有", "John 交了吗") are recognized AFTER the statement detector
and BEFORE the button routes, then answered deterministically from the
existing read endpoints (units/leases/tenants/incomes/overdue/properties).
No write path is ever reached for these queries.

SLICE2-RENT-005: partial payments ("1608 付了 40000", "1608 又付了 30000")
flow through the same statement -> matcher -> confirm chain; the matcher now
returns the period receivable (due_amount), the confirmed total before this
payment (paid_amount) and the balance remaining after it, and guards
overpayments (never book, explain instead). Status queries additionally
answer "交了多少 / 交清了吗" with the same read-only data.

SLICE2-RENT-006 (correction): Owner/Secretary correction statements
("不是 1608，是 1708" / "not 1608, it's 1708" / "608 应是 1708") are
recognized as rent-payment statements (never "unknown"), and the negated
value is normalized to the corrected one before the matcher is called, so
the confirm card / pending path always shows the corrected unit (1708, not
1608). Status queries reuse the same normalization for their unit token.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes

from pasay_bot.api_client import (
    PasayApiConflictError,
    PasayApiError,
    PasayApiPermissionError,
    PasayApiTimeoutError,
)
from pasay_bot.handlers import commands as pages
from pasay_bot.keyboards import (
    confirm_income_keyboard,
    menu_keyboard,
    new_nonce,
    now_ts,
    rent_match_keyboard,
    rent_status_candidates_keyboard,
    secretary_registered_keyboard,
)
from pasay_bot.render import html as H
from pasay_bot.render import cards
from pasay_bot.render.i18n import t
from pasay_bot.roles import (
    PERMISSION_RENT_ENTRY,
    PERMISSION_RENT_CONFIRM,
    Role,
    has_permission,
    has_read_permission,
    locale_for,
    role_for_telegram_id,
    telegram_id_for_role,
)

HTML = "HTML"
logger = logging.getLogger(__name__)

_RENT_WORDS_CN = ("租金", "房租")
_RENT_WORD_EN = re.compile(r"\brent\b")
_RECEIVE_VERBS_CN = ("收到", "到了", "到账", "已收", "入账", "收款", "付了", "支付")
_RECEIVE_VERB_EN = re.compile(r"\b(received|paid)\b")
_AMOUNT_IN_TEXT = re.compile(r"\d[\d,]{4,}")
_AMOUNT_ZERO = re.compile(r"(?<!\d)0(?:\.0+)?(?!\d)")
_AMOUNT_NEGATIVE = re.compile(r"(?<!\d)[-−]\s*\d[\d,]*")
_QUERY_SUFFIX = re.compile(r"(吗|么|？|\?|没有|没)\s*$")

# --- SLICE2-RENT-006: correction statements --------------------------------
# "不是 1608，是 1708" / "是 1708 不是 1608" / "not 1608, it's 1708" /
# "it's 1708 not 1608". Tokens must look like unit ids / amounts (>=2 chars
# and at least one digit) so month words or single digits never misfire.
_CORRECTION_RE = re.compile(
    r"不是\s*(?P<neg1>[A-Za-z0-9][A-Za-z0-9._-]*)\s*[,，]?\s*是\s*(?P<pos1>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"|是\s*(?P<pos2>[A-Za-z0-9][A-Za-z0-9._-]*)\s*[,，]?\s*不是\s*(?P<neg2>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"|\bnot\s+(?P<neg3>[A-Za-z0-9][A-Za-z0-9._-]*)\s*,?\s+(?:it'?s|it is|is)\s+(?P<pos3>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"|\b(?:it'?s|it is|is)\s+(?P<pos4>[A-Za-z0-9][A-Za-z0-9._-]*)\s*,?\s+not\s+(?P<neg4>[A-Za-z0-9][A-Za-z0-9._-]*)",
    re.IGNORECASE,
)

# --- V1.3 Slice 2 (Entry C): read-only rent status queries ------------------
_WHO_UNPAID_CN = (
    "这个月谁还没交", "谁还没交", "谁没交", "还没交房租", "没交房租",
    "谁还没交租", "谁没交租", "还没交租金", "没交租金", "未交房租", "未交租金",
)
_WHO_UNPAID_EN = re.compile(
    r"\bwho\s+(?:hasn't|has not|hasnt)\s+paid\b"
    r"|\bwho\s+(?:didn't|did not|didnt)\s+pay\b"
    r"|\bunpaid\s+this\s+month\b",
    re.IGNORECASE,
)
_UNIT_TOKEN = re.compile(
    r"(?<![A-Za-z0-9-])("
    r"(?:[A-Za-z]+-)+[A-Za-z]*\d{1,4}[A-Za-z]?"  # prefixed ids: DEV-BAY-1608
    r"|[A-Za-z]?\d{1,4}[A-Za-z]?"                 # plain ids: 1608 / 16B / 2C
    r")(?![A-Za-z0-9-])"
)
_NAME_TOKEN = re.compile(r"\b([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)?)\b")
_QUERY_VERB_CN = (
    "交了没有", "交了没", "交了吗", "交了么", "交没交", "有没有交",
    "还欠多少", "欠多少", "欠多少钱", "没交吗", "没交钱吗", "交钱了吗",
    "付了吗", "付了没有", "还没交", "没交租", "没交租金", "交了钱吗",
    "交了多少", "付了多少", "交清了吗", "交清没有", "付清了吗", "付清没有",
)
_QUERY_VERB_EN = re.compile(
    r"\b(?:has|hasn't|has not|hasnt|did|didn't|did not|didnt|does)\b.{0,24}\b(?:paid|pay)\b"
    r"|\bhow\s+much\b.{0,24}\bowe[sd]?\b"
    r"|\bhow\s+much\b.{0,24}\b(?:paid|pay)\b"
    r"|\bpaid\s+in\s+full\b"
    r"|\bowe[sd]?\b.{0,24}\?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RentStatusQuery:
    """Deterministic read-only rent status NL query (V1.3 Slice 2, Entry C).

    ``kind`` is one of who_unpaid / unit / tenant; unit/tenant carry the token
    to resolve exactly against the API data (never auto-selected here)."""

    kind: str
    unit_token: str = ""
    tenant_token: str = ""


def detect_rent_status_query(text: str) -> Optional[RentStatusQuery]:
    """Deterministic detector for the three read-only rent status queries.

    Runs AFTER ``is_rent_payment_statement`` (so statements never reach it)
    and BEFORE ``route_for_text`` (so queries never fall into page routes or
    the "unknown" reply). No LLM, no writes.
    """
    raw = _normalize_correction((text or "").strip())
    if not raw:
        return None
    if any(w in raw for w in _WHO_UNPAID_CN) or _WHO_UNPAID_EN.search(raw):
        return RentStatusQuery(kind="who_unpaid")
    lowered = raw.lower()
    has_verb = any(v in raw for v in _QUERY_VERB_CN) or bool(
        _QUERY_VERB_EN.search(lowered)
    )
    if not has_verb:
        return None
    unit_m = _UNIT_TOKEN.search(raw)
    if unit_m:
        return RentStatusQuery(kind="unit", unit_token=unit_m.group(1))
    name_m = _NAME_TOKEN.search(raw)
    if name_m:
        return RentStatusQuery(kind="tenant", tenant_token=name_m.group(1))
    return None


_ROUTES = [
    (("房源", "property", "properties"), "properties"),
    (("财务", "finance", "summary", "收支", "报表", "record", "records"), "finance"),
    (("逾期", "overdue", "欠租", "overdue rent"), "overdue"),
    (("收租", "rent", "登记"), "rent"),
    (("待处理", "pending", "待办", "todo", "tasks", "待确认"), "pending"),
    (("更多", "more"), "more"),
    (("菜单", "menu", "主菜单", "home", "start"), "menu"),
    (("帮助", "help", "帮助"), "help"),
    # SLICE3-UX-PERSISTENT-MENU-001: Secretary fixed-menu entries without a
    # dedicated page handler get friendly guidance to existing capabilities
    # (tenant status NL queries / to-do center), never a silent no-op.
    (("租客", "tenant", "tenants"), "tenants"),
    (("维修", "maintenance"), "maintenance"),
]


def is_rent_payment_statement(text: str) -> bool:
    """Deterministic detector for "rent was received" statements. Queries
    ("收到租金了吗？") and menu words ("收租") are NOT statements."""
    raw = text or ""
    lowered = raw.strip().lower()
    if not lowered:
        return False
    if _QUERY_SUFFIX.search(raw):
        return False
    if _correction_values(raw) is not None:
        # "不是 1608，是 1708" / "not 1608, it's 1708": a correction to a rent
        # report is a statement, never an unknown menu word.
        return True
    has_rent_word = any(w in raw for w in _RENT_WORDS_CN) or bool(_RENT_WORD_EN.search(lowered))
    has_verb = any(v in raw for v in _RECEIVE_VERBS_CN) or bool(_RECEIVE_VERB_EN.search(lowered))
    if has_rent_word and has_verb:
        return True
    # A payment verb with a number (including an explicit zero or negative
    # amount) is a statement; the backend matcher then rejects invalid amounts
    # with a friendly message instead of booking anything.
    return has_verb and bool(
        _AMOUNT_IN_TEXT.search(lowered)
        or _AMOUNT_ZERO.search(lowered)
        or _AMOUNT_NEGATIVE.search(lowered)
    )


def _correction_values(text: str) -> Optional[tuple[str, str]]:
    """Return (negated, corrected) value pair from a correction phrase, or
    None when the text carries no usable correction. Only unit/amount-like
    tokens (>=2 chars with at least one digit) qualify, so plain month words
    or single digits never get rewritten."""
    match = _CORRECTION_RE.search(text or "")
    if not match:
        return None
    neg = next(
        (match.group(k) for k in ("neg1", "neg2", "neg3", "neg4") if match.group(k)),
        None,
    )
    pos = next(
        (match.group(k) for k in ("pos1", "pos2", "pos3", "pos4") if match.group(k)),
        None,
    )
    if not neg or not pos:
        return None
    if (
        len(neg) < 2 or len(pos) < 2
        or not any(c.isdigit() for c in neg)
        or not any(c.isdigit() for c in pos)
    ):
        return None
    return neg, pos


def _normalize_correction(text: str) -> str:
    """Rewrite a correction phrase so the negated value becomes the corrected
    one ("不是 1608，是 1708" -> "不是 1708，是 1708"). The backend matcher and
    the status-query unit token then only ever see the corrected value (1708,
    never the negated 1608). Non-correction text is returned unchanged."""
    raw = text or ""
    match = _CORRECTION_RE.search(raw)
    if not match:
        return raw
    neg, pos = _correction_values(raw) or (None, None)
    if neg is None or pos is None or neg == pos:
        return raw
    segment = match.group(0)
    fixed = re.sub(re.escape(neg), pos, segment, count=1)
    return raw[:match.start()] + fixed + raw[match.end():]


def route_for_text(text: str):
    lowered = (text or "").strip().lower()
    if lowered.startswith("/"):
        return None  # real commands are handled by CommandHandler
    for keywords, route in _ROUTES:
        for kw in keywords:
            if kw in lowered:
                return route
    return None


async def handle_nl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle a free-text message that has no active conversation state."""
    pages._bind_identity(update, context)
    user = update.effective_user
    role = role_for_telegram_id(user.id if user else None)
    locale = locale_for(role)
    chat_id = update.effective_chat.id
    text = update.effective_message.text or ""

    if is_rent_payment_statement(text):
        await _handle_rent_payment_statement(update, context, text, role, locale)
        return

    query = detect_rent_status_query(text)
    if query is not None:
        await _handle_rent_status_query(update, context, query, role, locale)
        return

    route = route_for_text(text)
    if route in (
        "properties", "finance", "overdue", "rent", "pending", "menu", "more",
        "tenants", "maintenance",
    ):
        if not has_read_permission(role):
            await context.bot.send_message(
                chat_id,
                H.escape(t("common.no_permission", locale)),
                parse_mode=HTML,
            )
            return
    if route == "properties":
        await pages.show_properties(context, chat_id, role, locale, page=1)
    elif route == "finance":
        await pages.show_finance(context, chat_id, locale)
    elif route == "overdue":
        await pages.show_overdue(context, chat_id, locale, page=1)
    elif route == "rent":
        await pages.show_rent(context, chat_id, locale)
    elif route == "pending":
        await pages.show_todo(context, chat_id, role, locale)
    elif route == "more":
        await pages.show_dashboard(
            context, chat_id, locale, role=role, fallback_inline=True
        )
    elif route == "menu":
        await pages.show_menu(context, chat_id, locale, role=role)
    elif route == "help":
        await context.bot.send_message(
            chat_id,
            f"📖 <b>{H.escape(t('help.title', locale))}</b>\n\n{H.escape(t('help.text', locale))}",
            parse_mode=HTML,
            reply_markup=pages.reply_keyboard(role),
        )
    elif route == "tenants":
        await context.bot.send_message(
            chat_id,
            H.escape(t("menu.tenants_hint", locale)),
            parse_mode=HTML,
            reply_markup=pages.reply_keyboard(role),
        )
    elif route == "maintenance":
        await context.bot.send_message(
            chat_id,
            H.escape(t("menu.maintenance_hint", locale)),
            parse_mode=HTML,
            reply_markup=pages.reply_keyboard(role),
        )
    else:
        await context.bot.send_message(
            chat_id,
            H.escape(t("common.unknown", locale)),
            parse_mode=HTML,
            reply_markup=menu_keyboard(locale),
        )


async def _handle_rent_status_query(update, context, query, role, locale):
    """V1.3 Slice 2, Entry C: read-only rent status answers.

    All branches first require ``has_read_permission`` (OWNER/SECRETARY);
    unknown users are refused with the shared no-permission copy. Only GET
    endpoints are called — there is no create/confirm/reverse/register path."""
    chat_id = update.effective_chat.id
    if not has_read_permission(role):
        await context.bot.send_message(
            chat_id,
            H.escape(t("common.no_permission", locale)),
            parse_mode=HTML,
        )
        return
    try:
        if query.kind == "who_unpaid":
            await _answer_who_unpaid(context, chat_id, locale)
        elif query.kind == "unit":
            await _answer_unit_status(
                context, chat_id, query.unit_token, locale,
                user_id=update.effective_user.id if update.effective_user else None,
            )
        else:
            await _answer_tenant_status(
                context, chat_id, query.tenant_token, locale,
                user_id=update.effective_user.id if update.effective_user else None,
            )
    except PasayApiError:
        # Never leak HTTP status / error codes / DB terms to the user.
        await context.bot.send_message(
            chat_id,
            H.escape(t("rent_status.error", locale)),
            parse_mode=HTML,
        )
        return


async def _answer_who_unpaid(context, chat_id, locale):
    """'这个月谁还没交' / 'who hasn't paid this month?' — the overdue rows
    whose current period is still outstanding (reuses /reports/overdue-rents,
    the same source as the overdue page and the finance warning)."""
    api = context.bot_data["api_client"]
    rows = await api.get_overdue_rents()
    prop_by_unit: dict[int, str] = {}
    try:
        units, properties = await asyncio.gather(
            api.get_units(), api.get_properties()
        )
        by_pid = {p.id: p.name for p in properties}
        prop_by_unit = {
            u.id: by_pid[u.property_id]
            for u in units
            if u.property_id in by_pid
        }
    except PasayApiError:
        pass  # property names are a nice-to-have, same as the overdue page
    month = pages._current_month()
    unpaid = [
        r
        for r in rows
        if any(
            str(p.get("month", "")) == month
            for p in (r.overdue_periods or [])
        )
    ]
    await context.bot.send_message(
        chat_id,
        H.truncate(cards.unpaid_list_card(unpaid, month, locale, prop_by_unit)),
        parse_mode=HTML,
    )


def _unit_number_matches(token: str, unit_number: str) -> bool:
    """Normalized unit-number match (mirrors payment_match._unit_matches).

    Trailing sentence punctuation ("1608?") must not hide the unit hint.
    Suffix matches require a non-digit boundary so "608" never answers
    "1608" / "DEV-BAY-1608", while "1608" still resolves the prefixed
    building-unit style (DEV-BAY-1608) used by the dev data.
    """
    t = (token or "").lower().strip().rstrip(".,;:!?")
    u = (unit_number or "").lower().strip()
    if not u or not t:
        return False
    if t == u:
        return True
    if u.endswith(t) and not u[len(u) - len(t) - 1].isdigit():
        return True
    if t.endswith(u) and not t[len(t) - len(u) - 1].isdigit():
        return True
    return False


def _period_paid(incomes, lease_id: int, month: str) -> Decimal:
    """Confirmed income total for one rent period (mirrors the backend's
    ``confirmed_paid_by_period`` attribution: description period first,
    received-date month as fallback). Read-only."""
    total = Decimal("0")
    for inc in incomes:
        if inc.lease_id != lease_id or inc.status != "confirmed":
            continue
        if month in (inc.description or ""):
            total += inc.amount
            continue
        if inc.received_date and inc.received_date.strftime("%Y-%m") == month:
            total += inc.amount
    return total


def _status_candidates(
    units, leases, tenants, incomes, overdue, properties, *, unit_ids=None, tenant_ids=None
):
    """Read-only rent status candidates for the matched units or tenants
    (shared by the unit and tenant NL answer paths). One entry per active
    lease; never selects or writes anything."""
    month = pages._current_month()
    candidates: list[dict] = []
    for lease in leases:
        if lease.status != "active":
            continue
        if unit_ids is not None and lease.unit_id not in unit_ids:
            continue
        if tenant_ids is not None and lease.tenant_id not in tenant_ids:
            continue
        unit = next((u for u in units if u.id == lease.unit_id), None)
        tenant = next((tn for tn in tenants if tn.id == lease.tenant_id), None)
        if unit is None or tenant is None:
            continue
        prop = next((p for p in properties if p.id == unit.property_id), None)
        due_amount = lease.monthly_rent
        paid_amount = _period_paid(incomes, lease.id, month)
        remaining = max(due_amount - paid_amount, Decimal("0"))
        paid_full = paid_amount >= due_amount
        ovd = next((r for r in overdue if r.lease_id == lease.id), None)
        candidates.append(
            {
                "tenant_name": tenant.full_name,
                "unit_number": unit.unit_number,
                "property_name": prop.name if prop else "",
                "monthly_rent": lease.monthly_rent,
                "paid": paid_full,
                "paid_amount": paid_amount,
                "due_amount": due_amount,
                "outstanding": remaining,
                "remaining": remaining,
                "overdue_days": ovd.overdue_days if ovd else 0,
                "overdue_months": ovd.overdue_months if ovd else 0,
                "month": month,
            }
        )
    return candidates


def _selector_candidate(c: dict) -> dict:
    """JSON-safe display-only candidate row for the selector store.

    Only the fields shown on a status card are kept — no lease_id / unit_id /
    tenant_id / income_id ever leaves the API layer."""
    return {
        "tenant_name": str(c.get("tenant_name") or ""),
        "unit_number": str(c.get("unit_number") or ""),
        "property_name": str(c.get("property_name") or ""),
        "monthly_rent": str(c.get("monthly_rent") or "0"),
        "paid": bool(c.get("paid")),
        "paid_amount": str(c.get("paid_amount") or "0"),
        "due_amount": str(c.get("due_amount") or "0"),
        "outstanding": str(c.get("outstanding") or "0"),
        "remaining": str(c.get("remaining") or c.get("outstanding") or "0"),
        "overdue_days": int(c.get("overdue_days") or 0),
        "overdue_months": int(c.get("overdue_months") or 0),
        "month": str(c.get("month") or ""),
    }


async def _send_status_answer(context, chat_id, candidates, locale, user_id=None):
    """One match -> the single status card; several -> read-only selector
    buttons (one inline button per candidate, resolved by the tap handler;
    never auto-selects, never writes)."""
    if len(candidates) == 1:
        c = _selector_candidate(candidates[0])
        await context.bot.send_message(
            chat_id,
            H.truncate(cards.rent_status_card_for_candidate(c, locale)),
            parse_mode=HTML,
        )
        return
    payload = [_selector_candidate(c) for c in candidates]
    nonce = new_nonce()
    ts = now_ts()
    if user_id is not None:
        context.bot_data["store"].save_rent_status_selector(
            nonce, chat_id, user_id, payload
        )
    await context.bot.send_message(
        chat_id,
        H.truncate(cards.rent_status_selector_card(candidates, locale)),
        parse_mode=HTML,
        reply_markup=rent_status_candidates_keyboard(
            candidates, locale, nonce=nonce, ts=ts
        ),
    )


async def _answer_unit_status(context, chat_id, unit_token, locale, user_id=None):
    """'1608 交了没有 / 还欠多少' — normalized unit-number match (punctuation
    tolerant, "1608" resolves "DEV-BAY-1608"), then a paid/unpaid + owed +
    overdue answer from the existing read endpoints. Multiple unit hits
    render the read-only candidates card instead of guessing."""
    api = context.bot_data["api_client"]
    units, leases, tenants, incomes, overdue, properties = await asyncio.gather(
        api.get_units(),
        api.get_leases(),
        api.get_tenants(),
        api.list_incomes(),
        api.get_overdue_rents(),
        api.get_properties(),
    )
    matched = [u for u in units if _unit_number_matches(unit_token, u.unit_number)]
    if not matched:
        await context.bot.send_message(
            chat_id,
            H.escape(t("rent_status.no_unit", locale, unit=unit_token)),
            parse_mode=HTML,
        )
        return
    candidates = _status_candidates(
        units, leases, tenants, incomes, overdue, properties,
        unit_ids={u.id for u in matched},
    )
    if not candidates:
        await context.bot.send_message(
            chat_id,
            H.escape(t("rent_status.no_active_lease", locale)),
            parse_mode=HTML,
        )
        return
    await _send_status_answer(context, chat_id, candidates, locale, user_id=user_id)


async def _answer_tenant_status(context, chat_id, tenant_token, locale, user_id=None):
    """'John 交了吗 / did John pay?' — match tenant full/first name on active
    leases; multiple hits render a read-only candidate list (no auto-select)."""
    api = context.bot_data["api_client"]
    units, leases, tenants, incomes, overdue, properties = await asyncio.gather(
        api.get_units(),
        api.get_leases(),
        api.get_tenants(),
        api.list_incomes(),
        api.get_overdue_rents(),
        api.get_properties(),
    )
    token = tenant_token.lower()
    matched = [
        tn
        for tn in tenants
        if re.search(rf"\b{re.escape(token)}\b", tn.full_name.lower())
    ]
    if not matched:
        await context.bot.send_message(
            chat_id,
            H.escape(t("rent_status.no_tenant", locale, name=tenant_token)),
            parse_mode=HTML,
        )
        return
    candidates = _status_candidates(
        units, leases, tenants, incomes, overdue, properties,
        tenant_ids={tn.id for tn in matched},
    )
    if not candidates:
        await context.bot.send_message(
            chat_id,
            H.escape(t("rent_status.no_active_lease_tenant", locale)),
            parse_mode=HTML,
        )
        return
    await _send_status_answer(context, chat_id, candidates, locale, user_id=user_id)


async def _handle_rent_payment_statement(update, context, text, role, locale):
    """Entry B: NL statement -> backend match -> action-at-source card."""
    if not has_read_permission(role):
        await context.bot.send_message(
            update.effective_chat.id,
            H.escape(t("common.no_permission", locale)),
            parse_mode=HTML,
        )
        return
    api = context.bot_data["api_client"]
    chat_id = update.effective_chat.id
    try:
        result = await api.match_rent_payment(_normalize_correction(text))
    except PasayApiError:
        await context.bot.send_message(
            chat_id, H.escape(t("rent.match_error", locale)), parse_mode=HTML
        )
        return
    best = result.best
    if best is None:
        await context.bot.send_message(
            chat_id, H.escape(t("rent.match_none", locale)), parse_mode=HTML
        )
        return
    if best.kind == "duplicate":
        # Already booked: friendly message, zero writes, no second record.
        if role == Role.SECRETARY:
            await context.bot.send_message(
                chat_id,
                cards.secretary_already_confirmed_card(best, locale),
                parse_mode=HTML,
            )
            return
        await context.bot.send_message(
            chat_id, cards.rent_already_booked_card(best, locale), parse_mode=HTML
        )
        return
    if best.kind == "overpayment":
        # Cumulative payments would exceed this period's receivable: explain
        # and stop. Prepayment / next-month offset is a future slice.
        await context.bot.send_message(
            chat_id, cards.rent_overpayment_card(best, locale), parse_mode=HTML
        )
        return
    if best.kind == "invalid_amount":
        await context.bot.send_message(
            chat_id, cards.rent_invalid_amount_card(locale), parse_mode=HTML
        )
        return
    if best.kind == "pending":
        if role == Role.SECRETARY:
            # Same payment already reported and waiting for the Owner: never
            # create a second pending income (duplicate rule A).
            await context.bot.send_message(
                chat_id,
                cards.secretary_already_waiting_card(best, locale),
                parse_mode=HTML,
            )
            return
        await _render_pending_match(update, context, best, result, role, locale)
        return
    if best.confidence != "high":
        await _send_ambiguous(update, context, result, locale)
        return
    if role == Role.SECRETARY:
        # Unique HIGH-confidence exact match: register ONE pending income and
        # hand the confirmation card to the Owner (action-at-source).
        await _register_pending_for_owner(update, context, best, result, role, locale)
        return
    await _render_match_confirm(update, context, best, result, role, locale)


async def _register_pending_for_owner(update, context, candidate, result, role, locale):
    """Secretary one-line register (V1.3 Slice 2, Entry B).

    Reuses the existing Income create + idempotency_key chain (backend is the
    atomic backstop; ``created_by``/audit actor stays the Secretary subject)
    and the Owner-only confirm chain for the follow-up card. Never asks the
    Secretary for month / property / amount / date the matcher already knows.
    """
    store = context.bot_data["store"]
    guard = context.bot_data["idempotency"]
    api = context.bot_data["api_client"]
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if not has_permission(role, PERMISSION_RENT_ENTRY):
        await context.bot.send_message(
            chat_id, H.escape(t("common.no_permission", locale)), parse_mode=HTML
        )
        return

    period = candidate.period or str(result.received_date)[:7]
    received = result.received_date.isoformat()
    method = store.get_user_default_method(user_id)
    remaining_before = max(
        Decimal(candidate.due_amount) - Decimal(candidate.paid_amount),
        Decimal("0"),
    )
    # Deterministic business-fact key: identical duplicate reports (and
    # Telegram redeliveries) share one backend idempotency_key, so a race can
    # never produce a second pending income. The remaining balance before the
    # payment is part of the key so two genuine partials of the same amount
    # (e.g. 30k when 60k then 30k remain) still get separate records, while a
    # replay against the same balance reuses the first record.
    key = (
        f"ik:sec:{candidate.lease_id}:{period}:{received}:"
        f"{candidate.amount}:{remaining_before}"
    )
    status = guard.acquire(key, kind="income", resource="")
    if status in ("done", "in_flight"):
        # Replay / concurrent duplicate: no second pending row, and the Owner
        # has already been notified once.
        await context.bot.send_message(
            chat_id,
            cards.secretary_already_waiting_card(candidate, locale),
            parse_mode=HTML,
        )
        return

    income = None
    try:
        income = await api.create_income(
            lease_id=candidate.lease_id,
            amount=candidate.amount,
            received_date=received,
            payment_method=method,
            description=f"rent {period}",
            idempotency_key=key,
        )
        guard.settle(key, income.as_dict(), resource=str(income.id))
    except PasayApiConflictError:
        # A concurrent identical create landed server-side under the same key.
        await context.bot.send_message(
            chat_id,
            cards.secretary_already_waiting_card(candidate, locale),
            parse_mode=HTML,
        )
        return
    except PasayApiTimeoutError:
        # The create may have landed: reconcile before telling anyone to retry,
        # so a committed pending row is never duplicated.
        current = await api.find_income(
            lease_id=candidate.lease_id,
            amount=candidate.amount,
            received_date=received,
            payment_method=method,
            idempotency_key=key,
        )
        if current is not None:
            income = current
            guard.settle(key, income.as_dict(), resource=str(income.id))
        else:
            guard.fail(key)
            await context.bot.send_message(
                chat_id, H.escape(t("rent.match_error", locale)), parse_mode=HTML
            )
            return
    except PasayApiPermissionError:
        guard.fail(key)
        await context.bot.send_message(
            chat_id, H.escape(t("common.no_permission", locale)), parse_mode=HTML
        )
        return
    except PasayApiError:
        guard.fail(key)
        await context.bot.send_message(
            chat_id, H.escape(t("rent.match_error", locale)), parse_mode=HTML
        )
        return

    # English confirmation to the Secretary (no re-entry, no form).
    await context.bot.send_message(
        chat_id,
        H.truncate(cards.secretary_matched_reply(candidate, locale)),
        parse_mode=HTML,
    )

    # Chinese action-at-source card to the Owner's private chat.
    owner_chat_id = telegram_id_for_role(Role.OWNER)
    if owner_chat_id is None:
        logger.warning(
            "No Owner telegram id configured; pending income %s registered "
            "without an Owner confirmation card", income.id,
        )
        return
    nonce = new_nonce()
    ts = now_ts()
    payload = {
        "income_id": income.id,
        "lease_id": candidate.lease_id,
        "property_id": candidate.property_id,
        "property_name": candidate.property_name,
        "unit_number": candidate.unit_number,
        "period": period,
        "amount": str(income.amount),
        "received_date": received,
        "flow": "secretary_register",
        "registrar": "Secretary",
        "due_amount": str(candidate.due_amount),
        "paid_amount": str(candidate.paid_amount),
        "remaining_balance": str(candidate.remaining_balance),
    }
    store.save_conversation(
        owner_chat_id, owner_chat_id, "rent_secretary_confirm",
        payload, nonce=nonce,
    )
    await context.bot.send_message(
        owner_chat_id,
        H.truncate(cards.secretary_registered_card(candidate, "zh")),
        parse_mode=HTML,
        reply_markup=secretary_registered_keyboard(income.id, nonce, ts, "zh"),
    )


async def _render_match_confirm(update, context, candidate, result, role, locale):
    store = context.bot_data["store"]
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    payload = {
        "unit_id": candidate.unit_id,
        "lease_id": candidate.lease_id,
        "property_id": candidate.property_id,
        "property_name": candidate.property_name,
        "unit_number": candidate.unit_number,
        "monthly_rent": str(candidate.amount),
        "amount": str(candidate.amount),
        "received_date": result.received_date.isoformat(),
        "period": candidate.period,
        "method": store.get_user_default_method(user_id),
        "flow": "nl",
        "open_count": candidate.open_count,
        "due_amount": str(candidate.due_amount),
        "paid_amount": str(candidate.paid_amount),
        "remaining_balance": str(candidate.remaining_balance),
    }
    can_confirm = has_permission(role, PERMISSION_RENT_CONFIRM)
    nonce, ts = "", 0
    if can_confirm:
        nonce = new_nonce()
        ts = now_ts()
        store.save_conversation(chat_id, user_id, "rent_confirm", payload, nonce=nonce)
    text = cards.rent_match_card(
        candidate, result.received_date.isoformat(), locale, can_confirm=can_confirm
    )
    await context.bot.send_message(
        chat_id,
        H.truncate(text),
        parse_mode=HTML,
        reply_markup=rent_match_keyboard(nonce, ts, can_confirm, locale),
    )


async def _render_pending_match(update, context, candidate, result, role, locale):
    """A matching pending income already exists: confirm THAT record instead of
    ever creating a second one."""
    chat_id = update.effective_chat.id
    can_confirm = has_permission(role, PERMISSION_RENT_CONFIRM)
    text = cards.rent_match_pending_card(
        candidate, result.received_date.isoformat(), locale
    )
    keyboard = None
    if can_confirm:
        keyboard = confirm_income_keyboard(
            candidate.income_id or 0, new_nonce(), now_ts(),
            can_reverse=False, locale=locale,
        )
    await context.bot.send_message(
        chat_id, H.truncate(text), parse_mode=HTML, reply_markup=keyboard
    )


async def _send_ambiguous(update, context, result, locale):
    """Multiple plausible bills: a short human list, no menu detour. The
    selection/partial/overpayment cards are later slices."""
    chat_id = update.effective_chat.id
    lines = [H.escape(t("rent.match_ambiguous", locale))]
    for cand in result.candidates[:5]:
        lines.append(
            f"• {H.escape(cand.property_name)} {H.escape(cand.unit_number)}"
            f" · {cards.period_label(cand.period, locale)} · {H.money(cand.amount)}"
        )
    await context.bot.send_message(chat_id, "\n".join(lines), parse_mode=HTML)
