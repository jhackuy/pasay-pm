"""V1.3 Slice 1: expense approval action cards + unified to-do human text.

Covers the core loop: approval / rejection callbacks, idempotent duplicates,
Owner-only authorization, message mutation (no junk messages), edit-failure
fallback, human-readable status (no internal enums) and the role-specific
persistent bottom keyboards."""
from __future__ import annotations

from types import SimpleNamespace

from conftest import (
    OWNER_ID,
    SECRETARY_ID,
    FakeBot,
    make_callback_update,
    make_text_update,
    run_updates,
)
from telegram.error import BadRequest

from pasay_bot.api_client import Expense
from pasay_bot.keyboards import (
    decode,
    encode,
    expense_approval_keyboard,
    new_nonce,
    now_ts,
    reply_keyboard,
)
from pasay_bot.render import cards
from pasay_bot.roles import Role


def _expense_data(expense_id=5):
    return encode("exa", str(expense_id), "", nonce=new_nonce(), ts=now_ts())


def _reject_data(expense_id=5):
    return encode("exr", str(expense_id), "", nonce=new_nonce(), ts=now_ts())


def _detail_data(expense_id=5):
    return encode("exd", str(expense_id))


def _seed_pending_expense(env, expense_id=5, receipt=True):
    return env.backend.add_expense(
        expense_id=expense_id, category="维修", amount="5000.00",
        payee="Fix-It Co", unit_id=1, receipt_attachment_id=7 if receipt else None,
    )


class _FailingEditBot(FakeBot):
    """edit_message_text always fails (e.g. message deleted / chat not found):
    the handler must fall back to sending a new message with the result."""

    def __init__(self):
        super().__init__()
        self.edit_attempts = 0

    async def edit_message_text(self, text=None, chat_id=None, message_id=None,
                                parse_mode=None, reply_markup=None, **kw):
        self.edit_attempts += 1
        raise BadRequest("chat not found")


def _buttons(kb):
    if kb is None:
        return []
    return [b for row in kb.inline_keyboard for b in row]


def _action_labels(kb):
    return [
        (b.text, decode(b.callback_data)["action"] if decode(b.callback_data) else "")
        for b in _buttons(kb)
    ]


# --- persistent bottom navigation -------------------------------------------

def test_reply_keyboard_role_specific_labels():
    owner = reply_keyboard(Role.OWNER)
    secretary = reply_keyboard(Role.SECRETARY)
    owner_labels = [b.text for row in owner.keyboard for b in row]
    sec_labels = [b.text for row in secretary.keyboard for b in row]
    # BOT-V1-USABLE-001: one identical 4-button menu for both roles.
    assert owner_labels == ["🏠 首页", "✅ 待办", "💰 收租", "💸 支出"]
    assert sec_labels == ["🏠 首页", "✅ 待办", "💰 收租", "💸 支出"]
    assert owner.resize_keyboard is True
    assert secretary.resize_keyboard is True
    assert owner.is_persistent is True
    assert secretary.is_persistent is True


# --- human-readable cards ----------------------------------------------------

def test_expense_cards_never_show_internal_enums():
    expense = Expense.from_dict(
        {
            "id": 5,
            "expense_date": "2026-08-01",
            "due_date": "2026-08-15",
            "category": "维修",
            "amount": "5000.00",
            "payee": "Fix-It Co",
            "description": "空调维修",
            "unit_id": 1,
            "status": "pending",
            "receipt_attachment_id": 7,
        }
    )
    approval = cards.expense_approval_card(expense, "zh", location="Pasay Premier Residences · Unit 16B")
    assert "💳 支出待批准" in approval
    assert "Pasay Premier Residences · Unit 16B" in approval
    assert "₱5,000" in approval
    assert "Fix-It Co" in approval
    assert "✓ 有凭证" in approval
    for banned in ("APPROVAL_PENDING", "PAYMENT_PENDING", "RENT_DUE", "expense_id", "#5"):
        assert banned not in approval

    # Degraded path (property/unit lookup failed): no technical "Unit {id}"
    # fallback; the location line is hidden instead (SLICE1-UX-003).
    approval_no_loc = cards.expense_approval_card(expense, "zh")
    assert "Unit 1" not in approval_no_loc
    assert "Unit" not in approval_no_loc

    # Unknown status never renders as a raw enum in the detail card.
    unknown = Expense.from_dict(expense.as_dict() | {"status": "APPROVAL_PENDING"})
    detail_unknown = cards.expense_detail_card(unknown, "zh", location="Pasay Premier Residences · Unit 16B")
    assert "APPROVAL_PENDING" not in detail_unknown
    assert "状态：—" in detail_unknown

    approved = Expense.from_dict(expense.as_dict() | {"status": "approved"})
    result = cards.expense_result_card(approved, "zh")
    assert "✅ <b>已批准</b>" in result
    assert "下一步：等待付款" in result

    rejected = Expense.from_dict(expense.as_dict() | {"status": "rejected"})
    result_rej = cards.expense_result_card(rejected, "zh")
    assert "❌ <b>已拒绝</b>" in result_rej
    assert "已结束" in result_rej


def test_todo_unnamed_task_never_shows_internal_id():
    """A task without a title falls back to a human label, not #<id>."""
    task = SimpleNamespace(id=42, title=None, due_at=None)
    text = cards.todo_overview_card({"tasks": [task]}, "zh")
    assert "#42" not in text
    assert "🛠 事项" in text


# --- approval / rejection callbacks -----------------------------------------

def test_approval_callback_mutates_original_message(make_app):
    """★ Owner taps [✅ 批准] -> one backend write + the ORIGINAL message is
    edited to the result card; no new 'success' message is sent."""
    env = make_app()
    _seed_pending_expense(env)
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, _expense_data(), bot=env.bot)])
    assert env.backend.count_calls("POST", "/expenses/5/approve") == 1
    expense = env.backend._get_expense(5)
    assert expense["status"] == "approved"
    # message mutation: exactly one edit of message 10, zero new sends
    assert len(env.bot.sends()) == 0
    edits = env.bot.edits()
    assert len(edits) == 1
    assert edits[0]["message_id"] == 10
    assert "✅ <b>已批准</b>" in edits[0]["text"]
    assert "下一步：等待付款" in edits[0]["text"]
    assert env.bot.calls[0]["type"] == "answer_callback_query"  # answer first


def test_rejection_callback_mutates_original_message(make_app):
    env = make_app()
    _seed_pending_expense(env)
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, _reject_data(), bot=env.bot)])
    assert env.backend.count_calls("POST", "/expenses/5/reject") == 1
    assert env.backend._get_expense(5)["status"] == "rejected"
    assert len(env.bot.sends()) == 0
    edit = env.bot.last_edit()
    assert "❌ <b>已拒绝</b>" in edit["text"]
    assert "已结束" in edit["text"]


def test_duplicate_callback_idempotent_single_write(make_app):
    """★ Double-tap on the same approve button writes the backend exactly once."""
    env = make_app()
    _seed_pending_expense(env)
    data = _expense_data()
    run_updates(
        env,
        [
            make_callback_update(OWNER_ID, OWNER_ID, data, update_id=2, bot=env.bot),
            make_callback_update(OWNER_ID, OWNER_ID, data, update_id=3, bot=env.bot),
        ],
    )
    assert env.backend.count_calls("POST", "/expenses/5/approve") == 1
    assert env.bot.last_answer()["text"] == "✅ 这笔支出已处理过了。"


def test_already_processed_never_writes_again(make_app):
    """★ A stale approve tap on an already-rejected expense re-renders the
    current state card and never calls the backend write."""
    env = make_app()
    _seed_pending_expense(env)
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, _reject_data(), bot=env.bot)])
    assert env.backend._get_expense(5)["status"] == "rejected"
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, _expense_data(), update_id=4, bot=env.bot)])
    assert env.backend.count_calls("POST", "/expenses/5/approve") == 0
    assert "处理中" in (env.bot.last_answer()["text"] or "")
    assert "❌ <b>已拒绝</b>" in env.bot.last_edit()["text"]


def test_unauthorized_secretary_refused_card_unchanged(make_app):
    """★ SECRETARY tapping approve gets a human permission toast; the card and
    backend state stay untouched."""
    env = make_app()
    _seed_pending_expense(env)
    before = len(env.bot.calls)
    run_updates(
        env,
        [make_callback_update(SECRETARY_ID, SECRETARY_ID, _expense_data(), bot=env.bot)],
    )
    # SECRETARY locale is English -> human English permission toast.
    assert env.bot.last_answer()["text"] == "⚠️ Only the Owner can approve or reject expenses."
    assert env.backend.count_calls("POST", "/expenses/5/approve") == 0
    assert env.backend._get_expense(5)["status"] == "pending"
    assert len(env.bot.edits()) == 0  # original card unchanged
    assert len(env.bot.calls) == before + 1  # single answer, no edit


def test_backend_error_keeps_original_card(make_app):
    """★ A backend failure answers a human warning and does NOT destroy the
    original approval card."""
    env = make_app()
    _seed_pending_expense(env)
    env.backend.fail_status["/expenses/5/approve"] = 500
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, _expense_data(), bot=env.bot)])
    # one processing answer + durable error ON the card (never silent)
    assert "处理中" in (env.bot.last_answer()["text"] or "")
    assert "forced 500" in env.bot.last_edit()["text"]
    assert len(env.bot.sends()) == 0
    assert env.backend._get_expense(5)["status"] == "pending"


def test_edit_failure_falls_back_to_new_message(make_app):
    """★ When editMessageText fails, the result is still delivered as a new
    message instead of being lost."""
    env = make_app(bot=_FailingEditBot())
    _seed_pending_expense(env)
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, _expense_data(), bot=env.bot)])
    assert env.bot.edit_attempts >= 1
    assert env.backend.count_calls("POST", "/expenses/5/approve") == 1
    sends = env.bot.sends()
    assert sends, "edit failed -> result must be sent as a new message"
    assert "✅ <b>已批准</b>" in sends[-1]["text"]


def test_expense_detail_callback(make_app):
    """★ [📎 查看凭证/详情] shows the human detail; approve/reject stay while
    pending and disappear once the expense is processed."""
    env = make_app()
    _seed_pending_expense(env)
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, _detail_data(), bot=env.bot)])
    edit = env.bot.last_edit()
    assert "💳 支出详情" in edit["text"]
    assert "Pasay Premier Residences · 16B" in edit["text"]
    labels = [b.text for b in _buttons(edit["reply_markup"])]
    assert "✅ 批准" in labels and "❌ 拒绝" in labels

    # after approval, the detail card no longer offers approve/reject
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, _expense_data(), update_id=3, bot=env.bot)])
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, _detail_data(), update_id=4, bot=env.bot)])
    labels = [b.text for b in _buttons(env.bot.last_edit()["reply_markup"])]
    assert "✅ 批准" not in labels
    assert "❌ 拒绝" not in labels
    assert "🏠 首页" in labels


def test_todo_page_human_readable_no_internal_enums(make_app):
    """★ The unified to-do page renders expense + task rows in human text;
    APPROVAL_PENDING / RENT_DUE / PAYMENT_PENDING never reach the UI."""
    env = make_app()
    _seed_pending_expense(env)
    env.backend.add_ops_task(
        task_id=1, title="待批准支出 · 维修", task_type="APPROVAL_PENDING",
        due_at="2026-08-10T00:00:00+08:00",
    )
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "/todo", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "支出待批准 · 1笔" in text
    assert "待批准支出 · 维修" in text
    for banned in ("APPROVAL_PENDING", "RENT_DUE", "PAYMENT_PENDING"):
        assert banned not in text
    kb = env.bot.last_send()["reply_markup"]
    actions = [decode(b.callback_data)["action"] for b in _buttons(kb) if decode(b.callback_data)]
    assert "exa" in actions and "exr" in actions and "exd" in actions
    assert "tkc" in actions and "tkd" in actions


def test_expense_approval_keyboard_secondary_label_depends_on_receipt():
    """★ Secondary button says 📎 查看凭证 only when a receipt exists; without
    one it says 查看详情. The callback data stays v1:exd:<id> either way."""
    with_receipt = _buttons(expense_approval_keyboard(5, "zh", has_receipt=True))
    labels = [b.text for b in with_receipt]
    assert "📎 查看凭证" in labels
    assert "查看详情" not in labels

    without_receipt = _buttons(expense_approval_keyboard(5, "zh", has_receipt=False))
    labels = [b.text for b in without_receipt]
    assert "查看详情" in labels
    assert "📎 查看凭证" not in labels

    for kb in (with_receipt, without_receipt):
        detail = [b for b in kb if decode(b.callback_data)["action"] == "exd"]
        assert len(detail) == 1
        assert detail[0].callback_data == "v1:exd:5"


def test_todo_page_detail_button_label_depends_on_receipt(make_app):
    """★ The /todo expense row labels the detail button 📎 查看凭证 with a
    receipt attached and 查看详情 without one."""
    env = make_app()
    _seed_pending_expense(env, receipt=True)
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "/todo", bot=env.bot)])
    labels = [b.text for b in _buttons(env.bot.last_send()["reply_markup"])]
    assert "📎 查看凭证" in labels
    assert "查看详情" not in labels

    env = make_app()
    _seed_pending_expense(env, receipt=False)
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "/todo", bot=env.bot)])
    labels = [b.text for b in _buttons(env.bot.last_send()["reply_markup"])]
    assert "查看详情" in labels
    assert "📎 查看凭证" not in labels
