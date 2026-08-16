"""V1.2 待办中心: main-menu entry, sections, complete/snooze/detail callbacks,
editMessageText updates, and forged-callback RBAC refusals."""
from __future__ import annotations

from conftest import (
    OWNER_ID,
    SECRETARY_ID,
    TODAY,
    UNKNOWN_ID,
    _add_days,
    make_callback_update,
    make_text_update,
    run_updates,
)

from pasay_bot.keyboards import decode, encode

# ops center callback actions (mirror keyboards.py).
ACTION_OPS_NAV = "opn"
ACTION_TASK_COMPLETE = "tkc"
ACTION_TASK_SNOOZE = "tks"
ACTION_TASK_SNOOZE_PICK = "tsp"
ACTION_TASK_DETAIL = "tkd"
OPS_OVERVIEW = "ops"


def _button_labels(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


def _callback_of(env, call_type="edit_message_text", index=-1):
    call = env.bot.of_type(call_type)[index]
    return call["reply_markup"].inline_keyboard


def _find_button(kb, label):
    for row in kb.inline_keyboard:
        for b in row:
            if b.text == label:
                return b
    return None


def _open_ops(env):
    # PASAY-V2-FOUNDATION-001: /ops is pruned; the "tasks"/"todo" text keyword
    # still opens the unified Tasks page through the deterministic NL route.
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "tasks", bot=env.bot)])
    return env.bot.last_send()


def _open_section(env, section):
    """Open a legacy ops-center section directly via the still-supported
    callback (the section callbacks remain available in the callback layer)."""
    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, encode(ACTION_OPS_NAV, section), bot=env.bot)],
    )
    return env.bot.last_edit()


def test_dashboard_uses_persistent_todo_nav(make_app):
    env = make_app()
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "/start", bot=env.bot)])
    kb = env.bot.last_send()["reply_markup"]
    assert kb.__class__.__name__ == "ReplyKeyboardMarkup"
    labels = [b.text for row in kb.keyboard for b in row]
    assert "✅ Tasks" in labels
    assert "📋 待办中心" not in labels  # no longer a primary nav entry


def test_todo_command_shows_unified_page(make_app):
    """Secretary's unified to-do page lists operational work (maintenance
    task with action buttons). AI-OPS-FOUNDATION-001 §5: routine operational
    work lives on the SECRETARY's queue, not the Owner's Needs-You queue."""
    env = make_app()
    env.backend.add_ops_task(task_id=1, title="季度空调保养",
                             due_at=f"{TODAY}T00:00:00+08:00")
    run_updates(env, [make_text_update(SECRETARY_ID, SECRETARY_ID, "tasks", bot=env.bot)])
    send = env.bot.last_send()
    text = send["text"]
    assert "<b>✅ Tasks</b>" in text  # Secretary DM is English-first
    assert "季度空调保养" in text
    labels = _button_labels(send["reply_markup"])
    assert labels.count("✅ Done") == 1
    assert labels.count("👁 Detail") == 1
    assert "🏠 Home" in labels


def test_owner_todo_is_needs_you_queue_not_operational_work(make_app):
    """AI-OPS-FOUNDATION-001 §5: the Owner's to-do page is framed as
    '需要您处理 / Needs You' and excludes routine operational work (overdue
    rent / maintenance tasks) that belongs to the Secretary."""
    env = make_app()
    env.backend.add_ops_task(task_id=1, title="季度空调保养", task_type="AC_MAINTENANCE",
                             due_at=f"{TODAY}T00:00:00+08:00")
    env.backend.add_ops_task(task_id=2, title="租金逾期 · 1期", task_type="RENT_OVERDUE",
                             due_at=f"{TODAY}T00:00:00+08:00")
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "tasks", bot=env.bot)])
    send = env.bot.last_send()
    text = send["text"]
    assert "需要您处理" in text  # Owner DM is Chinese-first
    assert "季度空调保养" not in text
    assert "租金逾期" not in text


def test_ops_section_lists_tasks_with_actions(make_app):
    env = make_app()
    env.backend.add_ops_task(
        task_id=1, title="季度空调保养", task_type="AC_MAINTENANCE",
        due_at=f"{TODAY}T00:00:00+08:00", details={"amount": "3000.00", "period": "2026-Q3"},
    )
    edit = _open_section(env, "otd")
    text = edit["text"]
    assert "Pasay Premier Residences" in text  # property name resolved
    assert "季度空调保养" in text
    assert "₱3,000" in text  # amount from details
    assert "2026-08" in text or TODAY[:7] in text  # due date
    labels = _button_labels(edit["reply_markup"])
    assert labels.count("✅ 完成") == 1
    assert labels.count("⏰ 稍后提醒") == 1
    assert labels.count("👁 查看详情") == 1
    assert "◀️ 返回" in labels


def test_ops_section_splits_overdue_today_next7(make_app):
    env = make_app()
    env.backend.add_ops_task(task_id=1, title="逾期任务A", due_at=f"{_add_days(TODAY, -5)}T00:00:00+08:00")
    env.backend.add_ops_task(task_id=2, title="今日任务B", due_at=f"{TODAY}T00:00:00+08:00")
    env.backend.add_ops_task(task_id=3, title="未来任务C", due_at=f"{_add_days(TODAY, 3)}T00:00:00+08:00")
    overdue_text = _open_section(env, "oov")["text"]
    assert "逾期任务A" in overdue_text
    assert "今日任务B" not in overdue_text

    today_text = _open_section(env, "otd")["text"]
    assert "今日任务B" in today_text

    next7_text = _open_section(env, "on7")["text"]
    assert "今日任务B" in next7_text and "未来任务C" in next7_text


def test_ops_complete_edits_message(make_app):
    env = make_app()
    env.backend.add_ops_task(task_id=7, title="待付款支出 #5", due_at=f"{TODAY}T00:00:00+08:00")
    _open_section(env, "otd")
    done = _find_button(env.bot.last_edit()["reply_markup"], "✅ 完成")
    before = len(env.bot.calls)
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, done.callback_data, bot=env.bot)])
    # original message edited (no new send_message) + backend POST /complete
    assert len(env.bot.sends()) == 0, "complete must edit, not send a new message"
    assert ("POST", "/operations/tasks/7/complete") in [
        (m, p) for m, p, _ in env.backend.calls
    ]
    edit = env.bot.last_edit()
    assert "✅ <b>已完成</b>" in edit["text"]
    assert "待付款支出 #5" in edit["text"]
    task = env.backend._ops_task("/operations/tasks/7")
    assert task["status"] == "COMPLETED"
    assert "处理中" in (env.bot.last_answer()["text"] or "")


def test_ops_snooze_preset_calls_backend_and_edits(make_app):
    env = make_app()
    env.backend.add_ops_task(task_id=7, title="租金到期 2026-08", due_at=f"{TODAY}T00:00:00+08:00")
    _open_section(env, "otd")
    snooze = _find_button(env.bot.last_edit()["reply_markup"], "⏰ 稍后提醒")
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, snooze.callback_data, bot=env.bot)])
    picker = env.bot.last_edit()
    assert "⏰ 稍后提醒" in picker["text"]
    labels = _button_labels(picker["reply_markup"])
    for want in ("1 小时", "今天下午", "明天上午", "3 天后", "✏️ 自定义"):
        assert want in labels, want

    one_hour = _find_button(picker["reply_markup"], "1 小时")
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, one_hour.callback_data, bot=env.bot)])
    assert ("POST", "/operations/tasks/7/snooze") in [
        (m, p) for m, p, _ in env.backend.calls
    ]
    assert any(b and b.get("preset") == "1h" for _, _, b in env.backend.calls if b)
    edit = env.bot.last_edit()
    assert "⏰ <b>已稍后提醒</b>" in edit["text"]
    task = env.backend._ops_task("/operations/tasks/7")
    assert task["snoozed_until"] == "2026-08-10T13:00:00Z"


def test_ops_snooze_custom_flow(make_app):
    env = make_app()
    env.backend.add_ops_task(task_id=9, title="季度空调保养", due_at=f"{TODAY}T00:00:00+08:00")
    _open_section(env, "otd")
    snooze = _find_button(env.bot.last_edit()["reply_markup"], "⏰ 稍后提醒")
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, snooze.callback_data, bot=env.bot)])
    custom = _find_button(env.bot.last_edit()["reply_markup"], "✏️ 自定义")
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, custom.callback_data, bot=env.bot)])
    prompt = env.bot.last_edit()
    assert "请输入提醒时间" in prompt["text"]
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "2026-08-15 09:00", message_id=99, bot=env.bot)])
    # the original ops card (message_id of the snooze picker message) is edited
    edited = [c for c in env.bot.edits() if "已稍后提醒" in (c["text"] or "")]
    assert edited, "custom snooze must edit the original message"
    task = env.backend._ops_task("/operations/tasks/9")
    assert (task["snoozed_until"] or "").startswith("2026-08-15")


def test_ops_detail_card(make_app):
    env = make_app()
    env.backend.add_ops_task(task_id=3, title="待确认佣金结算 #2", task_type="SETTLEMENT_PENDING",
                             due_at=f"{_add_days(TODAY, 2)}T00:00:00+08:00",
                             details={"amount": "1500.00", "settlement_id": 2})
    _open_section(env, "on7")
    detail = _find_button(env.bot.last_edit()["reply_markup"], "👁 查看详情")
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, detail.callback_data, bot=env.bot)])
    edit = env.bot.last_edit()
    assert "<b>📄 任务详情</b>" in edit["text"]
    assert "待确认佣金结算 #2" in edit["text"]
    assert "SETTLEMENT_PENDING" not in edit["text"]  # V1.3: no raw enums in UI
    assert "₱1,500" in edit["text"]
    assert "✅ 完成" in _button_labels(edit["reply_markup"])
    assert "◀️ 返回" in _button_labels(edit["reply_markup"])


def test_ops_back_returns_to_overview(make_app):
    env = make_app()
    env.backend.add_ops_task(task_id=1, due_at=f"{TODAY}T00:00:00+08:00")
    _open_section(env, "otd")
    back = _find_button(env.bot.last_edit()["reply_markup"], "◀️ 返回")
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, back.callback_data, bot=env.bot)])
    assert "<b>📋 待办中心</b>" in env.bot.last_edit()["text"]


def test_forged_callback_unknown_user_refused(make_app):
    """Unknown telegram user cannot act on ops callbacks (role gate)."""
    env = make_app()
    env.backend.add_ops_task(task_id=1, due_at=f"{TODAY}T00:00:00+08:00")
    data = encode(ACTION_TASK_COMPLETE, "ops", "1", nonce="abcdef01", ts=1700000000)
    run_updates(env, [make_callback_update(UNKNOWN_ID, UNKNOWN_ID, data, bot=env.bot)])
    # Unknown telegram user -> zh locale refusal (role gate before backend).
    assert "无权限" in env.bot.last_answer()["text"]
    assert not any(m == "POST" and p.endswith("/complete") for m, p, _ in env.backend.calls)


def test_forged_callback_backend_403_refused(make_app):
    """Even a valid user pressing a backend-forbidden task gets refused —
    the backend RBAC is the final arbiter, not callback_data."""
    env = make_app()
    env.backend.add_ops_task(task_id=5, title="他人任务", due_at=f"{TODAY}T00:00:00+08:00")
    env.backend.ops_forbidden_task_ids.add(5)
    data = encode(ACTION_TASK_COMPLETE, "ops", "5", nonce="abcdef01", ts=1700000000)
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, data, bot=env.bot)])
    # backend 403 arrives after the processing ack -> durable on the card
    assert "无权操作该任务" in env.bot.last_edit()["text"]
    task = env.backend._ops_task("/operations/tasks/5")
    assert task["status"] == "PENDING", "forbidden task must stay pending"


def test_secretary_can_view_ops(make_app):
    """SECRETARY (manager-level) can open the task center."""
    env = make_app()
    env.backend.add_ops_task(task_id=1, due_at=f"{TODAY}T00:00:00+08:00")
    run_updates(env, [make_text_update(SECRETARY_ID, SECRETARY_ID, "tasks", bot=env.bot)])
    send = env.bot.last_send()
    # V1.3: the tasks keyword opens the unified Tasks page; SECRETARY locale is English.
    assert "<b>✅ Tasks</b>" in send["text"]
