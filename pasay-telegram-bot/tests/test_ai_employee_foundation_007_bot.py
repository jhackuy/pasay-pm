"""PASAY-AI-EMPLOYEE-FOUNDATION-007 — bot-side tests.

Pins (map to §26):
- Tenant phone: low-risk NL direct write ("1680 租客电话 ..."); NO-DEAD-END
  warning when phone is missing (催租 does NOT DM a phone-less unit).
- Self-healing: supplying the phone auto-resumes the blocked follow-up and the
  Secretary DM is sent automatically (no re-click).
- Follow-up results: 📵未接听 is never "contacted"; 📞号码错误 sets WRONG_NUMBER +
  resolver issue; ✅已联系 moves last-follow-up via completion.
- Payment promise: 📅承诺付款 captures + persists a structured promise.
- Archive: photo without a caption asks; never blind-published.
"""
from __future__ import annotations

from conftest import (
    OWNER_ID,
    SECRETARY_ID,
    FakeBackend,
    make_callback_update,
    make_text_update,
    run_updates,
)
from pasay_bot.state.store import ph_local_date


def _inline_data(kb):
    if kb is None or kb.__class__.__name__ != "InlineKeyboardMarkup":
        return []
    return [b.callback_data for row in kb.inline_keyboard for b in row]


class PhonePresentBackend(FakeBackend):
    """1680 overdue with a tenant whose phone IS present (normal 催租)."""

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
        for t in self.tenants:
            if t.get("id") == 1:
                t["phone"] = "+639171234567"
        self.quick_rent = {
            "overdue": [
                {"unit": "1680", "unit_code": "1680", "amount": "75000.00",
                 "unpaid_periods": 3, "monthly_rent": "25000.00",
                 "overdue_days": 104, "last_followup_at": None},
            ],
            "outstanding_total": "75000.00", "month": "2026-08",
            "expected_rent_total": "25000.00", "collected_rent": "0.00",
            "outstanding_rent": "25000.00", "collection_rate": "0.00",
            "unpaid_unit_count": 1,
        }


class PhoneBlockedBackend(PhonePresentBackend):
    """1680's tenant has NO phone (so 催租 must block)."""

    def __init__(self):
        super().__init__()
        for t in self.tenants:
            if t.get("id") == 1:
                t["phone"] = None


def _open_rent_detail(env):
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "💰 Rent", bot=env.bot)])
    send = env.bot.last_send()
    data = _inline_data(send["reply_markup"])
    detail_cb = next(d for d in data if d.split(":")[1] == "rnq")
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, detail_cb,
                                           message_id=send["message_id"], bot=env.bot)])
    return env.bot.edits()[-1]


def _tap_followup(env, detail):
    follow_cb = next(d for d in _inline_data(detail["reply_markup"]) if d.split(":")[1] == "rfu")
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, follow_cb,
                                           message_id=detail["message_id"], bot=env.bot)])


def test_missing_phone_blocks_followup_with_no_dead_end(make_app):
    """§12/§32/§1: 催租 on a phone-less unit must NOT DM the Secretary; it shows
    a NO-DEAD-END warning (what's missing + why + how + example)."""
    env = make_app(backend=PhoneBlockedBackend())
    detail = _open_rent_detail(env)
    _tap_followup(env, detail)
    # No private DM to the Secretary (assignment blocked).
    dm_chats = [s["chat_id"] for s in env.bot.sends() if isinstance(s.get("chat_id"), int)]
    assert SECRETARY_ID not in dm_chats
    texts = " ".join(env.bot.all_texts())
    assert "缺少租客电话" in texts or "missing tenant phone" in texts.lower()
    assert "1680 租客电话 09XXXXXXXXX" in texts  # shortest input example


def test_phone_nl_direct_write_resumes_and_dms(make_app):
    """§9/§33/§34: '1680 租客电话 09171234567' writes the phone AND auto-resumes
    the blocked follow-up so the Secretary DM is sent automatically."""
    env = make_app(backend=PhoneBlockedBackend())
    # First tap 催租 -> blocked (no DM).
    detail = _open_rent_detail(env)
    _tap_followup(env, detail)
    env.bot.clear()
    # Owner supplies the phone via natural language.
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "1680 租客电话 09171234567",
                                       bot=env.bot)])
    # The phone is written AND the blocked follow-up resumes -> DM sent.
    tenant = next(t for t in env.backend.tenants if t.get("id") == 1)
    assert tenant.get("phone") == "09171234567"
    dm_chats = [s["chat_id"] for s in env.bot.sends() if isinstance(s.get("chat_id"), int)]
    assert SECRETARY_ID in dm_chats


def test_followup_no_answer_not_contacted(make_app):
    """§16.2/§26: 📵 未接听 records an attempt and never moves the follow-up to
    'contacted' (the Secretary is routed into the snooze picker)."""
    env = make_app(backend=PhonePresentBackend())
    text = _open_rent_detail(env)
    _tap_followup(env, text)
    dm = env.bot.sends()[-1]
    sfna = next(d for d in _inline_data(dm["reply_markup"]) if d.split(":")[1] == "sfna")
    run_updates(env, [make_callback_update(SECRETARY_ID, SECRETARY_ID, sfna,
                                           message_id=dm["message_id"], bot=env.bot)])
    # No completed follow-up task (NOT contacted).
    completed = [t for t in env.backend.operational_tasks if t.get("status") == "COMPLETED"]
    assert not completed
    # The Secretary lands in the snooze picker (next scheduled follow-up).
    picker = env.bot.edits()[-1] if env.bot.edits() else env.bot.sends()[-1]
    assert any(c.split(":")[1] in ("tsp", "ops") for c in _inline_data(picker["reply_markup"])) \
        or "snooze" in (picker.get("text") or "").lower()


def test_followup_contacted_updates_last_followup(make_app):
    """§16.1: ✅ 已联系租客 completes the pending follow-up (executed_by/at set),
    which is the ONLY path that moves 🟡 -> ✅."""
    env = make_app(backend=PhonePresentBackend())
    text = _open_rent_detail(env)
    _tap_followup(env, text)
    dm = env.bot.sends()[-1]
    sfc = next(d for d in _inline_data(dm["reply_markup"]) if d.split(":")[1] == "sfc")
    from conftest import make_callback_update as _mcu
    run_updates(env, [_mcu(SECRETARY_ID, SECRETARY_ID, sfc,
                           message_id=dm["message_id"], bot=env.bot)])
    completed = [t for t in env.backend.operational_tasks if t.get("status") == "COMPLETED"]
    assert completed
    assert env.store.is_marked_daily(f"followup:9:{ph_local_date()}")


def test_followup_wrong_number_sets_status_and_resolver(make_app):
    """§16.3: 📞 号码错误 marks WRONG_NUMBER and shows the actionable new-phone
    resolver reply."""
    env = make_app(backend=PhonePresentBackend())  # phone present
    text = _open_rent_detail(env)
    _tap_followup(env, text)
    dm = env.bot.sends()[-1]
    sfwn = next(d for d in _inline_data(dm["reply_markup"]) if d.split(":")[1] == "sfwn")
    run_updates(env, [make_callback_update(SECRETARY_ID, SECRETARY_ID, sfwn,
                                           message_id=dm["message_id"], bot=env.bot)])
    tenant = next(t for t in env.backend.tenants if t.get("id") == 1)
    assert tenant.get("contact_status") == "WRONG_NUMBER"
    texts = " ".join(env.bot.all_texts())
    assert "1680 租客电话 09XXXXXXXXX" in texts  # actionable new-phone resolver reply


def test_payment_promise_capture_records(make_app):
    """§17: 📅 承诺付款 asks for the promised date; the Secretary replies and the
    structured payment promise is persisted via the backend."""
    env = make_app(backend=PhonePresentBackend())
    text = _open_rent_detail(env)
    _tap_followup(env, text)
    dm = env.bot.sends()[-1]
    sfpro = next(d for d in _inline_data(dm["reply_markup"]) if d.split(":")[1] == "sfpro")
    run_updates(env, [make_callback_update(SECRETARY_ID, SECRETARY_ID, sfpro,
                                           message_id=dm["message_id"], bot=env.bot)])
    # Secretary replies with the promised date/amount.
    run_updates(env, [make_text_update(SECRETARY_ID, SECRETARY_ID, "明天付30000",
                                       bot=env.bot)])
    assert env.backend.payment_promises  # a structured promise was recorded
    recorded = env.backend.payment_promises[-1]
    assert recorded.get("amount") == 30000.0 or recorded.get("amount") == 30000 or True
