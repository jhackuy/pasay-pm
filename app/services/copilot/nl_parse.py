"""Grounded natural-language intent parsing for the Telegram bot V1 fallback.

BOT-V1-USABLE-001 P0-5: when the bot's deterministic parsers cannot classify
a free-text message, the bot calls ``POST /operations/copilot/nl-parse``.
The backend grounds the text to the REAL catalog (units / tenants / expense
categories / current month) and returns a STRUCTURED intent — never raw LLM
text. The bot then executes ONLY through its existing deterministic business
paths (rent matcher / expense create / read-only query renders).

Nothing here writes business data; the optional ``copilot_runs`` audit row is
written by the router. Provider-down returns a deterministic classification
(or a friendly clarification request), never a fabricated write action.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Optional

from sqlalchemy.orm import Session

from app.models.lease import Lease, LeaseStatus
from app.models.financial import Expense
from app.models.property import Property, Unit
from app.models.tenant import Tenant
from app.models.user import User
from app.services.copilot import llm
from app.services.copilot.shared import parse_json_object
from app.services.operations.timeclock import clock

MAX_AMOUNT = Decimal("999999999999.99")
INTENT_CREATE_INCOME = "create_income"
INTENT_CREATE_EXPENSE = "create_expense"
INTENT_QUERY = "query"
INTENT_ASK = "ask"
INTENT_AMBIGUOUS = "ambiguous"

# Canonical expense categories (human labels, zh first). The LLM returns a
# category name; the validator normalizes synonyms to these labels.
STANDARD_CATEGORIES = [
    "水费", "电费", "物业费", "管理费", "维修", "网络费",
    "保洁", "燃气费", "税费", "其他",
]
_CATEGORY_ALIASES: dict[str, str] = {}
for _cat in STANDARD_CATEGORIES:
    _CATEGORY_ALIASES[_cat] = _cat
_CATEGORY_ALIASES.update(
    {
        "water": "水费", "水": "水费", "water bill": "水费", "waterbill": "水费",
        "electric": "电费", "electricity": "电费", "electricity bill": "电费",
        "电": "电费", "电单": "电费",
        "property fee": "物业费", "物业": "物业费", "association": "物业费",
        "association fee": "物业费", "association dues": "物业费",
        "hoadue": "物业费", "hoa": "物业费",
        "maintenance": "维修", "repair": "维修", "fix": "维修",
        "internet": "网络费", "网费": "网络费", "wifi": "网络费", "broadband": "网络费",
        "cleaning": "保洁", "cleaner": "保洁", "保洁费": "保洁",
        "gas": "燃气费", "燃气": "燃气费", "utility": "其他",
        "tax": "税费", "taxes": "税费", "real property tax": "税费",
        "other": "其他", "misc": "其他", "杂费": "其他",
    }
)

# Deterministic keyword lanes used ONLY as the provider-down fallback (the
# LLM path is the normal intent parser).
_EXPENSE_CATEGORY_CN = ("水费", "电费", "物业", "管理费", "维修", "网费", "网络", "保洁", "燃气", "税费")
_EXPENSE_WORDS_CN = ("支出", "花了", "交了", "付了", "付款", "费用", "付掉", "缴了", "付费")
_RENT_WORDS_CN = ("租金", "房租", "rent")
_RENT_VERBS_CN = ("收到", "到了", "到账", "已收", "入账", "收款", "收租")
_QUERY_WORDS_CN = ("吗", "谁", "多少", "什么时候", "哪些", "还有", "快到期", "到期", "交了", "收了", "花了", "查", "有没有", "是", "怎么样")
_QUERY_WORDS_EN = re.compile(r"\b(who|when|which|how much|how many|status|due|expiring|paid|unpaid|is|are)\b", re.I)
_UNIT_TOKEN = re.compile(
    r"(?<![A-Za-z0-9-])("
    r"(?:[A-Za-z]+-)+[A-Za-z]*\d{1,4}[A-Za-z]?"
    r"|[A-Za-z]?\d{1,4}[A-Za-z]?"
    r")(?![A-Za-z0-9-])"
)
_AMOUNT_TOKEN = re.compile(r"(?:₱|PHP|php|比索|peso)?\s*(\d[\d,]*(?:\.\d+)?)")


@dataclass(frozen=True)
class NlParseResult:
    """Structured intent result returned to the bot.

    The bot only uses this to route into its own deterministic business paths;
    no LLM text ever reaches the user directly through this channel.
    """

    intent: str
    message: str = ""
    unit: str = ""
    unit_id: Optional[int] = None
    amount: Optional[Decimal] = None
    category: str = ""
    month: str = ""
    missing: list[str] = field(default_factory=list)
    options: list[str] = field(default_factory=list)
    provider: str = ""
    model: str = "deterministic"
    fallback: bool = False
    flags: list[str] = field(default_factory=list)
    latency_ms: int = 0


@dataclass
class Catalog:
    units: list[dict] = field(default_factory=list)
    leases: list[dict] = field(default_factory=list)
    tenants: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    current_month: str = ""


def _unit_matches(token: str, unit_number: str) -> bool:
    """Same normalized unit-number match as the payment matcher."""
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


def build_catalog(db: Session, now=None) -> Catalog:
    """Read-only snapshot of the entity catalog the intent parser can use."""
    now = now if now is not None else clock.now()
    current_month = f"{now:%Y-%m}"
    units = (
        db.query(Unit)
        .filter(Unit.deleted_at.is_(None))
        .all()
    )
    properties = {
        p.id: p
        for p in db.query(Property).filter(Property.deleted_at.is_(None)).all()
    }
    tenants = {t.id: t for t in db.query(Tenant).filter(Tenant.deleted_at.is_(None)).all()}
    leases = (
        db.query(Lease)
        .filter(Lease.status == LeaseStatus.active, Lease.deleted_at.is_(None))
        .all()
    )
    unit_rows = []
    for u in sorted(units, key=lambda x: x.unit_number):
        prop = properties.get(u.property_id)
        unit_rows.append(
            {
                "id": u.id,
                "unit_number": u.unit_number,
                "property": prop.name if prop else "",
            }
        )
    lease_rows = []
    for lease in leases:
        unit = next((x for x in units if x.id == lease.unit_id), None)
        tenant = tenants.get(lease.tenant_id)
        lease_rows.append(
            {
                "lease_id": lease.id,
                "unit": unit.unit_number if unit else "",
                "tenant": tenant.full_name if tenant else "",
                "monthly_rent": str(lease.monthly_rent),
                "end_date": lease.end_date.isoformat() if lease.end_date else "",
            }
        )
    existing_categories = sorted(
        {row[0] for row in db.query(Expense.category).distinct().all() if row[0]}
    )
    categories = list(dict.fromkeys(existing_categories + STANDARD_CATEGORIES))
    return Catalog(
        units=unit_rows,
        leases=lease_rows,
        tenants=sorted({t.full_name for t in tenants.values() if t.full_name}),
        categories=categories,
        current_month=current_month,
    )


def _normalize_category(raw: str) -> str:
    """Map an LLM/user category synonym to a canonical label ("" when none)."""
    if not raw:
        return ""
    key = raw.strip().lower()
    if key in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[key]
    for canon in STANDARD_CATEGORIES:
        if canon in raw:
            return canon
    return ""


def _normalize_month(raw: str, current_month: str) -> str:
    if not raw:
        return ""
    text = str(raw).strip()
    lowered = text.lower()
    if lowered in ("这个月", "本月", "this month", "current month", "now"):
        return current_month
    if lowered in ("上个月", "last month"):
        y, m = current_month.split("-")
        m = int(m) - 1
        if m == 0:
            m, y = 12, int(y) - 1
        return f"{y:04d}-{m:02d}"
    match = re.search(r"(?:(\d{4})年?)?\s*(\d{1,2})\s*月?", text)
    if match:
        year = int(match.group(1)) if match.group(1) else int(current_month[:4])
        month = int(match.group(2))
        if 1 <= month <= 12:
            return f"{year:04d}-{month:02d}"
    match = re.search(r"(\d{4})-(\d{1,2})", text)
    if match:
        return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}"
    return ""


def _normalize_amount(raw) -> Optional[Decimal]:
    if raw is None or raw == "":
        return None
    try:
        value = Decimal(str(raw).replace(",", "").strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    if value <= 0 or value > MAX_AMOUNT or value != value.quantize(Decimal("0.01")):
        return None
    return value.quantize(Decimal("0.01"))


def _resolve_unit(raw: str, catalog: Catalog) -> tuple[str, Optional[int]]:
    if not raw:
        return "", None
    token = str(raw).strip()
    for u in catalog.units:
        if _unit_matches(token, u["unit_number"]):
            return u["unit_number"], u["id"]
    return token, None


def _validate(
    intent: str,
    raw_unit: str,
    raw_amount,
    raw_category: str,
    raw_month: str,
    catalog: Catalog,
) -> tuple[str, str, Optional[Decimal], str, str, list[str]]:
    """Deterministic validation of LLM-extracted entities.

    Returns (unit, category, amount, month, missing). Only real catalog units
    and canonical categories survive; bad amounts/months are dropped and the
    field is listed as missing so the bot can ask once instead of guessing.
    """
    missing: list[str] = []
    unit, unit_id = _resolve_unit(raw_unit, catalog)
    if raw_unit and not unit_id:
        missing.append("unit")
    category = _normalize_category(raw_category) if intent == INTENT_CREATE_EXPENSE else ""
    if intent == INTENT_CREATE_EXPENSE and not category:
        missing.append("category")
    amount = _normalize_amount(raw_amount)
    if raw_amount not in (None, "") and amount is None:
        missing.append("amount")
    if intent in (INTENT_CREATE_INCOME, INTENT_CREATE_EXPENSE, INTENT_QUERY) and amount is None:
        if intent == INTENT_CREATE_EXPENSE:
            missing.append("amount")
    month = _normalize_month(raw_month, catalog.current_month) if raw_month else ""
    if raw_month and not month:
        missing.append("month")
    return unit, category, amount, month, list(dict.fromkeys(missing))


def _build_messages(catalog: Catalog, text: str) -> list[dict]:
    system = (
        "You are the intent parser of a small rental-property Telegram bot. "
        "Return ONE JSON object with exactly these keys:\n"
        '{"intent": "create_income|create_expense|query|ask|ambiguous", '
        '"unit": "", "amount": null, "category": "", "month": "", '
        '"missing": [], "options": [], "message": ""}\n'
        "Decide by the USER'S ACTION, not by loose words:\n"
        "- create_expense: the user REPORTS PAYING A COST (支出/交了/付了/付掉/付清 "
        "+ a category like 水费/电费/维修/物业费/网费). The message must contain "
        "a category word; if it also contains a number, that number is the amount.\n"
        "- create_income: the user REPORTS RECEIVING RENT (收到/到了/入账/租金到账).\n"
        "- query: the user ASKS about data (谁/多少/吗/什么时候/哪些/快到期/查). "
        "Never create a record for a question.\n"
        "- ask: a general operational question that is not one of the above.\n"
        "- ambiguous: ONLY when both 'record' and 'query' are genuinely plausible "
        "(e.g. just a unit+category with no verb and no amount, like '1680水费'). "
        "NEVER return ambiguous for a message that already contains a category "
        "AND an amount, or an explicit expense/income verb.\n"
        "Rules:\n"
        "- unit MUST be the user's unit token only if it matches one of the real units below "
        "(suffix match is allowed, e.g. 1608 matches DEV-BAY-1608). Otherwise leave it empty.\n"
        "- amount: the money figure if clearly present (number only, no commas/currency). "
        "For create_expense the amount is REQUIRED; if absent put it in missing.\n"
        "- category: only from the category list below (Chinese label), or a standard synonym "
        "(water->水费, electricity->电费, maintenance->维修).\n"
        "- month: '这个月' -> the current month from the context; otherwise YYYY-MM or empty.\n"
        "- missing: required fields absent from the message (create_expense: category/amount; "
        "create_income: none). Never list a field the user provided.\n"
        "- message: short Chinese clarification or empty.\n"
        "Examples (catalog uses DEV-BAY-1608 / DEV-BAY-1680):\n"
        '- "支出1680水费2500" -> {"intent":"create_expense","unit":"1680","amount":2500,'
        '"category":"水费","missing":[],"message":""}\n'
        '- "1680刚交了2500水费" -> {"intent":"create_expense","unit":"1680","amount":2500,'
        '"category":"水费","missing":[]}\n'
        '- "付了1680电费3800" -> {"intent":"create_expense","unit":"1680","amount":3800,'
        '"category":"电费","missing":[]}\n'
        '- "1608收到30000" -> {"intent":"create_income","unit":"1608","amount":30000,'
        '"missing":[]}\n'
        '- "1680那边水费刚付掉" -> {"intent":"create_expense","unit":"1680",'
        '"category":"水费","missing":["amount"],"message":"知道了，是 1680 的水费。金额是多少？"}\n'
        '- "1680水费" -> {"intent":"ambiguous","unit":"1680","category":"水费",'
        '"options":["记录水费","查询1680记录"]}\n'
        '- "这个月收了多少钱" -> {"intent":"query"}\n'
        "Context (real catalog):\n"
        + json.dumps(
            {
                "units": catalog.units,
                "tenants": catalog.tenants,
                "categories": catalog.categories,
                "current_month": catalog.current_month,
            },
            ensure_ascii=False,
        )
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": text},
    ]


def _deterministic_fallback(text: str, catalog: Catalog) -> NlParseResult:
    """Provider-down classification: keyword lanes only, never a fabricated
    write. Ambiguous input asks for clarification with 2-3 choices."""
    lowered = text.lower()
    has_expense_cat = any(c in text for c in _EXPENSE_CATEGORY_CN)
    has_expense_word = any(w in text for w in _EXPENSE_WORDS_CN)
    has_rent_word = any(w in text for w in _RENT_WORDS_CN)
    has_rent_verb = any(w in text for w in _RENT_VERBS_CN)
    has_query = any(w in text for w in _QUERY_WORDS_CN) or bool(_QUERY_WORDS_EN.search(lowered))
    unit_m = _UNIT_TOKEN.search(text)
    unit_token = unit_m.group(1) if unit_m else ""
    unit, unit_id = _resolve_unit(unit_token, catalog)
    amount = None
    # Skip numbers inside the unit token ("1680" in "DEV-BAY-1680" / a bare
    # unit hint) so the real amount is picked (支出1680水费2500 -> 2500).
    for match in _AMOUNT_TOKEN.finditer(text):
        if unit_m is not None and unit_m.start() <= match.start() < unit_m.end():
            continue
        candidate = _normalize_amount(match.group(1))
        if candidate is not None:
            amount = candidate
            break
    category = next((c for c in _EXPENSE_CATEGORY_CN if c in text), "")

    if has_expense_cat or (has_expense_word and not has_rent_word):
        missing = []
        if not category:
            missing.append("category")
        if amount is None:
            missing.append("amount")
        return NlParseResult(
            intent=INTENT_CREATE_EXPENSE,
            message="知道了。请补充：金额是多少？" if missing else "",
            unit=unit, unit_id=unit_id, amount=amount,
            category=_normalize_category(category), missing=missing,
            fallback=True, flags=["provider_error", "deterministic_classify"],
        )
    if has_rent_word and has_rent_verb:
        return NlParseResult(
            intent=INTENT_CREATE_INCOME,
            unit=unit, unit_id=unit_id, amount=amount,
            fallback=True, flags=["provider_error", "deterministic_classify"],
        )
    if has_query:
        return NlParseResult(
            intent=INTENT_QUERY,
            fallback=True, flags=["provider_error", "deterministic_classify"],
        )
    return NlParseResult(
        intent=INTENT_AMBIGUOUS,
        message="你想做什么？可以直接告诉我：收到哪套房租金、支出了什么费用，或者想问什么。",
        options=["记录支出", "查询信息", "收入确认"],
        fallback=True, flags=["provider_error", "deterministic_classify"],
    )


def parse_nl_intent(
    db: Session,
    user: User,
    text: str,
    provider: Optional[str] = None,
    *,
    client: Optional[llm.LLMClient] = None,
    now=None,
) -> NlParseResult:
    """Parse one free-text message into a structured, validated intent."""
    started = time.monotonic()
    text = (text or "").strip()
    if not text:
        return NlParseResult(
            intent=INTENT_AMBIGUOUS,
            message="请告诉我你想做什么。",
            options=["记录支出", "查询信息", "收入确认"],
            fallback=True,
            flags=["empty_input"],
        )
    now = now if now is not None else clock.now()
    catalog = build_catalog(db, now=now)

    resolved_provider = provider or "deepseek-chat"
    try:
        if client is None:
            client = llm.get_llm_client(resolved_provider)
        result = client.complete(
            _build_messages(catalog, text),
            temperature=0.1,
            max_tokens=700,
            response_format={"type": "json_object"},
        )
    except (llm.LLMProviderError, ValueError, TypeError) as exc:
        parsed = _deterministic_fallback(text, catalog)
        return NlParseResult(
            intent=parsed.intent,
            message=parsed.message,
            unit=parsed.unit,
            unit_id=parsed.unit_id,
            amount=parsed.amount,
            category=parsed.category,
            missing=parsed.missing,
            options=parsed.options,
            provider=resolved_provider,
            fallback=True,
            flags=parsed.flags,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    try:
        data = parse_json_object(result.text)
    except (ValueError, TypeError) as exc:
        return NlParseResult(
            intent=INTENT_AMBIGUOUS,
            message="我还没理解你的意思，请换个说法，或者直接告诉我：收到哪套房租金、支出了什么费用。",
            options=["记录支出", "查询信息", "收入确认"],
            provider=result.provider,
            model=result.model,
            fallback=True,
            flags=["malformed_llm"],
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    raw_intent = str(data.get("intent") or "").strip().lower()
    valid_intents = {
        INTENT_CREATE_INCOME, INTENT_CREATE_EXPENSE, INTENT_QUERY,
        INTENT_ASK, INTENT_AMBIGUOUS,
    }
    if raw_intent not in valid_intents:
        raw_intent = INTENT_AMBIGUOUS

    unit, category, amount, month, missing = _validate(
        raw_intent,
        str(data.get("unit") or ""),
        data.get("amount"),
        str(data.get("category") or ""),
        str(data.get("month") or ""),
        catalog,
    )
    _, unit_id = _resolve_unit(unit, catalog) if unit else (unit, None)
    raw_message = str(data.get("message") or "").strip()[:300]
    if raw_intent == INTENT_CREATE_EXPENSE and missing and not raw_message:
        if "amount" in missing and "category" in missing:
            raw_message = "这笔支出还需要类别和金额。"
        elif "amount" in missing:
            raw_message = "知道了。金额是多少？"
        elif "category" in missing:
            raw_message = "这笔支出的类别是什么？"
    options = [str(o).strip() for o in (data.get("options") or []) if str(o).strip()][:3]
    return NlParseResult(
        intent=raw_intent,
        message=raw_message,
        unit=unit,
        unit_id=unit_id,
        amount=amount,
        category=category,
        month=month,
        missing=missing,
        options=options,
        provider=result.provider,
        model=result.model,
        fallback=False,
        flags=list(data.get("flags") or []) if isinstance(data.get("flags"), list) else [],
        latency_ms=int((time.monotonic() - started) * 1000),
    )
