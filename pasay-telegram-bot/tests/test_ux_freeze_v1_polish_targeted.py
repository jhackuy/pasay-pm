"""UX Freeze v1 final UI polish targeted regressions only."""
from __future__ import annotations

from telegram import Update

from conftest import OWNER_ID, SECRETARY_ID, make_text_update, run_updates
from pasay_bot.keyboards import (
    ACTION_QUICK_UNIT_VIEW,
    decode,
    fixed_menu_route_for,
    properties_quick_keyboard,
    reply_keyboard,
)
from pasay_bot.roles import Role

GROUP_CHAT_ID = -1001234567890


def _reply_labels(kb) -> list[str]:
    return [button.text for row in kb.keyboard for button in row]


def _reply_row_lengths(kb) -> list[int]:
    return [len(row) for row in kb.keyboard]


def _inline_rows(kb) -> list[list]:
    return kb.inline_keyboard


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


def test_units_10_items_are_rendered_as_3_3_3_1_navigation_rows():
    rows = [{"unit_code": str(unit)} for unit in (1203, 1608, 1680, 2208, 1103, 1805, 2308, 7777, 7789, 9950)]
    kb = properties_quick_keyboard(rows, "zh")

    assert [len(row) for row in _inline_rows(kb)] == [3, 3, 3, 1]
    assert [[button.text for button in row] for row in _inline_rows(kb)] == [
        ["1203", "1608", "1680"],
        ["2208", "1103", "1805"],
        ["2308", "7777", "7789"],
        ["9950"],
    ]


def test_unit_callback_mapping_is_unchanged_even_when_compact_labels_repeat():
    kb = properties_quick_keyboard(
        [{"unit_code": "BAY-1608"}, {"unit_code": "SHR-1608"}, {"unit_code": "9950"}],
        "zh",
    )

    buttons = [button for row in _inline_rows(kb) for button in row]
    assert [button.text for button in buttons] == ["1608", "1608", "9950"]

    decoded = [decode(button.callback_data) for button in buttons]
    assert [item["action"] for item in decoded] == [ACTION_QUICK_UNIT_VIEW] * 3
    assert [item["entity"] for item in decoded] == ["u", "u", "u"]
    assert [item["ref"] for item in decoded] == ["1", "2", "3"]


def test_owner_fixed_menu_is_3x2():
    kb = reply_keyboard(Role.OWNER)

    assert _reply_row_lengths(kb) == [3, 3]
    assert _reply_labels(kb) == [
        "🏠 首页",
        "🏘 房源",
        "✅ 待办",
        "💰 租金",
        "💸 支出",
        "☰ 更多",
    ]


def test_secretary_fixed_menu_is_3x2():
    kb = reply_keyboard(Role.SECRETARY)

    assert _reply_row_lengths(kb) == [3, 3]
    assert _reply_labels(kb) == [
        "🏠 Home",
        "🏘 Properties",
        "✅ Tasks",
        "💰 Rent",
        "💸 Expense",
        "☰ More",
    ]


def test_group_menu_is_3x2(make_app):
    env = make_app()
    run_updates(
        env,
        [_make_group_text_update(OWNER_ID, GROUP_CHAT_ID, "hello", bot=env.bot)],
    )

    reply_sends = [
        send
        for send in env.bot.sends()
        if send["reply_markup"] is not None
        and send["reply_markup"].__class__.__name__ == "ReplyKeyboardMarkup"
    ]
    assert reply_sends
    assert _reply_row_lengths(reply_sends[0]["reply_markup"]) == [3, 3]


def test_deterministic_menu_routing_is_unchanged():
    expected = {
        "🏠 首页": "home",
        "🏠 Home": "home",
        "🏘 房源": "properties",
        "🏘 Properties": "properties",
        "✅ 待办": "tasks",
        "✅ Tasks": "tasks",
        "💰 租金": "rent",
        "💰 Rent": "rent",
        "💸 支出": "expense",
        "💸 Expense": "expense",
        "☰ 更多": "more",
        "☰ More": "more",
        "🏠 Properties": "properties",
        "💰 收租": "rent",
        "更多": "more",
    }
    for label, route in expected.items():
        assert fixed_menu_route_for(label) == route


def test_old_chat_menu_migration_still_works(make_app):
    for user_id, label in (
        (OWNER_ID, "💰 租金"),
        (SECRETARY_ID, "💰 Rent"),
    ):
        env = make_app()
        env.app.bot_data.setdefault("menu_init_chats", {})[user_id] = "legacy_menu_v1"
        run_updates(env, [make_text_update(user_id, user_id, label, bot=env.bot)])

        reply_sends = [
            send
            for send in env.bot.sends()
            if send["reply_markup"] is not None
            and send["reply_markup"].__class__.__name__ == "ReplyKeyboardMarkup"
        ]
        assert reply_sends
        assert _reply_row_lengths(reply_sends[0]["reply_markup"]) == [3, 3]
