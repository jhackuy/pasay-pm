"""EXPENSE-UX-FIX-001: expense quick-view card rendering.

Card-level pins for the reworked 💸 Expense page:

1. Month total, then the pending-payment queue (APPROVED, unpaid) built from
   the REAL expense fields, then this month's PAID records.
2. Expense IDs render as plain ``E{id}`` — never the ``#E{id}`` Telegram
   hashtag.
3. No placeholder garbage (`??` / `None` / `null` / `undefined`) ever renders.
4. An APPROVED expense appears exactly once per page (no unresolved-task
   duplicate block).
5. PAID records keep rendering; bilingual zh / en / bi stays intact.
"""
from __future__ import annotations

import re

from pasay_bot.render import cards


def _record(status: str, *, amount: str = "6001.00", expense_id: int = 101,
            purpose: str = "Repair / 维修", category: str | None = None,
            unit: str = "1680", expense_date: str = "2026-08-15") -> dict:
    row = {
        "expense_id": expense_id,
        "unit": unit,
        "unit_code": unit,
        "purpose": purpose,
        "category": category,
        "amount": amount,
        "expense_date": expense_date,
        "status": status,
    }
    return {k: v for k, v in row.items() if v is not None}


def _payable_row(*, expense_id: int, amount: str = "7000.00",
                 purpose: str = "Repair / 维修", unit: str = "1680",
                 expense_date: str = "2026-08-15") -> dict:
    return {
        "kind": "payable_expense",
        "expense_id": expense_id,
        "unit": unit,
        "unit_code": unit,
        "purpose": purpose,
        "amount": amount,
        "expense_date": expense_date,
        "status": "approved",
    }


def _data(*, month_total: str = "9501.00", payable=None, paid_records=None,
          records=None, pending_count: int = 0, pending_amount: str = "0.00",
          unresolved: list | None = None) -> dict:
    return {
        "month_total": month_total,
        "pending_approval_count": pending_count,
        "pending_approval_amount": pending_amount,
        "unresolved_expense_tasks": unresolved if unresolved is not None else [],
        "records": records if records is not None else [],
        "payable": payable if payable is not None else [],
        "paid_records": paid_records if paid_records is not None else [],
    }


# --- 1/6: Expense IDs are plain text, never a Telegram hashtag -------------

def test_expense_quick_card_never_renders_hashtag_expense_id():
    text = cards.expense_quick_card(
        _data(
            payable=[_payable_row(expense_id=7), _payable_row(expense_id=8)],
            paid_records=[_record("paid", expense_id=23)],
        ),
        locale="zh",
    )
    assert "#E" not in text
    assert re.search(r"#E\d+", text) is None
    assert "E7" in text and "E8" in text and "E23" in text
    en = cards.expense_quick_card(
        _data(payable=[_payable_row(expense_id=7)]), locale="en"
    )
    assert re.search(r"#E\d+", en) is None


# --- 2/6: placeholder sentinels never render --------------------------------

def test_expense_quick_card_never_renders_placeholders():
    """`??`, None, null, undefined must never reach the page — including a
    payable row whose purpose mapping would have hit a legacy `??` category."""
    rows = [
        _payable_row(expense_id=7, purpose="Repair / 维修"),
        _payable_row(expense_id=8, purpose=""),
    ]
    rows[1]["category"] = "??"
    rows[1]["payee"] = "Carpenter"
    data = _data(
        payable=rows,
        paid_records=[_record("paid", expense_id=23, purpose="维修")],
        month_total="0.00",
    )
    for loc in ("zh", "en", "bi"):
        text = cards.expense_quick_card(data, locale=loc)
        for banned in ("??", "None", "null", "undefined"):
            assert banned not in text, f"{loc}: banned {banned!r} present"
        assert "Repair / 维修" in text
        assert "Carpenter" in text  # real payee fallback renders truthfully


# --- 3/6: APPROVED unpaid rows show the real fields -------------------------

def test_expense_quick_card_payable_row_shows_all_real_fields():
    text = cards.expense_quick_card(
        _data(
            payable=[_payable_row(expense_id=8, purpose="Repair / 维修")],
            month_total="7000.00",
        ),
        locale="bi",
    )
    assert "待付款 · 1" in text or "Pending payment" in text
    # Expense ID · Unit · Purpose · Amount · Date · pending-payment status
    assert "E8 · 1680 · Repair / 维修 · <b>₱7,000</b> · 08-15" in text
    assert "Approved / 待付款" in text


def test_expense_quick_card_payable_row_purpose_fallback_never_question_mark():
    """A legacy `??` category is dropped; the truthful payee renders instead
    of the placeholder (real-field mapping, not a text replacement)."""
    row = _payable_row(expense_id=7, purpose="")
    row["category"] = "??"
    row["payee"] = "Repair"
    text = cards.expense_quick_card(
        _data(payable=[row], month_total="7000.00"), locale="zh"
    )
    assert "E7 · 1680 · Repair" in text
    assert "??" not in text


# --- 4/6: the same APPROVED expense appears exactly once --------------------

def test_expense_quick_card_approved_expense_not_duplicated():
    """The unresolved-task text block is gone; a payable APPROVED expense is
    listed once in the pending-payment section and never repeated below."""
    data = _data(
        payable=[_payable_row(expense_id=7), _payable_row(expense_id=8)],
        paid_records=[_record("paid", expense_id=23)],
        records=[
            _record("paid", expense_id=23),
            _record("approved", expense_id=7, purpose="Repair / 维修"),
            _record("approved", expense_id=8, purpose="Repair / 维修"),
        ],
        unresolved=[
            {"title": "待付款支出 · ??", "status": "PENDING",
             "property_code": "1680", "due_at": "2026-08-15T00:00:00+08:00"},
        ],
        month_total="52603.00",
    )
    text = cards.expense_quick_card(data, locale="zh")
    # each expense id appears exactly once in the rendered page
    assert text.count("E7") == 1
    assert text.count("E8") == 1
    assert text.count("E23") == 1
    assert "待付款支出" not in text  # no unresolved task block / duplicate rows
    assert "未解决" not in text
    assert "无未解决支出事项" not in text


# --- 5/6: PAID records keep rendering ---------------------------------------

def test_expense_quick_card_paid_records_still_shown():
    text = cards.expense_quick_card(
        _data(paid_records=[_record("paid", expense_id=23)]), locale="zh"
    )
    assert "E23 · 1680 · Repair / 维修 · <b>₱6,001</b> · 08-15 · ✅ 已付款" in text


def test_expense_quick_card_true_empty_state_only_when_no_records():
    empty = cards.expense_quick_card(_data(month_total="0.00"), locale="zh")
    assert "本月暂无支出记录" in empty
    paid = cards.expense_quick_card(
        _data(paid_records=[_record("paid")], month_total="0.00"), locale="zh"
    )
    assert "本月暂无支出记录" not in paid
    assert "✅ 已付款" in paid


# --- 6/6: bilingual UX does not regress -------------------------------------

def test_expense_quick_card_bilingual_group():
    text = cards.expense_quick_card(
        _data(
            payable=[_payable_row(expense_id=8)],
            paid_records=[_record("paid", expense_id=23)],
        ),
        locale="bi",
    )
    assert "Expenses / 支出" in text
    assert "This month:" in text and "本月：" in text
    assert "Pending payment / 待付款" in text
    assert "Paid / 已付款" in text


def test_expense_quick_card_en_english_only():
    text = cards.expense_quick_card(
        _data(
            payable=[_payable_row(expense_id=8)],
            paid_records=[_record("paid", expense_id=23)],
        ),
        locale="en",
    )
    assert "Pending payment" in text
    assert "Paid" in text
    assert "已付款" not in text
    assert "待付款" not in text


# --- backward-compatible payload: derives paid from records -----------------

def test_expense_quick_card_falls_back_to_records_when_new_fields_absent():
    """An older backend payload without `payable`/`paid_records` still renders:
    paid records are derived from `records`, and APPROVED rows are NOT
    duplicated (records' approved rows are never re-rendered as payable)."""
    data = {
        "month_total": "9501.00",
        "pending_approval_count": 0,
        "pending_approval_amount": "0.00",
        "unresolved_expense_tasks": [],
        "records": [
            _record("paid", expense_id=101),
            _record("approved", expense_id=102, amount="3500.00"),
            _record("pending", expense_id=103, amount="1200.00"),
        ],
    }
    text = cards.expense_quick_card(data, locale="zh")
    assert "本月：₱9,501" in text
    assert "E101 · 1680 · Repair / 维修 · <b>₱6,001</b> · 08-15 · ✅ 已付款" in text
    # approved row is not rendered as a duplicate payable row in legacy mode
    assert text.count("E102") == 0
