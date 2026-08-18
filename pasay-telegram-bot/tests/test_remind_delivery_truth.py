"""WINDOWS-RUNTIME-REBOOT-RECOVERY-002, PHASE C — Remind-Owner delivery-truth tests.

Pins the corrected semantics:
- A dialog shows "Reminder sent to Owner" ONLY after Telegram confirms a
  delivery (a message_id is returned by send_message).
- Delivery truth (target / destination / sent_at / message_id) is persisted in
  SQLite (``reminder_deliveries``) and survives a process restart.
- The same-day gate is the PERSISTED delivery record, never the idempotency key
  (the root bug was a nonce-less Remind button collapsing the idempotency key to
  ``ik:rmo:{expense}:0`` and then replaying a fake "sent" on later days).
- A failed delivery persists nothing and does NOT consume the daily limit.
- ❌ Expense business state stays "approved" (Waiting-for-payment) after a reminder.

Covered (requirement F):
  1 successful delivery -> success response + delivery persisted
  2 failed delivery -> failure response + NOT persisted
  3 exception from Telegram client -> never reports success
  4 second click same day -> no second message ("already reminded")
  5 next day -> allowed again
  6 wrong/missing Owner destination -> explicit failure, no persistence
  7 Expense business state unchanged after reminder
  8 persisted state survives process restart
"""
from __future__ import annotations

from unittest import mock

from conftest import OWNER_ID, make_callback_update, make_text_update, run_updates
from pasay_bot.state.store import StateStore, ph_local_date

from test_zero_learning_004 import ZLBackend, _inline_data


def _open_expense_detail(env, expense_id: int):
    """Drive 💸 Expense -> open E<expense_id> detail; return the detail edit."""
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "💸 Expense", bot=env.bot)])
    send = env.bot.last_send()
    open_cb = next(d for d in _inline_data(send["reply_markup"]) if d.split(":")[1] == "exo")
    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, open_cb,
                              message_id=send["message_id"], bot=env.bot)],
    )
    return env.bot.edits()[-1]


def _remind_cb(detail):
    return next(d for d in _inline_data(detail["reply_markup"]) if d.split(":")[1] == "rmo")


def _has_substring(env, needle):
    return any(needle in (c.get("text") or "") for c in env.bot.calls)


# 1) successful delivery -> success response + delivery persisted
def test_remind_success_persists_delivery_truth(make_app):
    env = make_app(backend=ZLBackend())
    detail = _open_expense_detail(env, 7)
    remind_cb = _remind_cb(detail)
    before = len(env.bot.sends())
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, remind_cb,
                                           message_id=detail["message_id"], bot=env.bot)])
    assert len(env.bot.sends()) == before + 1
    dm = env.bot.sends()[-1]
    assert dm["chat_id"] == OWNER_ID
    assert dm["message_id"]  # Telegram-returned message id (delivery proof)
    # the card flipped to ✅ Reminded on the tapped message
    flipped = [b.text for row in env.bot.edits()[-1]["reply_markup"].inline_keyboard for b in row]
    assert any("Reminded" in l or "已提醒" in l for l in flipped)
    rec = env.store.get_reminder_delivery(7, ph_local_date())
    assert rec is not None
    assert rec["destination"] == str(OWNER_ID)
    assert str(rec["message_id"]) == str(dm["message_id"])
    assert rec["sent_at"]


# 2) failed recipient resolution -> failure response + NOT persisted
def test_remind_failure_resolution_not_persisted(make_app):
    env = make_app(backend=ZLBackend())
    env.backend.fail_status["/operations/remind-owner-target"] = 500
    detail = _open_expense_detail(env, 7)
    remind_cb = _remind_cb(detail)
    before = len(env.bot.sends())
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, remind_cb,
                                           message_id=detail["message_id"], bot=env.bot)])
    assert len(env.bot.sends()) == before  # no DM
    assert env.store.get_reminder_delivery(7, ph_local_date()) is None
    assert not env.store.is_marked_daily(f"remind_owner:7:{ph_local_date()}")
    assert _has_substring(env, "failed") or _has_substring(env, "失败")


# 3) exception from the Telegram client -> never reports success, not persisted
def test_remind_telegram_exception_never_reports_success(make_app):
    env = make_app(backend=ZLBackend())
    detail = _open_expense_detail(env, 7)
    remind_cb = _remind_cb(detail)
    bot = env.bot
    original = bot.send_message

    async def wrapper(chat_id, text=None, parse_mode=None, reply_markup=None, **kw):
        raise RuntimeError("Telegram send failed (simulated)")

    bot.send_message = wrapper
    try:
        run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, remind_cb,
                                               message_id=detail["message_id"], bot=env.bot)])
    finally:
        bot.send_message = original
    assert _has_substring(env, "failed") or _has_substring(env, "失败")
    assert env.store.get_reminder_delivery(7, ph_local_date()) is None
    assert not env.store.is_marked_daily(f"remind_owner:7:{ph_local_date()}")


def _remind_dm_count(env):
    """Count reminder DMs (the Owner-facing Payment-Reminder message)."""
    return sum(1 for c in env.bot.sends() if "Payment Reminder" in (c.get("text") or ""))


# 4) second click same day (same rendered button) -> no second message
def test_remind_second_click_same_day_no_second_send(make_app):
    env = make_app(backend=ZLBackend())
    detail = _open_expense_detail(env, 7)
    remind_cb = _remind_cb(detail)
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, remind_cb,
                                           message_id=detail["message_id"], bot=env.bot)])
    assert _remind_dm_count(env) == 1
    assert env.store.get_reminder_delivery(7, ph_local_date()) is not None
    # re-tap the SAME rendered button => daily gate: no second reminder DM
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, remind_cb,
                                           message_id=detail["message_id"], bot=env.bot)])
    assert _remind_dm_count(env) == 1                       # still exactly one DM
    assert _has_substring(env, "Already reminded") or _has_substring(env, "今日已提醒")


# 5) next PH day -> reminder allowed again (fresh real send)
def test_remind_next_day_allowed_again(make_app):
    env = make_app(backend=ZLBackend())
    detail = _open_expense_detail(env, 7)
    remind_cb = _remind_cb(detail)
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, remind_cb,
                                           message_id=detail["message_id"], bot=env.bot)])
    day1 = ph_local_date()
    assert env.store.get_reminder_delivery(7, day1) is not None
    assert _remind_dm_count(env) == 1

    day2 = "2099-12-31"
    with mock.patch("pasay_bot.state.store.ph_local_date", return_value=day2):
        run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, remind_cb,
                                               message_id=detail["message_id"], bot=env.bot)])
    # a brand-new day allows a real reminder again; day1's record stays intact.
    assert env.store.get_reminder_delivery(7, day2) is not None
    assert env.store.get_reminder_delivery(7, day1) is not None
    assert _remind_dm_count(env) == 2                       # one per day


# 6) wrong/missing Owner destination -> explicit failure, no persistence
def test_remind_missing_owner_destination_fails(make_app):
    env = make_app(backend=ZLBackend())
    env.backend.fail_status["/operations/remind-owner-target"] = 404
    detail = _open_expense_detail(env, 7)
    remind_cb = _remind_cb(detail)
    before = len(env.bot.sends())
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, remind_cb,
                                           message_id=detail["message_id"], bot=env.bot)])
    assert len(env.bot.sends()) == before
    assert env.store.get_reminder_delivery(7, ph_local_date()) is None
    assert not env.store.is_marked_daily(f"remind_owner:7:{ph_local_date()}")
    assert _has_substring(env, "failed") or _has_substring(env, "失败")


# 7) Expense business state unchanged after a reminder
def test_remind_does_not_change_expense_business_state(make_app):
    env = make_app(backend=ZLBackend())
    states = {e["id"]: e["status"] for e in env.backend.expenses}
    assert states[7] == "approved"
    detail = _open_expense_detail(env, 7)
    remind_cb = _remind_cb(detail)
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, remind_cb,
                                           message_id=detail["message_id"], bot=env.bot)])
    states_after = {e["id"]: e["status"] for e in env.backend.expenses}
    assert states_after[7] == "approved"
    assert env.store.get_reminder_delivery(7, ph_local_date()) is not None


# 8) persisted delivery survives a process restart
def test_remind_persisted_delivery_survives_restart(make_app):
    env = make_app(backend=ZLBackend())
    detail = _open_expense_detail(env, 7)
    remind_cb = _remind_cb(detail)
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, remind_cb,
                                           message_id=detail["message_id"], bot=env.bot)])
    todays = ph_local_date()
    assert env.store.get_reminder_delivery(7, todays) is not None

    db_path = env.store.db_path
    env.store.close()
    fresh = StateStore(db_path)
    try:
        assert fresh.get_reminder_delivery(7, todays) is not None
        assert fresh.get_reminder_delivery(7, todays)["message_id"]
    finally:
        fresh.close()
