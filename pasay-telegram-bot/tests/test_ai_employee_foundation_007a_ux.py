"""PASAY-AI-EMPLOYEE-FOUNDATION-007A — Owner Live UX Addendum (bot-side).

Pins:
- A) deterministic buttons are fast-path (empty ACK, no "处理中" toast for fast
  nav/detail; the phase profile is recorded: callback_ack_ms / backend_fetch_ms
  / render_ms / telegram_edit_ms / total_ms).
- B) HOME = global Operations Overview, BACK = business parent. Property /
  Rent / Expense detail cards carry ``◀ <parent> | 🏠 Home``.
- C) Global Home title: Owner zh ``运营总览``, Secretary en ``Pasay Operations``,
  ``Pasay Property`` is gone; Home stays separate from Properties.
- D) ⚠️ Today uses the SAME digest builder + renderer as the scheduled job
  (get_digest + active_tasks_digest_card), never the quick-tasks path.
"""
from __future__ import annotations

from conftest import (
    OWNER_ID,
    SECRETARY_ID,
    make_callback_update,
    make_text_update,
    run_updates,
)
from pasay_bot.keyboards import ACTION_NAV, ACTION_HOME_NAV, encode
from pasay_bot.state.latency import LatencyTracker


def _labels(kb):
    if kb is None or kb.__class__.__name__ != "InlineKeyboardMarkup":
        return []
    return [b.text for row in kb.inline_keyboard for b in row]


def _inline_data(kb):
    if kb is None or kb.__class__.__name__ != "InlineKeyboardMarkup":
        return []
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _home_edit(env):
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, encode(ACTION_NAV, "home"),
                                           bot=env.bot)])
    return env.bot.edits()[-1]


# --- C) Global Home title ---------------------------------------------

def test_global_home_title_owner_zh_operations_overview(make_app):
    env = make_app()
    edit = _home_edit(env)
    assert "运营总览" in edit["text"]
    assert "Pasay Property" not in edit["text"]


def test_home_shows_collection_and_property_summary(make_app):
    env = make_app()
    edit = _home_edit(env)
    text = edit["text"]
    assert "本月应收" in text or "Expected" in text
    assert "收缴率" in text or "Collection rate" in text
    assert "历史累计欠租" in text or "Total arrears" in text
    assert "总房源" in text or "Total units" in text
    assert "已出租" in text or "Occupied" in text


def test_global_home_separate_from_properties(make_app):
    """Home still carries the situational ⚠️ Today / 🔄 Refresh actions and is
    a God View - it never merges into the Properties index."""
    env = make_app()
    edit = _home_edit(env)
    labels = _labels(edit["reply_markup"])
    assert "⚠️ Today" in labels and "🏢 Properties" in labels and "🔄 Refresh" in labels


def test_home_properties_child_navigation(make_app):
    env = make_app()
    edit = _home_edit(env)
    props_cb = next(d for d in _inline_data(edit["reply_markup"]) if d.split(":")[1] == "nav" and d.split(":")[2] == "properties")
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, props_cb,
                                           message_id=edit["message_id"], bot=env.bot)])
    text = env.bot.edits()[-1]["text"]
    assert "Property Overview" in text or "房源概况" in text


# --- B) Back semantics (back = parent, home = global) -----------------

def test_property_quick_unit_view_back_and_home(make_app):
    """Properties quick-index unit view: ◀ 房源 (parent) + 🏠 Home."""
    env = make_app()
    env.backend.quick_properties = [
        {"unit_code": "16B", "status": "occupied", "amount": "55000.00",
         "days": 5, "open_maintenance": 0},
    ]
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "🏠 Properties", bot=env.bot)])
    # open a unit Quick View via rnv/quick-unit-view callback
    data = _inline_data(env.bot.last_send()["reply_markup"])
    quick_cb = next((d for d in data if "quv" in d), None)
    if quick_cb is not None:
        run_updates(env, [make_callback_update(
            OWNER_ID, OWNER_ID, quick_cb, message_id=env.bot.last_send()["message_id"],
            bot=env.bot)])
        labels = _labels(env.bot.edits()[-1]["reply_markup"])
        assert any("房源" in l or "Properties" in l for l in labels)
        assert any("Home" in l for l in labels)
        # Home routes to the global overview (ACTION_NAV home), never to parent
        assert any(d.replace(" ", "").split(":")[1] == "hm" for d in _inline_data(
            env.bot.edits()[-1]["reply_markup"]))


def test_rent_detail_back_rent_and_home(make_app):
    """Rent detail: ◀ 租金 | 🏠 Home."""
    from pasay_bot.keyboards import rent_detail_keyboard
    kb = rent_detail_keyboard(123, "zh")
    labels = _labels(kb)
    assert any("◀ 租金" in l for l in labels)
    assert any("🏠 Home" in l for l in labels)


def test_expense_detail_back_expense_and_home(make_app):
    """Expense detail: ◀ 支出 | 🏠 Home."""
    from pasay_bot.keyboards import expense_open_keyboard
    kb = expense_open_keyboard(7, status="approved", locale="zh")
    labels = _labels(kb)
    assert any("◀ 支出" in l for l in labels)
    assert any("🏠 Home" in l for l in labels)


# --- D) Today uses the SAME digest builder+renderer --------------------

def test_home_today_uses_digest_single_path(make_app):
    """⚠️ Today calls get_digest (same as scheduled) and renders the digest card
    (🔴现在处理 / 🟡即将处理 / ✅今日完成) - not the quick-tasks list."""
    env = make_app()
    env.backend.digest = {
        "act_now": [{"kind": "rent_overdue", "unit": "1680", "amount": "75000.00",
                     "unpaid_periods": 3, "overdue_days": 104,
                     "business_dedupe_key": "lease:1:RENT_OVERDUE"}],
        "upcoming": [{"kind": "lease_expiring", "unit": "1608", "days_to_expiry": 19,
                      "business_dedupe_key": "lease:2:LEASE_EXPIRING"}],
        "done_today": [{"kind": "rent_followup", "unit": "1680",
                        "business_dedupe_key": "committed:3:f"}],
        "hidden": {"act_now": 0, "upcoming": 0, "done_today": 0},
        "counts": {"act_now": 1, "upcoming": 1, "done_today": 1},
    }
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID,
                                           encode(ACTION_HOME_NAV, "today"), bot=env.bot)])
    text = env.bot.edits()[-1]["text"]
    assert "今日待办" in text or "Today" in text
    assert "现在处理" in text or "Act now" in text
    assert "即将处理" in text or "Upcoming" in text
    assert "今日完成" in text or "Done today" in text


# --- A) fast-path: fast ack + latency phase profile --------------------

def test_fast_nav_ack_is_empty_no_processing_toast(make_app):
    """A deterministic nav (Home) answers with an EMPTY ack (no '处理中' toast)."""
    env = make_app()
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, encode(ACTION_NAV, "home"),
                                           bot=env.bot)])
    answers = [a for a in env.bot.calls if a.get("type") == "answer_callback_query"]
    # the Home render ACKs with empty text; a '处理中' toast is not used for
    # fast deterministic navigation.
    assert all((a.get("text") or "") == "" for a in answers)


def test_latency_record_phases_breaks_down_callback(make_app):
    """The tracker records the phase profile (callback_ack/backend/render/
    telegram/total) for a callback."""
    env = make_app()
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, encode(ACTION_NAV, "home"),
                                           bot=env.bot)])
    samples = env.app.bot_data["latency"].snapshot()
    assert samples
    last = samples[-1]
    for key in ("callback_ack_ms", "backend_fetch_ms", "render_ms",
                "business_completed_ms",
                "telegram_edit_ms", "total_ms", "elapsed_ms"):
        assert key in last, f"missing phase key {key}"
    assert isinstance(last["total_ms"], (int, float))
    assert last["elapsed_ms"] >= 0


def test_latency_tracker_record_phases_unit():
    tr = LatencyTracker()
    tr.record_phases("callback", "home", callback_ack_ms=5.0, backend_fetch_ms=120.0,
                     render_ms=8.0, telegram_edit_ms=40.0,
                     business_completed_ms=150.0, total_ms=180.0)
    s = tr.snapshot()[0]
    assert s["callback_ack_ms"] == 5.0
    assert s["backend_fetch_ms"] == 120.0
    assert s["telegram_edit_ms"] == 40.0
    assert s["business_completed_ms"] == 150.0
    assert s["total_ms"] == 180.0
