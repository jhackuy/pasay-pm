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

import concurrent.futures
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


def _latest_assigned_task(env):
    assigned = [t for t in env.backend.operational_tasks if (t.get("details") or {}).get("assigned_to")]
    assert assigned
    return assigned[-1]


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
    # The group card is 🟡 (assigned), not ✅, and the button is flipped to the
    # assigned state (so it doesn't look pending/executable).
    group_after = env.bot.edits()[-1]
    assert "已交秘书" in group_after["text"] or "Assigned" in group_after["text"]
    labels = _inline_labels(group_after["reply_markup"])
    assert any("已交秘书" in (lbl or "") or "Assigned" in (lbl or "") for lbl in labels)

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


def test_owner_followup_second_click_no_second_dm(make_app):
    """PASAY-VNEXT-FOLLOWUP-FEEDBACK-005A: re-tapping Follow Up must be
    idempotent: no second Secretary DM, and the user gets a clear
    already-assigned toast + the card stays in assigned state."""
    env = make_app(backend=ClosureBackend())
    detail = _owner_open_rent_detail(env)
    dm = _owner_tap_followup(env, detail)
    sends = [c for c in env.bot.calls if c.get("type") == "send_message" and c.get("chat_id") == SECRETARY_ID]
    assert len(sends) == 1
    # After the first assign, the group card was edited in place; tap the
    # follow-up button again from the latest markup.
    group_after = env.bot.edits()[-1]
    follow_cb = next(d for d in _inline_data(group_after["reply_markup"]) if d.split(":")[1] == "rfu")
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, follow_cb,
                                           message_id=group_after["message_id"], update_id=99, bot=env.bot)])
    sends2 = [c for c in env.bot.calls if c.get("type") == "send_message" and c.get("chat_id") == SECRETARY_ID]
    assert len(sends2) == 1  # still only one real DM
    # The already-assigned feedback must be visible.
    acks = [c for c in env.bot.calls if c.get("type") == "answer_callback_query"]
    assert any("Already assigned" in (a.get("text") or "") or "无需重复" in (a.get("text") or "") for a in acks)


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
    # 1) Snooze path (edits the DM in place).
    env = make_app(backend=ClosureBackend())
    detail = _owner_open_rent_detail(env)
    dm = _owner_tap_followup(env, detail)
    dm_data = _inline_data(dm["reply_markup"])
    sfs = next(d for d in dm_data if d.split(":")[1] == "sfs")
    run_updates(env, [make_callback_update(SECRETARY_ID, SECRETARY_ID, sfs,
                                           message_id=dm["message_id"], bot=env.bot)])
    snooze_picker = env.bot.edits()[-1]
    assert any(c.split(":")[1] == "tsp" for c in _inline_data(snooze_picker["reply_markup"]))

    # 2) Payment path (fresh env so follow-up idempotency does not suppress a
    # second DM in the same test).
    env2 = make_app(backend=ClosureBackend())
    detail2 = _owner_open_rent_detail(env2)
    dm2 = _owner_tap_followup(env2, detail2)
    sfp = next(d for d in _inline_data(dm2["reply_markup"]) if d.split(":")[1] == "sfp")
    run_updates(env2, [make_callback_update(SECRETARY_ID, SECRETARY_ID, sfp,
                                           message_id=dm2["message_id"], bot=env2.bot)])
    after = env2.bot.edits()[-1]
    assert "method" in after["text"].lower() or "收款" in after["text"] or "method" in str(after)


def test_followup_delivery_does_not_complete_business_task(make_app):
    env = make_app(backend=ClosureBackend())
    detail = _owner_open_rent_detail(env)
    _owner_tap_followup(env, detail)
    task = _latest_assigned_task(env)
    assert task.get("status") != "COMPLETED"
    assert task.get("completed_at") is None


def test_followup_callback_replay_no_second_dm(make_app):
    env = make_app(backend=ClosureBackend())
    detail = _owner_open_rent_detail(env)
    follow_cb = next(d for d in _inline_data(detail["reply_markup"]) if d.split(":")[1] == "rfu")
    replay = make_callback_update(
        OWNER_ID, OWNER_ID, follow_cb, message_id=detail["message_id"], update_id=77, bot=env.bot
    )
    run_updates(env, [replay, replay])
    sends = [c for c in env.bot.calls if c.get("type") == "send_message" and c.get("chat_id") == SECRETARY_ID]
    assert len(sends) == 1


def test_followup_delivery_failure_then_retry_success(make_app):
    backend = ClosureBackend()
    backend.followup_delivery_failures["next"] = 1
    env = make_app(backend=backend)
    detail = _owner_open_rent_detail(env)
    follow_cb = next(d for d in _inline_data(detail["reply_markup"]) if d.split(":")[1] == "rfu")
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, follow_cb,
                                           message_id=detail["message_id"], bot=env.bot)])
    sends_after_fail = [c for c in env.bot.calls if c.get("type") == "send_message" and c.get("chat_id") == SECRETARY_ID]
    assert len(sends_after_fail) == 0
    assert not any((t.get("details") or {}).get("assigned_to") for t in env.backend.operational_tasks)
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, follow_cb,
                                           message_id=detail["message_id"], update_id=88, bot=env.bot)])
    sends_after_retry = [c for c in env.bot.calls if c.get("type") == "send_message" and c.get("chat_id") == SECRETARY_ID]
    assert len(sends_after_retry) == 1
    task = _latest_assigned_task(env)
    assert (task.get("details") or {}).get("assigned_to") == 2


def test_followup_restart_persistence_no_second_dm(make_app, tmp_path):
    backend = ClosureBackend()
    db_path = str(tmp_path / "followup-state.sqlite3")
    env1 = make_app(backend=backend, store_path=db_path)
    detail = _owner_open_rent_detail(env1)
    _owner_tap_followup(env1, detail)
    sends1 = [c for c in env1.bot.calls if c.get("type") == "send_message" and c.get("chat_id") == SECRETARY_ID]
    assert len(sends1) == 1

    env2 = make_app(backend=backend, store_path=db_path)
    detail2 = _owner_open_rent_detail(env2)
    follow_cb = next(d for d in _inline_data(detail2["reply_markup"]) if d.split(":")[1] == "rfu")
    run_updates(env2, [make_callback_update(OWNER_ID, OWNER_ID, follow_cb,
                                           message_id=detail2["message_id"], bot=env2.bot)])
    sends2 = [c for c in env2.bot.calls if c.get("type") == "send_message" and c.get("chat_id") == SECRETARY_ID]
    assert len(sends2) == 0
    acks = env2.bot.answers()
    assert (
        any("Already assigned" in (a.get("text") or "") or "无需重复" in (a.get("text") or "") for a in acks)
        or any("已交秘书" in (e.get("text") or "") or "Assigned" in (e.get("text") or "") for e in env2.bot.edits())
    )


def test_followup_concurrent_duplicate_single_secretary_dm(make_app):
    backend = ClosureBackend()
    env = make_app(backend=backend)
    detail = _owner_open_rent_detail(env)
    follow_cb = next(d for d in _inline_data(detail["reply_markup"]) if d.split(":")[1] == "rfu")

    def _tap(update_id: int):
        run_updates(env, [make_callback_update(
            OWNER_ID, OWNER_ID, follow_cb, message_id=detail["message_id"], update_id=update_id, bot=env.bot
        )])

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_tap, 201), pool.submit(_tap, 202)]
        for f in futures:
            f.result(timeout=30)

    sends = [c for c in env.bot.calls if c.get("type") == "send_message" and c.get("chat_id") == SECRETARY_ID]
    assert len(sends) == 1
    task = _latest_assigned_task(env)
    assert (task.get("details") or {}).get("assigned_to") == 2
