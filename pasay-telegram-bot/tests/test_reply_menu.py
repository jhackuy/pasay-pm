"""BOT-V1-USABLE-001: persistent Reply Keyboard menu tests.

Both roles share ONE identical 4-button persistent menu (首页 / 待办 / 收租 /
支出). Each fixed button exact-matches to a deterministic page BEFORE any
NL/AI path; unknown users cannot reach a page through a menu button.
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


EXPECTED_LABELS = ["🏠 首页", "✅ 待办", "💰 收租", "💸 支出"]


# --- keyboard structure: identical for both roles --------------------------

def test_owner_reply_keyboard_structure():
    kb = reply_keyboard(Role.OWNER)
    assert kb.resize_keyboard is True
    assert kb.is_persistent is True
    assert _labels(kb) == EXPECTED_LABELS


def test_secretary_reply_keyboard_structure():
    kb = reply_keyboard(Role.SECRETARY)
    assert kb.resize_keyboard is True
    assert kb.is_persistent is True
    assert _labels(kb) == EXPECTED_LABELS
    # Owner-only abilities (confirm/finalize/reverse/approve) are never shown.
    joined = " ".join(_labels(kb)).lower()
    for forbidden in ("confirm", "finalize", "reverse", "approve", "more"):
        assert forbidden not in joined


# --- button -> deterministic page reuse (both roles) ------------------------

def _home_marker(text):
    return "Pasay Property" in text


@pytest.mark.parametrize(
    "user_id", [OWNER_ID, SECRETARY_ID],
)
def test_home_button_routes_to_summary(make_app, user_id):
    env = make_app()
    run_updates(env, [make_text_update(user_id, user_id, "🏠 首页", bot=env.bot)])
    assert "处理中" in env.bot.last_send()["text"] or "Processing" in env.bot.last_send()["text"]
    page = env.bot.last_edit()
    assert _home_marker(page["text"])
    assert _markup_name(page) == "InlineKeyboardMarkup"


@pytest.mark.parametrize(
    ("user_id", "marker"),
    [(OWNER_ID, "逾期租金"), (SECRETARY_ID, "Overdue rent")],
)
def test_todo_button_routes_to_unified_todo(make_app, user_id, marker):
    env = make_app()
    run_updates(env, [make_text_update(user_id, user_id, "✅ 待办", bot=env.bot)])
    page = env.bot.last_edit()
    assert marker in page["text"]
    assert _markup_name(page) == "InlineKeyboardMarkup"


@pytest.mark.parametrize(
    ("user_id", "marker"),
    [(OWNER_ID, "选择未付款"), (SECRETARY_ID, "Select unpaid unit")],
)
def test_rent_button_routes_to_collect_page(make_app, user_id, marker):
    env = make_app()
    run_updates(env, [make_text_update(user_id, user_id, "💰 收租", bot=env.bot)])
    page = env.bot.last_edit()
    assert marker in page["text"]
    assert _markup_name(page) == "InlineKeyboardMarkup"


@pytest.mark.parametrize(
    ("user_id", "marker"),
    [(OWNER_ID, "直接告诉我这笔支出"), (SECRETARY_ID, "Just tell me the expense")],
)
def test_expense_button_routes_to_expense_page(make_app, user_id, marker):
    env = make_app()
    run_updates(env, [make_text_update(user_id, user_id, "💸 支出", bot=env.bot)])
    page = env.bot.last_edit()
    assert marker in page["text"]
    assert _markup_name(page) == "InlineKeyboardMarkup"


# --- RBAC is not bypassed by menu buttons -----------------------------------

def test_unknown_user_menu_button_cannot_reach_page(make_app):
    env = make_app()
    run_updates(env, [make_text_update(UNKNOWN_ID, UNKNOWN_ID, "✅ 待办", bot=env.bot)])
    send = env.bot.last_send()
    assert "权限" in send["text"] or "permission" in send["text"]
    assert "逾期租金" not in send["text"]
