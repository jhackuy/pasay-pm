"""SLICE3-UX-PERSISTENT-MENU-002: no "please type /start" in normal flows.

Covers the acceptance matrix:
- /start stays compatible (regression);
- an identified user's normal message auto-restores the persistent menu;
- dashboard/backend failure does not block menu initialization;
- Owner / Secretary menus never cross roles;
- group new-member onboarding is handled (neutral welcome, fail-closed on any
  role-menu broadcast, never a /start prompt);
- no duplicate welcome messages / menu spam.
"""
from __future__ import annotations

import time

from telegram import Update

from conftest import (
    OWNER_ID,
    SECRETARY_ID,
    UNKNOWN_ID,
    make_text_update,
    run_updates,
)

GROUP_CHAT_ID = -1001234567890

# one identical 4-button English menu for every role
# (Home / Tasks / Rent / Expense), is_persistent=True.
OWNER_LABELS = ["🏠 Home", "✅ Tasks", "💰 Rent", "💸 Expense"]
SECRETARY_LABELS = OWNER_LABELS

FORBIDDEN_PROMPTS = (
    "请输入 /start", "请输入/start", "type /start", "send /start",
    "please type /start", "输入 /start", "/start",
)


def _labels(kb):
    if kb is None or kb.__class__.__name__ != "ReplyKeyboardMarkup":
        return []
    return [b.text for row in kb.keyboard for b in row]


def _reply_sends(env):
    return [
        s for s in env.bot.sends()
        if s["reply_markup"] is not None
        and s["reply_markup"].__class__.__name__ == "ReplyKeyboardMarkup"
    ]


def _assert_no_start_prompt(text: str):
    lowered = text.lower()
    for phrase in FORBIDDEN_PROMPTS:
        assert phrase not in lowered


def make_join_update(user_ids, chat_id, message_id=1, update_id=1, bot=None):
    """Group service message: one or more users joined (adder is an outsider)."""
    return Update.de_json(
        {
            "update_id": update_id,
            "message": {
                "message_id": message_id,
                "date": int(time.time()),
                "chat": {"id": chat_id, "type": "group", "title": "Pasay Group"},
                "from": {
                    "id": UNKNOWN_ID, "is_bot": False,
                    "first_name": "Adder", "username": "adder",
                },
                "new_chat_members": [
                    {
                        "id": uid, "is_bot": False,
                        "first_name": f"U{uid}", "username": f"u{uid}",
                    }
                    for uid in user_ids
                ],
            },
        },
        bot,
    )


def make_group_text_update(user_id, chat_id, text, bot=None):
    return Update.de_json(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": int(time.time()),
                "chat": {"id": chat_id, "type": "group", "title": "Pasay Group"},
                "from": {
                    "id": user_id, "is_bot": False,
                    "first_name": "T", "username": "t",
                },
                "text": text,
            },
        },
        bot,
    )


# --- /start regression -------------------------------------------------------

def test_start_still_works_regression(make_app):
    env = make_app()
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "/start", bot=env.bot)])
    send = env.bot.last_send()
    # V2: /start is technical recovery only and shows the short greeting.
    assert "Hello" in send["text"]
    assert _labels(send["reply_markup"]) == OWNER_LABELS
    assert send["reply_markup"].is_persistent is True
    _assert_no_start_prompt("\n".join(env.bot.all_texts()))


# --- private chat: normal message auto-restores the menu ---------------------

def test_identified_user_any_message_restores_menu(make_app):
    env = make_app()
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "hello", bot=env.bot)])
    reply_sends = _reply_sends(env)
    assert len(reply_sends) == 1
    assert _labels(reply_sends[0]["reply_markup"]) == OWNER_LABELS
    assert reply_sends[0]["reply_markup"].is_persistent is True
    # the normal processing still happened (V2 short greeting answer)
    assert any("Hello" in (s["text"] or "") for s in env.bot.sends())
    _assert_no_start_prompt("\n".join(env.bot.all_texts()))


def test_secretary_any_message_restores_menu(make_app):
    env = make_app()
    run_updates(env, [make_text_update(SECRETARY_ID, SECRETARY_ID, "hi", bot=env.bot)])
    reply_sends = _reply_sends(env)
    assert len(reply_sends) == 1
    assert _labels(reply_sends[0]["reply_markup"]) == SECRETARY_LABELS
    _assert_no_start_prompt("\n".join(env.bot.all_texts()))


def test_menu_restore_dedup_no_spam(make_app):
    env = make_app()
    run_updates(
        env,
        [
            make_text_update(OWNER_ID, OWNER_ID, "first message", message_id=1,
                             update_id=1, bot=env.bot),
            make_text_update(OWNER_ID, OWNER_ID, "second message", message_id=2,
                             update_id=2, bot=env.bot),
        ],
    )
    # exactly one menu message despite two normal messages
    assert len(_reply_sends(env)) == 1


def test_start_then_message_does_not_duplicate_menu(make_app):
    env = make_app()
    run_updates(
        env,
        [
            make_text_update(OWNER_ID, OWNER_ID, "/start", message_id=1,
                             update_id=1, bot=env.bot),
            make_text_update(OWNER_ID, OWNER_ID, "hello again", message_id=2,
                             update_id=2, bot=env.bot),
        ],
    )
    # /start mounted the keyboard on the dashboard; the message must not re-send
    assert len(_reply_sends(env)) == 1


def test_unknown_user_never_gets_menu_restore(make_app):
    env = make_app()
    run_updates(env, [make_text_update(UNKNOWN_ID, UNKNOWN_ID, "hello", bot=env.bot)])
    assert _reply_sends(env) == []
    assert "menu.ready" not in "\n".join(env.bot.all_texts())


# --- dashboard/backend failure does not block menu init ----------------------

def test_start_menu_init_independent_of_dashboard_failure(make_app):
    env = make_app()
    env.backend.fail_status["/reports/financial-summary"] = 500
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "/start", bot=env.bot)])
    sends = env.bot.sends()
    # V2 greeting never depends on the financial dashboard.
    assert len(sends) == 1
    assert "Hello" in sends[0]["text"]
    assert _labels(sends[0]["reply_markup"]) == OWNER_LABELS


def test_message_menu_init_independent_of_dashboard_failure(make_app):
    env = make_app()
    env.backend.fail_status["/reports/financial-summary"] = 500
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "菜单", bot=env.bot)])
    reply_sends = _reply_sends(env)
    assert len(reply_sends) == 1
    assert _labels(reply_sends[0]["reply_markup"]) == OWNER_LABELS
    assert any("Hello" in (s["text"] or "") for s in env.bot.sends())


# --- Owner / Secretary share the BOT-V1 menu --------------------------------

def test_owner_and_secretary_share_bot_v1_menu(make_app):
    env = make_app()
    run_updates(
        env,
        [
            make_text_update(OWNER_ID, OWNER_ID, "hi", message_id=1,
                             update_id=1, bot=env.bot),
            make_text_update(SECRETARY_ID, SECRETARY_ID, "hi", message_id=2,
                             update_id=2, bot=env.bot),
        ],
    )
    reply_sends = _reply_sends(env)
    assert len(reply_sends) == 2
    assert _labels(reply_sends[0]["reply_markup"]) == OWNER_LABELS
    assert _labels(reply_sends[1]["reply_markup"]) == SECRETARY_LABELS
    # Identical V2 English Quick View menu for both roles.
    assert _labels(reply_sends[0]["reply_markup"]) == OWNER_LABELS
    assert _labels(reply_sends[1]["reply_markup"]) == SECRETARY_LABELS


# --- group new-member onboarding (fail-closed) -------------------------------

def test_new_member_unknown_gets_neutral_welcome_only(make_app):
    env = make_app()
    run_updates(env, [make_join_update([UNKNOWN_ID], GROUP_CHAT_ID, bot=env.bot)])
    sends = env.bot.sends()
    assert len(sends) == 1
    text = sends[0]["text"]
    assert "Welcome" in text and "欢迎" in text
    # V2: the neutral fixed keyboard is attached (same menu for every role).
    assert _labels(sends[0]["reply_markup"]) == OWNER_LABELS
    _assert_no_start_prompt(text)


def test_new_member_identified_gets_neutral_welcome_no_role_menu(make_app):
    env = make_app()
    run_updates(env, [make_join_update([OWNER_ID], GROUP_CHAT_ID, bot=env.bot)])
    sends = env.bot.sends()
    assert len(sends) == 1
    text = sends[0]["text"]
    assert "Welcome" in text and "欢迎" in text
    # Same neutral fixed menu as every other group member (no role leak).
    assert _labels(sends[0]["reply_markup"]) == OWNER_LABELS
    for label in OWNER_LABELS:
        assert label not in text
    _assert_no_start_prompt(text)


def test_new_member_join_dedupes_multi_member_event(make_app):
    env = make_app()
    run_updates(env, [make_join_update([OWNER_ID, SECRETARY_ID], GROUP_CHAT_ID, bot=env.bot)])
    sends = env.bot.sends()
    assert len(sends) == 1  # one welcome per join event, no per-member spam
    assert "Welcome" in sends[0]["text"] and "欢迎" in sends[0]["text"]
    assert _labels(sends[0]["reply_markup"]) == OWNER_LABELS


def test_group_text_message_self_heals_neutral_menu(make_app):
    """PASAY-V2-FOUNDATION-001: group text self-heals the neutral fixed
    keyboard (same English Quick View menu for every role; no role leak)."""
    env = make_app()
    run_updates(
        env,
        [make_group_text_update(OWNER_ID, GROUP_CHAT_ID, "hello", bot=env.bot)],
    )
    reply_sends = _reply_sends(env)
    assert len(reply_sends) == 1
    assert _labels(reply_sends[0]["reply_markup"]) == OWNER_LABELS
    assert "menu.ready" not in "\n".join(env.bot.all_texts())
