"""SLICE3-UX-PERSISTENT-MENU-001: persistent Reply Keyboard menu tests.

Covers the fixed bottom-menu structure (Owner Chinese / Secretary English),
the persistent flag, button -> nl_bridge route reuse for every fixed button,
and RBAC preservation (no Owner-only action is exposed to Secretary, and an
unknown user cannot reach a page through a menu button)."""
from __future__ import annotations

from conftest import OWNER_ID, SECRETARY_ID, UNKNOWN_ID, make_text_update, run_updates
from pasay_bot.keyboards import reply_keyboard
from pasay_bot.roles import Role


def _labels(kb):
    return [b.text for row in kb.keyboard for b in row]


def _markup_name(send):
    kb = send["reply_markup"]
    return kb.__class__.__name__ if kb is not None else None


# --- keyboard structure ------------------------------------------------------

def test_owner_reply_keyboard_structure():
    kb = reply_keyboard(Role.OWNER)
    assert kb.resize_keyboard is True
    assert kb.is_persistent is True
    assert _labels(kb) == ["🏠 房源", "✅ 待办", "💰 财务", "☰ 更多"]


def test_secretary_reply_keyboard_structure():
    kb = reply_keyboard(Role.SECRETARY)
    assert kb.resize_keyboard is True
    assert kb.is_persistent is True
    assert _labels(kb) == [
        "🏠 Properties", "👥 Tenants",
        "💵 Rent", "✅ Tasks",
        "🔧 Maintenance", "📋 Records",
        "⚠️ Overdue",
    ]
    # Owner-only abilities (confirm/finalize/reverse) are never exposed.
    joined = " ".join(_labels(kb)).lower()
    for forbidden in ("confirm", "finalize", "reverse", "approve"):
        assert forbidden not in joined


# --- button -> existing route reuse (Secretary, English) ---------------------

def test_secretary_properties_button_routes_to_properties_page(make_app):
    env = make_app()
    run_updates(env, [make_text_update(SECRETARY_ID, SECRETARY_ID, "🏠 Properties", bot=env.bot)])
    send = env.bot.last_send()
    assert "Property Overview" in send["text"]
    assert _markup_name(send) == "InlineKeyboardMarkup"


def test_secretary_tenants_button_routes_to_guidance(make_app):
    env = make_app()
    run_updates(env, [make_text_update(SECRETARY_ID, SECRETARY_ID, "👥 Tenants", bot=env.bot)])
    send = env.bot.last_send()
    assert "Tenant status" in send["text"]
    assert _markup_name(send) == "ReplyKeyboardMarkup"


def test_secretary_rent_button_routes_to_rent_collect_page(make_app):
    env = make_app()
    run_updates(env, [make_text_update(SECRETARY_ID, SECRETARY_ID, "💵 Rent", bot=env.bot)])
    send = env.bot.last_send()
    assert "Select unpaid unit" in send["text"]
    assert _markup_name(send) == "InlineKeyboardMarkup"


def test_secretary_tasks_button_routes_to_todo_page(make_app):
    env = make_app()
    run_updates(env, [make_text_update(SECRETARY_ID, SECRETARY_ID, "✅ Tasks", bot=env.bot)])
    send = env.bot.last_send()
    assert "Nothing to do" in send["text"]
    assert _markup_name(send) == "InlineKeyboardMarkup"


def test_secretary_maintenance_button_routes_to_guidance(make_app):
    env = make_app()
    run_updates(env, [make_text_update(SECRETARY_ID, SECRETARY_ID, "🔧 Maintenance", bot=env.bot)])
    send = env.bot.last_send()
    assert "Maintenance jobs" in send["text"]
    assert _markup_name(send) == "ReplyKeyboardMarkup"


def test_secretary_records_button_routes_to_finance_page(make_app):
    env = make_app()
    run_updates(env, [make_text_update(SECRETARY_ID, SECRETARY_ID, "📋 Records", bot=env.bot)])
    send = env.bot.last_send()
    assert "Finance" in send["text"]
    assert _markup_name(send) == "InlineKeyboardMarkup"


def test_secretary_overdue_button_routes_to_overdue_page(make_app):
    env = make_app()
    run_updates(env, [make_text_update(SECRETARY_ID, SECRETARY_ID, "⚠️ Overdue", bot=env.bot)])
    send = env.bot.last_send()
    assert "Overdue Rent" in send["text"]
    assert _markup_name(send) == "InlineKeyboardMarkup"


# --- Owner (Chinese) buttons -------------------------------------------------

def test_owner_properties_button_routes_to_properties_page(make_app):
    env = make_app()
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "🏠 房源", bot=env.bot)])
    send = env.bot.last_send()
    assert "房源概况" in send["text"]
    assert _markup_name(send) == "InlineKeyboardMarkup"


def test_owner_finance_button_routes_to_finance_page(make_app):
    env = make_app()
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "💰 财务", bot=env.bot)])
    send = env.bot.last_send()
    assert "财务" in send["text"]
    assert _markup_name(send) == "InlineKeyboardMarkup"


# --- RBAC is not bypassed by menu buttons ------------------------------------

def test_unknown_user_menu_button_cannot_reach_page(make_app):
    env = make_app()
    run_updates(env, [make_text_update(UNKNOWN_ID, UNKNOWN_ID, "✅ Tasks", bot=env.bot)])
    send = env.bot.last_send()
    # Unknown users fall back to the zh no-permission copy (locale_for(None)).
    assert "权限" in send["text"] or "permission" in send["text"]
    assert "Nothing to do" not in send["text"]
