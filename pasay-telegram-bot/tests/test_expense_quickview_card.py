"""P1-EXPENSE-QUICKVIEW-LIST-001: expense quick-view card rendering.

Card-level pins for the expense record list that appears under the month
total: Unit · Purpose · Amount · MM-DD · Status, PAID included, bilingual in
groups, and a real empty state only when there are no records.
"""
from __future__ import annotations

from pasay_bot.render import cards


def _record(status: str, *, amount: str = "6001.00") -> dict:
    return {
        "unit": "1680",
        "unit_code": "1680",
        "purpose": "Repair / 维修",
        "amount": amount,
        "expense_date": "2026-08-15",
        "status": status,
    }


def _data(*records: dict, month_total: str = "9501.00") -> dict:
    return {
        "month_total": month_total,
        "pending_approval_count": 0,
        "pending_approval_amount": "0.00",
        "unresolved_expense_tasks": [],
        "records": list(records),
    }


def test_expense_quick_card_zh_lists_paid_and_approved():
    text = cards.expense_quick_card(
        _data(
            _record("paid"),
            _record("approved", amount="3500.00"),
            _record("pending"),
        ),
        locale="zh",
    )
    assert "本月支出记录" in text
    assert "1680 · Repair / 维修 · <b>₱6,001</b> · 08-15 · ✅ 已付款" in text
    assert "1680 · Repair / 维修 · <b>₱3,500</b> · 08-15 · 📋 已批准" in text
    assert "1680 · Repair / 维修 · <b>₱6,001</b> · 08-15 · ⏳ 待批准" in text
    assert "本月：₱9,501" in text
    # auxiliary unresolved status stays, but never replaces the list
    assert "无未解决支出事项" in text


def test_expense_quick_card_en_lists_records_english_only():
    text = cards.expense_quick_card(
        _data(_record("paid"), _record("approved")), locale="en"
    )
    assert "This month expenses" in text
    assert "✅ Paid" in text
    assert "📋 Approved" in text
    assert "08-15" in text
    assert "已付款" not in text


def test_expense_quick_card_group_bilingual():
    text = cards.expense_quick_card(_data(_record("paid")), locale="bi")
    assert "This month expenses / 本月支出记录" in text
    assert "Paid / 已付款" in text


def test_expense_quick_card_true_empty_state_only_when_no_records():
    empty = cards.expense_quick_card(_data(month_total="0.00"), locale="zh")
    assert "本月暂无支出记录" in empty
    assert "无未解决支出事项" in empty
    # a zero month total with real records still shows the list, never empty
    paid = cards.expense_quick_card(
        _data(_record("paid"), month_total="0.00"), locale="zh"
    )
    assert "本月暂无支出记录" not in paid
    assert "✅ 已付款" in paid


def test_expense_quick_card_purpose_never_renders_question_placeholders():
    """PASAY-V2-EXPENSE-UX-AUDIT-005 Test B: purpose falls back
    purpose -> category -> description, else `Other / 其他`. `??`, None, null
    and empty values never render, and no raw placeholder appears."""
    rows = [
        {**_record("paid"), "purpose": "??", "category": "??"},
        {**_record("approved", amount="3500.00"), "purpose": None,
         "category": None, "description": "Water / 水费"},
        {**_record("pending", amount="500.00"), "purpose": None,
         "category": "", "description": ""},
        {**_record("paid", amount="777.00"), "purpose": "Repair / 维修"},
    ]
    zh = cards.expense_quick_card(_data(*rows), locale="zh")
    assert "· Other / 其他 ·" in zh
    assert "Water / 水费" in zh
    assert "Repair / 维修" in zh
    assert "??" not in zh  # placeholder never reaches the render
    en = cards.expense_quick_card(_data(*rows), locale="en")
    assert "· Other ·" in en
    assert "??" not in en
