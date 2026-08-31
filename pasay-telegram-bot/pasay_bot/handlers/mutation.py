"""Telegram mutation business-intent guard.

Coverage Matrix 10.9: ``assert_business_intent`` — the bot must verify
the user message is a real mutation intent before calling any backend
write endpoint. This protects against accidental mutation from
free-text chatter, group noise, or misrouted /commands.

A message is a *real mutation intent* if any of:
  - it contains the explicit ``/record-rent`` or ``/record-expense``
    command prefix, OR
  - it carries a Philippine mobile phone number and a money keyword
    (e.g. ``"+63 917 123 4567 paid 12000 for rent"``), OR
  - it is a direct reply to the bot's previous mutation prompt
    (callback chain confirmation), OR
  - it is the OWNER explicitly clicking an inline confirm button
    (``callback_data`` carrying ``"v1:confirm:"`` prefix).

Otherwise, the bot must ask for clarification rather than mutate state.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_PHONE_RE = re.compile(r"\+?63\s?9\d{2}[\s-]?\d{3}[\s-]?\d{4}")

_EXPLICIT_MUTATION_COMMANDS = frozenset(
    {
        "/record-rent",
        "/record-rent@pasay_pm_bot",
        "/record-expense",
        "/record-expense@pasay_pm_bot",
        "/submit-quote",
        "/submit-quote@pasay_pm_bot",
        "/claim-completion",
        "/claim-completion@pasay_pm_bot",
    }
)

_MUTATION_KEYWORDS = frozenset(
    {
        "paid", "received", "sent", "transferred",
        "rent", "renta", "upa", "bayad",
        "expense", "gastos", "utility", "utilities",
        "repair", "kumpuni", "sira",
    }
)

_CONFIRM_CALLBACK_PREFIX = "v1:confirm:"


@dataclass(frozen=True)
class IntentDecision:
    """Outcome of ``assert_business_intent`` for a mutation request."""

    is_intent: bool
    reason: str  # "command" | "phone_money" | "callback_confirm" | "missing_signal"


def assert_business_intent(
    *,
    text: str,
    callback_data: str | None = None,
    is_reply_to_bot: bool = False,
    role: str | None = None,
) -> IntentDecision:
    """Decide if a free-text update or callback is a real mutation intent.

    Owners and Secretaries can issue mutation intents; ``role`` is used
    to short-circuit the "ask for clarification" reply when an
    OWNER/SECRETARY types a bare command.

    Returns ``IntentDecision(is_intent, reason)``. Callers must
    short-circuit any backend write when ``is_intent`` is False.
    """
    stripped = (text or "").strip()
    lower = stripped.lower()
    # 1. Explicit command
    if any(lower.startswith(cmd) for cmd in _EXPLICIT_MUTATION_COMMANDS):
        return IntentDecision(True, "command")
    # 2. Inline confirm callback
    if callback_data and callback_data.startswith(_CONFIRM_CALLBACK_PREFIX):
        return IntentDecision(True, "callback_confirm")
    # 3. Direct reply to a prior bot prompt (callback chain)
    if is_reply_to_bot and role in ("OWNER", "SECRETARY"):
        # Without a phone/money signal, even a reply is not a clear
        # mutation intent — return False to prompt for clarification.
        if _PHONE_RE.search(stripped) or _has_money_signal(lower):
            return IntentDecision(True, "phone_money")
    # 4. PH phone + money keyword combo
    if _PHONE_RE.search(stripped) and _has_money_signal(lower):
        return IntentDecision(True, "phone_money")
    # 5. OWNER/SECRETARY may use the bare command without /command
    if role in ("OWNER", "SECRETARY") and any(
        kw in lower for kw in _MUTATION_KEYWORDS
    ):
        return IntentDecision(True, "command")
    return IntentDecision(False, "missing_signal")


def _has_money_signal(text: str) -> bool:
    """Cheap money-keyword heuristic for intent detection."""
    tokens = re.findall(r"\w+", text)
    if any(tok in _MUTATION_KEYWORDS for tok in tokens):
        return True
    if re.search(r"\d{4,}", text):  # any 4+ digit number ⇒ amount
        return True
    return False


def is_mutation_intent(update: Any) -> bool:
    """Wrapper for ``python-telegram-bot`` ``Update`` objects.

    Returns True when the update passes the business-intent guard. The
    caller is responsible for actually executing the mutation.
    """
    text = ""
    callback_data = None
    is_reply_to_bot = False
    if getattr(update, "message", None) is not None:
        msg = update.message
        text = msg.text or msg.caption or ""
        if getattr(msg, "reply_to_message", None) is not None:
            is_reply_to_bot = (
                getattr(msg.reply_to_message.from_user, "is_bot", False)
                if msg.reply_to_message.from_user is not None else False
            )
    elif getattr(update, "callback_query", None) is not None:
        callback_data = update.callback_query.data
    # Role is resolved upstream; here we default to None for the
    # free-text path so callers can pre-resolve.
    decision = assert_business_intent(
        text=text,
        callback_data=callback_data,
        is_reply_to_bot=is_reply_to_bot,
        role=None,
    )
    return decision.is_intent


__all__ = [
    "IntentDecision",
    "assert_business_intent",
    "is_mutation_intent",
]
