"""PASAY-V2-FOUNDATION-001 OWNER UX REPAIR: Journey A-J regression tests.

These mirror the real Telegram acceptance failures:
  A Repair (report -> progress -> finish, never a generic assistant)
  B Repair quote -> Expense Approval (report itself is NOT an expense)
  C Expense approval -> group bilingual notification
  D Expense reject -> group bilingual notification + task closed
  E Approved expense "已经付款" -> PAID on the SAME record (no new expense)
  F Cash payment completes without receipt
  G Optional receipt never blocks completion
  H Group replies are English + 中文
  I Menu self-healing sends the latest English keyboard
  J Correction changes the repair task association
"""
from __future__ import annotations

import time

from telegram import Update

from conftest import (
    OWNER_ID,
    SECRETARY_ID,
    make_callback_update,
    make_text_update,
    run_updates,
)
from pasay_bot.keyboards import encode, new_nonce, now_ts


GROUP = -100555666777


def _seed_units(env):
    """FakeBackend defaults only have 16B/17A/2C; add the acceptance units
    1680 (Bayshore) and 1805 (Solemare)."""
    env.backend.units.append(
        {"id": 8, "property_id": 1, "unit_number": "1680", "floor": "16",
         "size_sqm": "40.00", "monthly_rent": "75000.00", "status": "occupied",
         "is_active": True}
    )
    env.backend.units.append(
        {"id": 9, "property_id": 2, "unit_number": "1805", "floor": "18",
         "size_sqm": "40.00", "monthly_rent": "60000.00", "status": "occupied",
         "is_active": True}
    )
    env.store.remember_group(GROUP, "Pasay Group")


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


def _labels(kb):
    return [b.text for row in kb.keyboard for b in row]


def _sends(env):
    return [
        s for s in env.bot.sends()
        if s.get("text") is not None
    ]


def _last_text(env) -> str:
    return env.bot.last_send()["text"]


# --- Journey A: Repair lifecycle --------------------------------------------

def test_journey_a_repair_report_creates_pending_no_amount_question(make_app):
    env = make_app()
    _seed_units(env)
    run_updates(env, [make_group_text_update(OWNER_ID, GROUP, "1680 aircon leaking", bot=env.bot)])
    text = _last_text(env)
    assert "Repair reported" in text or "已登记维修" in text
    assert "金额" not in text and "amount" not in text.lower()
    assert env.backend.operational_tasks and env.backend.operational_tasks[-1]["status"] == "PENDING"


def test_journey_a_progress_updates_task_and_next_check(make_app):
    env = make_app()
    _seed_units(env)
    run_updates(env, [make_group_text_update(OWNER_ID, GROUP, "1680 aircon leaking", bot=env.bot)])
    run_updates(
        env,
        [make_group_text_update(OWNER_ID, GROUP, "technician coming tomorrow",
                                message_id=2, update_id=2, bot=env.bot)],
    )
    task = env.backend.operational_tasks[-1]
    assert task["status"] == "IN_PROGRESS"
    assert task["next_action"]
    assert task["next_check_at"]
    text = _last_text(env)
    assert "Repair in progress" in text or "维修处理中" in text


def test_journey_a_finished_completes_task(make_app):
    env = make_app()
    _seed_units(env)
    run_updates(env, [make_group_text_update(OWNER_ID, GROUP, "1680 aircon leaking", bot=env.bot)])
    run_updates(
        env,
        [make_group_text_update(OWNER_ID, GROUP, "technician coming tomorrow",
                                message_id=2, update_id=2, bot=env.bot)],
    )
    run_updates(
        env,
        [make_group_text_update(OWNER_ID, GROUP, "finished",
                                message_id=3, update_id=3, bot=env.bot)],
    )
    task = env.backend.operational_tasks[-1]
    assert task["status"] == "COMPLETED"
    assert "Task completed" in _last_text(env) or "任务已完成" in _last_text(env)


def test_journey_a_equivalent_phrases_work(make_app):
    """Not hard-coded strings: equivalent progress/completion expressions work."""
    env = make_app()
    _seed_units(env)
    run_updates(env, [make_group_text_update(OWNER_ID, GROUP, "1680 aircon leaking", bot=env.bot)])
    run_updates(
        env,
        [make_group_text_update(OWNER_ID, GROUP, "AC guy coming later",
                                message_id=2, update_id=2, bot=env.bot)],
    )
    assert env.backend.operational_tasks[-1]["status"] == "IN_PROGRESS"
    run_updates(
        env,
        [make_group_text_update(OWNER_ID, GROUP, "fixed already",
                                message_id=3, update_id=3, bot=env.bot)],
    )
    assert env.backend.operational_tasks[-1]["status"] == "COMPLETED"


# --- Journey B: Repair quote -> Expense Approval -----------------------------

def test_journey_b_repair_report_not_expense(make_app):
    """The report itself never creates an expense or asks for an amount."""
    env = make_app()
    _seed_units(env)
    run_updates(env, [make_group_text_update(OWNER_ID, GROUP, "1680 aircon leaking", bot=env.bot)])
    assert env.backend.expenses == []
    assert "金额" not in _last_text(env)


def test_journey_b_quote_creates_expense_approval(make_app):
    env = make_app()
    _seed_units(env)
    run_updates(env, [make_group_text_update(OWNER_ID, GROUP, "1680 aircon leaking", bot=env.bot)])
    run_updates(
        env,
        [make_group_text_update(OWNER_ID, GROUP, "technician quoted 7000",
                                message_id=2, update_id=2, bot=env.bot)],
    )
    assert env.backend.expenses, "quote must create one pending expense"
    assert env.backend.expenses[-1]["status"] == "pending"
    assert str(env.backend.expenses[-1]["amount"]).replace(".00", "") == "7000"
    assert len(env.backend.expenses) == 1


# --- Journey C/D: Approval / Reject group bilingual notification ------------

def test_journey_c_approve_pushes_group_bilingual_result(make_app):
    env = make_app()
    _seed_units(env)
    env.backend.add_expense(expense_id=61, category="维修", amount="7000.00",
                            payee="Fix-It Co", unit_id=1)
    approve = encode("exa", "61", "", nonce=new_nonce(), ts=now_ts())
    run_updates(env, [make_callback_update(OWNER_ID, GROUP, approve, bot=env.bot)])
    texts = "\n".join(env.bot.all_texts())
    assert "Approved" in texts and "已批准" in texts


def test_journey_d_reject_pushes_group_bilingual_result(make_app):
    env = make_app()
    _seed_units(env)
    env.backend.add_expense(expense_id=62, category="维修", amount="6000.00",
                            payee="Fix-It Co", unit_id=1)
    reject = encode("exr", "62", "", nonce=new_nonce(), ts=now_ts())
    run_updates(env, [make_callback_update(OWNER_ID, GROUP, reject, bot=env.bot)])
    texts = "\n".join(env.bot.all_texts())
    assert "Rejected" in texts and "已拒绝" in texts


# --- Journey E/F/G: Approved expense payment ---------------------------------

def _approved_expense_in_context(env, expense_id=71, amount="7000.00"):
    _seed_units(env)
    env.backend.add_expense(expense_id=expense_id, category="维修", amount=amount,
                            payee="Fix-It Co", unit_id=1)
    approve = encode("exa", str(expense_id), "", nonce=new_nonce(), ts=now_ts())
    run_updates(env, [make_callback_update(OWNER_ID, GROUP, approve, bot=env.bot)])
    return expense_id


def test_journey_e_already_paid_advances_same_expense(make_app):
    env = make_app()
    expense_id = _approved_expense_in_context(env)
    run_updates(
        env,
        [make_group_text_update(OWNER_ID, GROUP, "已经付款",
                                message_id=2, update_id=2, bot=env.bot)],
    )
    expense = next(e for e in env.backend.expenses if e["id"] == expense_id)
    assert expense["status"] == "paid"
    assert len(env.backend.expenses) == 1
    text = _last_text(env)
    assert "Paid" in text or "已付款" in text


def test_journey_f_cash_paid_completes_no_receipt(make_app):
    env = make_app()
    expense_id = _approved_expense_in_context(env, expense_id=72, amount="1500.00")
    run_updates(
        env,
        [make_group_text_update(OWNER_ID, GROUP, "paid cash",
                                message_id=2, update_id=2, bot=env.bot)],
    )
    expense = next(e for e in env.backend.expenses if e["id"] == expense_id)
    assert expense["status"] == "paid"
    assert "Receipt" in _last_text(env) or "凭证" in _last_text(env)


def test_journey_g_receipt_optional_no_block(make_app):
    """No image required: payment completes and never waits for a receipt."""
    env = make_app()
    expense_id = _approved_expense_in_context(env, expense_id=73, amount="900.00")
    run_updates(
        env,
        [make_group_text_update(OWNER_ID, GROUP, "payment done",
                                message_id=2, update_id=2, bot=env.bot)],
    )
    assert next(e for e in env.backend.expenses if e["id"] == expense_id)["status"] == "paid"
    assert "RECEIPT_MISSING" not in "\n".join(env.bot.all_texts())


# --- Journey H: group bilingual ----------------------------------------------

def test_journey_h_group_replies_bilingual(make_app):
    env = make_app()
    _seed_units(env)
    for label, en_word, zh_word in [
        ("🏠 Properties", "Properties", "房源"),
        ("✅ Tasks", "Tasks", "待办"),
        ("💰 Rent", "Rent", "租金"),
        ("💸 Expense", "Expenses", "支出"),
    ]:
        run_updates(
            env,
            [make_group_text_update(OWNER_ID, GROUP, label, message_id=1, update_id=1, bot=env.bot)],
        )
        text = _last_text(env)
        assert en_word in text and zh_word in text, f"{label}: {text[:120]}"


# --- Journey I: menu self-healing with the latest English keyboard ----------

def test_journey_i_group_interaction_attaches_latest_english_keyboard(make_app):
    env = make_app()
    _seed_units(env)
    run_updates(
        env,
        [make_group_text_update(OWNER_ID, GROUP, "hello", message_id=1, update_id=1, bot=env.bot)],
    )
    reply_sends = [
        s for s in env.bot.sends()
        if s["reply_markup"] is not None
        and s["reply_markup"].__class__.__name__ == "ReplyKeyboardMarkup"
    ]
    assert reply_sends
    assert _labels(reply_sends[-1]["reply_markup"]) == [
        "🏠 Properties", "✅ Tasks", "💰 Rent", "💸 Expense",
    ]


# --- Journey J: correction ------------------------------------------------

def test_journey_j_correction_changes_task_association(make_app):
    env = make_app()
    _seed_units(env)
    run_updates(env, [make_group_text_update(OWNER_ID, GROUP, "1680 aircon leaking", bot=env.bot)])
    run_updates(
        env,
        [make_group_text_update(OWNER_ID, GROUP, "不是1680，是1805",
                                message_id=2, update_id=2, bot=env.bot)],
    )
    ctx = env.store.get_v2_context(GROUP, OWNER_ID)
    assert ctx and ctx["payload"].get("unit_token") == "1805"
