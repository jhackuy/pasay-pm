"""Pasay Telegram UX Freeze v1 targeted checks.

Focused pins for this slice only:
- frozen 4-button IA with role-language split;
- Home / Tasks / Rent / Expense / Units rendering convergence;
- Payments vs Activity separation;
- detail keyboards remove inline Home;
- deterministic fast-path for menu / callback.
"""
from __future__ import annotations

import pytest
from telegram import Update

from conftest import OWNER_ID, SECRETARY_ID, make_callback_update, make_text_update, run_updates
from pasay_bot.handlers import nl_bridge
from pasay_bot.keyboards import (
    ACTION_EXPENSE_OPEN,
    ACTION_RENT_QUICK_DETAIL,
    decode,
    expense_open_keyboard,
    fixed_menu_route_for,
    home_summary_keyboard,
    properties_quick_keyboard,
    rent_detail_keyboard,
    reply_keyboard,
    tasks_digest_keyboard,
    unit_detail_keyboard,
)
from pasay_bot.render import cards
from pasay_bot.roles import Role


def _labels(kb) -> list[str]:
    return [b.text for row in kb.keyboard for b in row]


def _inline_labels(kb) -> list[str]:
    return [b.text for row in kb.inline_keyboard for b in row]


def _group_text_update(user_id, chat_id, text, update_id=1, bot=None):
    return Update.de_json(
        {
            "update_id": update_id,
            "message": {
                "message_id": 1,
                "date": 0,
                "chat": {"id": chat_id, "type": "group"},
                "from": {"id": user_id, "is_bot": False, "first_name": "T", "username": "t"},
                "text": text,
            },
        },
        bot,
    )


def _seed_expense(env, expense_id=7, status="approved"):
    env.backend.add_expense(
        expense_id=expense_id,
        status=status,
        category="Repair",
        amount="7000.00",
        payee="Fix-It Co",
        unit_id=1,
    )


def _rent_row(unit="1680", amount="75000.00"):
    return {
        "kind": "rent_overdue",
        "unit": unit,
        "amount": amount,
        "overdue_days": 5,
        "unpaid_periods": 3,
    }


def _expense_row(expense_id=7, amount="7000.00", purpose="Repair"):
    return {
        "kind": "payable_expense",
        "expense_id": expense_id,
        "purpose": purpose,
        "amount": amount,
        "status": "approved",
    }


def test_owner_reply_keyboard_freeze_labels():
    assert _labels(reply_keyboard(Role.OWNER)) == ["🏠 首页", "✅ 待办", "💰 租金", "💸 支出"]


def test_secretary_reply_keyboard_freeze_labels():
    assert _labels(reply_keyboard(Role.SECRETARY)) == ["🏠 Home", "✅ Tasks", "💰 Rent", "💸 Expense"]


@pytest.mark.parametrize(
    ("label", "route"),
    [
        ("🏠 首页", "home"),
        ("✅ 待办", "tasks"),
        ("💰 租金", "rent"),
        ("💸 支出", "expense"),
    ],
)
def test_owner_fixed_menu_routes_are_frozen(label, route):
    assert fixed_menu_route_for(label) == route


@pytest.mark.parametrize(
    ("label", "route"),
    [
        ("🏠 Home", "home"),
        ("✅ Tasks", "tasks"),
        ("💰 Rent", "rent"),
        ("💸 Expense", "expense"),
    ],
)
def test_secretary_fixed_menu_routes_are_frozen(label, route):
    assert fixed_menu_route_for(label) == route


def test_group_menu_ia_does_not_expose_properties():
    owner_labels = _labels(reply_keyboard(Role.OWNER))
    secretary_labels = _labels(reply_keyboard(Role.SECRETARY))
    assert all("Properties" not in label for label in owner_labels + secretary_labels)


def test_home_keyboard_only_has_units_and_refresh():
    kb = home_summary_keyboard("zh")
    labels = _inline_labels(kb)
    assert labels == ["🏢 房源", "🔄 Refresh"]
    assert "🏠 Home" not in labels
    assert "⚠️ Today" not in labels


def test_home_card_is_summary_only():
    text = cards.home_summary_card(
        expected="363000.00",
        collected="150000.00",
        outstanding="213000.00",
        total_arrears="671000.00",
        overdue_count=6,
        expiring_count=1,
        vacant_count=1,
        payable_count=2,
        today_count=6,
        property_total=10,
        occupied_count=9,
        locale="zh",
    )
    assert "📞" not in text
    assert "1608" not in text and "1680" not in text
    assert "今日待办 6" in text


def test_tasks_keyboard_rent_rows_open_rent_detail():
    kb = tasks_digest_keyboard({"act_now": [_rent_row()]}, "zh")
    assert kb is not None
    parsed = decode(kb.inline_keyboard[0][0].callback_data)
    assert parsed["action"] == ACTION_RENT_QUICK_DETAIL


def test_tasks_keyboard_expense_rows_open_expense_detail():
    kb = tasks_digest_keyboard({"act_now": [_expense_row()]}, "en")
    assert kb is not None
    parsed = decode(kb.inline_keyboard[0][0].callback_data)
    assert parsed["action"] == ACTION_EXPENSE_OPEN


def test_tasks_keyboard_has_no_done_only_buttons():
    assert tasks_digest_keyboard({"done_today": [_rent_row()]}, "zh") is None


def test_rent_card_has_no_object_row_duplication():
    text = cards.rent_quick_card(
        {
            "expected_rent_total": "363000.00",
            "collected_rent": "150000.00",
            "outstanding_rent": "213000.00",
            "collection_rate": 41.3,
            "outstanding_total": "671000.00",
            "overdue": [_rent_row(unit="1680")],
        },
        "zh",
    )
    assert "1680" not in text
    assert "历史欠租" in text


def test_rent_keyboard_is_navigation_only_and_has_no_home_row():
    kb = rent_detail_keyboard(1, "zh", followed_up_today=True, followup_assigned=False)
    labels = _inline_labels(kb)
    assert "🏠 Home" not in labels
    assert "💳 付款记录" in labels
    assert "🕘 动态" in labels


def test_completed_today_rent_detail_hides_followup():
    kb = rent_detail_keyboard(1, "zh", followed_up_today=True, followup_assigned=False)
    assert "📞 催租" not in _inline_labels(kb)


def test_actionable_rent_detail_shows_followup():
    kb = rent_detail_keyboard(1, "en", followed_up_today=False, followup_assigned=False)
    assert "📞 Follow up" in _inline_labels(kb)


def test_payments_and_activity_are_two_distinct_callbacks():
    kb = rent_detail_keyboard(1, "zh", followed_up_today=False, followup_assigned=False)
    callbacks = [decode(btn.callback_data) for row in kb.inline_keyboard for btn in row if btn.callback_data]
    entities = {parsed["entity"] for parsed in callbacks}
    assert "unitpay" in entities
    assert "unitact" in entities


def test_units_keyboard_is_single_row_navigation_only():
    kb = properties_quick_keyboard(
        [{"unit_code": "1680", "property_name": "Bayshore", "status": "occupied", "tenant_name": "Juan"}],
        "zh",
        archive_link="https://example.com/archive",
    )
    assert _inline_labels(kb) == ["1680"]


def test_unit_detail_keyboard_has_no_inline_home():
    kb = unit_detail_keyboard(1, "zh", archive_link="https://example.com/archive")
    labels = _inline_labels(kb)
    assert "🏠 Home" not in labels
    assert "💰 租金" in labels and "📁 文件" in labels and "🕘 动态" in labels


def test_expense_card_is_summary_only():
    text = cards.expense_quick_card(
        {
            "month_total": "14000.00",
            "pending_approval_count": 2,
            "payable": [_expense_row(), _expense_row(expense_id=8)],
            "paid_records": [{"expense_id": 6, "purpose": "Utility", "amount": "5000.00", "status": "paid"}],
        },
        "zh",
    )
    assert "E7" not in text and "E8" not in text and "E6" not in text
    assert "待审批 2" in text and "待付款 2" in text


def test_expense_detail_keyboard_has_activity_and_no_home():
    kb = expense_open_keyboard(7, status="approved", locale="en")
    labels = _inline_labels(kb)
    assert "🏠 Home" not in labels
    assert "🕘 Activity" in labels


def test_owner_private_page_is_not_field_by_field_bilingual():
    text = cards.home_summary_card(
        expected="100.00",
        collected="50.00",
        outstanding="50.00",
        total_arrears="80.00",
        overdue_count=1,
        expiring_count=1,
        vacant_count=0,
        payable_count=1,
        today_count=1,
        property_total=2,
        occupied_count=2,
        locale="zh",
    )
    assert "Collection rate" not in text
    assert "Tasks today" not in text


def test_secretary_private_page_is_not_field_by_field_bilingual():
    text = cards.home_summary_card(
        expected="100.00",
        collected="50.00",
        outstanding="50.00",
        total_arrears="80.00",
        overdue_count=1,
        expiring_count=1,
        vacant_count=0,
        payable_count=1,
        today_count=1,
        property_total=2,
        occupied_count=2,
        locale="en",
    )
    assert "收缴率" not in text
    assert "今日待办" not in text


def test_menu_deterministic_path_bypasses_nl(make_app, monkeypatch):
    calls = []

    async def boom(*args, **kwargs):
        calls.append(True)
        raise AssertionError("NL bridge must not run")

    monkeypatch.setattr(nl_bridge, "handle_nl", boom)
    env = make_app()
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "🏠 首页", bot=env.bot)])
    assert calls == []
    assert "🏠 首页" in env.bot.last_send()["text"]


def test_callback_ack_precedes_render_and_local_ack_under_300ms(make_app):
    env = make_app()
    _seed_expense(env, expense_id=7, status="approved")
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, decode(encode:=None) if False else "v1:exo:7", bot=env.bot)])
    assert env.bot.calls[0]["type"] == "answer_callback_query"
    sample = env.app.bot_data["latency"].last("callback")
    assert 0 <= sample["callback_ack_ms"] < 300
    assert sample["callback_ack_ms"] <= sample["business_completed_ms"] <= sample["total_ms"]


def test_group_home_uses_same_ia_not_properties(make_app):
    env = make_app()
    run_updates(env, [_group_text_update(OWNER_ID, -10001, "🏠 Home", bot=env.bot)])
    send = env.bot.last_send()
    assert "Properties" not in send["text"]
    assert "🏢 房源" in send["text"] or "🏢 Units" in send["text"]
