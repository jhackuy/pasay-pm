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
             "amount": "75000.00", "days": 12},
            {"unit_code": "1805", "status": "lease_expiring", "days": 28},
            {"unit_code": "2208", "status": "paid"},
            {"unit_code": "2106", "status": "vacant"},
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
        }
        self.quick_expense = {
            "month_total": "19650.00",
            "pending_approval_count": 1,
            "pending_approval_amount": "3500.00",
            "unresolved_expense_tasks": [],
            "recent_expenses": [
                {"id": 1, "unit": "1680", "purpose": "Repair / 维修",
                 "category": "Repair / 维修", "amount": "6001.00",
                 "expense_date": "2026-08-15", "date": "2026-08-15",
                 "status": "paid"},
                {"id": 2, "unit": "1680", "purpose": "Water / 水费",
                 "category": "Water / 水费", "amount": "3500.00",
                 "expense_date": "2026-08-02", "date": "2026-08-02",
                 "status": "pending"},
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
    assert "Properties / 房源" in text
    assert "Rent ₱75,000 overdue" in text
    assert "租金 ₱75,000 逾期" in text
    assert "Vacant" in text and "空置" in text
    assert _labels(send["reply_markup"]) == V2_LABELS
    assert _copilot_calls(env) == []


def test_rent_quick_view_owner_chinese(make_app):
    env = make_app(backend=V2FakeBackend())
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "💰 Rent", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "逾期租金" in text
    assert "₱75,000" in text
    assert "未收总额：₱99,000" in text
    assert "overdue 12d" not in text  # owner private chat is Chinese-first


def test_rent_quick_view_secretary_english(make_app):
    env = make_app(backend=V2FakeBackend())
    run_updates(env, [make_text_update(SECRETARY_ID, SECRETARY_ID, "💰 Rent", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "Overdue rent" in text
    assert "overdue 12d" in text
    assert "逾期" not in text  # secretary private chat is English only


def test_expense_quick_view_shows_amounts(make_app):
    env = make_app(backend=V2FakeBackend())
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "💸 Expense", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "本月：₱19,650" in text
    assert "待审批：1 笔 · ₱3,500" in text
    assert "无未解决支出事项" in text


def test_expense_quick_view_shows_recent_records_with_statuses(make_app):
    """PASAY-V2-EXPENSE-LIST-003 Cases A, B, C.

    A: current month holds one PENDING + one PAID — both appear.
    B: the PAID record is not an unresolved task but stays in expense history.
    C: each row exposes unit · purpose · amount · date · status.
    """
    env = make_app(backend=V2FakeBackend())
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "💸 Expense", bot=env.bot)])
    text = env.bot.last_send()["text"]
    # Case C: row shape unit · purpose · amount · date(MM-DD) · status.
    assert "1680 · Repair / 维修 ·" in text
    assert "₱6,001 · 08-15 · 已付款" in text
    assert "1680 · Water / 水费 ·" in text
    assert "₱3,500 · 08-02 · 待批准" in text
    # Case B: a paid record must be visible and there are no unresolved tasks.
    assert "已付款" in text
    assert "无未解决支出事项" in text


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
