"""BOT-V1-USABLE-001 P0-3: deterministic NL queries return real data."""
from __future__ import annotations

from datetime import date, timedelta

from conftest import OWNER_ID, SECRETARY_ID, make_text_update, run_updates


def _send(env, user_id, text, message_id=1, update_id=1):
    run_updates(
        env,
        [make_text_update(user_id, user_id, text, message_id=message_id,
                          update_id=update_id, bot=env.bot)],
    )
    return env.bot.last_send()["text"]


def test_income_summary_query(make_app):
    """★ '这个月收了多少钱' -> direct income data, no menu."""
    env = make_app()
    text = _send(env, OWNER_ID, "这个月收了多少钱")
    assert "收入" in text
    assert "已收租金" in text
    assert "₱190,000" in text
    assert "未收租金" in text


def test_income_summary_month_query(make_app):
    env = make_app()
    text = _send(env, OWNER_ID, "8月份收入多少")
    assert "8月" in text
    assert "₱190,000" in text


def test_expense_summary_query(make_app):
    """★ '这个月花了多少钱' -> direct expense total + net income."""
    env = make_app()
    text = _send(env, OWNER_ID, "这个月花了多少钱")
    assert "支出" in text
    assert "₱19,650" in text
    assert "净收入" in text


def test_unit_expense_history_query(make_app):
    """★ '16B最近有什么支出' -> recent expenses of that unit (read-only)."""
    env = make_app()
    env.backend.add_expense(expense_id=5, category="维修", amount="3000.00",
                            payee="Fix-It Co", unit_id=1)
    text = _send(env, OWNER_ID, "16B最近有什么支出")
    assert "最近支出" in text
    assert "16B" in text
    assert "维修" in text
    assert "₱3,000" in text


def test_unit_info_query(make_app):
    """★ '16B是谁租的' / '16B租金多少' -> tenant, rent and lease end."""
    env = make_app()
    text = _send(env, OWNER_ID, "16B是谁租的")
    assert "房源信息" in text
    assert "Juan Dela Cruz" in text
    assert "₱55,000" in text
    assert "2026-12-31" in text


def test_contracts_expiring_query(make_app):
    """★ '有哪些合同快到期' -> real leases ending within 30 days."""
    env = make_app()
    end = (date.today() + timedelta(days=20)).isoformat()
    env.backend.leases[0]["end_date"] = end
    text = _send(env, OWNER_ID, "有哪些合同快到期")
    assert "合同快到期" in text
    assert "16B" in text
    assert "Juan Dela Cruz" in text
    assert end in text


def test_contracts_window_query(make_app):
    """★ '30天内有哪些合同到期' -> same direct data."""
    env = make_app()
    end = (date.today() + timedelta(days=10)).isoformat()
    env.backend.leases[0]["end_date"] = end
    text = _send(env, SECRETARY_ID, "30天内有哪些合同到期")
    assert "30" in text
    assert "16B" in text
    assert "Juan Dela Cruz" in text
    assert end in text


def test_queries_never_write(make_app):
    env = make_app()
    for text in ("这个月收了多少钱", "这个月花了多少钱", "有哪些合同快到期"):
        _send(env, OWNER_ID, text, message_id=2, update_id=2)
    for method, path, _body in env.backend.calls:
        assert method not in ("POST", "PATCH", "DELETE"), (method, path)
