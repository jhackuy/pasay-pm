"""Telegram group-chat silence middleware.

Coverage Matrix 10.6: ``GroupSilenceMiddleware.should_respond``.

A group chat is silent by default. The bot only speaks when:
  - the message is an explicit ``/start`` or ``/help`` invocation, OR
  - the message contains a real business signal (PH phone number,
    rent amount, expense keyword, or an explicit @-mention of the
    bot username), OR
  - the message is a direct reply to one of the bot's earlier messages
    (callback chain), OR
  - the message is from an OWNER/SECRETARY role (chatter is intentional).

Everything else is dropped silently — no echo, no reply, no error.

This module is consumed by both ``pasay-telegram-bot/pasay_bot/main.py``
(during integration) and the regression test
``pasay-telegram-bot/tests/test_v1_adapter_regressions.py`` to verify
the silence behaviour in CI without spinning up a real Telegram client.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

# --- signals that warrant a group response ---------------------------------


_PHONE_RE = re.compile(r"\+?63\s?9\d{2}[\s-]?\d{3}[\s-]?\d{4}")
_MONEY_RE = re.compile(
    r"\b(?:rent|paid|received|kwarta|halaga|due|balance|paid|received)\b.*?"
    r"(?:₱|php|peso|pesos|\$|\d{1,3}(?:[,]\d{3})+|\d{4,})",
    re.IGNORECASE,
)
# Recognized expense / business keywords (Taglish + English)
_BUSINESS_KEYWORDS = frozenset(
    {
        # Rent
        "rent", "renta", "upa", "bayad", "kaltas", "dues",
        # Expense
        "expense", "gastos", "utility", "utilities", "maintenance",
        "repair", "kumpuni", "sira",
        # Money movement
        "paid", "received", "sent", "transferred",
        # Acknowledgement
        "ok", "sige", "yes", "no", "wala", "meron",
    }
)
_GROUP_INVOKE_COMMANDS = frozenset({"/start", "/start@pasay_pm_bot", "/help"})


@dataclass(frozen=True)
class SilenceDecision:
    """The outcome of ``should_respond`` for a group-chat message."""

    should_respond: bool
    reason: str  # "explicit_command" | "business_signal" | "owner_intent" | "silence"


def should_respond(
    *,
    chat_type: str,
    text: str,
    role: str | None,
    is_reply_to_bot: bool = False,
    bot_username: str | None = None,
    extra_keywords: Iterable[str] = (),
) -> SilenceDecision:
    """Decide whether the bot should respond in a group chat.

    ``chat_type`` is the Telegram ``chat.type`` field — one of
    ``"private"``, ``"group"``, ``"supergroup"``, or ``"channel"``.

    ``text`` is the message body (after stripping any bot command
    prefix). ``role`` is the resolved role (``"OWNER"`` / ``"SECRETARY"``
    / ``None``) — used to short-circuit silence for the OWNER's own
    chatter. ``is_reply_to_bot`` is True when the message is a direct
    reply to one of the bot's earlier messages.

    ``extra_keywords`` extends the recognized business keyword set —
    useful for tests that inject domain-specific signals.
    """
    if chat_type != "group" and chat_type != "supergroup":
        # Private DMs always respond (1×1 UX, no silence needed).
        return SilenceDecision(True, "explicit_command")
    stripped = (text or "").strip()
    lower = stripped.lower()
    keywords = _BUSINESS_KEYWORDS | frozenset(extra_keywords)
    # 1. Explicit command
    if any(lower.startswith(cmd) for cmd in _GROUP_INVOKE_COMMANDS):
        return SilenceDecision(True, "explicit_command")
    # 2. Direct reply to bot
    if is_reply_to_bot:
        return SilenceDecision(True, "explicit_command")
    # 3. Bot mention (e.g. "@pasay_pm_bot")
    if (
        bot_username
        and f"@{bot_username.lower()}" in lower
    ):
        return SilenceDecision(True, "explicit_command")
    # 4. Owner/Secretary intent — intentional chatter from authorized users
    if role in ("OWNER", "SECRETARY"):
        return SilenceDecision(True, "owner_intent")
    # 5. Business signal: PH phone number or money keyword
    if _PHONE_RE.search(stripped):
        return SilenceDecision(True, "business_signal")
    if _MONEY_RE.search(stripped):
        return SilenceDecision(True, "business_signal")
    tokens = re.findall(r"\w+", lower)
    if any(tok in keywords for tok in tokens):
        return SilenceDecision(True, "business_signal")
    # 6. Otherwise — silence.
    return SilenceDecision(False, "silence")


def is_silent(
    *,
    chat_type: str,
    text: str,
    role: str | None,
    is_reply_to_bot: bool = False,
    bot_username: str | None = None,
) -> bool:
    """Convenience boolean wrapper around ``should_respond``."""
    return not should_respond(
        chat_type=chat_type,
        text=text,
        role=role,
        is_reply_to_bot=is_reply_to_bot,
        bot_username=bot_username,
    ).should_respond


__all__ = [
    "SilenceDecision",
    "should_respond",
    "is_silent",
    "PHONE_RE",
]
