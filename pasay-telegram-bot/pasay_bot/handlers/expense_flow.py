"""Deterministic expense entry flow (BOT-V1-USABLE-001 P0-2).

Natural-language expense statements ("支出1680水费2500", "1680刚交了2500水费",
"付了1680电费3800") are parsed deterministically into a confirmation card;
only the [提交审批] tap creates the PENDING expense through the existing
backend service. Owner approval/rejection stays the existing deterministic
callback path — no LLM is ever involved in a button.

The parser runs BEFORE the rent-statement parser so expense phrases that also
contain payment verbs ("付了1680电费3800") never fall into the rent matcher.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes

from pasay_bot.api_client import (
    PasayApiConflictError,
    PasayApiError,
    PasayApiPermissionError,
    PasayApiTimeoutError,
)
from pasay_bot.handlers.edit_utils import edit_message_text_idempotent
from pasay_bot.keyboards import (
    expense_approval_keyboard,
    expense_confirm_keyboard,
    home_keyboard,
    new_nonce,
    now_ts,
)
from pasay_bot.render import cards
from pasay_bot.render import html as H
from pasay_bot.render.i18n import t
from pasay_bot.roles import (
    Role,
    has_read_permission,
    telegram_id_for_role,
)

HTML = "HTML"
MAX_AMOUNT = Decimal("999999999999.99")

# P1-PASAY-NIGHTLY-PRODUCT-HARDENING-008 A2: placeholder/empty text is never a
# meaningful expense identity. Mirrors the backend schema validator so the bot
# refuses to create a meaningless expense even if a future path forgets.
_PLACEHOLDER_LABELS = {"??", "?", "--", "none", "null", "n/a", "na", "unknown"}


def is_meaningful_label(value) -> bool:
    """True when the text is a real human label (not empty/whitespace/placeholder)."""
    text = " ".join(str(value or "").split())
    if not text:
        return False
    return text.lower() not in _PLACEHOLDER_LABELS

# Canonical expense categories + aliases (zh/en). The backend accepts any
# label; canonical Chinese labels keep cards consistent across roles.
CATEGORY_ALIASES: dict[str, str] = {
    "水费": "水费", "水": "水费", "water": "水费", "water bill": "水费", "waterbill": "水费",
    "电费": "电费", "电": "电费", "electricity": "电费", "electric": "电费", "electric bill": "电费",
    "物业费": "物业费", "管理费": "物业费", "物业": "物业费", "association": "物业费", "association fee": "物业费",
    "维修": "维修", "维修费": "维修", "maintenance": "维修", "repair": "维修", "fix": "维修",
    "网络费": "网络费", "网费": "网络费", "internet": "网络费", "wifi": "网络费", "broadband": "网络费",
    "保洁": "保洁", "清洁": "保洁", "cleaning": "保洁", "cleaner": "保洁",
    "燃气费": "燃气费", "燃气": "燃气费", "gas": "燃气费",
    "税费": "税费", "税": "税费", "tax": "税费", "taxes": "税费",
    "其他": "其他", "other": "其他", "杂费": "其他", "misc": "其他",
}
_CATEGORY_PATTERN = re.compile("|".join(re.escape(k) for k in sorted(CATEGORY_ALIASES, key=len, reverse=True)), re.IGNORECASE)

_EXPENSE_VERBS = ("支出", "花了", "交了", "付了", "付款", "缴了", "付掉", "付费", "付的", "缴纳")
_QUERY_SUFFIX = re.compile(r"(吗|么|多少|多少钱|？|\?|没有|没|查|哪些|是什么)\s*$")
_UNIT_TOKEN = re.compile(
    r"(?<![A-Za-z0-9-])("
    r"(?:[A-Za-z]+-)+[A-Za-z]*\d{1,4}[A-Za-z]?"
    r"|[A-Za-z]?\d{1,4}[A-Za-z]?"
    r")(?![A-Za-z0-9-])"
)
_NUMBER = re.compile(r"(?<![\d.])(\d[\d,]*(?:\.\d+)?)(?![\d])")
_FULL_DATE = re.compile(r"(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})日?")
_ISO_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")


@dataclass(frozen=True)
class ExpenseStatement:
    category: str
    unit_token: str = ""
    amount: Optional[Decimal] = None
    expense_date: str = field(default_factory=lambda: date.today().isoformat())


def normalize_category(raw: str) -> str:
    if not raw:
        return ""
    match = _CATEGORY_PATTERN.search(raw)
    if not match:
        return ""
    return CATEGORY_ALIASES.get(match.group(0).lower(), match.group(0))


def parse_expense_date(text: str) -> str:
    """昨天/今天 keywords or explicit dates -> ISO date."""
    if "大前天" in text:
        return (date.today() - timedelta(days=3)).isoformat()
    if "前天" in text:
        return (date.today() - timedelta(days=2)).isoformat()
    if "昨天" in text:
        return (date.today() - timedelta(days=1)).isoformat()
    if "今天" in text or "今日" in text:
        return date.today().isoformat()
    for pattern in (_FULL_DATE, _ISO_DATE):
        match = pattern.search(text)
        if match:
            try:
                return date(
                    int(match.group(1)), int(match.group(2)), int(match.group(3))
                ).isoformat()
            except ValueError:
                break
    return date.today().isoformat()


def extract_amounts(text: str) -> list[Decimal]:
    values: list[Decimal] = []
    for match in _NUMBER.finditer(text):
        try:
            value = Decimal(match.group(1).replace(",", ""))
        except InvalidOperation:
            continue
        if value >= 100 and value <= MAX_AMOUNT:
            values.append(value)
    return values


def parse_expense_statement(text: str) -> Optional[ExpenseStatement]:
    """Deterministic expense-statement parser (never a query, never /help)."""
    raw = (text or "").strip()
    if not raw or _QUERY_SUFFIX.search(raw):
        return None
    category = normalize_category(raw)
    if not category:
        return None
    has_verb = any(w in raw for w in _EXPENSE_VERBS)
    unit_m = _UNIT_TOKEN.search(raw)
    unit_token = unit_m.group(1) if unit_m else ""
    if unit_m:
        # Numbers inside the unit token ("1680" in "DEV-BAY-1680") are unit
        # hints, never amounts; the real amount is the remaining number.
        candidates = []
        for match in _NUMBER.finditer(raw):
            if unit_m.start() <= match.start() < unit_m.end():
                continue
            try:
                value = Decimal(match.group(1).replace(",", ""))
            except InvalidOperation:
                continue
            if 100 <= value <= MAX_AMOUNT:
                candidates.append(value)
        amounts = candidates
    else:
        amounts = extract_amounts(raw)
    # A bare "1680水费" (category + unit, no verb/amount) is genuinely
    # ambiguous -> falls through to the AI lane which offers 2-3 choices.
    if not has_verb and not amounts:
        return None
    amount = amounts[0] if amounts else None
    return ExpenseStatement(
        category=category,
        unit_token=unit_token,
        amount=amount,
        expense_date=parse_expense_date(raw),
    )


def detect_expense_statement(text: str) -> bool:
    return parse_expense_statement(text) is not None


def detect_expense_ambiguity(text: str) -> Optional[ExpenseStatement]:
    """'1680水费' (unit + category, no verb/amount/query words) is genuinely
    ambiguous: record vs query. Returns the parsed statement so the bot can
    offer exactly 2-3 explicit choices (spec P0-5), without an LLM."""
    raw = (text or "").strip()
    if not raw or _QUERY_SUFFIX.search(raw):
        return None
    if any(w in raw for w in _EXPENSE_VERBS):
        return None
    category = normalize_category(raw)
    if not category:
        return None
    unit_m = _UNIT_TOKEN.search(raw)
    if unit_m is None:
        return None
    unit_token = unit_m.group(1)
    # Only the unit token may carry digits; a second number is an amount and
    # therefore a statement, not an ambiguity.
    numbers = [m for m in _NUMBER.finditer(raw) if not (unit_m.start() <= m.start() < unit_m.end())]
    if numbers:
        return None
    if any(w in raw for w in ("记录", "查", "多少", "有", "吗", "记", "帮", "请")):
        return None
    # The message must be essentially unit + category only ("1680水费",
    # "1680 的水费"). Anything else ("帮我记一笔1680的水费") is a statement.
    remainder = raw[:unit_m.start()] + raw[unit_m.end():]
    remainder = remainder.replace(category, "")
    remainder = re.sub(r"[\s,，。、的]+", "", remainder)
    if remainder:
        return None
    return ExpenseStatement(
        category=category, unit_token=unit_token,
        expense_date=date.today().isoformat(),
    )


def unit_matches(token: str, unit_number: str) -> bool:
    """Same normalized unit-number match used by the rent matcher."""
    tok = (token or "").lower().strip().rstrip(".,;:!?")
    unit = (unit_number or "").lower().strip()
    if not tok or not unit:
        return False
    if tok == unit:
        return True
    if unit.endswith(tok) and not unit[len(unit) - len(tok) - 1].isdigit():
        return True
    if tok.endswith(unit) and not tok[len(tok) - len(unit) - 1].isdigit():
        return True
    return False


async def resolve_unit(context, token: str):
    """Resolve a user unit token to (unit_id, unit_number, property_name);
    (None, token, "") when no unit matches."""
    if not token:
        return None, token, ""
    api = context.bot_data["api_client"]
    try:
        units, properties = await asyncio_gather_units(api)
    except PasayApiError:
        return None, token, ""
    by_prop = {p.id: p.name for p in properties}
    for unit in units:
        if unit_matches(token, unit.unit_number):
            return unit.id, unit.unit_number, by_prop.get(unit.property_id, "")
    return None, token, ""


async def asyncio_gather_units(api):
    import asyncio
    units, properties = await asyncio.gather(api.get_units(), api.get_properties())
    return units, properties


async def handle_expense_statement(update, context, statement, role, locale):
    """Deterministic expense statement -> confirmation card (P0-2)."""
    if not has_read_permission(role):
        await context.bot.send_message(
            update.effective_chat.id,
            H.escape(t("common.no_permission", locale)),
            parse_mode=HTML,
        )
        return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    unit_id, unit_number, property_name = await resolve_unit(context, statement.unit_token)
    if statement.unit_token and unit_id is None:
        # Unknown unit token: explain, never guess.
        await context.bot.send_message(
            chat_id,
            H.escape(t("expense.unit_not_found", locale, unit=statement.unit_token)),
            parse_mode=HTML,
            reply_markup=home_keyboard(locale),
        )
        return
    payload = {
        "unit_id": unit_id,
        "unit_number": unit_number or statement.unit_token or "",
        "property_name": property_name,
        "category": statement.category,
        "amount": str(statement.amount) if statement.amount is not None else "",
        "expense_date": statement.expense_date,
        "payee": "",
        "description": "",
    }
    if statement.amount is None:
        # Category + unit known, amount missing -> ask once (never /help).
        store = context.bot_data["store"]
        store.save_conversation(chat_id, user_id, "expense_edit_amount", payload)
        await context.bot.send_message(
            chat_id,
            H.escape(t(
                "ai.ask_amount", locale,
                unit=payload["unit_number"] or "该房源",
                category=payload["category"],
            )),
            parse_mode=HTML,
        )
        return
    await render_expense_confirm(update, context, payload, role, locale)


async def render_expense_confirm(
    update, context, payload, role, locale, *, message_id: Optional[int] = None,
):
    """Render (or edit) the expense confirmation card + deterministic buttons."""
    store = context.bot_data["store"]
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    nonce = new_nonce()
    ts = now_ts()
    store.save_conversation(
        chat_id, user_id, "expense_confirm", payload, nonce=nonce,
    )
    text = cards.expense_confirm_card(
        unit_number=payload.get("unit_number") or "",
        property_name=payload.get("property_name") or "",
        category=payload.get("category") or "",
        amount=payload.get("amount") or "0",
        expense_date=payload.get("expense_date") or date.today().isoformat(),
        locale=locale,
    )
    if message_id:
        await edit_message_text_idempotent(
            context.bot,
            chat_id=chat_id,
            message_id=message_id,
            text=H.truncate(text),
            parse_mode=HTML,
            reply_markup=expense_confirm_keyboard(nonce, ts, locale),
        )
        payload["confirm_message_id"] = int(message_id)
        store.save_conversation(
            chat_id, user_id, "expense_confirm", payload, nonce=nonce,
        )
    else:
        sent = await context.bot.send_message(
            chat_id,
            H.truncate(text),
            parse_mode=HTML,
            reply_markup=expense_confirm_keyboard(nonce, ts, locale),
        )
        payload["confirm_message_id"] = int(sent.message_id)
        store.save_conversation(
            chat_id, user_id, "expense_confirm", payload, nonce=nonce,
        )


async def submit_expense(update, context, payload, role, locale):
    """[提交审批] tap: create ONE PENDING expense (idempotent), then hand the
    Owner the existing deterministic approval card (action-at-source)."""
    store = context.bot_data["store"]
    guard = context.bot_data["idempotency"]
    api = context.bot_data["api_client"]
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    unit_id = payload.get("unit_id") or None
    unit_number = payload.get("unit_number") or ""
    category = payload.get("category") or ""
    amount = payload.get("amount") or "0"
    expense_date = payload.get("expense_date") or date.today().isoformat()
    if not is_meaningful_label(category):
        # A2: never create an expense whose displayed identity would be a
        # placeholder (`??`, `-`, empty). Ask for the category instead of
        # booking a meaningless record.
        store.save_conversation(
            chat_id, user_id, "expense_edit_category", payload,
        )
        await context.bot.send_message(
            chat_id,
            H.escape(t("expense.ask_category", locale)),
            parse_mode=HTML,
        )
        return
    try:
        amount_dec = Decimal(str(amount))
    except InvalidOperation:
        await context.bot.send_message(
            chat_id, H.escape(t("expense.amount_invalid", locale)),
            parse_mode=HTML, reply_markup=home_keyboard(locale),
        )
        return
    key = f"ik:exp:{unit_id or 0}:{category}:{amount}:{expense_date}"
    status = guard.acquire(key, kind="expense", resource="")
    if status in ("done", "in_flight"):
        await context.bot.send_message(
            chat_id, H.escape(t("expense.already_processed", locale)),
            parse_mode=HTML, reply_markup=home_keyboard(locale),
        )
        return

    try:
        expense = await api.create_expense(
            category=category,
            amount=amount_dec,
            expense_date=expense_date,
            unit_id=unit_id,
            payee=payload.get("payee") or "-",
            description=(payload.get("description") or "").strip() or None,
            status="pending",
        )
        guard.settle(key, expense.as_dict(), resource=str(expense.id))
    except PasayApiConflictError:
        await context.bot.send_message(
            chat_id, H.escape(t("expense.already_processed", locale)),
            parse_mode=HTML, reply_markup=home_keyboard(locale),
        )
        return
    except PasayApiTimeoutError:
        guard.fail(key)
        await context.bot.send_message(
            chat_id, H.escape(t("common.timeout", locale)),
            parse_mode=HTML, reply_markup=home_keyboard(locale),
        )
        return
    except PasayApiPermissionError:
        guard.fail(key)
        await context.bot.send_message(
            chat_id, H.escape(t("common.no_permission", locale)),
            parse_mode=HTML, reply_markup=home_keyboard(locale),
        )
        return
    except PasayApiError:
        guard.fail(key)
        await context.bot.send_message(
            chat_id, H.escape(t("common.unexpected", locale)),
            parse_mode=HTML, reply_markup=home_keyboard(locale),
        )
        return

    store.delete_conversation(chat_id, user_id)
    submitted = H.truncate(cards.expense_submitted_card(
        unit_number=unit_number,
        property_name=payload.get("property_name") or "",
        category=category,
        amount=amount,
        locale=locale,
    ))
    try:
        tapped_message_id = update.callback_query.message.message_id
    except Exception:  # noqa: BLE001 - callback-only path; fall back to send
        tapped_message_id = None
    if tapped_message_id:
        try:
            await edit_message_text_idempotent(
                context.bot,
                chat_id=chat_id,
                message_id=tapped_message_id,
                text=submitted,
                parse_mode=HTML,
                reply_markup=home_keyboard(locale),
            )
        except Exception:  # noqa: BLE001 - fallback must never lose feedback
            await context.bot.send_message(
                chat_id, submitted, parse_mode=HTML,
                reply_markup=home_keyboard(locale),
            )
    else:
        await context.bot.send_message(
            chat_id, submitted, parse_mode=HTML,
            reply_markup=home_keyboard(locale),
        )
    await _send_owner_approval_card(context, expense, locale)


async def _send_owner_approval_card(context, expense, locale):
    """Action-at-source approval card to the Owner's private chat. The
    approve/reject callbacks are the existing deterministic paths."""
    owner_chat_id = telegram_id_for_role(Role.OWNER)
    if owner_chat_id is None:
        return
    try:
        units, properties = await asyncio_gather_units(context.bot_data["api_client"])
    except PasayApiError:
        units, properties = [], []
    location = ""
    if expense.unit_id:
        unit = next((u for u in units if u.id == expense.unit_id), None)
        if unit is not None:
            prop = next((p for p in properties if p.id == unit.property_id), None)
            location = " · ".join(
                x for x in ((prop.name if prop else ""), unit.unit_number) if x
            )
    text = cards.expense_approval_card(
        expense, "zh", location=location,
    )
    await context.bot.send_message(
        owner_chat_id,
        H.truncate(text),
        parse_mode=HTML,
        reply_markup=expense_approval_keyboard(
            expense.id, "zh", has_receipt=bool(expense.receipt_attachment_id),
        ),
    )
