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
_RECEIVE_VERBS_CN = ("收到", "到了", "到账", "已收", "入账", "收款")
_RECEIVE_VERB_EN = re.compile(r"\b(received|paid)\b")
_AMOUNT_IN_TEXT = re.compile(r"\d[\d,]{4,}")
_QUERY_SUFFIX = re.compile(r"(吗|么|？|\?|没有|没)\s*$")

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
_UNIT_TOKEN = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]?\d{1,4}[A-Za-z]?)(?![A-Za-z0-9])")
_NAME_TOKEN = re.compile(r"\b([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)?)\b")
_QUERY_VERB_CN = (
    "交了没有", "交了没", "交了吗", "交了么", "交没交", "有没有交",
    "还欠多少", "欠多少", "欠多少钱", "没交吗", "没交钱吗", "交钱了吗",
    "付了吗", "付了没有", "还没交", "没交租", "没交租金", "交了钱吗",
)
_QUERY_VERB_EN = re.compile(
    r"\b(?:has|hasn't|has not|hasnt|did|didn't|did not|didnt|does)\b.{0,24}\b(?:paid|pay)\b"
    r"|\bhow\s+much\b.{0,24}\bowe[sd]?\b"
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
    raw = (text or "").strip()
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
    (("财务", "finance", "summary", "收支", "报表"), "finance"),
    (("逾期", "overdue", "欠租", "overdue rent"), "overdue"),
    (("收租", "rent", "登记"), "rent"),
    (("待处理", "pending", "待办", "todo", "tasks", "待确认"), "pending"),
    (("更多", "more"), "more"),
    (("菜单", "menu", "主菜单", "home", "start"), "menu"),
    (("帮助", "help", "帮助"), "help"),
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
    has_rent_word = any(w in raw for w in _RENT_WORDS_CN) or bool(_RENT_WORD_EN.search(lowered))
    has_verb = any(v in raw for v in _RECEIVE_VERBS_CN) or bool(_RECEIVE_VERB_EN.search(lowered))
    if has_rent_word and has_verb:
        return True
    return has_verb and bool(_AMOUNT_IN_TEXT.search(lowered))


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
    if route in ("properties", "finance", "overdue", "rent", "pending", "menu", "more"):
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
            await _answer_unit_status(context, chat_id, query.unit_token, locale)
        else:
            await _answer_tenant_status(context, chat_id, query.tenant_token, locale)
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


async def _answer_unit_status(context, chat_id, unit_token, locale):
    """'1608 交了没有 / 还欠多少' — exact unit-number match, then a
    paid/unpaid + owed + overdue answer from the existing read endpoints."""
    api = context.bot_data["api_client"]
    units, leases, tenants, incomes, overdue, properties = await asyncio.gather(
        api.get_units(),
        api.get_leases(),
        api.get_tenants(),
        api.list_incomes(),
        api.get_overdue_rents(),
        api.get_properties(),
    )
    unit = next(
        (u for u in units if u.unit_number.lower() == unit_token.lower()), None
    )
    if unit is None:
        await context.bot.send_message(
            chat_id,
            H.escape(t("rent_status.no_unit", locale, unit=unit_token)),
            parse_mode=HTML,
        )
        return
    lease = next(
        (l for l in leases if l.unit_id == unit.id and l.status == "active"), None
    )
    if lease is None:
        await context.bot.send_message(
            chat_id,
            H.escape(t("rent_status.no_active_lease", locale)),
            parse_mode=HTML,
        )
        return
    month = pages._current_month()
    paid = pages._period_covered(incomes, lease.id, month)
    ovd = next((r for r in overdue if r.lease_id == lease.id), None)
    outstanding = (
        ovd.total_outstanding
        if ovd
        else (Decimal("0") if paid else lease.monthly_rent)
    )
    tenant = next((tn for tn in tenants if tn.id == lease.tenant_id), None)
    prop = next((p for p in properties if p.id == unit.property_id), None)
    await context.bot.send_message(
        chat_id,
        H.truncate(
            cards.rent_status_card(
                locale=locale,
                unit_number=unit.unit_number,
                property_name=prop.name if prop else "",
                tenant_name=tenant.full_name if tenant else "",
                monthly_rent=lease.monthly_rent,
                paid=paid,
                outstanding=outstanding,
                overdue_days=ovd.overdue_days if ovd else 0,
                overdue_months=ovd.overdue_months if ovd else 0,
                month=month,
            )
        ),
        parse_mode=HTML,
    )


async def _answer_tenant_status(context, chat_id, tenant_token, locale):
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
    month = pages._current_month()
    candidates: list[dict] = []
    for tn in matched:
        for lease in (
            l for l in leases if l.tenant_id == tn.id and l.status == "active"
        ):
            unit = next((u for u in units if u.id == lease.unit_id), None)
            prop = (
                next((p for p in properties if p.id == unit.property_id), None)
                if unit
                else None
            )
            paid = pages._period_covered(incomes, lease.id, month)
            ovd = next((r for r in overdue if r.lease_id == lease.id), None)
            candidates.append(
                {
                    "tenant_name": tn.full_name,
                    "unit_number": unit.unit_number if unit else "",
                    "property_name": prop.name if prop else "",
                    "monthly_rent": lease.monthly_rent,
                    "paid": paid,
                    "outstanding": (
                        ovd.total_outstanding
                        if ovd
                        else (Decimal("0") if paid else lease.monthly_rent)
                    ),
                    "overdue_days": ovd.overdue_days if ovd else 0,
                    "overdue_months": ovd.overdue_months if ovd else 0,
                    "month": month,
                }
            )
    if not candidates:
        await context.bot.send_message(
            chat_id,
            H.escape(t("rent_status.no_active_lease_tenant", locale)),
            parse_mode=HTML,
        )
        return
    if len(candidates) == 1:
        c = candidates[0]
        await context.bot.send_message(
            chat_id,
            H.truncate(
                cards.rent_status_card(
                    locale=locale,
                    unit_number=c["unit_number"],
                    property_name=c["property_name"],
                    tenant_name=c["tenant_name"],
                    monthly_rent=c["monthly_rent"],
                    paid=c["paid"],
                    outstanding=c["outstanding"],
                    overdue_days=c["overdue_days"],
                    overdue_months=c["overdue_months"],
                    month=c["month"],
                )
            ),
            parse_mode=HTML,
        )
        return
    await context.bot.send_message(
        chat_id,
        H.truncate(cards.tenant_candidates_card(candidates, locale)),
        parse_mode=HTML,
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
        result = await api.match_rent_payment(text)
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
    # Deterministic business-fact key: identical duplicate reports (and
    # Telegram redeliveries) share one backend idempotency_key, so a race can
    # never produce a second pending income.
    key = f"ik:sec:{candidate.lease_id}:{period}:{received}:{candidate.amount}"
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
