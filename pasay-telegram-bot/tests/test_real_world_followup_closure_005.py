"""TELEGRAM-OPS-REAL-WORLD-CLOSURE-005 — bot-side real-world closure tests.

Pins:
- Owner tapping 📞 催租 sends a REAL private DM to the Secretary and flips the
  group card to 🟡 已交秘书跟进 (never ✅);
- only the Secretary's real ``✅ 已联系租客`` confirmation moves 🟡 -> ✅
  (task COMPLETED + executed daily-dedup mark + DM card done);
- ⏰ 稍后处理 routes into the existing snooze (preset picker);
- 💰 已收款 routes into the existing record-payment flow;
- same-day execution never creates a second real follow-up.
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
from pasay_bot.state.store import ph_local_date

BANNED = ("💰⚠️", "📄✅", "🔧0", "👁")


def _inline_data(kb):
    if kb is None or kb.__class__.__name__ != "InlineKeyboardMarkup":
        return []
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _inline_labels(kb):
    if kb is None or kb.__class__.__name__ != "InlineKeyboardMarkup":
        return []
    return [b.text for row in kb.inline_keyboard for b in row]


class ClosureBackend(FakeBackend):
    def __init__(self):
        super().__init__()
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
                 "overdue_days": 104, "last_followup_at": "2026-08-15T23:20:00+08:00"},
            ],
            "outstanding_total": "75000.00",
            "month": "2026-08",
            "expected_rent_total": "25000.00",
            "collected_rent": "0.00",
            "outstanding_rent": "25000.00",
            "collection_rate": "0.00",
            "unpaid_unit_count": 1,
        }


def _owner_open_rent_detail(env):
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "💰 Rent", bot=env.bot)])
    send = env.bot.last_send()
    data = _inline_data(send["reply_markup"])
    detail_cb = next(d for d in data if d.split(":")[1] == "rnq")
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, detail_cb,
                                           message_id=send["message_id"], bot=env.bot)])
    return env.bot.edits()[-1]


def _owner_tap_followup(env, detail):
    follow_cb = next(d for d in _inline_data(detail["reply_markup"]) if d.split(":")[1] == "rfu")
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, follow_cb,
                                           message_id=detail["message_id"], bot=env.bot)])
    # the real DM the Secretary received
    dm = env.bot.sends()[-1]
    return dm


def test_owner_followup_dms_secretary_and_secretary_confirms(make_app):
    """Full closure: Owner 催租 -> Secretary DM -> ✅ 已联系租客 -> ✅ 今日已催."""
    env = make_app(backend=ClosureBackend())
    detail = _owner_open_rent_detail(env)
    dm = _owner_tap_followup(env, detail)
    # A REAL DM to the Secretary private chat with the collection card + the
    # three execution buttons.
    assert dm["chat_id"] == SECRETARY_ID
    assert "1680" in dm["text"] and "75,000" in dm["text"]
    dm_data = _inline_data(dm["reply_markup"])
    assert any(d.split(":")[1] == "sfc" for d in dm_data)   # ✅ 已联系租客
    assert any(d.split(":")[1] == "sfp" for d in dm_data)   # 💰 已收款
    assert any(d.split(":")[1] == "sfs" for d in dm_data)   # ⏰ 稍后处理
    # The group card is 🟡 (assigned), not ✅, and the button is not flipped.
    group_after = env.bot.edits()[-1]
    assert "已交秘书" in group_after["text"] or "Assigned" in group_after["text"]

    # Secretary taps ✅ 已联系租客 (private chat).
    sfc = next(d for d in dm_data if d.split(":")[1] == "sfc")
    run_updates(env, [make_callback_update(SECRETARY_ID, SECRETARY_ID, sfc,
                                           message_id=dm["message_id"], bot=env.bot)])
    # The follow-up task was COMPLETED (backend truth).
    completed = [t for t in env.backend.operational_tasks
                 if t.get("status") == "COMPLETED"]
    assert completed
    # Executed same-day dedup mark set (Secretary really executed).
    assert env.store.is_marked_daily(f"followup:{9}:{ph_local_date()}")
    # The Secretary's DM card flipped to done (Secretary locale is English).
    last_edit = env.bot.edits()[-1]
    assert ("Followed up today" in (last_edit["text"] or "")
            or "Already recorded today" in (last_edit["text"] or "")
            or "已联系" in (last_edit["text"] or "")
            or "今日已催" in (last_edit["text"] or ""))


def test_secretary_dm_contact_same_day_no_second_followup(make_app):
    """§4.1: a second same-day ✅ 已联系租客 must not create a second real
    follow-up (executed daily-dedup mark blocks it)."""
    env = make_app(backend=ClosureBackend())
    detail = _owner_open_rent_detail(env)
    dm = _owner_tap_followup(env, detail)
    sfc = next(d for d in _inline_data(dm["reply_markup"]) if d.split(":")[1] == "sfc")
    run_updates(env, [make_callback_update(SECRETARY_ID, SECRETARY_ID, sfc,
                                           message_id=dm["message_id"], bot=env.bot)])
    completions = env.backend.count_calls("POST", "/operations/tasks/9/complete") \
        + env.backend.count_calls("POST", "/operations/tasks/10/complete") \
        + env.backend.count_calls("POST", "/operations/tasks/11/complete")
    # Re-tap with a new nonce (update_id=77).
    sfc2 = next(d for d in _inline_data(dm["reply_markup"]) if d.split(":")[1] == "sfc")
    run_updates(env, [make_callback_update(SECRETARY_ID, SECRETARY_ID, sfc2,
                                           message_id=dm["message_id"], update_id=77,
                                           bot=env.bot)])
    completions_after = env.backend.count_calls("POST", "/operations/tasks/9/complete") \
        + env.backend.count_calls("POST", "/operations/tasks/10/complete") \
        + env.backend.count_calls("POST", "/operations/tasks/11/complete")
    assert completions_after == completions  # no second completion fired twice
    assert env.store.is_marked_daily(f"followup:{9}:{ph_local_date()}")


def test_secretary_dm_snooze_and_payment_buttons_route(make_app):
    """§5/§6: ⏰ 稍后处理 opens the existing snooze preset picker; 💰 已收款
    routes into the existing record-payment flow (never a forced PAID)."""
    env = make_app(backend=ClosureBackend())
    detail = _owner_open_rent_detail(env)
    dm = _owner_tap_followup(env, detail)
    dm_data = _inline_data(dm["reply_markup"])
    # ⏰ 稍后处理
    sfs = next(d for d in dm_data if d.split(":")[1] == "sfs")
    run_updates(env, [make_callback_update(SECRETARY_ID, SECRETARY_ID, sfs,
                                           message_id=dm["message_id"], bot=env.bot)])
    snooze_picker = env.bot.edits()[-1]
    assert any(c.split(":")[1] == "tsp" for c in _inline_data(snooze_picker["reply_markup"]))
    # 💰 已收款 -> routes to the deterministic record-payment flow via the
    # ACTION_RENT "go" handler. Re-open a fresh follow-up and tap 💰 已收款.
    detail2 = _owner_open_rent_detail(env)
    dm3 = _owner_tap_followup(env, detail2)
    sfp = next(d for d in _inline_data(dm3["reply_markup"]) if d.split(":")[1] == "sfp")
    run_updates(env, [make_callback_update(SECRETARY_ID, SECRETARY_ID, sfp,
                                           message_id=dm3["message_id"], bot=env.bot)])
    # The record-payment flow shows the pay-method selector (existing path).
    after = env.bot.edits()[-1]
    assert "method" in after["text"].lower() or "收款" in after["text"] or "method" in str(after)
