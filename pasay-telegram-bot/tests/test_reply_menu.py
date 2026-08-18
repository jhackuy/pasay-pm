"""Persistent Reply Keyboard menu tests.

UX Freeze v1 pins one deterministic 6-button IA. Private chats keep role
language; fixed-menu taps render page content with inline keyboards while the
persistent reply keyboard stays pinned below the input.
"""
from __future__ import annotations

import pytest

from conftest import OWNER_ID, SECRETARY_ID, UNKNOWN_ID, make_text_update, run_updates
from pasay_bot.keyboards import reply_keyboard
from pasay_bot.roles import Role


def _labels(kb):
    return [b.text for row in kb.keyboard for b in row]


def _markup_name(send):
    kb = send["reply_markup"]
    return kb.__class__.__name__ if kb is not None else None


def _has_reply_keyboard(sends):
    return any(_markup_name(send) == "ReplyKeyboardMarkup" for send in sends)


OWNER_LABELS = ["\U0001f3e0 首页", "\U0001f3d8 房源", "\u2705 待办", "\U0001f4b0 租金", "\U0001f4b8 支出", "\U0001f4c1 档案"]
SECRETARY_LABELS = ["\U0001f3e0 Home", "\U0001f3d8 Properties", "\u2705 Tasks", "\U0001f4b0 Rent", "\U0001f4b8 Expense", "\U0001f4c1 Archive"]


# --- keyboard structure: identical for both roles --------------------------

def test_owner_reply_keyboard_structure():
    kb = reply_keyboard(Role.OWNER)
    assert kb.resize_keyboard is True
    assert kb.is_persistent is True
    assert _labels(kb) == OWNER_LABELS


def test_secretary_reply_keyboard_structure():
    kb = reply_keyboard(Role.SECRETARY)
    assert kb.resize_keyboard is True
    assert kb.is_persistent is True
    assert _labels(kb) == SECRETARY_LABELS
    # Owner-only abilities (confirm/finalize/reverse/approve) are never shown.
    joined = " ".join(_labels(kb)).lower()
    for forbidden in ("confirm", "finalize", "reverse", "approve", "more"):
        assert forbidden not in joined


# --- button -> deterministic Quick View (both roles) ------------------------

@pytest.mark.parametrize(
    ("user_id", "marker"),
    [(OWNER_ID, "🏠 首页"), (SECRETARY_ID, "🏠 Home")],
)
def test_home_button_routes_to_home_overview(make_app, user_id, marker):
    env = make_app()
    label = "🏠 首页" if user_id == OWNER_ID else "🏠 Home"
    run_updates(env, [make_text_update(user_id, user_id, label, bot=env.bot)])
    send = env.bot.last_send()
    assert marker in send["text"]
    assert _markup_name(send) == "InlineKeyboardMarkup"


def test_legacy_properties_button_still_routes_to_properties(make_app):
    env = make_app()
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "\U0001f3e0 Properties", bot=env.bot)])
    send = env.bot.last_send()
    assert "房源" in send["text"] or "Properties" in send["text"]
    assert _markup_name(send) == "ReplyKeyboardMarkup"


@pytest.mark.parametrize(
    ("user_id", "marker"),
    [(OWNER_ID, "今日待办"), (SECRETARY_ID, "Today")],
)
def test_tasks_button_routes_to_quick_view(make_app, user_id, marker):
    env = make_app()
    label = "✅ 待办" if user_id == OWNER_ID else "✅ Tasks"
    run_updates(env, [make_text_update(user_id, user_id, label, bot=env.bot)])
    sends = env.bot.sends()
    assert marker in sends[-1]["text"]
    assert _has_reply_keyboard(sends)


@pytest.mark.parametrize(
    ("user_id", "marker"),
    [(OWNER_ID, "\u79df\u91d1"), (SECRETARY_ID, "Rent")],
)
def test_rent_button_routes_to_quick_view(make_app, user_id, marker):
    env = make_app()
    label = "💰 租金" if user_id == OWNER_ID else "💰 Rent"
    run_updates(env, [make_text_update(user_id, user_id, label, bot=env.bot)])
    sends = env.bot.sends()
    assert marker in sends[-1]["text"]
    assert _has_reply_keyboard(sends)


@pytest.mark.parametrize(
    ("user_id", "marker"),
    [(OWNER_ID, "\u652f\u51fa"), (SECRETARY_ID, "Expense")],
)
def test_expense_button_routes_to_quick_view(make_app, user_id, marker):
    env = make_app()
    label = "💸 支出" if user_id == OWNER_ID else "💸 Expense"
    run_updates(env, [make_text_update(user_id, user_id, label, bot=env.bot)])
    sends = env.bot.sends()
    assert marker in sends[-1]["text"]
    assert _has_reply_keyboard(sends)


# --- RBAC is not bypassed by menu buttons -----------------------------------

def test_unknown_user_menu_button_cannot_reach_page(make_app):
    env = make_app()
    run_updates(env, [make_text_update(UNKNOWN_ID, UNKNOWN_ID, "✅ Tasks", bot=env.bot)])
    send = env.bot.last_send()
    assert "\u6743\u9650" in send["text"] or "permission" in send["text"]
    assert "Tasks" not in send["text"]
