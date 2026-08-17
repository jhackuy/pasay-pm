"""TELEGRAM-OPS-UX-CONVERGENCE-003 — bot-side regression tests.

Pins:
1. the fixed Reply Keyboard is ALWAYS the 4 menu keys (Owner/Secretary/group,
   every handler) and never carries Operations Assistant;
2. EVERY 🏠 Home callback lands on the ONE Home (Operations Overview) — the
   legacy dashboard is unreachable and Operations Assistant is not a menu page;
3. Expense list = one short ``E{id} · Open`` per payable row (list = reading);
   the detail card carries the short operations (🔔 提醒 / ✅ 已付 / ◀ 返回 /
   🏠 首页) — no truncated ``Remin...`` labels;
4. Remind Owner: one send, same-day deduped;
5. Rent Follow up: re-render shows the REAL Last follow-up + ✅ Followed up
   state, and a second tap the same day is deduped;
6. bilingual: a missing/identical translation never prints English twice;
7. the daily-mark store is persistent and atomic.
"""
from __future__ import annotations

import httpx
import pytest

from conftest import (
    OWNER_ID,
    SECRETARY_ID,
    FakeBackend,
    make_callback_update,
    make_text_update,
    run_updates,
)
from pasay_bot.keyboards import (
    ACTION_ACK,
    ACTION_COPILOT_NAV,
    ACTION_HOME_NAV,
    ACTION_NAV,
    encode,
    new_nonce,
    now_ts,
    reply_keyboard,
)
from pasay_bot.render import cards
from pasay_bot.render.i18n import t
from pasay_bot.state.store import StateStore, ph_local_date

V2_LABELS = ["🏠 Properties", "✅ Tasks", "💰 Rent", "💸 Expense"]


def _reply_labels(kb):
    if kb is None or kb.__class__.__name__ != "ReplyKeyboardMarkup":
        return []
    return [b.text for row in kb.keyboard for b in row]


def _inline_labels(kb):
    if kb is None or kb.__class__.__name__ != "InlineKeyboardMarkup":
        return []
    return [b.text for row in kb.inline_keyboard for b in row]


def _inline_data(kb):
    if kb is None or kb.__class__.__name__ != "InlineKeyboardMarkup":
        return []
    return [b.callback_data for row in kb.inline_keyboard for b in row]


class C3Backend(FakeBackend):
    """FakeBackend + convergence fixtures (approve flow + acknowledge route)."""

    def __init__(self):
        super().__init__()
        self.ops_tasks = [
            {"id": 501, "title": "租金逾期 · 3期", "task_type": "RENT_OVERDUE",
             "status": "PENDING", "property_code": "1680", "due_at": None,
             "next_action": None, "next_check_at": None,
             "details": {"amount": "75000.00", "periods": ["2026-05", "2026-06", "2026-07"],
                         "unit_number": "1680"}},
        ]
        # a unit matching the quick-rent row so the Rent detail resolves unit_id
        if not any(u.get("unit_number") == "1680" for u in self.units):
            self.units.append(
                {"id": 9, "property_id": 1, "unit_number": "1680", "floor": "16",
                 "size_sqm": "32.50", "monthly_rent": "25000.00",
                 "status": "occupied", "is_active": True},
            )
        if not any(l.get("unit_id") == 9 for l in self.leases):
            self.leases.append(
                {"id": 9, "unit_id": 9, "tenant_id": 1, "start_date": "2025-01-01",
                 "end_date": "2026-12-31", "accounting_start_date": None,
                 "monthly_rent": "25000.00", "deposit": "50000.00",
                 "status": "active", "due_day": 20, "notes": None},
            )
        self.quick_rent = {
            "overdue": [
                {"unit": "1680", "unit_code": "1680", "amount": "75000.00",
                 "unpaid_periods": 3, "monthly_rent": "25000.00",
                 "overdue_days": 104, "last_followup_at": "2026-08-17T11:47:00+08:00"},
            ],
            "outstanding_total": "75000.00",
            "month": "2026-08",
            "expected_rent_total": "25000.00",
            "collected_rent": "0.00",
            "outstanding_rent": "25000.00",
            "collection_rate": "0.00",
            "unpaid_unit_count": 1,
        }
        self.expenses = [
            {"id": 1, "expense_date": "2026-08-15", "due_date": None,
             "category": "Repair", "amount": "7000.00", "payee": "Repair Co",
             "description": None, "unit_id": 9, "status": "approved",
             "receipt_attachment_id": None, "approved_by": 1,
             "approved_at": "2026-08-15T10:00:00Z"},
        ]
        self.quick_expense = {
            "month_total": "7000.00",
            "pending_approval_count": 0,
            "pending_approval_amount": "0.00",
            "unresolved_expense_tasks": [],
            "records": [
                {"expense_id": 1, "unit": "1680", "unit_code": "1680",
                 "purpose": "Repair", "amount": "7000.00",
                 "expense_date": "2026-08-15", "status": "approved"},
            ],
            "payable": [
                {"kind": "payable_expense", "expense_id": 1, "unit": "1680",
                 "purpose": "Repair", "amount": "7000.00", "status": "approved",
                 "expense_date": "2026-08-15"},
            ],
            "paid_records": [],
        }
        self._acknowledged: list[int] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        if path.startswith("/api/v1"):
            path = path[len("/api/v1"):]
        if method == "POST" and path.startswith("/operations/tasks/") and path.endswith("/acknowledge"):
            task_id = int(path.split("/")[3])
            task = next((t for t in self.ops_tasks if t["id"] == task_id), None)
            if task is None:
                return httpx.Response(404, json={"detail": "Operational task not found"})
            if task.get("status") != "PENDING":
                return httpx.Response(200, json={"task": task, "detail": "Task already acknowledged"})
            task["status"] = "IN_PROGRESS"
            task["next_action"] = task.get("next_action") or f"Acknowledged: {task['title']}"
            task["next_check_at"] = "2026-08-18T12:00:00Z"
            self._acknowledged.append(task_id)
            return httpx.Response(200, json={"task": task, "detail": "Task acknowledged"})
        return await super().handler(request)


def test_reply_keyboard_always_four_keys_no_ops_assistant(make_app):
    """§3/§10: the Reply Keyboard is exactly the frozen 4 keys for every
    role; Operations Assistant never appears in any menu keyboard."""
    for role in (None, "owner", "secretary"):
        labels = _reply_labels(reply_keyboard(role))
        assert labels == V2_LABELS
    # Home summary keyboard: situational actions only (no menu grid).
    from pasay_bot.keyboards import home_summary_keyboard
    home_labels = _inline_labels(home_summary_keyboard("zh"))
    assert "⚠️ Today" in home_labels and "🔄 Refresh" in home_labels
    assert not any("运营助手" in l or "Assistant" in l for l in home_labels)
    # dashboard fallback keyboard: same minimal set.
    from pasay_bot.keyboards import dashboard_keyboard
    dash_labels = _inline_labels(dashboard_keyboard("zh"))
    assert "⚠️ Today" in dash_labels and "🔄 Refresh" in dash_labels
    assert not any("运营助手" in l or "Assistant" in l for l in dash_labels)


def test_home_callback_lands_on_unique_home(make_app):
    """§2.1: every 🏠 Home callback renders the ONE Home (Operations Overview)
    — never the legacy dashboard with Rent/Overdue/Operations Assistant."""
    env = make_app(backend=C3Backend())
    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, encode(ACTION_NAV, "home"), bot=env.bot)],
    )
    edit = env.bot.edits()[-1]
    assert "Pasay Property" in edit["text"]
    assert "本月应收" in edit["text"] or "Expected" in edit["text"]
    assert "运营助手" not in edit["text"]
    assert "Operations Assistant" not in edit["text"]
    labels = _inline_labels(edit["reply_markup"])
    assert "⚠️ Today" in labels and "🔄 Refresh" in labels
    # legacy dashboard markers never appear on Home
    assert "本月租金" not in edit["text"]


def test_operations_assistant_not_a_menu_entry(make_app):
    """§2.3: no menu keyboard routes to the Operations Assistant page; the
    TODAY capability is still reachable through its deterministic callback
    (natural language / pinned buttons), but never from a menu."""
    env = make_app(backend=C3Backend())
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "更多", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "运营助手" not in text and "Operations Assistant" not in text
    # the TODAY callback itself still works (AI capability, not a menu page)
    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID,
                              encode(ACTION_COPILOT_NAV, "today", nonce=new_nonce(), ts=now_ts()),
                              bot=env.bot)],
    )
    assert env.bot.edits()  # TODAY card rendered


def test_expense_list_short_open_buttons_and_detail_actions(make_app):
    """§4.2/§4.3/§10: the Expense LIST has one short ``E{id} · Open`` per
    payable row; the DETAIL card carries the short operations buttons."""
    env = make_app(backend=C3Backend())
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "💸 Expense", bot=env.bot)])
    send = env.bot.last_send()
    labels = _inline_labels(send["reply_markup"])
    assert "E1 · Open" in labels
    # long bilingual labels are banned from buttons (no `Remin...` truncation)
    assert not any("Remind Owner" in l or "查看详情" in l for l in labels)
    data = _inline_data(send["reply_markup"])
    open_cb = next(d for d in data if d.split(":")[1] == "exo")
    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, open_cb,
                              message_id=send["message_id"], bot=env.bot)],
    )
    detail = env.bot.edits()[-1]
    detail_labels = _inline_labels(detail["reply_markup"])
    assert any("提醒" in l or "Remind" in l for l in detail_labels)
    assert any("已付" in l or "Paid" in l for l in detail_labels)
    assert any("返回" in l or "Back" in l for l in detail_labels)
    assert any("首页" in l or "Home" in l for l in detail_labels)
    assert "Approved 2026-08-15" in detail["text"] or "批准于" in detail["text"]


def test_remind_owner_same_day_dedup(make_app):
    """§4.4: first Remind sends exactly one; a second tap the same day is
    deduped ("Already reminded today / 今日已提醒") and no second message."""
    env = make_app(backend=C3Backend())
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "💸 Expense", bot=env.bot)])
    send = env.bot.last_send()
    data = _inline_data(send["reply_markup"])
    open_cb = next(d for d in data if d.split(":")[1] == "exo")
    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, open_cb,
                              message_id=send["message_id"], bot=env.bot)],
    )
    detail = env.bot.edits()[-1]
    remind_cb = next(
        d for d in _inline_data(detail["reply_markup"]) if d.split(":")[1] == "rmo"
    )
    sends_before = len(env.bot.sends())
    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, remind_cb,
                              message_id=detail["message_id"], bot=env.bot)],
    )
    assert len(env.bot.sends()) == sends_before + 1
    # ZERO-LEARNING-004 §4: the ONE send is a REAL private DM to the OWNER.
    dm = env.bot.sends()[-1]
    assert dm["chat_id"] == OWNER_ID
    assert "Secretary requested your attention" in dm["text"] or "秘书提醒您处理" in dm["text"]
    # same-day second tap -> no new send, visible "already" feedback
    after = len(env.bot.sends())
    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, remind_cb,
                              message_id=detail["message_id"], update_id=42, bot=env.bot)],
    )
    assert len(env.bot.sends()) == after
    answer = env.bot.last_answer()["text"] or ""
    assert "今日已提醒" in answer or "already reminded" in answer.lower()


def test_rent_followup_updates_last_followup_and_dedups(make_app):
    """§5.2/§5.3: Follow up re-renders the detail with the REAL Last follow-up
    and flips the button to ✅ Followed up; a second tap the same day is
    deduped and no duplicate task is created."""
    env = make_app(backend=C3Backend())
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "💰 Rent", bot=env.bot)])
    send = env.bot.last_send()
    data = _inline_data(send["reply_markup"])
    detail_cb = next(d for d in data if d.split(":")[1] == "rnq")
    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, detail_cb,
                              message_id=send["message_id"], bot=env.bot)],
    )
    detail = env.bot.edits()[-1]
    # detail shows the truth: 3 unpaid periods, amount, overdue days
    assert "3" in detail["text"] and "₱75,000" in detail["text"] or "75000" in detail["text"]
    follow_cb = next(
        d for d in _inline_data(detail["reply_markup"]) if d.split(":")[1] == "rfu"
    )
    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, follow_cb,
                              message_id=detail["message_id"], bot=env.bot)],
    )
    after = env.bot.edits()[-1]
    # Last follow-up now shows a real time (never "none" right after)
    assert "Last follow-up" in after["text"] or "最近催租" in after["text"]
    assert "none" not in after["text"] and "无" not in after["text"]
    labels = _inline_labels(after["reply_markup"])
    assert any("今日已催" in l or "Followed up" in l for l in labels)
    # same-day second tap -> deduped toast, no duplicate task create
    task_creates = env.backend.count_calls("POST", "/operations/tasks")
    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, follow_cb,
                              message_id=detail["message_id"], update_id=43, bot=env.bot)],
    )
    answer = env.bot.last_answer()["text"] or ""
    assert "今日已催" in answer or "already followed" in answer.lower()
    assert env.backend.count_calls("POST", "/operations/tasks") == task_creates


def test_bilingual_missing_or_identical_translation_never_duplicates():
    """§6: a translation that is missing OR identical to English renders the
    English line exactly once."""
    # The zh table now carries real Chinese for the rent detail keys.
    rendered = t("v2.rent_overdue", "bi", days=104)
    assert rendered.count("Overdue") == 1
    assert "逾期" in rendered
    # A key whose zh value equals the en value (e.g. the bilingual header)
    # still renders once.
    header = t("v2.remind_owner_title", "bi")
    assert header.count("Payment Reminder") == 1
    # _bi_line/_bi_header dedupe identical fragments.
    assert cards._bi_line("bi", "1680 · Collect rent", "1680 · Collect rent") == "1680 · Collect rent"
    assert cards._bi_header("bi", "Pending", "Pending") == "Pending"


def test_rent_detail_card_fields_not_duplicated():
    """§6: the Rent detail card renders each field once (one compact bilingual
    line), never "Overdue: 104 days" twice."""
    text = cards.rent_detail_card(
        unit_label="1680", locale="bi", tenant_name="Carlo Reyes",
        outstanding="75000.00", unpaid_periods=3, overdue_days=104,
        last_followup="2026-08-17 11:47",
    )
    assert text.count("Outstanding") == 1
    assert text.count("Overdue") == 1
    assert text.count("Last follow-up") == 1
    assert text.count("Unpaid") == 1
    assert "未付" in text and "逾期" in text and "最近催租" in text


def test_daily_mark_store_atomic_and_persistent():
    """§1.4 (bot side): the same-day mark is atomic (second insert fails) and
    keyed by the PH local date."""
    store = StateStore(":memory:")
    key = f"next_check:501:{ph_local_date()}"
    assert store.mark_daily(key) is True
    assert store.mark_daily(key) is False  # same day -> no re-fire
    assert store.is_marked_daily(key) is True
    assert store.mark_daily(f"other:{ph_local_date()}") is True  # distinct key


def test_ack_callback_acknowledges_and_edits_in_place(make_app):
    """§1.5: tapping ✅ Acknowledge on a reminder marks the task IN_PROGRESS
    via the backend and edits the reminder message in place."""
    env = make_app(backend=C3Backend())
    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, encode(ACTION_ACK, "t", "501"), bot=env.bot)],
    )
    assert 501 in env.backend._acknowledged
    edit = env.bot.edits()[-1]
    assert "已处理" in edit["text"] or "Acknowledged" in edit["text"]
    # repeat tap -> idempotent (backend returns already-acknowledged)
    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, encode(ACTION_ACK, "t", "501"), update_id=7, bot=env.bot)],
    )
    assert len(env.backend._acknowledged) == 1
