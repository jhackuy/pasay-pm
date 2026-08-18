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
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
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
from pasay_bot.handlers.edit_utils import edit_message_text_idempotent
from pasay_bot.keyboards import (
    ai_choice_keyboard,
    confirm_income_keyboard,
    new_nonce,
    now_ts,
    rent_history_candidates_keyboard,
    rent_match_keyboard,
    rent_status_candidates_keyboard,
    repair_completion_candidates_keyboard,
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
    locale_for_chat,
    role_for_telegram_id,
    telegram_id_for_role,
)

HTML = "HTML"
logger = logging.getLogger(__name__)

# BOT-V1-USABLE-001: expense statements run BEFORE the rent statement parser
# ("付了1680电费3800" is an expense, never a rent payment) and the AI fallback
# is the last lane for any text the deterministic parsers cannot classify.
from pasay_bot.handlers.expense_flow import (
    MAX_AMOUNT,
    detect_expense_ambiguity,
    detect_expense_statement,
    handle_expense_statement,
    parse_expense_statement,
    render_expense_confirm,
    resolve_unit,
)
from pasay_bot.handlers import nl_queries

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

# --- PASAY-AI-EMPLOYEE-FOUNDATION-007 §8/§9: tenant phone low-risk direct
# write ("1680 租客电话 09171234567") + self-healing resume. ---
_TENANT_PHONE_KW = re.compile(
    r"(?:租客电话|租客联系电话|租客号码|租客手机|手机号|租客telegram|"
    r"tenant phone|tenant number|tenant\'?s phone|phone of tenant)\s*[:：]?\s*"
    r"(?P<phone>\+?\d[\d\s\-()]{6,19})",
    re.IGNORECASE,
)

# --- PASAY-V2-FOUNDATION-001: conversation -> Task (repair/maintenance) -----
# "1680 aircon leaking" / "aircon leaking again" / "ac not cold" create an
# AC_MAINTENANCE task; follow-ups ("technician coming tomorrow", "coming
# tomorrow") attach to the short-term context; "finished"/"done" advance it.
_MAINTENANCE_CN = (
    "\u7a7a\u8c03", "\u51b7\u6c14", "\u7ef4\u4fee", "\u5b89\u88c5",
    "\u6f0f\u6c34", "\u7535\u8def", "\u95e8\u9501", "\u7535\u6247", "\u9a6c\u6876",
)
_MAINTENANCE_EN = re.compile(
    r"\b(aircon|ac\b|air\s*conditioner|leak|plumb|electric|repair|fix|broken|not\s+cold|maintenance)\b",
    re.IGNORECASE,
)
_PROGRESS_CN = (
    "\u660e\u5929", "\u4e0b\u5468", "\u660e\u5929\u6765", "\u5df2\u7ecf\u7ea6\u4e86",
    "\u9884\u7ea6", "\u6392\u5728", "\u4eca\u5929\u6765", "\u5230\u4e86",
    "\u6b63\u5728\u5904\u7406", "\u5904\u7406\u4e2d",
)
_PROGRESS_EN = re.compile(
    r"\b(tomorrow|today|next\s+week|scheduled|scheduling|coming|on\s+the\s+way|arrived|arriving|started|in\s+progress|fixing|working\s+on)\b",
    re.IGNORECASE,
)
_COMPLETE_CN = (
    "\u5b8c\u6210", "\u641e\u5b9a", "\u4fee\u597d", "\u4fee\u5b8c",
    "\u7ed3\u675f", "\u505a\u5b8c",
)
_COMPLETE_EN = re.compile(r"\b(finished|done|completed|fixed|working now|all good|resolved)\b", re.IGNORECASE)
_CORRECTION_TASK_CN = re.compile(
    r"\u4e0d\u662f\s*([0-9A-Za-z-]+)\s*[\uff0c,]\s*\u662f\s*([0-9A-Za-z-]+)"
)
_CORRECTION_TASK_EN = re.compile(r"\bnot\s+([0-9A-Za-z-]+)\s*,?\s+(?:it'?s|is)\s+([0-9A-Za-z-]+)", re.IGNORECASE)
_GREETING_RE = re.compile(r"^\s*(hi|hello|hey|yo|你好|您好|嗨)\b[!.\s]*$", re.IGNORECASE)

# --- AI-OPS-FOUNDATION-001 §14/§17: Telegram-first Unit CRUD + viewings -----
# "Add unit 1609, rent 35000, vacant" / "新增 1609 单元 租金 35000 空置"
_ADD_UNIT_RE = re.compile(
    r"(?:\badd\s+unit\b|\bnew\s+unit\b|\u65b0\u589e|\u6dfb\u52a0|\u65b0\u52a0)\s*"
    r"[:\uff1a]?\s*"
    r"(?P<unit>[A-Za-z0-9][A-Za-z0-9-]*)\s*"
    r"(?:\u5355\u5143|\bunit\b)?\s*"
    r"(?:,|\uff0c)?\s*"
    r"(?:\u79df\u91d1|\u6708\u79df|\brent\b)?\s*"
    r"(?P<amount>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?:\u5143|\bphp\b|\bpesos?\b)?\s*"
    r"(?:,|\uff0c)?\s*"
    r"(?P<status>\u7a7a\u7f6e|\u5df2\u79df|\u5360\u7528|vacant|occupied)?",
    re.IGNORECASE,
)
_VIEWING_KW_CN = ("\u770b\u623f", "\u770b\u623f\u8005", "\u770b\u623f\u9884\u7ea6")
_VIEWING_KW_EN = re.compile(r"\bview(?:ing)?\b", re.IGNORECASE)
_TIME_CN_RE = re.compile(r"(?:\u660e\u5929|\u4eca\u5929)?\s*(?:\u4e0b\u5348|\u4e0a\u5348|\u665a\u4e0a)?\s*(\d{1,2})\s*(?:\u70b9|\uff1a)?(\d{0,2})\s*(?:pm|am)?")
_TIME_EN_RE = re.compile(r"(?P<day>tomorrow|today)?\s*(?:at\s*)?(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ampm>am|pm)", re.IGNORECASE)


def detect_unit_add_statement(text: str) -> Optional[dict]:
    """AI-OPS-FOUNDATION-001 §14: 'Add unit 1609, rent 35000, vacant' ->
    ``{"unit_number", "monthly_rent", "status"}`` for a confirmation card.
    Financial/expense/rent lanes keep priority (checked earlier)."""
    raw = (text or "").strip()
    if not raw:
        return None
    if detect_expense_statement(raw) or is_rent_payment_statement(raw):
        return None
    m = _ADD_UNIT_RE.search(raw)
    if not m or not m.group("amount"):
        return None
    status_raw = (m.group("status") or "").strip().lower()
    status = {
        "\u7a7a\u7f6e": "vacant", "vacant": "vacant",
        "\u5df2\u79df": "occupied", "\u5360\u7528": "occupied", "occupied": "occupied",
    }.get(status_raw)
    if status is None and status_raw:
        return None  # unknown status -> not confident, let NL fallback handle
    return {
        "unit_number": m.group("unit").upper(),
        "monthly_rent": m.group("amount").replace(",", ""),
        "status": status or "vacant",
    }


def detect_viewing_statement(text: str) -> Optional[dict]:
    """AI-OPS-FOUNDATION-001 §17: 'Someone will view 1608 tomorrow at 2pm' ->
    ``{"unit_token", "scheduled_at"}`` (ISO, local day/time). The unit token
    is the unit-like token NEAREST the viewing keyword (the sentence subject
    like "Someone" never wins)."""
    raw = (text or "").strip()
    if not raw:
        return None
    if detect_expense_statement(raw) or is_rent_payment_statement(raw):
        return None
    lowered = raw.lower()
    kw_positions = []
    for kw in _VIEWING_KW_CN:
        pos = raw.find(kw)
        if pos >= 0:
            kw_positions.append(pos)
    m = _VIEWING_KW_EN.search(lowered)
    if m:
        kw_positions.append(m.start())
    if not kw_positions:
        return None
    kw_pos = min(kw_positions)
    tokens = list(_UNIT_TOKEN.finditer(raw))
    if not tokens:
        return None
    unit_match = min(tokens, key=lambda tm: abs(tm.start() - kw_pos))
    unit_token = unit_match.group(1)
    when = _parse_viewing_time(raw)
    if when is None:
        return None
    return {"unit_token": unit_token, "scheduled_at": when.isoformat()}


def _parse_viewing_time(text: str) -> Optional[datetime]:
    """Deterministic local-time parse for viewing statements."""
    from datetime import datetime as _dt, time as _time, timedelta as _td

    lowered = (text or "").lower()
    base = _dt.now()
    day_offset = 0
    if "tomorrow" in lowered or "\u660e\u5929" in text:
        day_offset = 1
    hour = minute = None
    ampm = None
    m = _TIME_EN_RE.search(lowered)
    if m and m.group("h"):
        hour = int(m.group("h"))
        minute = int(m.group("m") or 0)
        ampm = (m.group("ampm") or "").lower()
    else:
        mc = _TIME_CN_RE.search(text)
        if mc and mc.group(1):
            hour = int(mc.group(1))
            minute = int(mc.group(2) or 0)
            if "\u4e0b\u5348" in text and hour < 12:
                hour += 12
            if "\u665a\u4e0a" in text and hour < 12:
                hour += 12
    if hour is None:
        return None
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    target = (base + _td(days=day_offset)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    return target
# PASAY-V2-FOUNDATION-001: Owner payment confirmations. These are Payment
# Events for an APPROVED expense, NEVER a new expense (no amount/category
# question, no receipt requirement, no duplicate record).
_PAYMENT_EVENT_CN = (
    "\u5df2\u7ecf\u4ed8\u6b3e", "\u5df2\u4ed8", "\u73b0\u91d1\u4ed8\u4e86",
    "\u4ed8\u4e86\u73b0\u91d1", "\u4ed8\u6b3e\u5b8c\u6210", "\u652f\u4ed8\u5b8c\u6210",
    "\u7ed3\u6e05\u4e86", "\u5df2\u7ed3\u7b97",
)
_PAYMENT_EVENT_EN = re.compile(
    r"\b(already\s+paid|paid\s+already|payment\s+(done|complete[d]?|made)|"
    r"paid\s+in\s+cash|cash\s+paid|paid\s+cash|payment\s+settled)\b",
    re.IGNORECASE,
)
_QUOTE_CN = ("\u62a5\u4ef7", "\u8d39\u7528", "\u8981\u4ef7", "\u4fee\u7406\u8d39", "\u9700\u8981\u591a\u5c11")
_QUOTE_EN = re.compile(r"\b(quote[d]?|costs?\b|will\s+cost|charges?|price)\b", re.IGNORECASE)
_AMOUNT = re.compile(r"(?<![\d.])(\d[\d,]*(?:\.\d+)?)(?![\d])")


def detect_repair_statement(text: str) -> Optional[str]:
    """Return the unit token when the text creates/mentions a repair event.

    Read-style queries ("Show 16B repair photos", "查看 1608 维修照片") are
    NOT repair reports — they are evidence/digital-file queries and must fall
    through to the read lanes (AI-OPS-FOUNDATION-001 §14)."""
    raw = text or ""
    if not raw.strip():
        return None
    lowered = raw.lower()
    if detect_expense_statement(raw) or is_rent_payment_statement(raw):
        return None  # financial lanes keep their existing priority
    if any(w in lowered for w in (
        "show", "view", "photos", "photo", "receipt", "picture", "history",
        "查看", "照片", "凭证", "图片", "证据", "相片", "历史",
    )):
        return None  # read/evidence query, not a new repair report
    has_issue = any(w in raw for w in _MAINTENANCE_CN) or bool(
        _MAINTENANCE_EN.search(lowered)
    )
    if not has_issue:
        return None
    match = _UNIT_TOKEN.search(raw)
    return match.group(1) if match else None


def detect_task_progress(text: str) -> bool:
    raw = text or ""
    lowered = raw.lower()
    return any(w in raw for w in _PROGRESS_CN) or bool(_PROGRESS_EN.search(lowered))


def detect_task_completion(text: str) -> bool:
    raw = text or ""
    lowered = raw.lower()
    return any(w in raw for w in _COMPLETE_CN) or bool(_COMPLETE_EN.search(lowered))


def detect_payment_event(text: str) -> bool:
    """Owner payment confirmation phrase (Payment Event, not a new expense)."""
    raw = text or ""
    lowered = raw.lower()
    return any(w in raw for w in _PAYMENT_EVENT_CN) or bool(
        _PAYMENT_EVENT_EN.search(lowered)
    )


def detect_repair_quote(text: str) -> Optional[Decimal]:
    """Extract a repair quote amount when the message is a quote (not a new
    repair report and not a payment confirmation)."""
    raw = text or ""
    lowered = raw.lower()
    is_quote = any(w in raw for w in _QUOTE_CN) or bool(_QUOTE_EN.search(lowered))
    if not is_quote:
        return None
    for match in _AMOUNT.finditer(raw):
        try:
            value = Decimal(match.group(1).replace(",", ""))
        except InvalidOperation:
            continue
        if 100 <= value <= MAX_AMOUNT:
            return value
    return None


def detect_task_correction(text: str) -> Optional[tuple[str, str]]:
    """(old_unit, corrected_unit) from "涓嶆槸1680锛屾槸1805" / "not 1680, it's 1805"."""
    m = _CORRECTION_TASK_CN.search(text or "") or _CORRECTION_TASK_EN.search(text or "")
    if not m:
        return None
    old, new = m.group(1), m.group(2)
    if not old or not new or old == new:
        return None
    return old, new
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

    ``kind`` is one of who_unpaid / unit / tenant / payment_history;
    unit/tenant carry the token to resolve exactly against the API data (never
    auto-selected here). ``month_window`` is "this_month" for payment-history
    questions scoped to the current rent period, else "" (all time)."""

    kind: str
    unit_token: str = ""
    tenant_token: str = ""
    month_window: str = ""


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


# P1-PASAY-NIGHTLY-PRODUCT-HARDENING-008 C: deterministic payment-history
# questions (0 LLM). Distinct from the status verbs in _QUERY_VERB_CN, so
# "累计交了多少" (cumulative history) wins over the plain "交了多少" (current
# month status) because the history detector runs first.
_HISTORY_CN = (
    "交了几次", "交过几次", "付了几次", "付过几次", "交了多少次",
    "累计交了多少", "累计付了多少", "累计交了", "累计付了",
    "一共交了", "一共付了", "总共交了", "总共付了",
    "最近什么时候交", "最近一次交", "最后一次交", "上次什么时候交",
    "什么时候交的租", "什么时候交的", "最近交租", "上次交租",
    "交租记录", "付款记录", "交租历史", "交租情况",
)
_HISTORY_EN = re.compile(
    r"\bhow\s+many\s+times\b.{0,24}\b(?:paid|pay)\b"
    r"|\b(?:paid|pay)\b.{0,24}\bhow\s+many\s+times\b"
    r"|\btotal\s+paid\b|\bpaid\s+in\s+total\b|\bcumulative\b"
    r"|\blast\s+(?:time\s+)?paid\b|\bwhen\s+did\b.{0,24}\blast\s+pay\b"
    r"|\bpayment\s+history\b|\brent\s+history\b",
    re.IGNORECASE,
)
_MONTH_WINDOW_CN = ("这个月", "本月", "这个月交", "这个月付")
_MONTH_WINDOW_EN = re.compile(r"\bthis\s+month\b", re.IGNORECASE)


def detect_rent_payment_history_query(text: str) -> Optional[RentStatusQuery]:
    """Deterministic detector for factual payment-history questions:
    "1608 交了几次 / 累计交了多少 / 最近什么时候交的", "Paolo 最近什么时候交租",
    "这个月 1608 交了几次". Runs BEFORE ``detect_rent_status_query`` so the
    cumulative/count/latest questions never fall into the AI lane. Requires a
    unit or tenant token (no entity -> not answerable here). No LLM, no writes.
    """
    raw = _normalize_correction((text or "").strip())
    if not raw:
        return None
    has_cn = any(w in raw for w in _HISTORY_CN)
    has_en = bool(_HISTORY_EN.search(raw.lower()))
    if not (has_cn or has_en):
        return None
    window = (
        "this_month"
        if any(w in raw for w in _MONTH_WINDOW_CN) or _MONTH_WINDOW_EN.search(raw)
        else ""
    )
    unit_m = _UNIT_TOKEN.search(raw)
    name_m = _NAME_TOKEN.search(raw)
    if unit_m is not None and not (name_m is not None and name_m.start() < unit_m.start()):
        return RentStatusQuery(
            kind="payment_history", unit_token=unit_m.group(1), month_window=window,
        )
    if name_m is not None:
        return RentStatusQuery(
            kind="payment_history", tenant_token=name_m.group(1), month_window=window,
        )
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

def _record_timeline_event(context, kind: str, label: str, started: float) -> None:
    tracker = context.bot_data.get("latency")
    if tracker is None:
        return
    try:
        tracker.record(kind, label, (time.monotonic() - started) * 1000)
    except Exception:  # noqa: BLE001 - instrumentation never blocks UX
        pass


def _normalize_phone_candidate(value: str) -> Optional[str]:
    phone = re.sub(r"[\s\-()]", "", str(value or ""))
    digit_count = sum(1 for c in phone if c.isdigit())
    if digit_count < 7 or digit_count > 15:
        return None
    return phone


def _pending_missing_phone_context(context, chat_id, user_id) -> Optional[dict]:
    store = context.bot_data.get("store")
    if store is None:
        return None
    ctx = store.get_v2_context(chat_id, user_id)
    if not ctx:
        return None
    payload = dict(ctx.get("payload") or {})
    pending = payload.get("missing_phone_followup")
    return dict(pending) if isinstance(pending, dict) else None


def detect_tenant_phone_update(text: str) -> Optional[dict]:
    """PASAY-AI-EMPLOYEE-FOUNDATION-007 §9: low-risk direct phone write
    ("1680 租客电话 09171234567" / "1608 tenant phone 0917 123 4567").

    Returns ``{"unit_token", "phone"}``; the caller resolves the unit -> lease
    and supplies it via the self-healing resume path so any blocked rent task
    auto-resumes (no re-click). Runs BEFORE the rent statement/routing lanes so
    a phone-fix message is never treated as a menu word or a rent statement.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    m = _TENANT_PHONE_KW.search(raw)
    if not m or not m.group("phone"):
        return None
    unit_m = _UNIT_TOKEN.search(raw)
    unit_token = unit_m.group(1) if unit_m else None
    phone = _normalize_phone_candidate(m.group("phone"))
    if not phone:
        return None
    return {"unit_token": unit_token, "phone": phone}


def detect_tenant_phone_update_fast_path(
    text: str,
    *,
    pending_unit_token: str | None = None,
) -> Optional[dict]:
    parsed = detect_tenant_phone_update(text)
    if parsed is not None:
        if parsed.get("unit_token") or not pending_unit_token:
            return parsed
        parsed["unit_token"] = pending_unit_token
        return parsed
    if not pending_unit_token:
        return None
    phone = _normalize_phone_candidate((text or "").strip())
    if not phone:
        return None
    return {"unit_token": pending_unit_token, "phone": phone}


def is_rent_payment_statement(text: str) -> bool:
    """Deterministic detector for "rent was received" statements. Queries
    ("收到租金了吗？") and menu words ("收租") are NOT statements."""
    raw = text or ""
    lowered = raw.strip().lower()
    if not lowered:
        return False
    if detect_expense_statement(raw):
        # Expense phrases containing payment verbs ("付了1680电费3800") are
        # expenses; they must never fall into the rent matcher.
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
    locale = locale_for_chat(
        update.effective_chat.type if update.effective_chat else None, role
    )
    chat_id = update.effective_chat.id
    text = update.effective_message.text or ""
    pending_phone = _pending_missing_phone_context(
        context,
        update.effective_chat.id if update.effective_chat else None,
        user.id if user else None,
    )

    # PASAY-V2-FOUNDATION-001: plain "hello"/"hi"/"你好" is a SHORT greeting
    # (never the old full Portfolio Summary / dashboard).
    if _GREETING_RE.match(text.strip()):
        if not has_read_permission(role):
            await context.bot.send_message(
                chat_id,
                H.escape(t("common.no_permission", locale)),
                parse_mode=HTML,
            )
            return
        await _v2_reply(
            update, context,
            cards.greeting_card(locale, reminder_count=0),
            role, locale,
        )
        return

    # PASAY-V2-FOUNDATION-001: correction to a live task context first
    # ("不是1680，是1805" fixes the association without re-asking).
    correction = detect_task_correction(text)
    if correction is not None:
        old_unit, new_unit = correction
        fixed = await _handle_task_correction(
            update, context, old_unit, new_unit, role, locale,
        )
        if fixed:
            return

    # PASAY-AI-EMPLOYEE-FOUNDATION-007 §9: low-risk tenant phone direct write +
    # self-healing resume. A "1680 租客电话 09XXXXXXXX" message supplies missing
    # phone data (and any blocked rent task auto-resumes). Runs BEFORE the
    # expense/rent statement lanes so the phone-fix is never misrouted.
    phone_fix = detect_tenant_phone_update_fast_path(
        text,
        pending_unit_token=(pending_phone or {}).get("unit_code"),
    )
    if phone_fix is not None and phone_fix.get("phone"):
        handled = await _handle_tenant_phone_update(update, context, phone_fix, role, locale)
        if handled:
            return

    # BOT-V1-USABLE-001 P0-2: expense statements first (they share payment
    # verbs with rent statements but carry a category signal).
    expense_stmt = parse_expense_statement(text)
    if expense_stmt is not None:
        await handle_expense_statement(
            update, context, expense_stmt, role, locale,
        )
        return

    # P0-5 spec: "1680水费" (unit + category only) is genuinely ambiguous ->
    # offer [记录水费] [查询1680记录] deterministically (no LLM).
    ambiguity = detect_expense_ambiguity(text)
    if ambiguity is not None:
        await _handle_expense_ambiguity(
            update, context, ambiguity, role, locale,
        )
        return

    if is_rent_payment_statement(text):
        await _handle_rent_payment_statement(update, context, text, role, locale)
        return

    # P1-...-008 C: payment-history questions ("1608 交了几次 / 累计交了多少 /
    # 最近什么时候交的") are deterministic read-only answers — they must beat
    # the plain rent-status lane ("交了多少" stays status) and the AI fallback.
    history = detect_rent_payment_history_query(text)
    if history is not None:
        await _handle_rent_payment_history(update, context, history, role, locale)
        return

    query = detect_rent_status_query(text)
    if query is not None:
        await _handle_rent_status_query(update, context, query, role, locale)
        return

    # PASAY-V2-FOUNDATION-001: conversation -> Task. Active Business Context
    # must beat the generic read-only query fallback (acceptance priority:
    # Active Business Context > Task State Transition > New Business Intent >
    # Generic AI / Read-only Query Fallback). Financial lanes above keep
    # priority (an expense statement is never a repair report).
    task_handled = await _handle_v2_task_event(update, context, text, role, locale)
    if task_handled:
        return

    # AI-OPS-FOUNDATION-001 §14: Telegram-first Unit CRUD fast path.
    unit_draft = detect_unit_add_statement(text)
    if unit_draft is not None:
        await _render_unit_add_confirm(update, context, unit_draft, role, locale)
        return

    # AI-OPS-FOUNDATION-001 §17: a message that describes a viewing is
    # persisted as a business event (never chat-only context).
    viewing_draft = detect_viewing_statement(text)
    if viewing_draft is not None:
        await _render_viewing_confirm(update, context, viewing_draft, role, locale)
        return

    # BOT-V1-USABLE-001 P0-3: income/expense summaries, unit info and
    # contract-expiry queries answer directly from existing read endpoints.
    general_query = nl_queries.detect_query(text)
    if general_query is not None:
        if not has_read_permission(role):
            await context.bot.send_message(
                chat_id,
                H.escape(t("common.no_permission", locale)),
                parse_mode=HTML,
            )
            return
        await nl_queries.handle_query(
            context, chat_id, general_query, locale,
        )
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
        # CONVERGENCE-003 §2.1: "更多" (legacy alias) lands on the ONE Home
        # (Operations Overview) — the legacy dashboard is never a route.
        await pages.show_home(context, chat_id, role, locale)
    elif route == "menu":
        # PASAY-V2-FOUNDATION-001: "menu"/"home"/"start" words open the short
        # greeting (never the old full dashboard).
        await pages.show_greeting(context, chat_id, role, locale)
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
        # BOT-V1-USABLE-001 P0-5: every unmatched ordinary text enters the AI
        # intent lane — never a /help fallback, never silence.
        await _handle_ai_fallback(update, context, text, role, locale)


async def _handle_tenant_phone_update(update, context, fix: dict, role, locale: str) -> bool:
    """§9 low-risk tenant phone direct write + §2 self-healing auto-resume.

    Resolves the entity from the unit token, supplies the phone via the
    ``/operations/resume`` endpoint (which saves it and returns the blocked
    action), then AUTO-EXECUTES the resumed action — the user never re-clicks
    the original 催租 (NO-DEAD-END §1 / §2)."""
    from pasay_bot.api_client import PasayApiError

    api = context.bot_data["api_client"]
    chat_id = update.effective_chat.id
    user = update.effective_user
    user_id = user.id if user else None
    started = time.monotonic()
    _record_timeline_event(context, "phone_fast_path", "phone_input_received", started)
    phone = fix.get("phone")
    unit_token = fix.get("unit_token")
    if not phone:
        return False
    if not has_read_permission(role):
        await context.bot.send_message(
            chat_id, H.escape(t("common.no_permission", locale)), parse_mode=HTML,
        )
        return True
    # Resolve the unit -> lease (needs the lease_id for a targeted resume).
    unit_id = lease_id = None
    try:
        units = await api.get_units()
        leases = await api.get_leases()
    except PasayApiError:
        units, leases = [], []
    pending = _pending_missing_phone_context(context, chat_id, user_id)
    if not unit_token and pending:
        unit_token = pending.get("unit_code")
    key = (unit_token or "").split("-")[-1]
    for u in units:
        if (u.unit_number or "").split("-")[-1] == key:
            unit_id = u.id
            break
    if unit_id is None:
        return False  # unresolved -> normal lanes / AI fallback
    _record_timeline_event(context, "phone_fast_path", "phone_parsed", started)
    active = next((l for l in leases if l.unit_id == unit_id and l.status == "active"), None)
    if active is not None:
        lease_id = active.id
    try:
        result = await api.resume_action(
            field="tenant_phone", value=phone,
            lease_id=lease_id, unit_id=unit_id,
        )
    except PasayApiError:
        return False
    _record_timeline_event(context, "phone_fast_path", "phone_saved", started)
    tenant_name = ""
    try:
        tenants = await api.get_tenants()
        if active is not None:
            tn = next((t for t in tenants if t.id == active.tenant_id), None)
            tenant_name = tn.full_name if tn else ""
    except PasayApiError:
        pass
    masked = H.mask_phone(phone) if hasattr(H, "mask_phone") else phone
    lines = [f"✅ 已记录 {H.escape(tenant_name or '租客')} 电话：{masked}"]
    resumed = result.get("blocked_action")
    if resumed:
        # Auto-resume: if the resumed action is a rent-follow-up assignment,
        # run the same 催租 flow so the Secretary DM is sent automatically.
        from pasay_bot.handlers import callback as cb

        try:
            if resumed == "assign_to_secretary" and unit_id:
                # Re-route to the deterministic 催租 action (it re-checks the
                # now-present phone and DMs the Secretary). Non-callback updates
                # carry no nonce/ts.
                await cb._handle_rent_followup(update, context, str(unit_id), "", None, role, locale)
                lines.append("✅ 已自动恢复原任务。")
                _record_timeline_event(context, "phone_fast_path", "task_resumed", started)
        except Exception:  # noqa: BLE001 - self-heal must never hard-fail
            lines.append("✅ 已自动恢复原任务。")
            _record_timeline_event(context, "phone_fast_path", "task_resumed", started)
    if user_id is not None:
        try:
            store = context.bot_data.get("store")
            if store is not None:
                ctx = store.get_v2_context(chat_id, user_id)
                payload = dict(ctx["payload"]) if ctx else {}
                payload.pop("missing_phone_followup", None)
                store.save_v2_context(chat_id, user_id, payload)
        except Exception:  # noqa: BLE001 - best effort cleanup only
            pass
    await _v2_reply(update, context, "\n".join(lines), role, locale)
    _record_timeline_event(context, "phone_fast_path", "reply_sent", started)
    return True


async def _v2_reply(update, context, text: str, role, locale: str):
    """Send one V2 reply carrying the fixed keyboard (self-healing reuse)."""
    from pasay_bot.handlers.commands import _is_menu_initialized, reply_keyboard
    chat_id = update.effective_chat.id
    try:
        keyboard = (
            reply_keyboard(role)
            if role and not _is_menu_initialized(context, chat_id)
            else None
        )
        await context.bot.send_message(
            chat_id,
            H.escape(text),
            parse_mode=HTML,
            reply_markup=keyboard,
        )
    except Exception:  # noqa: BLE001 - never let keyboard mounting break UX
        await context.bot.send_message(chat_id, H.escape(text), parse_mode=HTML)


async def _notify_groups_expense_paid(context, expense) -> None:
    """P0-EXPENSE-PAID-CLOSEOUT-001: after an expense is marked PAID, push a
    bilingual completion card (Unit · purpose · amount · Paid / 已付款 ·
    Expense completed / 支出已完成) to every known operation group."""
    store = context.bot_data["store"]
    groups = store.list_known_groups()
    if not groups:
        return
    location = ""
    if getattr(expense, "unit_id", None):
        try:
            units, properties = await asyncio.gather(
                context.bot_data["api_client"].get_units(),
                context.bot_data["api_client"].get_properties(),
            )
            location = pages._expense_location(expense, units, properties)
        except PasayApiError:
            location = ""
    text = cards.expense_paid_card(expense, "bi", location=location)
    for group in groups:
        try:
            await context.bot.send_message(group["chat_id"], H.truncate(text), parse_mode=HTML)
        except Exception as exc:  # noqa: BLE001 - one bad group never blocks the rest
            logger.warning("expense paid to group %s failed: %s", group["chat_id"], exc)


def _v2_task_card_text(event: str, task: dict, locale: str) -> str:
    """task_event_card from Subagent B, with a defensive inline fallback."""
    try:
        return cards.task_event_card(event, task, locale)
    except Exception:  # noqa: BLE001 - integration seam must never crash UX
        status = str(task.get("status") or "").upper()
        emoji = "\U0001f534" if status == "PENDING" else ("\U0001f7e1" if status == "IN_PROGRESS" else "\u2705")
        unit = task.get("property_code") or task.get("unit_code") or ""
        title = task.get("title") or ""
        lines = [f"{emoji} {unit} \u00b7 {title}".strip(" \u00b7")]
        if task.get("next_action"):
            lines.append("Next: " + str(task["next_action"]))
        return "\n".join(lines)


async def _handle_v2_task_event(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, role, locale: str,
) -> bool:
    """Deterministic conversation -> Task: create repair task, apply progress,
    complete, or correct the association. Returns True when handled."""
    if not has_read_permission(role):
        return False
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    store = context.bot_data["store"]
    api = context.bot_data["api_client"]
    ctx = store.get_v2_context(chat_id, user_id)
    payload = dict(ctx["payload"]) if ctx else {}
    task_ref = payload.get("task_ref")
    unit_token = payload.get("unit_token")
    expense_ref = payload.get("expense_ref")

    completion = detect_task_completion(text)
    progress = detect_task_progress(text)
    repair_unit = detect_repair_statement(text)
    payment = detect_payment_event(text)
    quote = detect_repair_quote(text)

    # PASAY-V2-FOUNDATION-001 P0-4/5: Owner's "已经付款/paid" with an APPROVED
    # expense in context is a Payment Event -> PAID/COMPLETED. No amount
    # question, no category question, no second expense, no receipt gate.
    if payment and role == Role.OWNER and expense_ref:
        try:
            expense = await api.get_expense(int(expense_ref))
        except (PasayApiError, ValueError):
            expense = None
        if expense is not None and (expense.status or "").lower() == "approved":
            try:
                expense = await api.pay_expense(int(expense_ref))
            except PasayApiError:
                return False
            store.clear_v2_context(chat_id, user_id)
            await _notify_groups_expense_paid(context, expense)
            await _v2_reply(
                update, context,
                cards.expense_paid_card(expense, locale),
                role, locale,
            )
            return True

    # P0-6 Owner authority fallback: even without a live context, an explicit
    # payment confirmation advances the single outstanding APPROVED expense
    # (never guesses among several; ambiguous state keeps the fallback lane).
    if payment and role == Role.OWNER:
        try:
            expenses = await api.list_expenses()
        except PasayApiError:
            expenses = []
        approved = [
            e for e in expenses
            if (e.status or "").lower() == "approved"
        ]
        if len(approved) == 1:
            latest = approved[0]
            try:
                expense = await api.pay_expense(latest.id)
            except PasayApiError:
                return False
            store.clear_v2_context(chat_id, user_id)
            await _notify_groups_expense_paid(context, expense)
            await _v2_reply(
                update, context,
                cards.expense_paid_card(expense, locale),
                role, locale,
            )
            return True

    # PASAY-V2-FOUNDATION-001 Journey B: "technician quoted 7000" with an
    # active repair context records the quote and creates the Expense Approval
    # through the existing deterministic expense path (unit + category known
    # from context; the first report never asked for an amount).
    if quote is not None and task_ref:
        unit_id, unit_number, property_name = await resolve_unit(
            context, unit_token or ""
        )
        if unit_id is not None:
            expense = await _submit_quote_expense(
                update, context, api, store, unit_id, unit_number, quote, text, role, locale,
            )
            if expense is not None:
                store.save_v2_context(
                    chat_id, user_id,
                    {
                        "task_ref": task_ref,
                        "unit_token": unit_number,
                        "intent": "repair",
                        "expense_ref": expense.id,
                    },
                )
                submitted = cards.expense_submitted_card(
                    unit_number=unit_number,
                    property_name=property_name,
                    category="维修",
                    amount=quote,
                    locale=locale,
                )
                await _v2_reply(
                    update, context,
                    submitted,
                    role, locale,
                )
                return True

    # AI-OPS-FOUNDATION-001 §9/§12: "finished"/"done" WITHOUT an active task
    # in context is ambiguous — never guess which repair to close. Show one
    # deterministic candidate button per active repair task; a single
    # candidate completes directly.
    if completion and not task_ref:
        try:
            active_repairs = await api.get_operational_tasks(status="PENDING")
        except PasayApiError:
            active_repairs = []
        candidates = [t for t in active_repairs if t.task_type == "AC_MAINTENANCE"]
        if len(candidates) == 1:
            task_ref = str(candidates[0].id)
            # fall through to the single-candidate completion below
        elif len(candidates) > 1:
            nonce = new_nonce()
            ts = now_ts()
            text = (
                f"<b>{H.escape(t('repair.who_finished', locale))}</b>\n\n"
                f"{H.escape(t('repair.who_finished', 'en' if locale != 'en' else 'zh'))}"
            )
            await context.bot.send_message(
                chat_id,
                H.truncate(text),
                parse_mode=HTML,
                reply_markup=repair_completion_candidates_keyboard(
                    candidates, locale, nonce=nonce, ts=ts,
                ),
            )
            return True

    # Completion of the active task in context. The backend closes the repair
    # and, when completion evidence is missing, assigns a SECRETARY
    # evidence-follow-up (AI-OPS-FOUNDATION-001 §13) — never the Owner.
    if completion and task_ref:
        try:
            task = await api.update_operational_task(
                int(task_ref), status="COMPLETED",
            )
        except PasayApiError:
            return False  # let the AI lane produce a friendly fallback
        store.clear_v2_context(chat_id, user_id)
        task_text = _v2_task_card_text(
            "completed",
            task.as_dict() if hasattr(task, "as_dict") else task.__dict__,
            locale,
        )
        # PASAY-V2-OWNER-SECRETARY-JOURNEY-AUDIT-006 (Journey M): append a
        # varied, positive deterministic closing line so repair completion is a
        # human acknowledgement, never a cold mechanical line.
        try:
            from pasay_bot.render import completion

            recent = context.bot_data.setdefault("completion_recent", set())
            key, closing = completion.select(locale, "task", recent)
            if closing:
                recent.add(key)
                if len(recent) > completion._RECENT_LIMIT:
                    recent.pop()
                task_text += "\n" + closing
        except Exception:  # noqa: BLE001 - completion feedback is best-effort
            pass
        await _v2_reply(
            update, context,
            task_text,
            role, locale,
        )
        return True

    # Progress update attaches to the active task (never re-asks property).
    if progress and task_ref:
        next_check = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        next_action = _progress_next_action(text, unit_token)
        # AI-OPS-FOUNDATION-001 §8: "Technician coming tomorrow" persists a
        # structured promise (follow_up_at / responsible party) on the task —
        # the system follows up instead of just acknowledging.
        promise = _promise_from_progress(text, task_ref, unit_token)
        try:
            task = await api.update_operational_task(
                int(task_ref),
                status="IN_PROGRESS",
                next_action=next_action,
                next_check_at=next_check,
                context=text,
                details={"promise": promise, "repair_stage": "SCHEDULED"},
            )
        except PasayApiError:
            return False
        store.save_v2_context(
            chat_id, user_id,
            {"task_ref": task_ref, "unit_token": unit_token, "intent": "repair"},
        )
        await _v2_reply(
            update, context,
            _v2_task_card_text(
                "updated",
                task.as_dict() if hasattr(task, "as_dict") else task.__dict__,
                locale,
            ),
            role, locale,
        )
        return True

    # New repair event -> create AC_MAINTENANCE task.
    if repair_unit is not None:
        unit_id, unit_number, property_name = await resolve_unit(context, repair_unit)
        if unit_id is None:
            return False  # unresolved unit -> normal lanes/AI fallback
        # PASAY-V2-FOUNDATION-001: POST /operations/tasks validates
        # property_id against the PROPERTIES table (never the unit id).
        # Resolve the owning property id from the same unit list.
        property_id = await _resolve_unit_property_id(context, unit_id)
        if property_id is None:
            return False
        if "air" in text.lower() or "\u7a7a\u8c03" in text:
            title = f"{unit_number} \u00b7 Aircon repair"
        else:
            title = f"{unit_number} \u00b7 Maintenance"
        try:
            task = await api.create_operational_task(
                task_type="AC_MAINTENANCE",
                title=title,
                property_id=property_id,
                description=text,
                context=text,
                source_event=text,
                completion_condition="Repair confirmed done",
                # AI-OPS-FOUNDATION-001 §13: track the repair lifecycle stage.
                details={"repair_stage": "ISSUE_REPORTED"},
                dedupe_key=f"conversation:{unit_number.lower()}:{int(time.time()) // 3600}",
            )
        except PasayApiError:
            return False
        store.save_v2_context(
            chat_id, user_id,
            {
                "task_ref": task.id,
                "unit_token": unit_number,
                "intent": "repair",
                "property_id": property_id,
            },
        )
        await _v2_reply(
            update, context,
            _v2_task_card_text(
                "created",
                task.as_dict() if hasattr(task, "as_dict") else task.__dict__,
                locale,
            ),
            role, locale,
        )
        return True

    return False


async def _resolve_unit_property_id(context, unit_id: int) -> Optional[int]:
    """Map a resolved unit id to its owning property id (backend FK)."""
    try:
        units = await context.bot_data["api_client"].get_units()
    except PasayApiError:
        return None
    for unit in units:
        if unit.id == unit_id:
            return unit.property_id
    return None


async def _submit_quote_expense(
    update, context, api, store,
    unit_id: int, unit_number: str, amount: Decimal, text: str, role, locale: str,
):
    """Journey B: a repair quote creates ONE pending expense via the existing
    financial path (no amount/category questions, no duplicate). Returns the
    created Expense or None on failure."""
    try:
        expense = await api.create_expense(
            category="维修",
            amount=amount,
            expense_date=date.today().isoformat(),
            unit_id=unit_id,
            payee="Repair",
            description=(text or "").strip() or None,
            status="pending",
        )
    except PasayApiError:
        return None
    return expense


def _progress_next_action(text: str, unit_token: Optional[str]) -> str:
    """Human next-action line from a progress message (short, actionable)."""
    lowered = (text or "").lower()
    if "tomorrow" in lowered or "\u660e\u5929" in text:
        return "Confirm repair completion tomorrow"
    if "today" in lowered or "\u4eca\u5929" in text:
        return "Confirm repair completion today"
    return "Confirm repair completion"


def _promise_from_progress(text: str, task_ref, unit_token: Optional[str]) -> dict:
    """AI-OPS-FOUNDATION-001 §8: persist a structured follow-up promise
    ("Technician coming tomorrow" / "Paolo will pay Friday") instead of only
    replying conversationally.

    Returns the promise dict stored in the task's JSONB details:
    promised_at / follow_up_at / responsible_party / related_entity / status.
    """
    lowered = (text or "").lower()
    now = datetime.now(timezone.utc)
    if "tomorrow" in lowered or "\u660e\u5929" in text:
        follow_up = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    elif "today" in lowered or "\u4eca\u5929" in text:
        follow_up = now.replace(hour=9, minute=0, second=0, microsecond=0)
        if follow_up <= now:
            follow_up += timedelta(days=1)
    else:
        follow_up = now + timedelta(hours=24)
    if any(w in lowered for w in ("technician", "tech", "repairman", "mechanic", "electrician")) \
            or any(w in text for w in ("\u5e08\u5085", "\u6280\u5e08", "\u7ef4\u4fee\u5de5", "\u5e08\u5085\u6765")):
        responsible = "technician"
    elif any(w in lowered for w in ("paolo", "tenant", "\u79df\u5ba2", "\u6237")):
        responsible = "tenant"
    else:
        responsible = "secretary"
    return {
        "promised_at": now.isoformat(),
        "follow_up_at": follow_up.isoformat(),
        "responsible_party": responsible,
        "related_entity": f"task:{task_ref}",
        "status": "open",
        "note": (text or "").strip()[:300],
    }


async def _render_unit_add_confirm(update, context, draft: dict, role, locale) -> None:
    """AI-OPS-FOUNDATION-001 §14: confirmation card BEFORE the state-changing
    Unit create (deterministic; the user confirms the parsed facts)."""
    from pasay_bot.keyboards import unit_add_confirm_keyboard

    store = context.bot_data["store"]
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    api = context.bot_data["api_client"]
    try:
        properties = await api.get_properties()
    except PasayApiError:
        properties = []
    if not properties:
        await context.bot.send_message(
            chat_id, H.escape(t("unit_add.no_property", locale)), parse_mode=HTML,
        )
        return
    draft = dict(draft)
    draft["property_id"] = properties[0].id
    draft["property_name"] = properties[0].name
    nonce = new_nonce()
    ts = now_ts()
    store.save_conversation(chat_id, user_id, "unit_add_confirm", draft, nonce=nonce)
    status_label = (
        "空置" if draft["status"] == "vacant" else "已租"
    )
    text = t("unit_add.card", locale, unit=draft["unit_number"],
             rent=draft["monthly_rent"], status=status_label)
    await context.bot.send_message(
        chat_id, H.truncate(text), parse_mode=HTML,
        reply_markup=unit_add_confirm_keyboard(nonce, ts, locale),
    )


async def _render_viewing_confirm(update, context, draft: dict, role, locale) -> None:
    """AI-OPS-FOUNDATION-001 §17: confirmation card for a detected viewing."""
    from pasay_bot.keyboards import viewing_confirm_keyboard

    store = context.bot_data["store"]
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    nonce = new_nonce()
    ts = now_ts()
    store.save_conversation(chat_id, user_id, "viewing_confirm", draft, nonce=nonce)
    text = t("viewing.card", locale, unit=draft["unit_token"],
             time=str(draft["scheduled_at"])[:16].replace("T", " "))
    await context.bot.send_message(
        chat_id, H.truncate(text), parse_mode=HTML,
        reply_markup=viewing_confirm_keyboard(nonce, ts, locale),
    )


async def _handle_task_correction(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    old_unit: str, new_unit: str, role, locale: str,
) -> bool:
    """V2 correction: fix the property association of the active draft/task
    in context. Confirmed financial data is NEVER touched here (existing
    reverse/audit rules own that path)."""
    if not has_read_permission(role):
        return False
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    store = context.bot_data["store"]
    api = context.bot_data["api_client"]
    ctx = store.get_v2_context(chat_id, user_id)
    payload = dict(ctx["payload"]) if ctx else {}
    task_ref = payload.get("task_ref")
    if task_ref is None:
        return False
    unit_id, unit_number, _property_name = await resolve_unit(context, new_unit)
    if unit_id is None:
        return False
    try:
        await api.update_operational_task(
            int(task_ref),
            context=f"Corrected {old_unit} -> {new_unit}",
        )
    except PasayApiError:
        return False
    store.save_v2_context(
        chat_id, user_id,
        {"task_ref": task_ref, "unit_token": unit_number, "intent": "repair"},
    )
    await _v2_reply(
        update, context,
        f"\u2705 {new_unit} / \u5df2\u4fee\u6b63\u4e3a {new_unit}",
        role, locale,
    )
    return True


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
    # The full /reports/overdue-rents result is the authority for "exists
    # outstanding": a unit that owes older periods (e.g. Jun-Jul while Aug is
    # not yet due) is still unpaid, so the "all rent collected" empty state is
    # only reachable when the overdue report is truly empty.
    # (P0-RENT-SECRETARY-STATUS-004)
    unpaid = rows
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


# ---------------------------------------------------------------------------
# P1-PASAY-NIGHTLY-PRODUCT-HARDENING-008 C: deterministic rent payment-history
# answers ("1608 交了几次 / 累计交了多少 / 最近什么时候交的 / Paolo 最近交租").
# 0 LLM; only the existing read endpoints; same canonical unit/tenant matching
# as the status lane; partial payments counted individually; only CONFIRMED
# incomes count (pending/reversed excluded); multi-match uses the same
# read-only candidate selector.
# ---------------------------------------------------------------------------

def _income_period(inc) -> str:
    """The rent period (YYYY-MM) an income maps to. Mirrors the backend and
    the status lane: an explicit YYYY-MM in the description is authoritative;
    the received-date month is the fallback (never both)."""
    desc = inc.description or ""
    match = re.search(r"(?<!\d)(\d{4})(?:[-/.])?(\d{1,2})(?!\d)", desc)
    if match is not None:
        year, month = int(match.group(1)), int(match.group(2))
        if 1 <= month <= 12:
            return f"{year:04d}-{month:02d}"
    rd = inc.received_date
    return rd.strftime("%Y-%m") if rd else ""


def _payment_history_stats(incomes, lease_id: int, month: str):
    """(count, cumulative, latest_date) of valid rent payments for a lease.

    A payment event is a CONFIRMED income row — partial payments each count
    once and their amounts sum into the cumulative total; pending and
    reversed rows are excluded per the existing financial semantics. When
    ``month`` is set, only payments attributed to that rent period count.
    """
    confirmed = [
        inc for inc in incomes
        if inc.lease_id == lease_id and inc.status == "confirmed"
    ]
    if month:
        confirmed = [inc for inc in confirmed if _income_period(inc) == month]
    count = len(confirmed)
    total = sum((inc.amount for inc in confirmed), Decimal("0"))
    latest = max((inc.received_date for inc in confirmed), default=None)
    return count, total, latest


def _payment_history_candidates(
    units, leases, tenants, incomes, properties, *, unit_ids=None, tenant_ids=None,
    month: str = "",
) -> list[dict]:
    """One read-only candidate per active lease matching the requested unit(s)
    or tenant(s); carries the history stats. Never selects, never writes."""
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
        count, total, latest = _payment_history_stats(incomes, lease.id, month)
        candidates.append(
            {
                "tenant_name": tenant.full_name,
                "unit_number": unit.unit_number,
                "property_name": prop.name if prop else "",
                "count": count,
                "cumulative": str(total),
                "latest_date": latest.isoformat() if latest else "",
                "month": month,
            }
        )
    return candidates


def _payment_history_candidate_row(c: dict) -> dict:
    """JSON-safe display-only row for the selector store (no internal ids)."""
    return {
        "tenant_name": str(c.get("tenant_name") or ""),
        "unit_number": str(c.get("unit_number") or ""),
        "property_name": str(c.get("property_name") or ""),
        "count": int(c.get("count") or 0),
        "cumulative": str(c.get("cumulative") or "0"),
        "latest_date": str(c.get("latest_date") or ""),
        "month": str(c.get("month") or ""),
    }


async def _handle_rent_payment_history(update, context, query, role, locale):
    """Read-only payment-history answer lane (0 LLM, no writes)."""
    chat_id = update.effective_chat.id
    if not has_read_permission(role):
        await context.bot.send_message(
            chat_id,
            H.escape(t("common.no_permission", locale)),
            parse_mode=HTML,
        )
        return
    try:
        await _answer_rent_payment_history(
            context, chat_id, query, locale,
            user_id=update.effective_user.id if update.effective_user else None,
        )
    except PasayApiError:
        await context.bot.send_message(
            chat_id,
            H.escape(t("rent_status.error", locale)),
            parse_mode=HTML,
        )
        return


async def _answer_rent_payment_history(context, chat_id, query, locale, user_id=None):
    """'1608 交了几次 / 累计交了多少 / 最近什么时候交的' and tenant variants.

    Single match -> the history card; several -> the same read-only candidate
    selector as the status lane (one inline button per candidate, resolved by
    the tap handler; never auto-selects, never writes)."""
    api = context.bot_data["api_client"]
    units, leases, tenants, incomes, properties = await asyncio.gather(
        api.get_units(),
        api.get_leases(),
        api.get_tenants(),
        api.list_incomes(),
        api.get_properties(),
    )
    month = ""
    if query.month_window == "this_month":
        month = pages._current_month()
    if query.unit_token:
        matched = [
            u for u in units
            if _unit_number_matches(query.unit_token, u.unit_number)
        ]
        if not matched:
            await context.bot.send_message(
                chat_id,
                H.escape(t("rent_status.no_unit", locale, unit=query.unit_token)),
                parse_mode=HTML,
            )
            return
        candidates = _payment_history_candidates(
            units, leases, tenants, incomes, properties,
            unit_ids={u.id for u in matched}, month=month,
        )
    elif query.tenant_token:
        token = query.tenant_token.lower()
        matched = [
            tn for tn in tenants
            if re.search(rf"\b{re.escape(token)}\b", tn.full_name.lower())
        ]
        if not matched:
            await context.bot.send_message(
                chat_id,
                H.escape(t("rent_status.no_tenant", locale, name=query.tenant_token)),
                parse_mode=HTML,
            )
            return
        candidates = _payment_history_candidates(
            units, leases, tenants, incomes, properties,
            tenant_ids={tn.id for tn in matched}, month=month,
        )
    else:
        return
    if not candidates:
        await context.bot.send_message(
            chat_id,
            H.escape(t("rent_status.no_active_lease", locale)),
            parse_mode=HTML,
        )
        return
    if len(candidates) == 1:
        row = _payment_history_candidate_row(candidates[0])
        await context.bot.send_message(
            chat_id,
            H.truncate(cards.rent_history_card_for_candidate(row, locale)),
            parse_mode=HTML,
        )
        return
    payload = [_payment_history_candidate_row(c) for c in candidates]
    nonce = new_nonce()
    ts = now_ts()
    if user_id is not None:
        context.bot_data["store"].save_rent_status_selector(
            nonce, chat_id, user_id, payload,
        )
    await context.bot.send_message(
        chat_id,
        H.truncate(cards.rent_history_selector_card(candidates, locale)),
        parse_mode=HTML,
        reply_markup=rent_history_candidates_keyboard(
            candidates, locale, nonce=nonce, ts=ts,
        ),
    )


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


# --- BOT-V1-USABLE-001 P0-5: AI fallback lane (never /help) ----------------

async def _edit_ai_status(context, chat_id, status, locale, text: str, keyboard=None):
    """Mutate the '理解中…' status message into the real answer (no junk
    messages); fall back to a new message if the edit fails."""
    try:
        await edit_message_text_idempotent(
            context.bot,
            chat_id=chat_id,
            message_id=status.message_id,
            text=H.truncate(text),
            parse_mode=HTML,
            reply_markup=keyboard,
        )
    except Exception:  # noqa: BLE001 - fallback must never lose feedback
        await context.bot.send_message(
            chat_id, H.truncate(text), parse_mode=HTML,
            reply_markup=keyboard,
        )


async def _handle_ai_fallback(update, context, text, role, locale):
    """P0-5: ordinary text the deterministic parsers cannot classify enters
    the backend's grounded AI intent lane. The structured intent then routes
    into the existing deterministic business paths only."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if not has_read_permission(role):
        await context.bot.send_message(
            chat_id,
            H.escape(t("common.no_permission", locale)),
            parse_mode=HTML,
        )
        return
    api = context.bot_data["api_client"]
    status = await context.bot.send_message(
        chat_id, H.escape(t("ai.thinking", locale)), parse_mode=HTML
    )
    try:
        result = await api.parse_nl_intent(text)
    except PasayApiError:
        await _edit_ai_status(context, chat_id, status, locale, t("ai.unknown", locale))
        return

    intent = result.intent
    if intent == "create_income":
        # AI confirms it is a rent-received statement -> the deterministic
        # matcher resolves the exact receivable (never AI-selected fields).
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=status.message_id)
        except Exception:  # noqa: BLE001 - status cleanup must never block UX
            logger.debug("ai status cleanup failed", exc_info=True)
        await _handle_rent_payment_statement(
            update, context, _normalize_correction(text), role, locale,
        )
        return
    if intent == "create_expense":
        await _handle_ai_expense(
            update, context, result, status, role, locale,
        )
        return
    if intent in ("query", "ask") and result.unit and result.category:
        # Weak LLM classifications ("1680水费" -> ask) still map to the
        # explicit record-vs-query choices instead of a generic answer.
        await _handle_ai_ambiguous(
            update, context, result, text, status, role, locale,
        )
        return
    if intent in ("query", "ask"):
        # Read-only grounded Q&A via the existing copilot service.
        try:
            ask = await api.copilot_ask(text)
        except PasayApiError:
            await _edit_ai_status(context, chat_id, status, locale, t("ai.unknown", locale))
            return
        await _edit_ai_status(
            context, chat_id, status, locale,
            cards.copilot_ask_card(ask.answer, fallback=ask.fallback, locale=locale),
        )
        return
    await _handle_ai_ambiguous(
        update, context, result, text, status, role, locale,
    )


async def _handle_ai_expense(update, context, result, status, role, locale):
    """AI-parsed expense intent -> deterministic confirmation flow. Missing
    fields are asked ONE at a time; the bot never guesses amounts/categories."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    store = context.bot_data["store"]
    unit_id, unit_number, property_name = await resolve_unit(
        context, result.unit,
    )
    if result.unit and unit_id is None:
        await _edit_ai_status(
            context, chat_id, status, locale,
            t("expense.unit_not_found", locale, unit=result.unit),
        )
        return
    payload = {
        "unit_id": unit_id,
        "unit_number": unit_number or result.unit or "",
        "property_name": property_name,
        "category": result.category or "",
        "amount": str(result.amount) if result.amount is not None else "",
        "expense_date": result.month or "",
        "payee": "",
        "description": "",
    }
    missing = list(result.missing or [])
    if not payload["category"]:
        missing.append("category")
    if not payload["amount"]:
        missing.append("amount")
    missing = list(dict.fromkeys(missing))
    if missing:
        store.save_conversation(
            chat_id, user_id, "ai_expense_partial", payload,
        )
        if "amount" in missing:
            ask_text = result.message or t(
                "ai.ask_amount", locale,
                unit=payload["unit_number"] or "该房源",
                category=payload["category"] or "",
            )
        elif "category" in missing:
            ask_text = t("ai.ask_category", locale)
        else:
            ask_text = t("ai.ask_unit", locale)
        await _edit_ai_status(context, chat_id, status, locale, ask_text)
        return
    await render_expense_confirm(
        update, context, payload, role, locale,
        message_id=status.message_id,
    )


async def _handle_ai_ambiguous(
    update, context, result, original_text, status, role, locale,
):
    """P0-5 ambiguity: 2-3 explicit choices (deterministic taps)."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    store = context.bot_data["store"]
    if result.unit and result.category:
        options = [
            t("ai.choice_record", locale, category=result.category),
            t("ai.choice_query", locale, unit=result.unit),
        ]
        choice_base = "expense"
    elif result.unit and result.amount is not None:
        options = [
            t("ai.choice_income_record", locale, unit=result.unit),
            t("ai.choice_income_query", locale, unit=result.unit),
        ]
        choice_base = "income"
    else:
        options = [
            t("ai.choice_expense", locale),
            t("ai.choice_general_query", locale),
            t("ai.choice_ask", locale),
        ]
        choice_base = "generic"
    nonce = new_nonce()
    ts = now_ts()
    store.save_conversation(
        chat_id, user_id, "ai_choice",
        {
            "text": original_text,
            "unit": result.unit,
            "unit_id": result.unit_id,
            "category": result.category,
            "amount": str(result.amount) if result.amount is not None else "",
            "month": result.month,
            "choice_base": choice_base,
            "options": options,
        },
        nonce=nonce,
    )
    header = result.message or t("ai.want", locale)
    await _edit_ai_status(
        context, chat_id, status, locale,
        header,
        keyboard=ai_choice_keyboard(nonce, ts, options, locale),
    )


async def _handle_expense_ambiguity(update, context, statement, role, locale):
    """Deterministic P0-5 ambiguity for '1680水费': two explicit choices."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    store = context.bot_data["store"]
    unit_id, unit_number, _property_name = await resolve_unit(
        context, statement.unit_token,
    )
    options = [
        t("ai.choice_record", locale, category=statement.category),
        t("ai.choice_query", locale, unit=statement.unit_token),
    ]
    nonce = new_nonce()
    ts = now_ts()
    store.save_conversation(
        chat_id, user_id, "ai_choice",
        {
            "text": "",
            "unit": statement.unit_token,
            "unit_id": unit_id,
            "category": statement.category,
            "amount": "",
            "month": "",
            "choice_base": "expense",
            "options": options,
        },
        nonce=nonce,
    )
    await context.bot.send_message(
        chat_id,
        H.escape(t("ai.want", locale)),
        parse_mode=HTML,
        reply_markup=ai_choice_keyboard(nonce, ts, options, locale),
    )
