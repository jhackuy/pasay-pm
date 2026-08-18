"""DAILY-DIGEST-TRUTH-CLEANUP-006 bot tests.

Covers:
- the production Telegram slash-command menu is cleared to EMPTY (default and
  every standard scope) — /new /stop /status /stress /help are never published
- the reply-keyboard four keys remain intact
- the Secretary role has no dev-command privileges
- the Daily Digest card renders the three user-semantic sections in
  single-language (Owner zh / Secretary en), one row per business object, with
  per-section caps + overflow counters.
"""
from __future__ import annotations

import asyncio

from conftest import FakeBot

from pasay_bot.render import cards
from pasay_bot.roles import Role, has_permission


def _run_context(env):
    # PTB only runs post_init during run_polling; call it directly so the test
    # exercises the exact startup publication (a transport error must be
    # swallowed by the production handler and never break startup).
    assert env.app.post_init is not None, "post_init (command menu clearing) is registered"
    asyncio.run(env.app.post_init(env.app))


# ---------------------------------------------------------------------------
# command menu: real Bot API publication is empty across every scope
# ---------------------------------------------------------------------------

def test_production_command_menu_published_empty(make_app):
    """post_init publishes an EMPTY command list to the default scope AND all
    standard scopes — no /new /stop /status /stress /debug /dev /test, and no
    /start /help /cancel either (PHASE 14)."""
    env = make_app()
    _run_context(env)
    published = env.bot.published_commands()
    assert published, "set_my_commands was never called on startup"
    # The default scope (key 'default') must be EMPTY, not stale commands.
    default = published.get("default", [])
    assert not [c for c in default if c]
    # Every standard scope we clear must be empty of dev commands.
    for scope, cmds in published.items():
        for name in (cmds or []):
            if not name:
                continue
            assert name not in ("new", "stop", "status", "stress",
                                "debug", "dev", "test", "ops", "todo", "copilot"), \
                f"dev command {name!r} published to scope {scope!r}"
            assert name not in ("start", "help", "cancel"), \
                f"rescue command {name!r} published to scope {scope!r}"


def test_old_default_and_group_private_admin_scopes_cleared(make_app):
    env = make_app()
    _run_context(env)
    published = env.bot.published_commands()
    # Every scope the bot touched must be cleared (empty command set).
    assert all(not [c for c in (published[s] or []) if c] for s in published)


def test_all_dev_commands_cleared_negative(make_app):
    """Even if a stale BotFather menu were present, the startup publish must
    actively send empty lists — the FakeBot records the real setMyCommands
    payloads (a registry would have been cleared)."""
    env = make_app()
    _run_context(env)
    calls = env.bot.of_type("set_my_commands")
    assert calls, "no setMyCommands calls recorded"
    for call in calls:
        assert not [c for c in call["commands"] if c], f"commands still published: {call}"


def test_request_error_never_breaks_startup(make_app):
    """A setMyCommands transport failure is cosmetic: the app still starts
    (the handler registry is what matters for live behaviour)."""

    class FailingBot(FakeBot):
        async def set_my_commands(self, commands, scope=None, language_code=None, **kw):
            raise RuntimeError("network down")

    env = make_app(bot=FailingBot())
    _run_context(env)  # must not raise


def test_fixed_reply_keyboard_unchanged():
    from pasay_bot.keyboards import reply_keyboard
    labels = [b.text for row in reply_keyboard("owner").keyboard for b in row]
    assert labels == ["🏠 Home", "✅ Tasks", "💰 Rent", "💸 Expense"]


def test_secretary_has_no_dev_command_permission():
    assert not has_permission(Role.SECRETARY, "dev_command")
    # Secretary can read ops but never a control/dev verb.
    assert has_permission(Role.SECRETARY, "operations")


def test_dev_commands_not_registered_handlers(make_app):
    env = make_app()
    commands = set()
    for handlers in env.app.handlers.values():
        for h in handlers:
            if type(h).__name__ == "CommandHandler":
                commands.update(h.commands)
    assert commands <= {"start", "help", "cancel"}
    assert commands.isdisjoint({"new", "stop", "status", "stress", "debug", "dev", "test"})


# ---------------------------------------------------------------------------
# digest card: three semantic sections, single-language, dedup, caps
# ---------------------------------------------------------------------------

_SEMANTIC_DIGEST = {
    "act_now": [
        {"business_dedupe_key": "lease:1:RENT_OVERDUE", "kind": "rent_overdue",
         "unit": "1680", "amount": "75000.00", "unpaid_periods": 3, "overdue_days": 104},
        {"business_dedupe_key": "expense:7:PAYMENT_PENDING", "kind": "payable_expense",
         "expense_id": 7, "unit": "", "purpose": "Repair", "amount": "7000.00"},
    ],
    "upcoming": [
        {"business_dedupe_key": "lease:2:LEASE_EXPIRING", "kind": "lease_expiring",
         "unit": "1608", "days_to_expiry": 18},
    ],
    "done_today": [
        {"business_dedupe_key": "lease:1:RENT_OVERDUE", "kind": "rent_followup",
         "unit": "1680", "expense_id": None, "amount": None},
    ],
    "counts": {"act_now": 2, "upcoming": 1, "done_today": 1},
    "hidden": {"act_now": 0, "upcoming": 0, "done_today": 0},
}


def test_digest_owner_chinese_single_language():
    text = cards.active_tasks_digest_card(dict(_SEMANTIC_DIGEST), "zh")
    assert "现在处理" in text
    assert "催租" in text and "75,000" in text and "3期" in text and "逾期" in text
    assert "即将处理" in text and "合同" in text and "18" in text
    assert "今日完成" in text and "已联系租客" in text
    # Single-language: no English duplication of the same line.
    assert "Act now" not in text
    assert "Follow up rent" not in text
    assert "Upcoming" not in text and "Done today" not in text


def test_digest_secretary_english_single_language():
    text = cards.active_tasks_digest_card(dict(_SEMANTIC_DIGEST), "en")
    assert "Act now" in text
    assert "Follow up rent" in text and "75,000" in text
    assert "Upcoming" in text and "Lease expires in 18d" in text
    assert "Done today" in text and "Followed up tenant" in text
    # Single-language: no Chinese duplication.
    assert "现在处理" not in text
    assert "催租" not in text
    assert "今日完成" not in text


def test_digest_expense_reads_pay_action():
    text = cards.active_tasks_digest_card(dict(_SEMANTIC_DIGEST), "zh")
    assert "E7" in text
    assert "付款" in text and "Repair" in text and "7,000" in text


def test_digest_one_business_object_once():
    # 20 identical business rows must yield at most one rent line each (backend
    # dedups; the card must not re-expand the same business object).
    data = dict(_SEMANTIC_DIGEST)
    data["act_now"] = data["act_now"] * 20
    text = cards.active_tasks_digest_card(data, "zh")
    # The 1680 rent action appears once; the 1680 "done today" line is a
    # different fact (and still legal), so count only the rent action line.
    assert text.count("1680 · 催租") <= 1
    assert text.count("₱75,000") == 1


def test_digest_caps_and_overflow():
    base = dict(_SEMANTIC_DIGEST)
    base["act_now"] = (_SEMANTIC_DIGEST["act_now"] * 5)[:9]
    base["counts"] = {"act_now": 10, "upcoming": 1, "done_today": 1}
    base["hidden"] = {"act_now": 2, "upcoming": 0, "done_today": 0}
    text = cards.active_tasks_digest_card(base, "zh")
    assert "另有 2 项" in text
    # Not flooded: the 9 capped rows are bounded (8 shown + overflow line).
    assert text.count("\n🔴 ") <= 8


def test_digest_empty_shows_nothing_here():
    text = cards.active_tasks_digest_card({}, "zh")
    assert "暂无内容" in text or "Nothing here" in text
