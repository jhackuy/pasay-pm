"""P0-EXPENSE-PAID-CLOSEOUT-001 targeted regression (no full suite).

Scenario: old ₱6,000 PAID + new ₱6,001 APPROVED. Owner says "已经付款".
Expect:
  - the NEW ₱6,001 record becomes PAID;
  - the OLD ₱6,000 record is untouched;
  - the linked approval/payment task for the new expense is CLOSED;
  - a bilingual group notification (Paid / 已付款 · Expense completed /
    支出已完成) is pushed to the known operation group;
  - no new expense is created, no second approval.
"""
from __future__ import annotations

import time

from telegram import Update

from conftest import OWNER_ID, make_text_update, run_updates
from pasay_bot.keyboards import encode, new_nonce, now_ts

GROUP = -100909090909


def make_group_text_update(user_id, chat_id, text, message_id=1, update_id=1, bot=None):
    return Update.de_json(
        {
            "update_id": update_id,
            "message": {
                "message_id": message_id,
                "date": int(time.time()),
                "chat": {"id": chat_id, "type": "group", "title": "Pasay Group"},
                "from": {"id": user_id, "is_bot": False, "first_name": "T", "username": "t"},
                "text": text,
            },
        },
        bot,
    )


def _expense(env, expense_id):
    return next(e for e in env.backend.expenses if e["id"] == expense_id)


def test_paid_closeout_isolates_old_and_new(make_app):
    env = make_app()
    env.store.remember_group(GROUP, "Pasay Group")

    # Old ₱6,000 already PAID.
    env.backend.add_expense(expense_id=6000, category="维修", amount="6000.00",
                            payee="Fix-It Co", unit_id=1, status="paid")
    # New ₱6,001 APPROVED + linked PAYMENT_PENDING task.
    env.backend.add_expense(expense_id=6001, category="维修", amount="6001.00",
                            payee="Fix-It Co", unit_id=1, status="approved")
    env.backend.add_ops_task(
        task_id=9001, title="等待付款 · 维修", task_type="PAYMENT_PENDING",
        source_type="expense", source_id=6001, status="PENDING",
        due_at="2026-08-10T00:00:00+08:00",
    )
    # Owner's live context points at the NEW approved expense (saved by the
    # approval callback in the real flow).
    env.store.save_v2_context(
        GROUP, OWNER_ID,
        {"expense_ref": "6001", "expense_status": "approved", "intent": "expense"},
    )

    run_updates(
        env,
        [make_group_text_update(OWNER_ID, GROUP, "已经付款",
                                message_id=2, update_id=2, bot=env.bot)],
    )

    # NEW record -> PAID; OLD record untouched.
    assert _expense(env, 6001)["status"] == "paid"
    assert _expense(env, 6000)["status"] == "paid"
    assert _expense(env, 6000)["amount"] == "6000.00"
    assert len(env.backend.expenses) == 2  # no new expense

    # Linked task closed (no more waiting for payment).
    task = next(t for t in env.backend.operational_tasks if t["id"] == 9001)
    assert task["status"] == "COMPLETED"
    assert task["completed_at"] is not None

    # Bilingual group notification pushed after the Owner's private reply.
    group_texts = [
        s["text"] for s in env.bot.sends()
        if str(s["chat_id"]) == str(GROUP) and s.get("text")
    ]
    assert group_texts, "group must receive a paid notification"
    combined = "\n".join(group_texts)
    assert "Paid" in combined and "已付款" in combined
    assert "Expense completed" in combined and "支出已完成" in combined
    assert "₱6,001" in combined
    # The group notification (not the Owner's private reply) must carry the
    # Unit. In this test the Owner types in the group, so at least one of the
    # group-bound sends must include the Unit label.
    assert any("16B" in (t or "") for t in group_texts)
    assert "6000" not in combined  # never mentions the old record
