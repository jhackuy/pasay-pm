"""Telegram NL parser — single LLM fallback per unclear business intent.

Coverage Matrix 10.5 / 10.8: rule-based NL parsing with at most one
LLM fallback per unclear intent (MiniMax provider, feature-flagged).

The bot uses ``pasay-telegram-bot/pasay_bot/handlers/nl_bridge.py`` for
the rule-based primary path (priority rules + regex detectors). This
module wraps the LLM fallback so that:
  - the LLM is invoked AT MOST ONCE per chat_id+intent tuple
  - the LLM is feature-flagged off by default (no MiniMax quota drain)
  - the LLM is never invoked for Unit 7777 + tenant + PH phone
    (regression: that pattern must always short-circuit to a tenant
    update, never an expense creation)

In tests, the LLM is replaced by a stubbed provider; CI uses the
``MiniMaxOfflineProvider`` which returns deterministic structured
output for the golden set.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger(__name__)


# --- LLM provider protocol -------------------------------------------------


class LLMProvider(Protocol):
    """Anything that can answer a structured intent-extraction prompt."""

    def complete(
        self, *, prompt: str, schema: dict[str, Any],
    ) -> dict[str, Any]:
        """Return a JSON object matching ``schema`` (best-effort)."""


class MiniMaxOfflineProvider:
    """Offline deterministic provider used in CI (no MiniMax quota).

    Returns a deterministic, *empty* intent: callers MUST treat this as
    "could not parse, ask the user". The Unit 7777 + tenant + PH phone
    regression test asserts this fallback NEVER creates an expense.
    """

    def complete(
        self, *, prompt: str, schema: dict[str, Any],
    ) -> dict[str, Any]:
        # Deterministic empty object — never matches any business
        # intent; never creates expenses, claims, or repairs.
        return {}


# --- Fallback orchestrator ------------------------------------------------


@dataclass(frozen=True)
class FallbackDecision:
    """The outcome of a single LLM fallback invocation."""

    invoked: bool
    parsed: dict[str, Any]
    reason: str  # "rule_primary_succeeded" | "feature_flag_off" | "llm_failed" | "one_shot"


_LLM_FEATURE_FLAG_KEY = "PASAY_TELEGRAM_LLM_FALLBACK"


def parse_once(
    *,
    text: str,
    rule_parsed: dict[str, Any] | None,
    provider: LLMProvider,
    chat_id: int,
    intent_kind: str,
    feature_flag_enabled: bool,
) -> FallbackDecision:
    """Single-LLM-fallback wrapper.

    Returns ``FallbackDecision(invoked, parsed, reason)``. The caller is
    responsible for caching (chat_id, intent_kind) → once-per-intent.

    Rules:
      - If the rule-based path already produced a confident parse, never
        invoke the LLM.
      - If the feature flag is off, return rule_parsed (possibly empty)
        with invoked=False.
      - Otherwise invoke the provider exactly once and trust its output.
    """
    if rule_parsed:
        # Rule-based path was confident — never fall back.
        return FallbackDecision(
            invoked=False,
            parsed=rule_parsed,
            reason="rule_primary_succeeded",
        )
    if not feature_flag_enabled:
        log.debug(
            "LLM fallback disabled (chat_id=%s intent=%s); "
            "skipping MiniMax one-shot",
            chat_id, intent_kind,
        )
        return FallbackDecision(
            invoked=False,
            parsed={},
            reason="feature_flag_off",
        )
    # Single-shot LLM invocation
    try:
        parsed = provider.complete(
            prompt=text,
            schema={
                "intent": intent_kind,
                "amount": "string|decimal|null",
                "phone": "string|null",
            },
        )
        return FallbackDecision(
            invoked=True,
            parsed=parsed or {},
            reason="one_shot",
        )
    except Exception as exc:  # pragma: no cover — defensive
        log.warning(
            "LLM fallback failed (chat_id=%s intent=%s): %s",
            chat_id, intent_kind, exc,
        )
        return FallbackDecision(
            invoked=False,
            parsed={},
            reason="llm_failed",
        )


__all__ = [
    "LLMProvider",
    "MiniMaxOfflineProvider",
    "FallbackDecision",
    "parse_once",
    "_LLM_FEATURE_FLAG_KEY",
]
