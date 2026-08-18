"""Telegram UX Freeze v1 archive + migration hotfix targeted tests only."""
from __future__ import annotations

from telegram import ReplyKeyboardMarkup, Update

from conftest import OWNER_ID, SECRETARY_ID, make_text_update, run_updates
from pasay_bot.keyboards import fixed_menu_route_for, reply_keyboard
from pasay_bot.roles import Role

GROUP_CHAT_ID = -1001234567890


def _reply_labels(kb: ReplyKeyboardMarkup) -> list[str]:
    return [button.text for row in kb.keyboard for button in row]


def _reply_rows(kb: ReplyKeyboardMarkup) -> list[list[str]]:
    return [[button.text for button in row] for row in kb.keyboard]


def _reply_row_lengths(kb: ReplyKeyboardMarkup) -> list[int]:
    return [len(row) for row in kb.keyboard]


def _reply_keyboard_sends(env) -> list[dict]:
    return [
        send
        for send in env.bot.sends()
        if isinstance(send.get("reply_markup"), ReplyKeyboardMarkup)
    ]


def _make_group_text_update(user_id, chat_id, text, bot=None):
    return Update.de_json(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 0,
                "chat": {"id": chat_id, "type": "group", "title": "Pasay Group"},
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


def test_owner_fixed_menu_is_3x2_archive():
    kb = reply_keyboard(Role.OWNER)

    assert _reply_row_lengths(kb) == [3, 3]
    assert _reply_labels(kb) == [
        "🏠 首页",
        "🏘 房源",
        "✅ 待办",
        "💰 租金",
        "💸 支出",
        "📁 档案",
    ]


def test_secretary_fixed_menu_is_3x2_archive():
    kb = reply_keyboard(Role.SECRETARY)

    assert _reply_row_lengths(kb) == [3, 3]
    assert _reply_labels(kb) == [
        "🏠 Home",
        "🏘 Properties",
        "✅ Tasks",
        "💰 Rent",
        "💸 Expense",
        "📁 Archive",
    ]


def test_group_menu_is_3x2_archive(make_app):
    env = make_app()
    run_updates(env, [_make_group_text_update(OWNER_ID, GROUP_CHAT_ID, "hello", bot=env.bot)])

    reply_sends = _reply_keyboard_sends(env)
    assert reply_sends
    assert _reply_row_lengths(reply_sends[0]["reply_markup"]) == [3, 3]
    assert "📁 档案" in _reply_labels(reply_sends[0]["reply_markup"])


def test_old_owner_chat_migration_delivers_new_keyboard(make_app):
    env = make_app()
    env.app.bot_data.setdefault("menu_init_chats", {})[OWNER_ID] = "legacy_menu_v1"
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "🏠 首页", bot=env.bot)])

    reply_sends = _reply_keyboard_sends(env)
    assert reply_sends
    assert _reply_rows(reply_sends[0]["reply_markup"]) == [
        ["🏠 首页", "🏘 房源", "✅ 待办"],
        ["💰 租金", "💸 支出", "📁 档案"],
    ]


def test_old_secretary_4key_chat_migration_delivers_new_keyboard(make_app):
    env = make_app()
    env.app.bot_data.setdefault("menu_init_chats", {})[SECRETARY_ID] = "legacy_secretary_4key"
    run_updates(env, [make_text_update(SECRETARY_ID, SECRETARY_ID, "🏠 Properties", bot=env.bot)])

    reply_sends = _reply_keyboard_sends(env)
    assert reply_sends
    assert _reply_rows(reply_sends[0]["reply_markup"]) == [
        ["🏠 Home", "🏘 Properties", "✅ Tasks"],
        ["💰 Rent", "💸 Expense", "📁 Archive"],
    ]


def test_secretary_clicking_tasks_causes_new_keyboard_delivery(make_app):
    env = make_app()
    env.app.bot_data.setdefault("menu_init_chats", {})[SECRETARY_ID] = "legacy_secretary_4key"
    run_updates(env, [make_text_update(SECRETARY_ID, SECRETARY_ID, "✅ Tasks", bot=env.bot)])

    reply_sends = _reply_keyboard_sends(env)
    assert reply_sends
    assert _reply_rows(reply_sends[0]["reply_markup"]) == [
        ["🏠 Home", "🏘 Properties", "✅ Tasks"],
        ["💰 Rent", "💸 Expense", "📁 Archive"],
    ]


def test_secretary_clicking_rent_causes_new_keyboard_delivery(make_app):
    env = make_app()
    env.app.bot_data.setdefault("menu_init_chats", {})[SECRETARY_ID] = "legacy_secretary_4key"
    run_updates(env, [make_text_update(SECRETARY_ID, SECRETARY_ID, "💰 Rent", bot=env.bot)])

    reply_sends = _reply_keyboard_sends(env)
    assert reply_sends
    assert _reply_rows(reply_sends[0]["reply_markup"]) == [
        ["🏠 Home", "🏘 Properties", "✅ Tasks"],
        ["💰 Rent", "💸 Expense", "📁 Archive"],
    ]


def test_more_no_longer_rendered():
    assert "☰ More" not in _reply_labels(reply_keyboard(Role.SECRETARY))
    assert "☰ 更多" not in _reply_labels(reply_keyboard(Role.OWNER))
    assert fixed_menu_route_for("☰ More") == "archive"
    assert fixed_menu_route_for("☰ 更多") == "archive"


def test_archive_handler_bypasses_nl_llm(make_app, monkeypatch):
    env = make_app()
    env.app.bot_data["settings"].archive_chat_id = "-1009876543210"
    called = {"nl": 0}

    async def _fail_nl(*args, **kwargs):
        called["nl"] += 1
        raise AssertionError("archive fixed-menu route must bypass NL/LLM")

    monkeypatch.setattr("pasay_bot.handlers.nl_bridge.handle_nl", _fail_nl)
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "📁 档案", bot=env.bot)])

    sends = env.bot.sends()
    assert sends
    assert "📁 房产档案" in (sends[-1]["text"] or "")
    assert called["nl"] == 0


def test_archive_launcher_uses_existing_authoritative_url(make_app):
    env = make_app()
    env.app.bot_data["settings"].archive_chat_id = "-1009876543210"
    run_updates(env, [make_text_update(SECRETARY_ID, SECRETARY_ID, "📁 Archive", bot=env.bot)])

    send = env.bot.sends()[-1]
    buttons = send["reply_markup"].inline_keyboard
    assert send["text"] == "📁 Property Archive"
    assert buttons[0][0].text == "📁 Open Archive Channel ↗"
    assert buttons[0][0].url == "https://t.me/c/9876543210"
