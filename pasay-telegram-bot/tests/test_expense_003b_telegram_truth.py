"""PASAY-VNEXT-EXPENSE-OPERATION-003B — Telegram UX truth (E16).

Proves that the rendered bot copy NEVER presents a pending-verification payment
or a partial payment as "Paid/已付款":
- a `payment_claimed` expense reads "Payment reported · verification pending"
  (never "Paid");
- a `partially_paid` expense shows verified + remaining (never "Paid");
- the amount shown for a partial is the REMAINING balance, not the total.
"""
from __future__ import annotations

from decimal import Decimal

from pasay_bot.api_client import Expense
from pasay_bot.render import cards


def _expense(status: str, *, amount="28000.00", verified="0.00", remaining="28000.00",
             pending_claims=0) -> Expense:
    e = Expense.from_dict({
        "id": 5,
        "expense_date": "2026-08-01",
        "category": "维修",
        "amount": amount,
        "payee": "Fix-It Co",
        "description": "aircon",
        "unit_id": 1,
        "status": status,
        "receipt_attachment_id": None,
        "payment": {
            "verified_paid": verified,
            "remaining": remaining,
            "fully_paid": False,
            "pending_claims": pending_claims,
            "claims": [],
        },
    })
    return e


def test_e16_pending_claim_never_says_paid():
    exp = _expense("payment_claimed", pending_claims=1)
    detail = cards.expense_detail_card(exp, "en")
    assert "verification pending" in detail
    assert "Payment reported" in detail
    zh = cards.expense_detail_card(exp, "zh")
    assert "待核验" in zh
    assert "已上报付款" in zh
    # NEVER claims paid anywhere.
    for text in (detail, zh, cards.expense_result_card(exp, "en"),
                 cards.expense_result_card(exp, "zh")):
        assert "Paid" not in text
        assert "已付款" not in text
        assert "PAYMENT_CLAIMED" not in text


def test_e16_partial_never_says_paid_shows_remaining():
    exp = _expense("partially_paid", verified="10000.00", remaining="18000.00")
    detail = cards.expense_detail_card(exp, "en")
    assert "Partially paid" in detail
    assert "₱10,000" in detail and "₱18,000" in detail
    zh = cards.expense_detail_card(exp, "zh")
    assert "部分付款" in zh
    for text in (detail, zh):
        assert "Paid" not in text.replace("Partially", "")
        assert "已付款" not in text


def test_e16_approved_shows_waiting_not_paid():
    exp = _expense("approved")
    text = cards.expense_detail_card(exp, "en")
    assert "Waiting for payment" in text
    assert "Paid" not in text
