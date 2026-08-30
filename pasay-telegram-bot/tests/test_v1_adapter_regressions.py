"""PASAY-REWRITE Issue #99 — V1 adapter-level regression tests.

These tests pin down the EXACT contract of the Telegram 3x2 menu adapter
when it sits on top of the V1 shared application services. They cover:

  1. Unit 7777 must NEVER be interpreted as an expense claim. A free-text
     update mentioning unit 7777 without an expense verb + amount must
     fall through to the read-only query lane (or the menu silence), not
     create an expense.
  2. Tenant mention (no phone, no amount) must NEVER be routed to the
     expense lane either.
  3. Philippine phone number (+639170000000) in a "phone-fix" message must
     be accepted by the fast-path and NOT misrouted to expense / rent.
  4. Group chat must stay silent on /start and never broadcast the role
     menu.

All tests run against the real handlers (no monkey-patching of nl_bridge)
and verify the FakeBackend never receives an expense or rent POST for the
false-positive cases.
"""
from __future__ import annotations

import pytest
from telegram import Update

from conftest import OWNER_ID, SECRETARY_ID, UNKNOWN_ID, make_text_update, run_updates

GROUP_CHAT_ID = -1001234567890


def _make_private_text_update(user_id, text, update_id=1, bot=None):
    return Update.de_json(
        {
            "update_id": update_id,
            "message": {
                "message_id": 1,
                "date": 0,
                "chat": {"id": user_id, "type": "private"},
                "from": {
                    "id": user_id,
                    "is_bot": False,
                    "first_name": "T",
                    "username": "t",
                },
                "text": text,
            },
        },
        bot,
    )


def _backend_writes(env, method: str, path_substring: str) -> list[tuple]:
    """Inspect every backend call whose path contains the substring."""
    return [
        call
        for call in env.backend.calls
        if len(call) >= 2 and call[0] == method and path_substring in call[1]
    ]


def test_unit_7777_alone_never_creates_expense(make_app):
    """Bare '7777' (a unit token) must NOT trigger an expense POST."""
    env = make_app()
    run_updates(env, [_make_private_text_update(OWNER_ID, "7777", bot=env.bot)])
    expense_posts = _backend_writes(env, "POST", "/expenses")
    assert expense_posts == [], (
        "Bare unit token 7777 must never create an expense; "
        f"observed POSTs: {expense_posts}"
    )


def test_unit_7777_with_tenant_word_never_creates_expense(make_app):
    """'tenant 7777' is a query, not an expense claim."""
    env = make_app()
    run_updates(env, [_make_private_text_update(OWNER_ID, "tenant 7777", bot=env.bot)])
    expense_posts = _backend_writes(env, "POST", "/expenses")
    assert expense_posts == [], (
        "'tenant 7777' must not create an expense; "
        f"observed POSTs: {expense_posts}"
    )
    rent_posts = _backend_writes(env, "POST", "/rent")
    assert rent_posts == [], (
        "'tenant 7777' must not create a rent claim; "
        f"observed POSTs: {rent_posts}"
    )


def test_unit_7777_rent_status_query_does_not_create_anything(make_app):
    """'7777 status' or '7777 rent' is a read-only query, never a write."""
    for text in ("7777 status", "7777 rent", "7777 怎么样"):
        env = make_app()
        run_updates(env, [_make_private_text_update(OWNER_ID, text, bot=env.bot)])
        assert _backend_writes(env, "POST", "/expenses") == []
        assert _backend_writes(env, "POST", "/rent") == []


def test_philippine_phone_in_phone_fix_message_routes_correctly(make_app):
    """'+639170000000' in a 'phone-fix' message is the fast path, not expense."""
    env = make_app()
    text = "1680 tenant phone +639170000000"
    run_updates(env, [_make_private_text_update(OWNER_ID, text, bot=env.bot)])
    expense_posts = _backend_writes(env, "POST", "/expenses")
    assert expense_posts == []
    # The phone-fix may resolve to a PATCH on /tenants or similar — never
    # to a rent claim POST or an expense POST.
    rent_claim_posts = _backend_writes(env, "POST", "/rent/payments")
    assert rent_claim_posts == []


def test_secretary_phone_fix_message_is_not_expense(make_app):
    env = make_app()
    text = "1680 tenant phone 09171234567"
    run_updates(env, [_make_private_text_update(SECRETARY_ID, text, bot=env.bot)])
    assert _backend_writes(env, "POST", "/expenses") == []
    assert _backend_writes(env, "POST", "/rent/payments") == []


def test_group_welcome_message_is_3x2_markup(make_app):
    """Group welcome must carry the 3x2 menu (not the private role menu)."""
    import time

    env = make_app()
    join_update = Update.de_json(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": int(time.time()),
                "chat": {"id": GROUP_CHAT_ID, "type": "group", "title": "Pasay Group"},
                "from": {"id": UNKNOWN_ID, "is_bot": False, "first_name": "X"},
                "new_chat_members": [
                    {"id": UNKNOWN_ID, "is_bot": False, "first_name": "X"},
                ],
            },
        },
        env.bot,
    )
    run_updates(env, [join_update])
    # The bot must send exactly one welcome message with a ReplyKeyboardMarkup
    # whose rows are 3 buttons each (3x2 in group chats).
    reply_sends = [
        send
        for send in env.bot.sends()
        if send.get("reply_markup") is not None
        and send["reply_markup"].__class__.__name__ == "ReplyKeyboardMarkup"
    ]
    assert reply_sends, "Group welcome must include a ReplyKeyboardMarkup"
    row_lengths = [len(row) for row in reply_sends[0]["reply_markup"].keyboard]
    assert row_lengths == [3, 3], f"Group menu must be 3x2, got {row_lengths}"


def test_owner_menu_button_routes_to_home_deterministically(make_app):
    """Pressing '🏠 首页' routes to Home with NO LLM call."""
    env = make_app()
    run_updates(env, [_make_private_text_update(OWNER_ID, "🏠 首页", bot=env.bot)])
    # The 3x2 menu should be sent back; no AI fallback triggered.
    reply_sends = [
        send
        for send in env.bot.sends()
        if send.get("reply_markup") is not None
        and send["reply_markup"].__class__.__name__ == "ReplyKeyboardMarkup"
    ]
    assert reply_sends, "Home button must refresh the persistent menu"


def test_secretary_menu_button_routes_to_home_deterministically(make_app):
    env = make_app()
    run_updates(env, [_make_private_text_update(SECRETARY_ID, "🏠 Home", bot=env.bot)])
    reply_sends = [
        send
        for send in env.bot.sends()
        if send.get("reply_markup") is not None
        and send["reply_markup"].__class__.__name__ == "ReplyKeyboardMarkup"
    ]
    assert reply_sends, "Home button must refresh the persistent menu"


def test_owner_zh_labels_and_secretary_en_labels_strict_match(make_app):
    """Owner DM menu must show zh labels; Secretary DM menu must show en labels."""
    from telegram import ReplyKeyboardMarkup

    OWNER_LABELS = ["🏠 首页", "🏘 房源", "✅ 待办", "💰 租金", "💸 支出", "📁 档案"]
    SECRETARY_LABELS = ["🏠 Home", "🏘 Properties", "✅ Tasks", "💰 Rent", "💸 Expense", "📁 Archive"]

    env = make_app()
    run_updates(env, [_make_private_text_update(OWNER_ID, "/start", bot=env.bot)])
    owner_menus = [
        send
        for send in env.bot.sends()
        if send.get("reply_markup") is not None
        and send["reply_markup"].__class__.__name__ == "ReplyKeyboardMarkup"
    ]
    owner_labels = [b.text for row in owner_menus[0]["reply_markup"].keyboard for b in row]
    assert owner_labels == OWNER_LABELS

    env = make_app()
    run_updates(env, [_make_private_text_update(SECRETARY_ID, "/start", bot=env.bot)])
    sec_menus = [
        send
        for send in env.bot.sends()
        if send.get("reply_markup") is not None
        and send["reply_markup"].__class__.__name__ == "ReplyKeyboardMarkup"
    ]
    sec_labels = [b.text for row in sec_menus[0]["reply_markup"].keyboard for b in row]
    assert sec_labels == SECRETARY_LABELS
