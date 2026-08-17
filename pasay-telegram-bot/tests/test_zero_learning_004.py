"""TELEGRAM-ZERO-LEARNING-UX-POLISH-004 — regression tests.

Pins the "zero learning" Telegram UX:
1. Properties: exceptions in WORDS (no 💰⚠️ / 📄✅ / 🔧0 / 👁); normal = OK;
   per-unit buttons are SHORT bare unit codes, 2 per row.
2. Home: the product button has ONE name — `🏠 Home` (never 首页 /
   Home 首页 / Dashboard); the Home card wording is explicit.
3. Expense: payee never falls back to the purpose; missing payee renders
   `Not recorded / 未登记`; the detail is compact (not an ERP field form).
4. Remind Owner: a REAL private DM to the Owner; Reminded only after the DM
   succeeds; a DM failure records NO success and keeps the Remind button;
   same-day dedup keeps exactly one DM.
5. Tasks: each payable expense appears EXACTLY ONCE (in the To-pay group);
   the mirrored PAYMENT_PENDING task is excluded from Pending.
"""
from __future__ import annotations

import httpx

from conftest import (
    OWNER_ID,
    FakeBackend,
    make_callback_update,
    make_text_update,
    run_updates,
)
from pasay_bot.keyboards import (
    encode,
    home_keyboard,
    reply_keyboard,
)
from pasay_bot.render import cards
from pasay_bot.render.i18n import t

V2_LABELS = ["🏠 Properties", "✅ Tasks", "💰 Rent", "💸 Expense"]
BANNED_CHIPS = ("💰⚠️", "📄✅", "📄⚠️", "🔧0", "👁")


def _inline_labels(kb):
    if kb is None or kb.__class__.__name__ != "InlineKeyboardMarkup":
        return []
    return [b.text for row in kb.inline_keyboard for b in row]


def _inline_data(kb):
    if kb is None or kb.__class__.__name__ != "InlineKeyboardMarkup":
        return []
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _reply_labels(kb):
    if kb is None or kb.__class__.__name__ != "ReplyKeyboardMarkup":
        return []
    return [b.text for row in kb.keyboard for b in row]


class ZLBackend(FakeBackend):
    """Fixture backend: one overdue+repair unit, one paid (OK) unit, one
    vacant, one lease-expiring; E7/E8 approved-unpaid; a PAYMENT_PENDING task
    mirroring E7 (the duplication case)."""

    def __init__(self):
        super().__init__()
        self.quick_properties = [
            {"unit_code": "DEV-BAY-1680", "status": "overdue_rent",
             "amount": "75000.00", "days": 104, "unpaid_periods": 3,
             "open_maintenance": 1},
            {"unit_code": "DEV-BAY-1203", "status": "paid", "open_maintenance": 0},
            {"unit_code": "DEV-SOL-1805", "status": "lease_expiring",
             "days": 18, "open_maintenance": 0},
            {"unit_code": "DEV-BAY-2308", "status": "vacant", "open_maintenance": 0},
        ]
        self.expenses = [
            {"id": 7, "expense_date": "2026-08-15", "due_date": None,
             "category": "??", "amount": "7000.00", "payee": "Repair",
             "description": None, "unit_id": 9, "status": "approved",
             "receipt_attachment_id": None, "approved_by": 1,
             "approved_at": "2026-08-15T10:00:00Z"},
            {"id": 8, "expense_date": "2026-08-15", "due_date": None,
             "category": "??", "amount": "7000.00", "payee": "Repair",
             "description": None, "unit_id": 9, "status": "approved",
             "receipt_attachment_id": None, "approved_by": 1,
             "approved_at": "2026-08-15T10:00:00Z"},
        ]
        self.quick_expense = {
            "month_total": "14000.00",
            "pending_approval_count": 0,
            "pending_approval_amount": "0.00",
            "unresolved_expense_tasks": [],
            "records": [],
            "payable": [
                {"kind": "payable_expense", "expense_id": 7, "unit": "1680",
                 "purpose": "Repair", "amount": "7000.00", "status": "approved",
                 "expense_date": "2026-08-15", "waiting_days": 2},
                {"kind": "payable_expense", "expense_id": 8, "unit": "1680",
                 "purpose": "Repair", "amount": "7000.00", "status": "approved",
                 "expense_date": "2026-08-15", "waiting_days": 2},
            ],
            "paid_records": [],
        }
        self.quick_tasks = [
            # E7 mirrored as a PAYMENT_PENDING operational task (the
            # duplication case: must NOT appear twice).
            {"id": 701, "task_type": "PAYMENT_PENDING", "title": "待付款支出 · 维修",
             "status": "PENDING", "property_code": "1680", "expense_id": 7,
             "amount": "7000.00", "purpose": "Repair",
             "overdue_days": 2, "next_action": "Pay the approved expense"},
            {"id": 702, "task_type": "RENT_OVERDUE", "title": "租金逾期 · 3期",
             "status": "PENDING", "property_code": "1680",
             "amount": "75000.00", "unpaid_periods": 3, "overdue_days": 104,
             "next_action": "Follow up with tenant"},
        ]


# ---------------------------------------------------------------------------
# Properties: words, not chips
# ---------------------------------------------------------------------------

def test_properties_never_renders_chip_codes(make_app):
    env = make_app(backend=ZLBackend())
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "🏠 Properties", bot=env.bot)])
    text = env.bot.last_send()["text"]
    for banned in BANNED_CHIPS:
        assert banned not in text
    # TELEGRAM-OPS-REAL-WORLD-CLOSURE-005 §1: ONE traffic light per unit,
    # exception in words, SHORT unit id, red-first order. zh owner chat.
    assert "🔴 1680 · 欠租104天" in text or "🔴 1680 · Rent overdue 104d" in text
    assert "3期" in text or "3 period(s)" in text
    assert "待修1项" in text or "Repair 1" in text
    assert "🟢 1203 · OK" in text       # paid/healthy -> OK
    assert "🟡 1805 · 合同18天到期" in text or "🟡 1805 · Lease expires in 18d" in text
    assert "🟡 2308 · 空置" in text or "🟡 2308 · Vacant" in text
    # Each unit row carries exactly ONE light (the summary line above carries a
    # separate 🔴 need-action count). Split into blocks: block0 = header,
    # block1 = summary, the rest are per-unit rows.
    blocks = [b for b in text.split("\n\n") if b]
    row_blocks = blocks[2:]
    row_text = "\n".join(row_blocks)
    assert row_text.count("🔴") == 1 and row_text.count("🟢") == 1 and row_text.count("🟡") == 2
    # Red-first ordering: 1680 (🔴) appears before 1805/2308 (🟡) before 1203 (🟢).
    assert text.index("🔴 1680") < text.index("🟡 1805") < text.index("🟢 1203")


def test_properties_short_unit_buttons_two_per_row(make_app):
    env = make_app(backend=ZLBackend())
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "🏠 Properties", bot=env.bot)])
    kb = env.bot.last_send()["reply_markup"]
    labels = _inline_labels(kb)
    # short bare unit codes, no 👁 prefix, no full property code in buttons
    assert "1680" in labels and "1203" in labels and "1805" in labels and "2308" in labels
    assert not any("👁" in lbl or "DEV-" in lbl for lbl in labels)
    # two buttons per row (last row may hold one)
    rows = kb.inline_keyboard
    assert all(len(row) <= 2 for row in rows)
    assert any(len(row) == 2 for row in rows)
    # compact archive entry does not dominate
    assert any("Archive" in lbl for lbl in labels)


# ---------------------------------------------------------------------------
# Home: one name, explicit wording
# ---------------------------------------------------------------------------

def test_home_button_has_single_name():
    for locale in ("zh", "en", "bi"):
        labels = _inline_labels(home_keyboard(locale))
        assert labels == ["🏠 Home"], (locale, labels)
    # the reply keyboard never contains a Home button (fixed 4-key menu only)
    assert "Home" not in _reply_labels(reply_keyboard(None))
    assert "首页" not in _reply_labels(reply_keyboard(None))


def test_home_wording_explicit(make_app):
    env = make_app(backend=ZLBackend())
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "更多", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "本月应收" in text or "Expected" in text
    assert "本月未收" in text or "This month outstanding" in text
    assert "历史累计欠租" in text or "Total arrears" in text
    assert "逾期租金" in text or "Overdue rents" in text
    # no terse unexplained terms
    for terse in ("Today 8", "Expiring 1", "Arrears ₱", "Outstanding ₱"):
        assert terse not in text
    # Home carries NO first-level business buttons: the fixed Reply Keyboard
    # stays the ONLY navigation (the situational ⚠️ Today / 🔄 Refresh ride on
    # edited cards, not on a fresh send).
    kb = env.bot.last_send()["reply_markup"]
    assert _reply_labels(kb) == V2_LABELS


# ---------------------------------------------------------------------------
# Expense: payee semantics + compact detail
# ---------------------------------------------------------------------------

def test_expense_payee_never_falls_back_to_purpose():
    from types import SimpleNamespace

    # E7/E8 legacy row: category='??', payee='Repair' — 'Repair' is a business
    # category word, NOT a vendor -> Not recorded.
    e7 = SimpleNamespace(category="??", description=None, payee="Repair")
    assert cards._expense_display_payee(e7) is None
    assert cards._expense_purpose_text(e7) == "Repair"  # purpose still truthful
    # a real vendor renders
    real = SimpleNamespace(category="维修", description=None, payee="Fix-It Co")
    assert cards._expense_display_payee(real) == "Fix-It Co"
    # the '-' unknown-vendor sentinel never renders
    dash = SimpleNamespace(category="维修", description=None, payee="-")
    assert cards._expense_display_payee(dash) is None


def test_expense_detail_compact_and_no_form_labels(make_app):
    env = make_app(backend=ZLBackend())
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "💸 Expense", bot=env.bot)])
    send = env.bot.last_send()
    data = _inline_data(send["reply_markup"])
    open_cb = next(d for d in data if d.split(":")[1] == "exo")
    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, open_cb,
                              message_id=send["message_id"], bot=env.bot)],
    )
    text = env.bot.edits()[-1]["text"]
    assert "💸" in text and "E7" in text
    assert "Repair" in text and "₱7,000" in text
    assert "未登记" in text or "Not recorded" in text
    # no ERP field-form labels
    for label in ("Purpose\n", "用途\n", "Amount\n", "金额\n", "Status\n", "状态\n"):
        assert label not in text
    # compact: at most 9 lines
    assert len([l for l in text.splitlines() if l.strip()]) <= 9


# ---------------------------------------------------------------------------
# Remind Owner: REAL DM semantics
# ---------------------------------------------------------------------------

def test_remind_owner_dm_failure_never_marks_reminded(make_app):
    """A DM failure (recipient resolution 500) must NOT record success: no
    send, a visible ⚠️ failure, and the button stays 🔔 Remind."""
    env = make_app(backend=ZLBackend())
    env.backend.fail_status["/operations/remind-owner-target"] = 500
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
    # no DM was sent (failure)
    assert len(env.bot.sends()) == sends_before
    # the failure is surfaced durably on the tapped card (the processing ack
    # already consumed the single callback answer)
    failed_edit = env.bot.edits()[-1]["text"]
    assert "提醒失败" in failed_edit or "failed" in failed_edit.lower()
    # the same-day mark was NOT recorded -> a retry stays allowed
    from pasay_bot.state.store import ph_local_date
    assert not env.store.is_marked_daily(f"remind_owner:7:{ph_local_date()}")


def test_remind_owner_dm_success_sends_to_owner_private(make_app):
    env = make_app(backend=ZLBackend())
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
    dm = env.bot.sends()[-1]
    assert dm["chat_id"] == OWNER_ID  # the Owner's PRIVATE chat
    # the button flipped to ✅ Reminded on the group card
    flipped = _inline_labels(env.bot.edits()[-1]["reply_markup"])
    assert any("已提醒" in l or "Reminded" in l for l in flipped)


# ---------------------------------------------------------------------------
# Tasks: one representation per business item
# ---------------------------------------------------------------------------

def test_tasks_payable_appears_once_not_in_pending(make_app):
    env = make_app(backend=ZLBackend())
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "✅ Tasks", bot=env.bot)])
    text = env.bot.last_send()["text"]
    # E7 appears EXACTLY ONCE (the To-pay group), never again under Pending
    assert text.count("E7") == 1
    # the To-pay section shows the single representation with waiting days
    assert "To pay 2" in text or "待付款 2" in text
    assert "waiting 2d" in text or "等待 2 天" in text
    # the RENT_OVERDUE operational task still renders under Pending
    assert "3期" in text or "3 periods" in text
    assert "逾期 104 天" in text or "overdue 104d" in text
