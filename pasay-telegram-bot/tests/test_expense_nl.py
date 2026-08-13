"""BOT-V1-USABLE-001 P0-2: deterministic expense entry + approval flow."""
from __future__ import annotations

from conftest import (
    OWNER_ID,
    SECRETARY_ID,
    make_callback_update,
    make_text_update,
    run_updates,
)
from pasay_bot.handlers.expense_flow import parse_expense_statement
from pasay_bot.keyboards import (
    decode,
    encode,
    new_nonce,
    now_ts,
)


def _buttons(kb):
    if kb is None:
        return []
    return [b for row in kb.inline_keyboard for b in row]


def _labels(kb):
    return [b.text for b in _buttons(kb)]


def _submit_data(env):
    conv = env.store.get_conversation(OWNER_ID, OWNER_ID)
    return encode("exc", "exp", nonce=conv["nonce"], ts=now_ts())


def _edit_data(sub):
    return encode("exe", sub)


# --- deterministic statement parsing ---------------------------------------

def test_expense_statement_parsing():
    cases = {
        "支出1680水费2500": ("水费", "1680", "2500"),
        "1680水费2500": ("水费", "1680", "2500"),
        "1680刚交了2500水费": ("水费", "1680", "2500"),
        "付了1680电费3800": ("电费", "1680", "3800"),
    }
    for text, (category, unit, amount) in cases.items():
        stmt = parse_expense_statement(text)
        assert stmt is not None, text
        assert stmt.category == category, text
        assert stmt.unit_token == unit, text
        assert str(stmt.amount) == amount, text


def test_expense_statement_rejects_queries_and_bare_unit():
    for text in (
        "1680这个月水费交了吗",
        "1680水费多少",
        "1680水费",
        "谁还没交房租",
    ):
        assert parse_expense_statement(text) is None, text


# --- flow: statement -> confirm -> submit -> Owner approval ----------------

def test_secretary_expense_statement_confirm_and_submit(make_app):
    """★ '支出16B水费2500' -> deterministic confirm card -> [提交审批] creates
    ONE pending expense and pushes the Owner approval card (action-at-source)."""
    env = make_app()
    run_updates(
        env,
        [make_text_update(SECRETARY_ID, SECRETARY_ID, "支出16B水费2500", bot=env.bot)],
    )
    card = env.bot.last_send()
    assert "Confirm expense" in card["text"]
    assert "16B" in card["text"]
    assert "水费" in card["text"]
    assert "₱2,500" in card["text"]
    labels = _labels(card["reply_markup"])
    assert "Submit for approval" in labels
    assert "✏️ Edit" in labels
    assert "❌ Cancel" in labels

    conv = env.store.get_conversation(SECRETARY_ID, SECRETARY_ID)
    assert conv is not None and conv["state"] == "expense_confirm"
    data = encode("exc", "exp", nonce=conv["nonce"], ts=now_ts())
    run_updates(
        env,
        [make_callback_update(SECRETARY_ID, SECRETARY_ID, data, bot=env.bot)],
    )
    assert env.backend.count_calls("POST", "/expenses") == 1
    expense = env.backend.expenses[-1]
    assert expense["status"] == "pending"
    assert expense["category"] == "水费"
    assert expense["unit_id"] == 1  # 16B
    # message mutation: the tapped card shows the submitted state
    edit = env.bot.last_edit()
    assert "Submitted for approval" in edit["text"]
    # Owner received the approval card with deterministic approve/reject
    owner_calls = [c for c in env.bot.calls if c.get("chat_id") == OWNER_ID]
    owner_texts = "".join(c.get("text") or "" for c in owner_calls)
    assert "支出待批准" in owner_texts
    owner_send = env.bot.sends()[-1]
    assert owner_send["chat_id"] == OWNER_ID
    labels = _labels(owner_send["reply_markup"])
    assert "✅ 批准" in labels and "❌ 拒绝" in labels


def test_owner_approves_secretary_expense(make_app):
    """★ Owner taps [✅ 批准] on the pushed card -> backend expense approved,
    card mutated to the result (existing deterministic path, no LLM)."""
    env = make_app()
    run_updates(
        env,
        [make_text_update(SECRETARY_ID, SECRETARY_ID, "支出16B电费800", bot=env.bot)],
    )
    conv = env.store.get_conversation(SECRETARY_ID, SECRETARY_ID)
    data = encode("exc", "exp", nonce=conv["nonce"], ts=now_ts())
    run_updates(
        env,
        [make_callback_update(SECRETARY_ID, SECRETARY_ID, data, bot=env.bot)],
    )
    expense_id = env.backend.expenses[-1]["id"]
    approve = encode("exa", str(expense_id), "", nonce=new_nonce(), ts=now_ts())
    before_calls = len(env.bot.calls)
    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, approve, bot=env.bot)],
    )
    assert env.backend._get_expense(expense_id)["status"] == "approved"
    edit = env.bot.last_edit()
    assert "已批准" in edit["text"]
    assert len(env.bot.calls) == before_calls + 1  # single answer, no junk


def test_expense_submit_is_idempotent(make_app):
    """★ double-tap [提交审批] -> exactly one POST /expenses."""
    env = make_app()
    run_updates(
        env,
        [make_text_update(SECRETARY_ID, SECRETARY_ID, "支出16B维修5000", bot=env.bot)],
    )
    conv = env.store.get_conversation(SECRETARY_ID, SECRETARY_ID)
    data = encode("exc", "exp", nonce=conv["nonce"], ts=now_ts())
    run_updates(
        env,
        [
            make_callback_update(SECRETARY_ID, SECRETARY_ID, data, update_id=2, bot=env.bot),
            make_callback_update(SECRETARY_ID, SECRETARY_ID, data, update_id=3, bot=env.bot),
        ],
    )
    assert env.backend.count_calls("POST", "/expenses") == 1
    assert len(env.backend.expenses) == 1


def test_expense_edit_amount_rerenders_confirm(make_app):
    """★ [✏️ 修改] -> 金额 -> free-text amount -> confirm card re-rendered
    (edit-first, no new card)."""
    env = make_app()
    run_updates(
        env,
        [make_text_update(SECRETARY_ID, SECRETARY_ID, "支出16B水费2500", bot=env.bot)],
    )
    card = env.bot.last_send()
    message_id = card["message_id"]
    run_updates(
        env,
        [make_callback_update(SECRETARY_ID, SECRETARY_ID, _edit_data("amount"), bot=env.bot)],
    )
    assert env.store.get_conversation(SECRETARY_ID, SECRETARY_ID)["state"] == "expense_edit_amount"
    run_updates(
        env,
        [make_text_update(SECRETARY_ID, SECRETARY_ID, "3000", message_id=3, update_id=3, bot=env.bot)],
    )
    edit = env.bot.last_edit()
    assert edit["message_id"] == message_id
    assert "₱3,000" in edit["text"]
    assert env.store.get_conversation(SECRETARY_ID, SECRETARY_ID)["state"] == "expense_confirm"


def test_expense_statement_unknown_unit_never_help(make_app):
    """★ Unknown unit token -> friendly explain (no /help, no menu detour)."""
    env = make_app()
    run_updates(
        env,
        [make_text_update(SECRETARY_ID, SECRETARY_ID, "支出9999水费100", bot=env.bot)],
    )
    text = env.bot.last_send()["text"]
    assert "No unit found for 9999" in text
    assert "/help" not in text
