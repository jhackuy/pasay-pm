"""PASAY-VNEXT-NAV-TASK-CONVERGENCE-006B targeted bot regressions."""
from __future__ import annotations

from conftest import (
    FakeBackend,
    OWNER_ID,
    SECRETARY_ID,
    make_callback_update,
    make_text_update,
    run_updates,
)
from pasay_bot.keyboards import ACTION_DETAIL, ACTION_HOME_NAV, ACTION_NAV, encode

V2_LABELS = ["🏠 Home", "✅ Tasks", "💰 Rent", "💸 Expense"]


def _reply_labels(kb):
    if kb is None or kb.__class__.__name__ != "ReplyKeyboardMarkup":
        return []
    return [b.text for row in kb.keyboard for b in row]


def _inline_buttons(kb):
    if kb is None or kb.__class__.__name__ != "InlineKeyboardMarkup":
        return []
    return [b for row in kb.inline_keyboard for b in row]


class NavTask006BBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.financial_summary = {
            "month": "2026-08",
            "expected_rent_total": "50000.00",
            "collected_rent": "25000.00",
            "outstanding_rent": "25000.00",
            "collection_rate": "50.00",
        }
        self.overdue = [
            {"lease_id": 1, "property": "Bayshore", "unit": "7789", "tenant": "Tenant A", "amount": "160000.00", "days_overdue": 104},
            {"lease_id": 2, "property": "Solemare", "unit": "9950", "tenant": "Tenant B", "amount": "90000.00", "days_overdue": 62},
        ]
        self.quick_rent = {
            "overdue": [
                {"unit": "7789", "unit_code": "7789", "amount": "160000.00", "unpaid_periods": 4, "monthly_rent": "40000.00", "overdue_days": 104, "last_followup_at": None},
                {"unit": "9950", "unit_code": "9950", "amount": "90000.00", "unpaid_periods": 3, "monthly_rent": "30000.00", "overdue_days": 62, "last_followup_at": None},
            ],
            "outstanding_total": "250000.00",
            "month": "2026-08",
            "expected_rent_total": "70000.00",
            "collected_rent": "25000.00",
            "outstanding_rent": "45000.00",
            "collection_rate": "35.71",
            "unpaid_unit_count": 2,
        }
        self.quick_expense = {
            "month_total": "7000.00",
            "pending_approval_count": 0,
            "pending_approval_amount": "0.00",
            "unresolved_expense_tasks": [],
            "records": [],
            "payable": [{"kind": "payable_expense", "expense_id": 8, "unit": "1680", "purpose": "Repair", "amount": "7000.00", "status": "approved", "expense_date": "2026-08-14"}],
            "paid_records": [],
        }
        self.digest = {
            "act_now": [
                {"kind": "rent_overdue", "unit": "2308", "amount": "160000.00", "unpaid_periods": 4, "overdue_days": 104, "business_dedupe_key": "lease:1:RENT_OVERDUE"},
                {"kind": "payable_expense", "expense_id": 8, "unit": "1680", "purpose": "Repair", "amount": "7000.00", "business_dedupe_key": "expense:8:PAYMENT_PENDING"},
            ],
            "upcoming": [{"kind": "lease_expiring", "unit": "1103", "days_to_expiry": 12, "business_dedupe_key": "lease:3:LEASE_EXPIRING"}],
            "done_today": [{"kind": "rent_followup", "unit": "7789", "business_dedupe_key": "lease:7789:RENT_OVERDUE"}],
            "hidden": {"act_now": 0, "upcoming": 0, "done_today": 0},
            "counts": {"act_now": 2, "upcoming": 1, "done_today": 1},
        }
        self.quick_properties = [
            {"unit_code": "7789", "status": "overdue_rent", "amount": "160000.00", "days": 104, "unpaid_periods": 4, "open_maintenance": 0},
            {"unit_code": "2308", "status": "vacant", "amount": None, "days": None, "open_maintenance": 0},
            {"unit_code": "1103", "status": "paid", "amount": None, "days": None, "open_maintenance": 0},
        ]
        self.units = [
            {"id": 1, "property_id": 1, "unit_number": "1680", "floor": "16", "size_sqm": "32.50", "monthly_rent": "25000.00", "status": "occupied", "is_active": True},
            {"id": 2, "property_id": 1, "unit_number": "7789", "floor": "7", "size_sqm": "30.00", "monthly_rent": "40000.00", "status": "occupied", "is_active": True},
            {"id": 3, "property_id": 2, "unit_number": "9950", "floor": "9", "size_sqm": "28.00", "monthly_rent": "30000.00", "status": "occupied", "is_active": True},
        ]
        self.tenants = [
            {"id": 1, "full_name": "Maria Santos", "phone": "+639170000001", "id_number": None, "id_registered": False},
            {"id": 2, "full_name": "Tenant A", "phone": "+639170000002", "id_number": None, "id_registered": False},
            {"id": 3, "full_name": "Tenant B", "phone": "+639170000003", "id_number": None, "id_registered": False},
        ]
        self.leases = [
            {"id": 1, "unit_id": 1, "tenant_id": 1, "start_date": "2025-01-01", "end_date": "2026-12-31", "accounting_start_date": None, "monthly_rent": "25000.00", "deposit": "50000.00", "status": "active", "due_day": 20, "notes": None},
            {"id": 2, "unit_id": 2, "tenant_id": 2, "start_date": "2025-01-01", "end_date": "2026-12-31", "accounting_start_date": None, "monthly_rent": "40000.00", "deposit": "80000.00", "status": "active", "due_day": 1, "notes": None},
            {"id": 3, "unit_id": 3, "tenant_id": 3, "start_date": "2025-01-01", "end_date": "2026-12-31", "accounting_start_date": None, "monthly_rent": "30000.00", "deposit": "60000.00", "status": "active", "due_day": 1, "notes": None},
        ]
        self.incomes = [
            {
                "id": 46,
                "lease_id": 1,
                "amount": "25000.00",
                "received_date": "2026-08-14",
                "payment_method": "bank",
                "status": "confirmed",
                "description": "2026-08 Rent",
                "idempotency_key": None,
                "confirmed_by": 1,
                "confirmed_at": "2026-08-14T12:00:00Z",
            }
        ]


def test_existing_chat_receives_new_menu_on_next_interaction(make_app):
    for user_id in (OWNER_ID, SECRETARY_ID):
        env = make_app(backend=NavTask006BBackend())
        env.app.bot_data.setdefault("menu_init_chats", {})[user_id] = "legacy_menu_v1"
        run_updates(env, [make_text_update(user_id, user_id, "💰 Rent", bot=env.bot)])
        reply_sends = [s for s in env.bot.sends() if _reply_labels(s["reply_markup"])]
        assert reply_sends, "expected a menu migration send"
        assert _reply_labels(reply_sends[0]["reply_markup"]) == V2_LABELS


def test_home_refresh_stays_home_and_has_no_today_button(make_app):
    env = make_app(backend=NavTask006BBackend())
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, encode(ACTION_NAV, "home"), bot=env.bot)])
    home_edit = env.bot.edits()[-1]
    labels = [b.text for b in _inline_buttons(home_edit["reply_markup"])]
    assert "⚠️ Today" not in labels
    assert "🏢 Properties" in labels
    assert "🔄 Refresh" in labels

    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, encode(ACTION_HOME_NAV, "refresh"), message_id=home_edit["message_id"], update_id=2, bot=env.bot)],
    )
    refreshed = env.bot.edits()[-1]
    assert "运营总览" in refreshed["text"] or "Operations Overview" in refreshed["text"]
    assert "Act now" not in refreshed["text"] and "Done today" not in refreshed["text"]


def test_tasks_uses_digest_authority_and_matches_home_action_count(make_app):
    env = make_app(backend=NavTask006BBackend())
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "🏠 Home", bot=env.bot)])
    home_text = env.bot.last_send()["text"]
    assert "今日待办 2" in home_text or "Today's actions 2" in home_text

    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "✅ Tasks", message_id=2, update_id=2, bot=env.bot)])
    tasks_text = env.bot.last_send()["text"]
    assert "Act now" in tasks_text or "现在处理" in tasks_text
    assert "Upcoming" in tasks_text or "即将处理" in tasks_text
    assert "Done today" in tasks_text or "今日完成" in tasks_text
    assert "2308" in tasks_text and "1680" in tasks_text


def test_properties_renders_units_directly_and_archive_is_url_button(make_app):
    env = make_app(backend=NavTask006BBackend())
    env.settings.archive_chat_id = "-1001234567890"
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "🏠 Properties", bot=env.bot)])
    send = env.bot.last_send()
    text = send["text"]
    assert "Property Overview" not in text and "房源概况" not in text
    assert "Units: 1" not in text and "总房源 1" not in text
    for unit in ("7789", "2308", "1103"):
        assert unit in text
    buttons = _inline_buttons(send["reply_markup"])
    archive = next(b for b in buttons if b.text == "📁 Open Property Archive")
    assert archive.url == "https://t.me/c/1234567890"
    assert archive.callback_data is None


def test_payment_record_includes_business_identity_fields(make_app):
    env = make_app(backend=NavTask006BBackend())
    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, encode(ACTION_DETAIL, "inc", "46"), bot=env.bot)],
    )
    text = env.bot.edits()[-1]["text"]
    assert "💰 付款记录" in text or "💰 Payment record" in text
    assert "1680 · 2026-08 Rent" in text
    assert "Tenant: Maria Santos" in text or "租客: Maria Santos" in text
    assert "Amount: ₱25,000" in text or "金额: ₱25,000" in text
    assert "Method: Bank" in text or "方式: Bank" in text
    assert "Status: Confirmed" in text or "状态: 已确认" in text
    assert "Date: 2026-08-14" in text or "日期: 2026-08-14" in text
    assert "#46" in text
