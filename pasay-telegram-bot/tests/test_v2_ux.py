"""PASAY-V2-FOUNDATION-001 — Telegram UX slice tests.

Pins the V2 behavior that intentionally supersedes the BOT-V1 menu:
- fixed keyboard = 4 English Quick View buttons, identical for every role;
- fixed buttons route to deterministic Quick Views (never an LLM);
- group replies are bilingual (English + 中文); Secretary DM English;
  Owner DM Chinese-first;
- /start is a short greeting (never the full dashboard) and only the rescue
  commands remain on the slash menu;
- new-chat-member (bot added to group) self-heals the fixed keyboard;
- approval buttons carry the amount.

The FakeBackend lives in tests/conftest.py (integrator-owned), so this file
subclasses it locally and never edits conftest.
"""
from __future__ import annotations

import time

import httpx
import pytest
from telegram import Update

from conftest import (
    OWNER_ID,
    SECRETARY_ID,
    FakeBackend,
    make_text_update,
    run_updates,
)
from pasay_bot.api_client import PasayApiClient
from pasay_bot.keyboards import (
    FIXED_MENU_ROUTES,
    LEGACY_MENU_ROUTES,
    expense_approval_keyboard,
    fixed_menu_route_for,
    reply_keyboard,
)
from pasay_bot.roles import Role

GROUP_CHAT_ID = -1001234567890

V2_LABELS = ["🏠 Properties", "✅ Tasks", "💰 Rent", "💸 Expense"]

_V2_CLIENT_ROUTES = {
    "get_quick_tasks": "/operations/quick/tasks",
    "get_quick_properties": "/operations/quick/properties",
    "get_quick_rent": "/operations/quick/rent",
    "get_quick_expense": "/operations/quick/expense",
    "get_digest": "/operations/digest",
}


@pytest.fixture(autouse=True)
def _ensure_v2_api_client_methods(monkeypatch):
    """Unit isolation for the UX slice: if Subagent C's api_client methods
    have not landed yet, install thin GET stubs so the routing/cards can be
    tested against the fake backend. Real client plumbing is C's own tests."""
    installed = []
    for name, path in _V2_CLIENT_ROUTES.items():
        if not hasattr(PasayApiClient, name):
            async def _stub(self, _path=path):
                return await self._request("GET", _path)

            _stub.__name__ = name
            monkeypatch.setattr(PasayApiClient, name, _stub, raising=False)
            installed.append(name)
    return installed


class V2FakeBackend(FakeBackend):
    """FakeBackend + the V2 deterministic quick-view endpoints."""

    def __init__(self):
        super().__init__()
        self.quick_properties = [
            {"unit_code": "1680", "status": "overdue_rent",
             "amount": "75000.00", "days": 12, "open_maintenance": 1},
            {"unit_code": "1805", "status": "lease_expiring", "days": 28, "open_maintenance": 0},
            {"unit_code": "2208", "status": "paid", "open_maintenance": 0},
            {"unit_code": "2106", "status": "vacant", "open_maintenance": 0},
        ]
        self.quick_tasks = [
            {"task_id": 1, "property_code": "1680", "title": "Rent",
             "status": "PENDING", "due_at": "2026-08-10T00:00:00+08:00",
             "overdue_days": 4, "next_action": "Collect payment",
             "next_check_at": "2026-08-15T00:00:00+08:00"},
            {"task_id": 2, "property_code": "1805", "title": "Lease renewal",
             "status": "PENDING", "due_at": "2026-09-01T00:00:00+08:00",
             "due_in_days": 18, "next_action": "Renew lease"},
            {"task_id": 3, "property_code": "2208", "title": "Aircon repair",
             "status": "IN_PROGRESS", "next_action": "Confirm repair completion",
             "next_check_at": "2026-08-17T00:00:00+08:00"},
        ]
        self.quick_rent = {
            "overdue": [
                {"unit": "1680", "amount": "75000.00", "overdue_days": 12},
                {"unit": "1805", "amount": "24000.00", "overdue_days": 4},
            ],
            "outstanding_total": "99000.00",
            "month": "2026-08",
            "expected_rent_total": "150000.00",
            "collected_rent": "51000.00",
            "outstanding_rent": "99000.00",
            "collection_rate": "34.00",
            "unpaid_unit_count": 2,
        }
        self.quick_expense = {
            "month_total": "19650.00",
            "pending_approval_count": 1,
            "pending_approval_amount": "3500.00",
            "unresolved_expense_tasks": [],
            "records": [
                {
                    "expense_id": 101,
                    "unit": "1680",
                    "unit_code": "1680",
                    "purpose": "Repair / 维修",
                    "amount": "6001.00",
                    "expense_date": "2026-08-15",
                    "status": "paid",
                },
                {
                    "expense_id": 102,
                    "unit": "1680",
                    "unit_code": "1680",
                    "purpose": "Water / 水费",
                    "amount": "3500.00",
                    "expense_date": "2026-08-02",
                    "status": "approved",
                },
                {
                    "expense_id": 103,
                    "unit": "1680",
                    "unit_code": "1680",
                    "purpose": "Electric / 电费",
                    "amount": "1200.00",
                    "expense_date": "2026-08-01",
                    "status": "pending",
                },
            ],
            # EXPENSE-UX-FIX-001: pending-payment (APPROVED unpaid) and this
            # month's PAID records are separate sections.
            "payable": [
                {
                    "kind": "payable_expense",
                    "expense_id": 102,
                    "unit": "1680",
                    "unit_code": "1680",
                    "purpose": "Water / 水费",
                    "amount": "3500.00",
                    "status": "approved",
                    "expense_date": "2026-08-02",
                },
            ],
            "paid_records": [
                {
                    "expense_id": 101,
                    "unit": "1680",
                    "unit_code": "1680",
                    "purpose": "Repair / 维修",
                    "amount": "6001.00",
                    "expense_date": "2026-08-15",
                    "status": "paid",
                },
            ],
        }
        self.digest = {
            "pending": self.quick_tasks[:2],
            "in_progress": [self.quick_tasks[2]],
            "recently_completed": [
                {"property_code": "1608", "title": "Water bill",
                 "status": "COMPLETED"},
            ],
        }

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/api/v1"):
            path = path[len("/api/v1"):]
        quick = {
            "/operations/quick/tasks": self.quick_tasks,
            "/operations/quick/properties": self.quick_properties,
            "/operations/quick/rent": self.quick_rent,
            "/operations/quick/expense": self.quick_expense,
            "/operations/digest": self.digest,
        }
        if path in quick:
            self.calls.append((request.method, path, None))
            self.auth_calls.append(request.headers.get("authorization") or "")
            self.telegram_user_calls.append(
                request.headers.get("x-telegram-user-id")
            )
            return httpx.Response(200, json=quick[path])
        return await super().handler(request)


def _labels(kb):
    if kb is None or kb.__class__.__name__ != "ReplyKeyboardMarkup":
        return []
    return [b.text for row in kb.keyboard for b in row]


def _inline_labels(kb):
    if kb is None or kb.__class__.__name__ != "InlineKeyboardMarkup":
        return []
    return [b.text for row in kb.inline_keyboard for b in row]


def _reply_sends(env):
    return [
        s for s in env.bot.sends()
        if s["reply_markup"] is not None
        and s["reply_markup"].__class__.__name__ == "ReplyKeyboardMarkup"
    ]


def make_group_text_update(user_id, chat_id, text, bot=None):
    return Update.de_json(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": int(time.time()),
                "chat": {"id": chat_id, "type": "supergroup", "title": "Pasay Group"},
                "from": {
                    "id": user_id, "is_bot": False,
                    "first_name": "T", "username": "t",
                },
                "text": text,
            },
        },
        bot,
    )


def make_join_update(user_ids, chat_id, bot=None):
    return Update.de_json(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": int(time.time()),
                "chat": {"id": chat_id, "type": "supergroup", "title": "Pasay Group"},
                "from": {
                    "id": 999000111, "is_bot": False,
                    "first_name": "Adder", "username": "adder",
                },
                "new_chat_members": [
                    {
                        "id": uid, "is_bot": False,
                        "first_name": f"U{uid}", "username": f"u{uid}",
                    }
                    for uid in user_ids
                ],
            },
        },
        bot,
    )


def make_photo_update(user_id, chat_id, bot=None):
    return Update.de_json(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": int(time.time()),
                "chat": {"id": chat_id, "type": "private"},
                "from": {
                    "id": user_id, "is_bot": False,
                    "first_name": "T", "username": "t",
                },
                "photo": [
                    {"file_id": "AAA", "file_unique_id": "aaa",
                     "width": 10, "height": 10, "file_size": 100}
                ],
            },
        },
        bot,
    )


def _copilot_calls(env):
    return [
        p for _, p, _ in env.backend.calls
        if "/copilot" in p or p in ("/today", "/reports/tasks")
    ]


# --- fixed keyboard: V2 labels, identical for every role --------------------

def test_reply_keyboard_v2_labels_identical_for_roles():
    owner = reply_keyboard(Role.OWNER)
    secretary = reply_keyboard(Role.SECRETARY)
    assert _labels(owner) == V2_LABELS
    assert _labels(secretary) == V2_LABELS
    assert owner.resize_keyboard is True
    assert owner.is_persistent is True
    assert secretary.resize_keyboard is True
    assert secretary.is_persistent is True


def test_fixed_menu_routes_v2_english_labels():
    assert FIXED_MENU_ROUTES == {
        "🏠 Properties": "properties",
        "✅ Tasks": "tasks",
        "💰 Rent": "rent",
        "💸 Expense": "expense",
    }
    for label, route in FIXED_MENU_ROUTES.items():
        assert fixed_menu_route_for(label) == route


def test_legacy_chinese_aliases_still_route():
    assert LEGACY_MENU_ROUTES == {
        "🏠 首页": "home",
        "✅ 待办": "pending",
        "💰 收租": "rent",
        "💸 支出": "expense",
    }
    for label, route in LEGACY_MENU_ROUTES.items():
        assert fixed_menu_route_for(label) == route


# --- quick views: deterministic, no LLM, one reply --------------------------

def test_tasks_quick_view_deterministic_no_llm(make_app):
    env = make_app(backend=V2FakeBackend())
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "✅ Tasks", bot=env.bot)])
    sends = env.bot.sends()
    assert len(sends) == 1  # single card, no processing stub / junk
    text = sends[0]["text"]
    assert "1680" in text
    assert "未完成" in text
    assert "进行中" in text
    assert "下一步：Collect payment" in text
    assert _labels(sends[0]["reply_markup"]) == V2_LABELS
    assert _copilot_calls(env) == []


def test_properties_quick_view_group_bilingual(make_app):
    env = make_app(backend=V2FakeBackend())
    run_updates(
        env,
        [make_group_text_update(OWNER_ID, GROUP_CHAT_ID, "🏠 Properties", bot=env.bot)],
    )
    send = env.bot.last_send()
    text = send["text"]
    assert "Properties / 房源 · 4" in text  # frozen: title carries the unit count
    # frozen high-density index: one line per unit with rent/maint/lease chips.
    assert "1680　💰⚠️　🔧1" in text     # overdue rent + 1 open maintenance
    assert "1805" in text and "📄⚠️" in text  # lease expiring
    assert "2208" in text and "💰✅" in text and "📄✅" in text  # paid + lease ok
    assert "2106　VACANT / 空置" in text
    # no tenant / deposit / contract detail is expanded on the index page.
    assert "Juan" not in text and "deposit" not in text.lower()
    # The index now carries per-unit Quick View entries + the archive deep link
    # as INLINE buttons (the persistent reply keyboard stays pinned separately).
    inline_labels = _inline_labels(send["reply_markup"])
    assert "👁 1680" in inline_labels and "👁 1805" in inline_labels
    assert "📄 Property Archive" in inline_labels
    assert set(inline_labels) != set(V2_LABELS)  # inline, not the reply menu
    assert _copilot_calls(env) == []


def test_properties_quick_view_occupancy_summary(make_app):
    """PASAY-V2-OWNER-SECRETARY-JOURNEY-AUDIT-006 (Journey A): the 🏠 Properties
    quick view must show the occupancy summary (total / occupied / vacant /
    occupancy rate) above the unit list, and rent delinquency is never mixed
    into it. 4 fixture units (3 leased of any status + 1 vacant) -> 75%."""
    env = make_app(backend=V2FakeBackend())
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "🏠 Properties", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "总计 4" in text and "已出租 3" in text and "空置 1" in text and "出租率 75%" in text
    # Occupancy stats must NOT quote the overdue rent amount (no delinquency mix).
    assert "租金 ₱75,000" not in text.split("📊")[0]
    assert _copilot_calls(env) == []


def test_rent_quick_view_owner_chinese(make_app):
    env = make_app(backend=V2FakeBackend())
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "💰 Rent", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "逾期租金" in text
    assert "₱75,000" in text
    # CONVERGENCE-003 §9: distinct labels — this-month outstanding vs arrears.
    assert "本月未收 ₱99,000" in text
    assert "历史累计欠租 ₱99,000" in text
    assert "overdue 12d" not in text  # owner private chat is Chinese-first


def test_rent_quick_view_secretary_english(make_app):
    env = make_app(backend=V2FakeBackend())
    run_updates(env, [make_text_update(SECRETARY_ID, SECRETARY_ID, "💰 Rent", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "Overdue rent" in text
    assert "overdue 12d" in text
    assert "逾期" not in text  # secretary private chat is English only


def test_rent_quick_view_month_statistics(make_app):
    """PASAY-V2-OWNER-SECRETARY-JOURNEY-AUDIT-006 (Journey B): the 💸 Rent quick
    view must show current-month expected / collected / outstanding / collection
    rate / unpaid unit count. outstanding = expected - collected (partial
    payments reduce it). 150000 expected, 51000 collected -> 99000 outstanding
    (kept in the summary, distinct from the overdue aggregate)."""
    env = make_app(backend=V2FakeBackend())
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "💰 Rent", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "本月应收 ₱150,000" in text
    assert "已收 ₱51,000" in text
    assert "本月未收 ₱99,000" in text
    assert "收缴率 34%" in text
    assert "未缴房间 2 间" in text
    # The overdue aggregate line stays too (distinct label, §9).
    assert "历史累计欠租 ₱99,000" in text


def test_expense_quick_view_shows_amounts(make_app):
    env = make_app(backend=V2FakeBackend())
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "💸 Expense", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "本月：₱19,650" in text
    assert "待审批：1 笔 · ₱3,500" in text
    assert "待付款 · 1" in text
    assert "无未解决支出事项" not in text


def test_expense_quick_view_lists_month_records_owner_chinese(make_app):
    """EXPENSE-UX-FIX-001: the 💸 Expense quick view shows the pending-payment
    queue (APPROVED unpaid, real fields, plain E{id}) first, then this
    month's PAID records. Same-date/same-amount records stay distinguishable
    by the plain-text Expense ID. Owner private chat is Chinese."""
    env = make_app(backend=V2FakeBackend())
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "💸 Expense", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "本月：₱19,650" in text
    # pending payment section first, with every real field
    assert "E102 · 1680 · Water / 水费 · <b>₱3,500</b> · 08-02 · 📋 待付款" in text
    # then the PAID records section
    assert "E101 · 1680 · Repair / 维修 · <b>₱6,001</b> · 08-15 · ✅ 已付款" in text
    # PENDING expenses stay a summary count only — never a duplicate row
    assert "E103" not in text
    # no unresolved-task duplicate block
    assert "未解决" not in text
    assert "无未解决支出事项" not in text


def test_expense_quick_view_group_bilingual_records(make_app):
    """EXPENSE-UX-FIX-001: group replies keep English + 中文 on the expense
    page, including the pending-payment and paid section chips."""
    env = make_app(backend=V2FakeBackend())
    run_updates(
        env,
        [make_group_text_update(OWNER_ID, GROUP_CHAT_ID, "💸 Expense", bot=env.bot)],
    )
    text = env.bot.last_send()["text"]
    assert "Pending payment / 待付款" in text
    assert "Paid / 已付款" in text
    assert "Approved / 待付款" in text


class EmptyExpenseBackend(V2FakeBackend):
    """This month has no expenses at all -> the quick view shows the real
    empty state instead of a record list."""

    def __init__(self):
        super().__init__()
        self.quick_expense = {
            "month_total": "0.00",
            "pending_approval_count": 0,
            "pending_approval_amount": "0.00",
            "unresolved_expense_tasks": [],
            "records": [],
            "payable": [],
            "paid_records": [],
        }


def test_expense_quick_view_true_empty_state(make_app):
    env = make_app(backend=EmptyExpenseBackend())
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "💸 Expense", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "本月暂无支出记录" in text
    assert "无未解决支出事项" not in text


def test_expense_quick_view_live_shaped_payload_clean_and_unique(make_app):
    """EXPENSE-UX-FIX-001 end-to-end: a live-shaped payload (E7/E8 APPROVED
    with a legacy `??` category, E23 PAID) renders WITHOUT the `#E` hashtag,
    WITHOUT `??`, and WITHOUT any duplicated APPROVED row."""
    env = make_app(backend=V2FakeBackend())
    env.backend.quick_expense = {
        "month_total": "21002.00",
        "pending_approval_count": 0,
        "pending_approval_amount": "0.00",
        "unresolved_expense_tasks": [
            {"task_type": "PAYMENT_PENDING", "title": "待付款支出 · ??",
             "status": "PENDING", "due_at": "2026-08-15T09:26:18+08:00",
             "property_code": "DEV-BAY-1680", "source_type": "expense",
             "source_id": 7},
        ],
        "records": [
            {"expense_id": 7, "unit": "DEV-BAY-1680", "unit_code": "DEV-BAY-1680",
             "purpose": "Repair", "amount": "7000.00",
             "expense_date": "2026-08-15", "status": "approved"},
            {"expense_id": 8, "unit": "DEV-BAY-1680", "unit_code": "DEV-BAY-1680",
             "purpose": "Repair", "amount": "7000.00",
             "expense_date": "2026-08-15", "status": "approved"},
            {"expense_id": 23, "unit": "DEV-BAY-1680", "unit_code": "DEV-BAY-1680",
             "purpose": "维修", "amount": "6002.00",
             "expense_date": "2026-08-15", "status": "paid"},
        ],
        "payable": [
            {"kind": "payable_expense", "expense_id": 8,
             "unit": "DEV-BAY-1680", "unit_code": "DEV-BAY-1680",
             "purpose": "Repair", "amount": "7000.00", "status": "approved",
             "expense_date": "2026-08-15"},
            {"kind": "payable_expense", "expense_id": 7,
             "unit": "DEV-BAY-1680", "unit_code": "DEV-BAY-1680",
             "purpose": "Repair", "amount": "7000.00", "status": "approved",
             "expense_date": "2026-08-15"},
        ],
        "paid_records": [
            {"expense_id": 23, "unit": "DEV-BAY-1680", "unit_code": "DEV-BAY-1680",
             "purpose": "维修", "amount": "6002.00",
             "expense_date": "2026-08-15", "status": "paid"},
        ],
    }
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "💸 Expense", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "#E" not in text
    for banned in ("??", "None", "null", "undefined"):
        assert banned not in text
    # E7/E8 appear exactly once (pending payment), E23 once (paid)
    assert text.count("E7") == 1
    assert text.count("E8") == 1
    assert text.count("E23") == 1
    assert "待付款支出" not in text
    assert "本月：₱21,002" in text


def test_legacy_rent_alias_routes_to_quick_view(make_app):
    env = make_app(backend=V2FakeBackend())
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "💰 收租", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "逾期租金" in text


def test_quick_views_never_call_llm_or_copilot(make_app):
    env = make_app(backend=V2FakeBackend())
    for label in ("🏠 Properties", "✅ Tasks", "💰 Rent", "💸 Expense"):
        run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, label, bot=env.bot)])
        assert _copilot_calls(env) == []
        assert "处理中" not in "\n".join(env.bot.all_texts())
        assert "Processing" not in "\n".join(env.bot.all_texts())


# --- greeting /start: short, no dashboard -----------------------------------

def test_start_shows_short_greeting_not_dashboard(make_app):
    env = make_app(backend=V2FakeBackend())
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "/start", bot=env.bot)])
    send = env.bot.last_send()
    assert "Hello / 你好" in send["text"]
    assert "直接告诉我发生了什么" in send["text"]
    assert "Pasay Property" not in send["text"]
    assert "本月已收" not in send["text"]
    assert _labels(send["reply_markup"]) == V2_LABELS
    assert send["reply_markup"].is_persistent is True


def test_start_greeting_secretary_english(make_app):
    env = make_app(backend=V2FakeBackend())
    run_updates(env, [make_text_update(SECRETARY_ID, SECRETARY_ID, "/start", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "Just tell me what happened" in text
    assert "直接告诉我" not in text


# --- slash menu: only rescue commands ---------------------------------------

def test_only_rescue_commands_registered(make_app):
    env = make_app(backend=V2FakeBackend())
    names = set()
    for handlers in env.app.handlers.values():
        for h in handlers:
            cmds = getattr(h, "commands", None)
            if cmds:
                names.update(cmds)
    assert names == {"start", "help", "cancel"}


# --- keyboard self-healing: bot added to group ------------------------------

def test_new_chat_member_group_gets_fixed_keyboard_no_start_prompt(make_app):
    env = make_app(backend=V2FakeBackend())
    run_updates(env, [make_join_update([OWNER_ID], GROUP_CHAT_ID, bot=env.bot)])
    send = env.bot.last_send()
    assert "Welcome to the Pasay group" in send["text"]
    assert "欢迎加入 Pasay 群组" in send["text"]
    assert _labels(send["reply_markup"]) == V2_LABELS
    assert "/start" not in "\n".join(env.bot.all_texts()).lower()


# --- media ack: photo carries the keyboard ----------------------------------

def test_photo_message_ack_received_with_keyboard(make_app):
    env = make_app(backend=V2FakeBackend())
    run_updates(env, [make_photo_update(OWNER_ID, OWNER_ID, bot=env.bot)])
    send = env.bot.last_send()
    assert "已收到，后台处理中" in send["text"]
    assert _labels(send["reply_markup"]) == V2_LABELS


# --- approval buttons carry the value ---------------------------------------

def test_approval_button_includes_amount_and_unit():
    kb = expense_approval_keyboard(5, amount="3500.00", unit="1680")
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert "Approve 1680 · ₱3.5K" in labels
    assert "Reject" in labels


def test_approval_button_includes_amount_only():
    kb = expense_approval_keyboard(5, amount="3500.00")
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert "Approve ₱3,500" in labels


def test_approval_button_fallback_without_amount():
    kb = expense_approval_keyboard(5, locale="zh")
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert "✅ 批准" in labels  # legacy fallback when amount unknown
