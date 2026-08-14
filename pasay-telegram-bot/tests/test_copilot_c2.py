"""V1.2.2 Phase C2 — confirmed-action copilot bot UX (owner, zh).

Covers:
- new callback actions encode/decode round-trip within the 64-byte limit
- suggestion rows only for actionable items (task items get snooze)
- full flow: TODAY -> WHY -> suggestion -> /recommend -> confirmation card ->
  [✅ 确认安排] -> /confirm + /execute -> success card
- snooze entry point: [明天再提醒] -> /recommend preset -> EXACT resolved due
- decline -> /cancel; double-confirm -> one execute + human replay card
- failure UX: stale 409 / replay / notification-retry -> human strings only
- no proposal_id / raw enums / JSON / status in any rendered text
- zh owner text + en key presence
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from conftest import (
    OWNER_ID,
    SECRETARY_ID,
    make_callback_update,
    make_text_update,
    run_updates,
)

from pasay_bot.api_client import CopilotExecute, CopilotRecommend, CopilotRecommendCard
from pasay_bot.keyboards import (
    MAX_CALLBACK_BYTES,
    decode,
    encode,
    new_nonce,
    now_ts,
)
from pasay_bot.render import cards
from pasay_bot.render.i18n import STRINGS

MANILA = ZoneInfo("Asia/Manila")


def _button_labels(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


def _find_button(kb, label):
    for row in kb.inline_keyboard:
        for b in row:
            if b.text == label:
                return b
    return None


def _expected_due(iso: str) -> str:
    """Manila-local human due text — mirrors cards._format_due so tests stay
    date-robust."""
    dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(MANILA)
    now = datetime.now(MANILA)
    hm = dt.strftime("%H:%M")
    if dt.date() == now.date():
        return f"今天 {hm}"
    if dt.date() == now.date() + timedelta(days=1):
        return f"明天 {hm}"
    return f"{dt.strftime('%m月%d日')} {hm}"


def _open_followup_confirm(env):
    """TODAY -> [1 为什么?] -> WHY -> [📞 安排秘书跟进] -> confirmation card."""
    # PASAY-V2-FOUNDATION-001: /copilot is pruned; open TODAY via the
    # dashboard "更多" fallback's 🤖 运营助手 nav button.
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "更多", bot=env.bot)])
    dashboard = env.bot.last_send()
    copilot_btn = _find_button(dashboard["reply_markup"], "🤖 运营助手")
    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, copilot_btn.callback_data, bot=env.bot)],
    )
    today = env.bot.last_edit()
    why_btn = _find_button(today["reply_markup"], "1 为什么?")
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, why_btn.callback_data, bot=env.bot)])
    follow = _find_button(env.bot.last_edit()["reply_markup"], "📞 安排秘书跟进")
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, follow.callback_data, bot=env.bot)])
    return env.bot.last_edit()


# ---------------------------------------------------------------------------
# callback_data encode/decode
# ---------------------------------------------------------------------------

def test_copilot_callback_actions_roundtrip_within_size():
    ts = now_ts()
    cases = [
        ("cps", "follow", "1"),
        ("cps", "snooze", "2"),
        ("cps", "dismiss", "1"),
        ("cpc", "101", "0"),
        ("cpd", "101", "0"),
        ("cpe", "menu", "101"),
        ("cpe", "who", "101"),
        ("cpe", "due", "101"),
        ("cpr", "today", ""),
        ("csp", "tomorrow", "101"),
        ("cap", "sec", "101"),
        ("cap", "me", "101"),
    ]
    for action, entity, ref in cases:
        data = encode(action, entity, ref, nonce=new_nonce(), ts=ts)
        assert len(data.encode("ascii")) <= MAX_CALLBACK_BYTES, data
        parsed = decode(data)
        assert parsed["action"] == action, data
        assert parsed["entity"] == entity, data
        assert parsed["ref"] == ref, data
        assert parsed["ts"] == ts, data


def test_copilot_suggestion_button_encodes_index_not_ref():
    """The suggestion button carries the 1-based TODAY index (never a backend
    ref) — the handler re-fetches TODAY to resolve the item."""
    from pasay_bot import keyboards
    from pasay_bot.api_client import CopilotTodayItem

    item = CopilotTodayItem(
        item_ref="lease:3", reason_why_important="x", suggested_action="跟进"
    )
    kb = keyboards.copilot_why_keyboard(2, item, "zh", can_suggest=True)
    btn = _find_button(kb, "📞 安排秘书跟进")
    parsed = decode(btn.callback_data)
    assert parsed["action"] == "cps"
    assert parsed["entity"] == "follow"
    assert parsed["ref"] == "2"
    assert parsed["nonce"] and parsed["ts"]


# ---------------------------------------------------------------------------
# suggestion rows (actionable only)
# ---------------------------------------------------------------------------

def test_copilot_why_keyboard_only_actionable_items():
    from pasay_bot import keyboards
    from pasay_bot.api_client import CopilotTodayItem

    expense = CopilotTodayItem(
        item_ref="expense:2", reason_why_important="x", suggested_action="审批"
    )
    kb = keyboards.copilot_why_keyboard(1, expense, "zh", can_suggest=True)
    labels = _button_labels(kb)
    assert "📞 安排秘书跟进" not in labels  # expense is not follow-up eligible

    empty = CopilotTodayItem(item_ref="task:9", reason_why_important="x", suggested_action="")
    kb2 = keyboards.copilot_why_keyboard(1, empty, "zh", can_suggest=True)
    assert "📞 安排秘书跟进" not in _button_labels(kb2)

    task = CopilotTodayItem(item_ref="task:9", reason_why_important="x", suggested_action="安排技师")
    kb3 = keyboards.copilot_why_keyboard(1, task, "zh", can_suggest=True)
    labels3 = _button_labels(kb3)
    assert "📞 安排秘书跟进" in labels3
    assert "⏰ 明天再提醒" in labels3  # task items can snooze

    lease = CopilotTodayItem(item_ref="lease:3", reason_why_important="x", suggested_action="联系租客")
    kb4 = keyboards.copilot_why_keyboard(1, lease, "zh", can_suggest=True)
    labels4 = _button_labels(kb4)
    assert "📞 安排秘书跟进" in labels4
    assert "⏰ 明天再提醒" not in labels4  # leases cannot snooze


# ---------------------------------------------------------------------------
# cards (render-safe, owner zh)
# ---------------------------------------------------------------------------

def test_copilot_suggest_card_zh_owner_no_leak():
    rec = CopilotRecommend(
        proposal_id=42,
        action_type="create_followup_task",
        status="PENDING",
        card=CopilotRecommendCard(
            action_type="create_followup_task",
            target_type="lease",
            target_id=3,
            target_label="Lease #3 · 1608 · Juan",
            reason_code="FOLLOWUP",
            assignee_user_id=2,
            assignee_name="Maria",
            due_at="2026-08-12T01:00:00+00:00",
            note="联系租客确认付款日期",
            display_context={"unit": "1608", "tenant": "Juan Dela Cruz", "lease_id": 3},
        ),
    )
    text = cards.copilot_suggest_card(rec)
    assert "📋 准备安排跟进" in text
    assert "房产：Unit 1608" in text
    assert "事项：联系租客确认付款日期" in text
    assert "负责人：Maria" in text
    assert f"截止：{_expected_due('2026-08-12T01:00:00+00:00')}" in text
    assert "秘书将收到英文任务通知。" in text
    for banned in ("42", "proposal_id", "PENDING", "FOLLOWUP", "reason_code",
                   "display_context", "lease:3", "target_id"):
        assert banned not in text, banned


def test_copilot_snooze_card_shows_exact_due():
    rec = CopilotRecommend(
        proposal_id=42,
        action_type="snooze_task",
        status="PENDING",
        card=CopilotRecommendCard(
            action_type="snooze_task",
            target_type="task",
            target_id=9,
            target_label="#9 空调保养",
            due_at="2026-08-12T01:00:00+00:00",
            display_context={"task_id": 9, "title": "空调保养"},
        ),
    )
    text = cards.copilot_suggest_card(rec)
    assert "事项：空调保养" in text
    assert f"截止：{_expected_due('2026-08-12T01:00:00+00:00')}" in text
    assert "42" not in text and "snooze_task" not in text and "PENDING" not in text


def test_copilot_success_card_zh_owner_no_leak():
    result = CopilotExecute(
        action_type="create_followup_task",
        target_type="lease",
        target_id=3,
        task_id=77,
        assignee_user_id=2,
        due_at="2026-08-12T01:00:00+00:00",
        executed_at="2026-08-11T04:00:00+00:00",
        status="EXECUTED",
        replay=False,
        detail="Proposal executed",
        proposal_id=42,
    )
    text = cards.copilot_success_card(result, assignee_name="Maria")
    assert "已安排给 Maria" in text
    assert "她将在 Telegram 收到任务通知" in text
    assert "我会继续跟踪这个事项。" in text
    for banned in ("42", "proposal_id", "EXECUTED", "status", "replay"):
        assert banned not in text, banned


def test_copilot_failure_cards_human_only():
    assert "这个事项刚刚已经发生变化，我没有执行旧操作。" in cards.copilot_stale_card()
    assert "这个操作已经执行过了。" in cards.copilot_replayed_card()
    assert "通知暂时失败，系统会自动重试。" in cards.copilot_notify_retry_card()
    for text in (cards.copilot_stale_card(), cards.copilot_replayed_card(),
                 cards.copilot_notify_retry_card()):
        for banned in ("error_code", "409", "STALE", "replay", "status"):
            assert banned not in text, banned


def test_copilot_i18n_zh_owner_and_en_mirror():
    zh = STRINGS["zh"]
    en = STRINGS["en"]
    keys = (
        "copilot.suggest_title", "copilot.suggest_follow", "copilot.suggest_snooze",
        "copilot.suggest_dismiss", "copilot.confirm_title", "copilot.confirm_yes",
        "copilot.confirm_edit", "copilot.confirm_cancel", "copilot.success_follow",
        "copilot.role_property", "copilot.role_topic", "copilot.role_owner",
        "copilot.role_due", "copilot.hint_secretary_note", "copilot.stale",
        "copilot.executed_already", "copilot.notify_retry", "copilot.ask_who",
        "copilot.ask_due", "copilot.who_me",
    )
    for key in keys:
        assert key in zh, key
        assert key in en, key
    # owner zh text must be Chinese, never a translated secretary card.
    assert "秘书将收到英文任务通知。" in zh["copilot.hint_secretary_note"]
    assert "已安排给" in zh["copilot.success_follow"]
    assert "这个操作已经执行过了。" in zh["copilot.executed_already"]
    assert zh["copilot.success_follow"] != en["copilot.success_follow"]


# ---------------------------------------------------------------------------
# full flow: suggest -> confirm -> execute
# ---------------------------------------------------------------------------

def test_copilot_followup_confirm_flow(make_app):
    env = make_app()
    backend = env.backend
    confirm = _open_followup_confirm(env)

    # POST /recommend carried the resolved source refs (from the re-fetched
    # TODAY item), never a raw backend ref in the callback.
    rec_bodies = [b for m, p, b in backend.calls if p == "/operations/copilot/recommend"]
    assert rec_bodies, "must call POST /operations/copilot/recommend"
    body = rec_bodies[-1]
    assert body["intent"] == "followup"
    assert body["source_type"] == "lease"
    assert body["source_id"] == 3
    assert body["note"] == "联系租客确认付款日期。"

    assert "📋 准备安排跟进" in confirm["text"]
    assert "房产：Unit 1608" in confirm["text"]
    assert "负责人：Maria" in confirm["text"]
    assert "秘书将收到英文任务通知。" in confirm["text"]
    labels = _button_labels(confirm["reply_markup"])
    assert "✅ 确认安排" in labels and "✏️ 修改" in labels and "暂不处理" in labels
    assert "proposal_id" not in confirm["text"]

    yes = _find_button(confirm["reply_markup"], "✅ 确认安排")
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, yes.callback_data, bot=env.bot)])
    paths = [(m, p) for m, p, _ in backend.calls]
    assert ("POST", "/operations/copilot/proposals/101/confirm") in paths
    assert ("POST", "/operations/copilot/proposals/101/execute") in paths

    success = env.bot.last_edit()
    assert "已安排给 Maria" in success["text"]
    assert "她将在 Telegram 收到任务通知" in success["text"]
    assert "我会继续跟踪这个事项。" in success["text"]
    labels = _button_labels(success["reply_markup"])
    assert "查看任务" in labels and "◀️ 返回今日重点" in labels
    assert "101" not in success["text"] and "proposal_id" not in success["text"]
    assert "EXECUTED" not in success["text"]
    # single answer = processing ack; the success card is the durable result
    assert "处理中" in (env.bot.last_answer()["text"] or "")


def test_copilot_snooze_entry_preset_and_execute(make_app):
    env = make_app()
    backend = env.backend
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "更多", bot=env.bot)])
    dashboard = env.bot.last_send()
    copilot_btn = _find_button(dashboard["reply_markup"], "🤖 运营助手")
    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, copilot_btn.callback_data, bot=env.bot)],
    )
    today = env.bot.last_edit()
    why_btn = _find_button(today["reply_markup"], "2 为什么?")  # task item
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, why_btn.callback_data, bot=env.bot)])
    snooze = _find_button(env.bot.last_edit()["reply_markup"], "⏰ 明天再提醒")
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, snooze.callback_data, bot=env.bot)])

    body = [b for m, p, b in backend.calls if p == "/operations/copilot/recommend"][-1]
    assert body["intent"] == "snooze"
    assert body["task_ref"] == 9
    assert body["preset"] == "tomorrow_morning"

    confirm = env.bot.last_edit()
    due = "2026-08-12T01:00:00+00:00"  # fake backend resolved tomorrow-morning
    assert f"截止：{_expected_due(due)}" in confirm["text"], confirm["text"]
    assert "📋 准备安排跟进" in confirm["text"]
    assert "proposal_id" not in confirm["text"]

    yes = _find_button(confirm["reply_markup"], "✅ 确认安排")
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, yes.callback_data, bot=env.bot)])
    paths = [(m, p) for m, p, _ in backend.calls]
    assert ("POST", "/operations/copilot/proposals/101/confirm") in paths
    assert ("POST", "/operations/copilot/proposals/101/execute") in paths
    assert "已安排给" in env.bot.last_edit()["text"] or "我会继续跟踪这个事项。" in env.bot.last_edit()["text"]


def test_copilot_decline_cancels_proposal(make_app):
    env = make_app()
    backend = env.backend
    _open_followup_confirm(env)
    decline = _find_button(env.bot.last_edit()["reply_markup"], "暂不处理")
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, decline.callback_data, bot=env.bot)])
    paths = [(m, p) for m, p, _ in backend.calls]
    assert ("POST", "/operations/copilot/proposals/101/cancel") in paths
    assert not any(p.endswith("/execute") for _, p in paths), "decline must never execute"
    assert "❌ 已取消安排" in env.bot.last_edit()["text"]
    assert "◀️ 返回今日重点" in _button_labels(env.bot.last_edit()["reply_markup"])


def test_copilot_double_confirm_one_execute_no_second_mutation(make_app):
    env = make_app()
    backend = env.backend
    _open_followup_confirm(env)
    yes = _find_button(env.bot.last_edit()["reply_markup"], "✅ 确认安排")
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, yes.callback_data, bot=env.bot)])
    assert len([c for c in backend.calls if c[1].endswith("/execute")]) == 1
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, yes.callback_data, bot=env.bot)])
    assert len([c for c in backend.calls if c[1].endswith("/execute")]) == 1
    assert "这个操作已经执行过了。" in env.bot.last_edit()["text"]


# ---------------------------------------------------------------------------
# failure UX (human strings, never raw codes)
# ---------------------------------------------------------------------------

def test_copilot_execute_stale_409_human_card(make_app):
    env = make_app()
    backend = env.backend
    backend.copilot_execute_error = (
        409, {"message": "task is no longer pending", "error_code": "business_stale"},
    )
    _open_followup_confirm(env)
    yes = _find_button(env.bot.last_edit()["reply_markup"], "✅ 确认安排")
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, yes.callback_data, bot=env.bot)])
    edit = env.bot.last_edit()
    assert "这个事项刚刚已经发生变化，我没有执行旧操作。" in edit["text"]
    assert "business_stale" not in edit["text"] and "409" not in edit["text"]
    assert "🔄 刷新最新状态" in _button_labels(edit["reply_markup"])


def test_copilot_confirm_stale_409_no_execute(make_app):
    env = make_app()
    backend = env.backend
    backend.copilot_confirm_error = (
        409, {"message": "task is no longer pending", "error_code": "business_stale"},
    )
    _open_followup_confirm(env)
    yes = _find_button(env.bot.last_edit()["reply_markup"], "✅ 确认安排")
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, yes.callback_data, bot=env.bot)])
    assert not any(p.endswith("/execute") for _, p, _ in backend.calls), "no execute after stale confirm"
    assert "这个事项刚刚已经发生变化，我没有执行旧操作。" in env.bot.last_edit()["text"]


def test_copilot_execute_replay_result_human_card(make_app):
    env = make_app()
    backend = env.backend
    backend.copilot_execute_response = {
        "proposal": {"id": 101},
        "result": {
            "action_type": "create_followup_task",
            "target_type": "lease",
            "target_id": 3,
            "task_id": 77,
            "assignee_user_id": 2,
            "due_at": "2026-08-12T01:00:00+00:00",
            "executed_at": "2026-08-11T04:00:00+00:00",
            "status": "EXECUTED",
            "replay": True,
            "detail": "Proposal already executed (idempotent replay)",
        },
    }
    _open_followup_confirm(env)
    yes = _find_button(env.bot.last_edit()["reply_markup"], "✅ 确认安排")
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, yes.callback_data, bot=env.bot)])
    edit = env.bot.last_edit()
    assert "这个操作已经执行过了。" in edit["text"]
    assert "replay" not in edit["text"] and "101" not in edit["text"]
    assert len([c for c in backend.calls if c[1].endswith("/execute")]) == 1


def test_copilot_notify_retry_409_human_card(make_app):
    env = make_app()
    backend = env.backend
    backend.copilot_execute_error = (
        409, {"message": "telegram notification pending retry", "error_code": "notification_retry"},
    )
    _open_followup_confirm(env)
    yes = _find_button(env.bot.last_edit()["reply_markup"], "✅ 确认安排")
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, yes.callback_data, bot=env.bot)])
    edit = env.bot.last_edit()
    assert "任务已建立。通知暂时失败，系统会自动重试。" in edit["text"]
    assert "notification_retry" not in edit["text"] and "error_code" not in edit["text"]


# ---------------------------------------------------------------------------
# role awareness (owner flow only)
# ---------------------------------------------------------------------------

def test_copilot_why_suggestions_owner_only_and_forged_confirm_refused(make_app):
    env = make_app()
    run_updates(env, [make_text_update(SECRETARY_ID, SECRETARY_ID, "更多", bot=env.bot)])
    dashboard = env.bot.last_send()
    copilot_btn = _find_button(dashboard["reply_markup"], "🤖 Operations Assistant")
    run_updates(
        env,
        [make_callback_update(SECRETARY_ID, SECRETARY_ID, copilot_btn.callback_data, bot=env.bot)],
    )
    today = env.bot.last_edit()
    why_btn = today["reply_markup"].inline_keyboard[0][0]  # secretary locale: "1 Why?"
    run_updates(env, [make_callback_update(SECRETARY_ID, SECRETARY_ID, why_btn.callback_data, bot=env.bot)])
    labels = _button_labels(env.bot.last_edit()["reply_markup"])
    assert "📞 安排秘书跟进" not in labels  # secretary gets English via outbox, not here
    assert "◀️ Back to Today" in labels

    forged = encode("cpc", "101", "0", nonce=new_nonce(), ts=now_ts())
    run_updates(
        env,
        [make_callback_update(SECRETARY_ID, SECRETARY_ID, forged, update_id=99, bot=env.bot)],
    )
    answer = env.bot.last_answer()["text"] or ""
    assert "permission" in answer.lower() or "无权限" in answer
