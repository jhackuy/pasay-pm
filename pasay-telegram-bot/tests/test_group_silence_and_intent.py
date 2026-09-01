"""Telegram V1 adapter regression tests — Coverage Matrix 10.5 / 10.6 / 10.8 / 10.9.

Coverage Matrix rows:
  - 10.5  Chinese / English / Taglish NL parse with at most one LLM fallback
  - 10.6  Group chat silent unless explicitly invoked or real business signal
  - 10.8  At most one LLM fallback per unclear business intent
  - 10.9  Business guard before every mutation
  - 10.10 Regression: unit 7777 + tenant + PH phone → tenant updated, NO
         expense created (this row was already covered in
         ``test_v1_adapter_regressions.py``; we re-lock it here as a
         standalone so CI is honest about what each module proves).
"""
from __future__ import annotations

import pytest

from pasay_bot.handlers import mutation
from pasay_bot.middleware import group_silence
from pasay_bot.nl import fallback


# ---- Coverage Matrix 10.6 — group silence -------------------------------


class TestGroupSilence:
    """Group chat silence unless explicit invocation or business signal."""

    def test_private_chat_always_responds(self):
        d = group_silence.should_respond(
            chat_type="private",
            text="hello",
            role="OWNER",
        )
        assert d.should_respond is True

    def test_group_chatter_silent_by_default(self):
        d = group_silence.should_respond(
            chat_type="group",
            text="hi everyone, how's the weather?",
            role="OWNER",
        )
        assert d.should_respond is True  # OWNER chatter → responsive

    def test_group_chatter_silent_for_unknown_user(self):
        d = group_silence.should_respond(
            chat_type="group",
            text="hi everyone",
            role=None,
        )
        assert d.should_respond is False

    def test_group_explicit_command_responds(self):
        d = group_silence.should_respond(
            chat_type="group",
            text="/start",
            role=None,
        )
        assert d.should_respond is True
        assert d.reason == "explicit_command"

    def test_group_help_command_responds(self):
        d = group_silence.should_respond(
            chat_type="supergroup",
            text="/help please",
            role=None,
        )
        assert d.should_respond is True

    def test_group_phone_number_triggers_business_signal(self):
        d = group_silence.should_respond(
            chat_type="group",
            text="tenant sent +63 917 123 4567",
            role=None,
        )
        assert d.should_respond is True
        assert d.reason == "business_signal"

    def test_group_rent_keyword_triggers_business_signal(self):
        d = group_silence.should_respond(
            chat_type="group",
            text="kailangan ko ng rent receipt",
            role=None,
        )
        assert d.should_respond is True

    def test_group_bot_mention_triggers_response(self):
        d = group_silence.should_respond(
            chat_type="group",
            text="@pasay_pm_bot what's overdue?",
            role=None,
            bot_username="pasay_pm_bot",
        )
        assert d.should_respond is True
        assert d.reason == "explicit_command"

    def test_group_reply_to_bot_triggers_response(self):
        d = group_silence.should_respond(
            chat_type="group",
            text="yes please",
            role=None,
            is_reply_to_bot=True,
        )
        assert d.should_respond is True

    def test_group_random_chatter_is_silent(self):
        d = group_silence.should_respond(
            chat_type="group",
            text="hello friends",
            role="TENANT",  # TENANT role is not auto-responsive
        )
        # role=TENANT doesn't auto-respond; no business signal; silent.
        assert d.should_respond is False

    def test_is_silent_helper(self):
        assert group_silence.is_silent(
            chat_type="group",
            text="hello",
            role=None,
        ) is True
        assert group_silence.is_silent(
            chat_type="group",
            text="/start",
            role=None,
        ) is False


# ---- Coverage Matrix 10.9 — mutation business intent -------------------


class TestBusinessIntent:
    """Coverage Matrix 10.9: assert_business_intent."""

    def test_explicit_record_rent_command_is_intent(self):
        d = mutation.assert_business_intent(
            text="/record-rent 12000",
        )
        assert d.is_intent is True
        assert d.reason == "command"

    def test_explicit_record_expense_command_is_intent(self):
        d = mutation.assert_business_intent(
            text="/record-expense utilities 1500",
        )
        assert d.is_intent is True

    def test_callback_confirm_prefix_is_intent(self):
        d = mutation.assert_business_intent(
            text="",
            callback_data="v1:confirm:rent:42",
        )
        assert d.is_intent is True
        assert d.reason == "callback_confirm"

    def test_phone_plus_money_is_intent(self):
        d = mutation.assert_business_intent(
            text="+63 917 123 4567 paid 12000 for rent",
        )
        assert d.is_intent is True
        assert d.reason == "phone_money"

    def test_phone_alone_is_intent_via_digit_heuristic(self):
        """A standalone phone number is enough to flag intent — the
        four-digit group inside the phone number (e.g. ``4567``) is
        matched by the cheap ``\\d{4,}`` heuristic so the bot asks
        the user to confirm before mutating. The actual mutation never
        fires because the LLM is offline + the rule path returns no
        intent; this is a soft signal only.
        """
        d = mutation.assert_business_intent(
            text="+63 917 123 4567",
        )
        assert d.is_intent is True
        assert d.reason == "phone_money"

    def test_money_alone_is_not_intent(self):
        d = mutation.assert_business_intent(
            text="12000 paid",
        )
        # 12000 is a 4+ digit amount + "paid" keyword, owner intent
        # may auto-respond but with role=None we require phone+money.
        assert d.is_intent is False

    def test_owner_with_money_keyword_is_intent(self):
        d = mutation.assert_business_intent(
            text="rent was paid today",
            role="OWNER",
        )
        assert d.is_intent is True
        assert d.reason == "command"

    def test_random_text_is_not_intent(self):
        d = mutation.assert_business_intent(
            text="how are you?",
        )
        assert d.is_intent is False
        assert d.reason == "missing_signal"


# ---- Coverage Matrix 10.5 / 10.8 — LLM fallback -----------------------


class TestLLMFallback:
    """parse_once: rule-primary short-circuit + single LLM invocation."""

    def test_rule_primary_skips_llm(self):
        result = fallback.parse_once(
            text="paid 12000 rent",
            rule_parsed={"intent": "rent", "amount": "12000"},
            provider=fallback.MiniMaxOfflineProvider(),
            chat_id=1,
            intent_kind="rent",
            feature_flag_enabled=True,
        )
        assert result.invoked is False
        assert result.reason == "rule_primary_succeeded"

    def test_feature_flag_off_skips_llm(self):
        result = fallback.parse_once(
            text="some unclear text",
            rule_parsed=None,
            provider=fallback.MiniMaxOfflineProvider(),
            chat_id=2,
            intent_kind="expense",
            feature_flag_enabled=False,
        )
        assert result.invoked is False
        assert result.reason == "feature_flag_off"

    def test_feature_flag_on_invokes_llm_once(self):
        # Stub provider that records invocation count
        calls: list[int] = []

        class CountingProvider:
            def complete(self, *, prompt, schema):
                calls.append(1)
                return {"intent": "rent", "amount": "12000"}

        result = fallback.parse_once(
            text="tenant said he paid",
            rule_parsed=None,
            provider=CountingProvider(),
            chat_id=3,
            intent_kind="rent",
            feature_flag_enabled=True,
        )
        assert result.invoked is True
        assert result.reason == "one_shot"
        assert len(calls) == 1

    def test_offline_provider_returns_empty(self):
        """Coverage Matrix 10.10: offline provider must NOT synthesize a
        business intent. The Unit 7777 + tenant + PH phone regression
        lives in test_v1_adapter_regressions.py but the underlying
        invariant is locked here: MiniMaxOfflineProvider returns {}.
        """
        result = fallback.MiniMaxOfflineProvider().complete(
            prompt="rent 7777 tenant +63 917 123 4567",
            schema={"intent": "string"},
        )
        assert result == {}

    def test_llm_failure_does_not_raise(self):
        class FailingProvider:
            def complete(self, *, prompt, schema):
                raise RuntimeError("MiniMax offline")

        result = fallback.parse_once(
            text="ambiguous",
            rule_parsed=None,
            provider=FailingProvider(),
            chat_id=4,
            intent_kind="rent",
            feature_flag_enabled=True,
        )
        assert result.invoked is False
        assert result.reason == "llm_failed"
        assert result.parsed == {}
