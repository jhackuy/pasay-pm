"""TELEGRAM-OPS-UX-CONVERGENCE-001 — regression tests.

Pins the converged Telegram Operations UX:
1. the fixed bottom Reply Keyboard never drifts by role/chat-type/language;
2. Properties renders as a high-density one-line-per-unit index;
3. no Owner/Secretary-visible text ever leaks `??` / raw placeholder / enum;
4. Expense shows Category/Purpose with a safe unspecified fallback;
5. Rent overdue rows are directly actionable (Follow up -> Rent detail);
6. a waiting-payment expense gives the Secretary a working Remind Owner action
   that sends exactly one reminder message with full context;
7. inline detail navigation edits the tapped message instead of spamming the
   group with new messages.

The fake backend lives in tests/conftest.py (integrator-owned); this file
subclasses it locally like test_v2_ux.py does.
"""
from __future__ import annotations

import time

import pytest
from telegram import InlineKeyboardMarkup, ReplyKeyboardMarkup, Update

from conftest import (
    OWNER_ID,
    SECRETARY_ID,
    FakeBackend,
    make_callback_update,
    make_text_update,
    run_updates,
)
from pasay_bot.keyboards import (
    FIXED_MENU_ROUTES,
    fixed_menu_route_for,
    LEGACY_MENU_ROUTES,
    reply_keyboard,
)
from pasay_bot.render.i18n import t

V2_LABELS = ["🏠 Properties", "✅ Tasks", "💰 Rent", "💸 Expense"]
BANNED_TEXT = ("??", "None", "null", "undefined", "NoneType")
GROUP_CHAT_ID = -1009876543210


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


class ConvergeBackend(FakeBackend):
    """FakeBackend + small deterministic quick-view fixtures for the checks."""

    def __init__(self):
        super().__init__()
        # add a unit matching the quick-view unit codes so unit resolution works
        self.units = [
            {"id": 1, "property_id": 1, "unit_number": "16B", "floor": "16",
             "size_sqm": "40.00", "monthly_rent": "55000.00", "status": "occupied", "is_active": True},
            {"id": 2, "property_id": 1, "unit_number": "17A", "floor": "17",
             "size_sqm": "35.00", "monthly_rent": "45000.00", "status": "vacant", "is_active": True},
            {"id": 3, "property_id": 2, "unit_number": "2C", "floor": "2",
             "size_sqm": "25.00", "monthly_rent": "12000.00", "status": "occupied", "is_active": True},
            {"id": 9, "property_id": 1, "unit_number": "1680", "floor": "16",
             "size_sqm": "32.50", "monthly_rent": "75000.00", "status": "occupied", "is_active": True},
            {"id": 10, "property_id": 1, "unit_number": "1702", "floor": "17",
             "size_sqm": "32.50", "monthly_rent": "70000.00", "status": "vacant", "is_active": True},
        ]
        self.leases = [
            {"id": 1, "unit_id": 1, "tenant_id": 1, "start_date": "2026-01-01",
             "end_date": "2026-12-31", "accounting_start_date": None, "monthly_rent": "55000.00",
             "deposit": "110000.00", "status": "active", "due_day": 5, "notes": None},
            {"id": 2, "unit_id": 3, "tenant_id": 2, "start_date": "2026-03-01",
             "end_date": "2026-12-31", "accounting_start_date": None, "monthly_rent": "12000.00",
             "deposit": "24000.00", "status": "active", "due_day": 10, "notes": None},
            {"id": 9, "unit_id": 9, "tenant_id": 1, "start_date": "2026-01-01",
             "end_date": "2026-12-31", "accounting_start_date": None, "monthly_rent": "75000.00",
             "deposit": "150000.00", "status": "active", "due_day": 5, "notes": None},
        ]
        self.quick_properties = [
            {"unit_code": "1680", "status": "overdue_rent",
             "amount": "75000.00", "days": 12, "open_maintenance": 1},
            {"unit_code": "1702", "status": "vacant", "open_maintenance": 0},
        ]
        self.quick_rent = {
            "overdue": [
                {"unit": "1680", "amount": "75000.00", "overdue_days": 12},
            ],
            "outstanding_total": "75000.00",
            "month": "2026-08",
            "expected_rent_total": "150000.00",
            "collected_rent": "75000.00",
            "outstanding_rent": "75000.00",
            "collection_rate": "50.00",
            "unpaid_unit_count": 1,
        }
        # Approved-unpaid expenses with a `??` category. E1 has a real payee
        # (the backend resolves it as the purpose); E2 has none.
        self.expenses = [
            {"id": 1, "expense_date": "2026-08-15", "due_date": None,
             "category": "??", "amount": "2500.00", "payee": "Aircon Repair Co",
             "description": None, "unit_id": 9, "status": "approved",
             "receipt_attachment_id": None, "approved_by": 1,
             "approved_at": "2026-08-15T10:00:00Z"},
            {"id": 2, "expense_date": "2026-08-16", "due_date": None,
             "category": "??", "amount": "1800.00", "payee": "-",
             "description": None, "unit_id": 9, "status": "approved",
             "receipt_attachment_id": None, "approved_by": 1,
             "approved_at": "2026-08-16T10:00:00Z"},
        ]
        self.quick_expense = {
            "month_total": "4300.00",
            "pending_approval_count": 0,
            "pending_approval_amount": "0.00",
            "unresolved_expense_tasks": [],
            # Mirror the backend: purpose = resolved category->payee chain.
            "records": [
                {"expense_id": 1, "unit": "1680", "unit_code": "1680",
                 "purpose": "Aircon Repair Co", "amount": "2500.00",
                 "expense_date": "2026-08-15", "status": "approved"},
                {"expense_id": 2, "unit": "1680", "unit_code": "1680",
                 "purpose": "", "amount": "1800.00",
                 "expense_date": "2026-08-16", "status": "approved"},
            ],
            "payable": [
                {"kind": "payable_expense", "expense_id": 1, "unit": "1680",
                 "purpose": "Aircon Repair Co", "amount": "2500.00", "status": "approved",
                 "expense_date": "2026-08-15"},
                {"kind": "payable_expense", "expense_id": 2, "unit": "1680",
                 "purpose": "", "amount": "1800.00", "status": "approved",
                 "expense_date": "2026-08-16"},
            ],
            "paid_records": [],
        }
        # quick-tasks with a legacy `??`-suffixed title -> must not leak `??`.
        self.quick_tasks = [
            {"task_id": 501, "property_code": "1680", "title": "待付款支出 · ??",
             "status": "PENDING", "due_at": "2026-08-20T00:00:00+08:00",
             "overdue_days": 2, "next_action": "Follow up Owner payment"},
        ]


def test_fixed_keyboard_is_identical_and_english_for_all_roles(make_app):
    """Section 一: the Reply Keyboard is one fixed English 4-button menu,
    identical for Owner and Secretary and in every chat locale — no drift to
    Chinese, no per-role re-generation."""
    owner_menu = reply_keyboard(None)
    assert _reply_labels(owner_menu) == V2_LABELS
    # The English labels are the ONLY primary menu labels.
    for row in owner_menu.keyboard:
        for btn in row:
            assert btn.text in V2_LABELS

    for role_author in (OWNER_ID, SECRETARY_ID):
        env = make_app(backend=ConvergeBackend())
        # Owner private (zh), Secretary private (en), and a group (bi).
        for chat_id in (role_author, GROUP_CHAT_ID):
            env2 = make_app(backend=ConvergeBackend())
            run_updates(
                env2,
                [make_group_text_update(role_author, chat_id, "🏠 Properties", bot=env2.bot)]
                if chat_id == GROUP_CHAT_ID
                else [make_text_update(role_author, chat_id, "🏠 Properties", bot=env2.bot)],
            )
            sends = env2.bot.sends()
            # find any reply keyboard sent on this tap flow
            rk_labels = []
            for s in sends:
                rk = _reply_labels(s["reply_markup"])
                if rk:
                    rk_labels.append(rk)
            # The persistent reply keyboard that any flow re-mounts is the
            # fixed English menu (messages carrying inline keyboards keep the
            # pinned reply menu unchanged).
            for labels in rk_labels:
                assert labels == V2_LABELS, f"drifting keyboard for {role_author}/chat {chat_id}: {labels}"

    # Every fixed label routes deterministically; no fixed label is Chinese.
    for label in V2_LABELS:
        assert fixed_menu_route_for(label) in ("properties", "tasks", "rent", "expense")
        assert label not in LEGACY_MENU_ROUTES


def test_properties_index_one_unit_per_line_and_no_expansion(make_app):
    """Section 二/三 + ZERO-LEARNING-004 §1 + TELEGRAM-OPS-REAL-WORLD-CLOSURE-005
    §1: high-density index, ONE traffic light per unit, exceptions in WORDS,
    normal units OK, no 💰⚠️ / 📄✅ / 🔧0 / 👁, per-unit SHORT buttons."""
    env = make_app(backend=ConvergeBackend())
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "🏠 Properties", bot=env.bot)])
    send = env.bot.last_send()
    text = send["text"]
    assert "Properties · 2" in text or "房源 · 2" in text
    assert "🔴 1680" in text  # overdue + 1 open repair -> red
    assert "欠租12天" in text or "Rent overdue 12d" in text
    assert "待修1项" in text or "Repair 1" in text
    assert "🟡 1702" in text and ("空置" in text or "Vacant" in text)  # vacant -> yellow
    assert text.count("\n\n") <= 3  # compact, high-density (not a tall card)
    for banned in BANNED_TEXT + ("💰⚠️", "📄✅", "🔧0", "👁", "⚪", "🔵"):
        assert banned not in text
    inline = _inline_labels(send["reply_markup"])

    def has(label):
        return any(label in lbl for lbl in inline)

    assert has("1680") and has("1702")
    assert not any("👁" in lbl for lbl in inline)
    assert has("📄 Archive")
    # no expansion of full tenant / deposit / contract on the index screen
    assert "deposit" not in text.lower() and "tenant" not in text.lower()


def test_quick_tasks_never_leaks_placeholder_or_question_marks(make_app):
    """Section 十: a legacy `待付款支出 · ??` task title never reaches a visible
    Tasks card."""
    env = make_app(backend=ConvergeBackend())
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "✅ Tasks", bot=env.bot)])
    text = env.bot.last_send()["text"]
    for banned in BANNED_TEXT:
        assert banned not in text


def test_expense_safe_purpose_fallback_never_shows_placeholder(make_app):
    """Section 九/十: an APPROVED expense with a `??`/empty purpose resolves to
    its truthful payee, or to the neutral unspecified label — never `??`."""
    env = make_app(backend=ConvergeBackend())
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "💸 Expense", bot=env.bot)])
    text = env.bot.last_send()["text"]
    for banned in BANNED_TEXT:
        assert banned not in text
    # E1 has a real payee -> the truthful payee surfaces.
    assert "Aircon Repair Co" in text
    # E1 and E2 each appear once (no duplicate rows).
    assert text.count("E1") == 1 and text.count("E2") == 1
    # E2 has no truthful purpose -> the neutral per-locale label, not `??`.
    assert "Other" in text or "其他" in text


def test_rent_overdue_followup_reachable_and_detail_actionable(make_app):
    """Section 七/八: overdue rows on the Rent quick view are statusful
    navigation entries that open a Rent detail with the executable follow-up
    action living on the detail card."""
    env = make_app(backend=ConvergeBackend())
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "💰 Rent", bot=env.bot)])
    send = env.bot.last_send()
    inline = _inline_labels(send["reply_markup"])

    def has(label):
        return any(label in lbl for lbl in inline)

    assert has("1680")
    assert any("Pending" in lbl or "待催" in lbl or "Followed up" in lbl or "已交秘书" in lbl for lbl in inline)
    # Tap the quick-row navigation -> Rent detail card with the 3 actions.
    data = _inline_data(send["reply_markup"])
    followup_cb = next(d for d in data if d.split(":")[1] == "rnq")
    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, followup_cb, message_id=send["message_id"], bot=env.bot)],
    )
    detail_edit = env.bot.edits()[-1]
    detail_text = detail_edit["text"]
    assert "1680" in detail_text
    assert "未付" in detail_text or "Outstanding" in detail_text
    detail_labels = _inline_labels(detail_edit["reply_markup"])

    def has_detail(label):
        return any(label in lbl for lbl in detail_labels)

    assert any("催租" in lbl or "Follow up" in lbl for lbl in detail_labels)
    assert any("收租" in lbl or "Collect" in lbl for lbl in detail_labels)
    assert any("记录" in lbl or "History" in lbl for lbl in detail_labels)


def test_rent_overdue_followup_edits_in_place_not_new_message(make_app):
    """Section 八/十一: opening the Rent detail EDITS the tapped message (no
    new send), so navigation never pollutes the group flow."""
    env = make_app(backend=ConvergeBackend())
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "💰 Rent", bot=env.bot)])
    send = env.bot.last_send()
    data = _inline_data(send["reply_markup"])
    followup_cb = next(d for d in data if d.split(":")[1] == "rnq")
    sends_before = len(env.bot.sends())
    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, followup_cb, message_id=send["message_id"], bot=env.bot)],
    )
    assert len(env.bot.edits()) >= 1  # detail rendered by editing the card
    assert len(env.bot.sends()) == sends_before  # no new send for navigation


def test_secretary_remind_owner_waits_payment_sends_exactly_one(make_app):
    """CONVERGENCE-003 §4.2/§4.4: the Expense LIST carries one short
    ``E{id} · Open`` button per payable row (list = reading); the Remind
    action lives on the DETAIL card and sends exactly one reminder message
    with full context. A second tap the same day is deduped
    ("Already reminded today / 今日已提醒")."""
    env = make_app(backend=ConvergeBackend())
    run_updates(
        env,
        [make_group_text_update(SECRETARY_ID, GROUP_CHAT_ID, "💸 Expense", bot=env.bot)],
    )
    send = env.bot.last_send()
    inline = _inline_labels(send["reply_markup"])

    def has(label):
        return any(label in lbl for lbl in inline)

    # The list must NOT carry the long per-row Remind buttons any more.
    assert has("E1 · Open")
    assert has("E2 · Open")
    assert not any("Remind Owner" in lbl for lbl in inline)
    data = _inline_data(send["reply_markup"])
    open_cb = next(d for d in data if d.split(":")[1] == "exo")
    run_updates(
        env,
        [make_callback_update(SECRETARY_ID, GROUP_CHAT_ID, open_cb,
                              message_id=send["message_id"], bot=env.bot)],
    )
    detail = env.bot.edits()[-1]
    detail_labels = _inline_labels(detail["reply_markup"])
    assert any("提醒" in lbl or "Remind" in lbl for lbl in detail_labels)

    remind_cb = next(
        d for d in _inline_data(detail["reply_markup"]) if d.split(":")[1] == "rmo"
    )
    sends_before = len(env.bot.sends())
    run_updates(
        env,
        [make_callback_update(SECRETARY_ID, GROUP_CHAT_ID, remind_cb,
                              message_id=detail["message_id"], bot=env.bot)],
    )
    # Exactly one new reminder message — a REAL private DM to the Owner
    # (ZERO-LEARNING-004 §4), never a group re-post.
    assert len(env.bot.sends()) == sends_before + 1
    dm = env.bot.sends()[-1]
    assert dm["chat_id"] == OWNER_ID
    reminder = dm["text"]
    assert "Payment Reminder" in reminder
    assert "1680" in reminder and ("2,500" in reminder or "2500" in reminder)
    # The DM speaks the OWNER's language (zh private chat).
    assert "批准于 2026-08-15" in reminder or "Approved 2026-08-15" in reminder
    for banned in BANNED_TEXT:
        assert banned not in reminder
    # Same-day repeat tap -> no second DM ("already reminded").
    sends_after_first = len(env.bot.sends())
    run_updates(
        env,
        [make_callback_update(SECRETARY_ID, GROUP_CHAT_ID, remind_cb,
                              message_id=detail["message_id"], update_id=77, bot=env.bot)],
    )
    assert len(env.bot.sends()) == sends_after_first  # deduped, no new send


def test_property_archive_link_present(make_app):
    """Section 三: the Properties index exposes the 📄 Property Archive deep
    link to the archive channel (index in group, full archive in channel)."""
    env = make_app(backend=ConvergeBackend())
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "🏠 Properties", bot=env.bot)])
    data = _inline_data(env.bot.last_send()["reply_markup"])
    archive_cb = next(d for d in data if d.split(":")[1] == "par")
    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, archive_cb,
                              message_id=env.bot.last_send()["message_id"], bot=env.bot)],
    )
    edit = env.bot.edits()[-1]
    assert "Property Archive" in edit["text"]
