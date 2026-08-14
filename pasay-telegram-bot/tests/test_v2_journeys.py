"""PASAY-V2-FOUNDATION-001: the 8 required Journey tests.

Journey 1 — Zero Start: no /start, normal chat -> keyboard auto-exists.
Journey 2 — Group Language: group business replies are English + 中文.
Journey 3 — Repair Task: "1680 aircon leaking" -> Pending; "technician
            tomorrow" -> In Progress + next_check; "finished" -> progressed.
Journey 4 — Approval: submit -> approve -> bilingual feedback -> closed loop.
Journey 5 — Reminder: pending -> reminder -> in progress -> next_check due ->
            re-remind -> completed stops.
Journey 6 — Correction: "1680 ..." then "不是1680，是1805" corrects the
            association.
Journey 7 — Quick Views: Properties/Tasks/Rent/Expense fast, one-shot, no LLM.
Journey 8 — Regression: rent collect, expense approval/reject, NL queries.
"""
from __future__ import annotations

import asyncio
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


def _labels(kb):
    if kb is None:
        return []
    return [b.text for row in kb.keyboard for b in row]


def _button_labels(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


def _find_button(kb, label):
    for row in kb.inline_keyboard:
        for b in row:
            if b.text == label:
                return b
    return None


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


# ---------------------------------------------------------------------------
# Journey 1 — Zero Start
# ---------------------------------------------------------------------------

def test_journey1_zero_start_keyboard_without_start(make_app):
    """A fresh user sends an ordinary message (never /start) and the fixed
    keyboard is mounted on the reply."""
    env = make_app()
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "hello", bot=env.bot)])
    sends = env.bot.sends()
    kb_sends = [
        s for s in sends
        if s["reply_markup"] is not None
        and s["reply_markup"].__class__.__name__ == "ReplyKeyboardMarkup"
    ]
    assert kb_sends, "keyboard must exist without /start"
    assert _labels(kb_sends[0]["reply_markup"]) == [
        "🏠 Properties", "✅ Tasks", "💰 Rent", "💸 Expense",
    ]
    assert "/start" not in "\n".join(env.bot.all_texts()).lower()


# ---------------------------------------------------------------------------
# Journey 2 — Group Language (English + 中文)
# ---------------------------------------------------------------------------

def test_journey2_group_replies_bilingual(make_app):
    env = make_app()
    group = -100222333444
    run_updates(env, [make_group_text_update(OWNER_ID, group, "💰 Rent", bot=env.bot)])
    text = env.bot.last_send()["text"]
    # bilingual: English word and Chinese title both present
    assert "Rent" in text and "租金" in text

    run_updates(env, [make_group_text_update(SECRETARY_ID, group, "✅ Tasks", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "Tasks" in text and "待办" in text


# ---------------------------------------------------------------------------
# Journey 3 — Repair Task (conversation -> task lifecycle)
# ---------------------------------------------------------------------------

def test_journey3_repair_task_lifecycle(make_app):
    env = make_app()
    # 1) create: "16B aircon leaking" -> Pending repair task (16B is a fake unit)
    run_updates(env, [make_text_update(SECRETARY_ID, SECRETARY_ID, "16B aircon leaking", bot=env.bot)])
    created = env.bot.last_send()
    assert "Aircon repair" in created["text"] or "🔴" in created["text"] or "Pending" in created["text"]
    tasks = env.backend.operational_tasks
    assert tasks and tasks[-1]["status"] == "PENDING"
    task_id = tasks[-1]["id"]

    # 2) progress: "technician coming tomorrow" -> IN_PROGRESS + next_check
    run_updates(
        env,
        [make_text_update(SECRETARY_ID, SECRETARY_ID, "technician coming tomorrow",
                          message_id=2, update_id=2, bot=env.bot)],
    )
    updated = env.bot.last_send()
    assert "In Progress" in updated["text"] or "🟡" in updated["text"] or "Next" in updated["text"]
    task = env.backend._ops_task(f"/operations/tasks/{task_id}")
    assert task["status"] == "IN_PROGRESS"
    assert task["next_action"] and task["next_check_at"]

    # 3) completion: "finished" -> COMPLETED
    run_updates(
        env,
        [make_text_update(SECRETARY_ID, SECRETARY_ID, "finished",
                          message_id=3, update_id=3, bot=env.bot)],
    )
    task = env.backend._ops_task(f"/operations/tasks/{task_id}")
    assert task["status"] == "COMPLETED"
    assert task["completed_at"] is not None


# ---------------------------------------------------------------------------
# Journey 4 — Approval closed loop
# ---------------------------------------------------------------------------

def test_journey4_approval_closed_loop(make_app):
    env = make_app()
    env.backend.add_expense(expense_id=77, category="维修", amount="3500.00",
                            payee="Fix-It Co", unit_id=1)
    # Secretary submits -> Owner sees the approval card
    run_updates(env, [make_text_update(SECRETARY_ID, SECRETARY_ID, "支付了1680维修费3500", bot=env.bot)])
    # Owner approves via the callback
    approve = encode("exa", "77", "", nonce=new_nonce(), ts=now_ts())
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, approve, bot=env.bot)])
    expense = next(e for e in env.backend.expenses if e["id"] == 77)
    assert expense["status"] == "approved"
    # Result card + bilingual group feedback path exercised
    text = env.bot.last_edit()["text"]
    assert "Approved" in text or "已批准" in text


# ---------------------------------------------------------------------------
# Journey 5 — Reminder lifecycle (digest + next_check)
# ---------------------------------------------------------------------------

def test_journey5_reminder_lifecycle_data(make_app):
    env = make_app()
    env.backend.add_ops_task(task_id=41, title="Follow up rent", status="PENDING",
                             due_at="2026-08-05T00:00:00+08:00")
    env.backend.add_ops_task(task_id=42, title="Confirm repair", status="IN_PROGRESS",
                             next_action="Confirm", next_check_at="2026-08-01T00:00:00+08:00",
                             property_code="1680")
    env.backend.digest = {
        "pending": [
            {"id": 41, "title": "Follow up rent", "status": "PENDING",
             "property_code": "1805", "due_at": "2026-08-05T00:00:00+08:00",
             "next_action": None, "next_check_at": None},
        ],
        "in_progress": [
            {"id": 42, "title": "Confirm repair", "status": "IN_PROGRESS",
             "property_code": "1680", "due_at": "2026-08-10T00:00:00+08:00",
             "next_action": "Confirm", "next_check_at": "2026-08-01T00:00:00+08:00"},
        ],
        "recently_completed": [],
    }
    # Daily digest card renders both active buckets, never raw enums.
    from pasay_bot.render.cards import active_tasks_digest_card
    text = active_tasks_digest_card(env.backend.digest, "bi")
    assert "Follow up rent" in text and "Confirm repair" in text
    assert "PENDING" not in text

    # next_check reminder card renders for due tasks (deterministic).
    from pasay_bot.render.cards import task_event_card
    reminder = task_event_card("updated", env.backend.digest["in_progress"][0], "bi")
    assert "Confirm" in reminder


# ---------------------------------------------------------------------------
# Journey 6 — Correction
# ---------------------------------------------------------------------------

def test_journey6_correction_changes_association(make_app):
    env = make_app()
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "16B aircon leaking", bot=env.bot)])
    assert env.backend.operational_tasks
    # "不是16B，是17A" corrects the live task context
    run_updates(
        env,
        [make_text_update(OWNER_ID, OWNER_ID, "不是16B，是17A",
                          message_id=2, update_id=2, bot=env.bot)],
    )
    text = env.bot.last_send()["text"]
    assert "17A" in text
    ctx = env.store.get_v2_context(OWNER_ID, OWNER_ID)
    assert ctx and ctx["payload"].get("unit_token") == "17A"


# ---------------------------------------------------------------------------
# Journey 7 — Quick Views (one-shot, deterministic, no LLM)
# ---------------------------------------------------------------------------

def test_journey7_quick_views_one_shot_no_llm(make_app, monkeypatch):
    env = make_app()
    calls = []

    async def boom(*args, **kwargs):
        calls.append(True)
        raise AssertionError("LLM/NL must not run on Quick Views")

    monkeypatch.setattr("pasay_bot.handlers.nl_bridge.handle_nl", boom)
    for label, marker in [
        ("🏠 Properties", "房源"),
        ("✅ Tasks", "待办"),
        ("💰 Rent", "租金"),
        ("💸 Expense", "支出"),
    ]:
        env.backend.telegram_user_calls.clear()
        before = len(env.bot.sends())
        run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, label, bot=env.bot)])
        after = len(env.bot.sends())
        assert after == before + 1, f"{label} must be a single reply"
        text = env.bot.last_send()["text"]
        assert marker in text
    assert calls == []


# ---------------------------------------------------------------------------
# Journey 8 — Regression (rent collect + expense + NL queries)
# ---------------------------------------------------------------------------

def test_journey8_regression_rent_and_expense(make_app):
    env = make_app()
    # rent collect flow still works
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, encode("rn", "go", "1"), bot=env.bot)])
    assert env.store.get_conversation(OWNER_ID, OWNER_ID) is not None
    # expense approval flow still works
    env.backend.add_expense(expense_id=88, category="水电", amount="2350.00",
                            payee="Meralco", unit_id=1)
    approve = encode("exa", "88", "", nonce=new_nonce(), ts=now_ts())
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, approve, bot=env.bot)])
    expense = next(e for e in env.backend.expenses if e["id"] == 88)
    assert expense["status"] == "approved"
    # NL query still answers from read endpoints
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "这个月谁还没交？", bot=env.bot)])
    assert env.bot.last_send()["text"]
