"""SLICE2-RENT-005 — partial payment & remaining balance (bot flow).

Covers the acceptance scenarios: first partial payment (40k of 100k),
multi-payment accumulation (40k + 30k + 30k), remaining-balance queries
("1608 还欠多少 / 交了多少 / 交清了吗"), paid-off state, request-level
idempotency, pending re-report (confirm the existing record, never a second
create), overpayment protection (explain, zero writes), invalid amounts, and
the read-only query path.
"""
from datetime import date

from conftest import OWNER_ID, SECRETARY_ID, make_callback_update, make_text_update, run_updates
from pasay_bot.handlers.nl_bridge import (
    detect_rent_status_query,
    is_rent_payment_statement,
)
from pasay_bot.keyboards import decode


def add_unit_1608_100k(env):
    """Unit 1608, monthly rent 100k, Jan–Jul fully paid; the current month is
    the single outstanding bill (mirrors the task's ₱100,000 example)."""
    env.backend.tenants.append({
        "id": 88, "full_name": "John Dela Cruz", "phone": None,
        "email": None, "is_active": True,
    })
    env.backend.units.append({
        "id": 100, "property_id": 2, "unit_number": "1608", "floor": "16",
        "size_sqm": "40.00", "monthly_rent": "100000.00",
        "status": "occupied", "is_active": True,
    })
    env.backend.leases.append({
        "id": 100, "unit_id": 100, "tenant_id": 88, "start_date": "2026-01-01",
        "end_date": "2026-12-31", "accounting_start_date": None,
        "monthly_rent": "100000.00", "deposit": "200000.00",
        "status": "active", "due_day": 5, "notes": None,
    })
    for m in range(1, 8):
        env.backend.add_income(
            status="confirmed", lease_id=100, amount="100000.00",
            received_date=f"2026-{m:02d}-10",
            description=f"rent 2026-{m:02d}", income_id=1000 + m,
        )


def _current_month():
    return date.today().strftime("%Y-%m")


def _confirm_data(env):
    call = env.bot.sends()[-1]
    kb = call["reply_markup"]
    row = [b for row2 in kb.inline_keyboard for b in row2]
    return next(
        b.callback_data for b in row
        if decode(b.callback_data) is not None
        and decode(b.callback_data)["action"] == "cnf"
    )


def _month_incomes(env):
    month = _current_month()
    return [
        inc for inc in env.backend.incomes
        if inc["lease_id"] == 100 and f"rent {month}" in (inc.get("description") or "")
    ]


def _confirm(env, update_id=2):
    data = _confirm_data(env)
    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, data, message_id=10,
                              update_id=update_id, bot=env.bot)],
    )


# --- detector --------------------------------------------------------------

def test_partial_statement_and_query_detectors():
    assert is_rent_payment_statement("1608 付了 40000")
    assert is_rent_payment_statement("1608 又付了 30000")
    assert is_rent_payment_statement("1608 付了 0")
    assert is_rent_payment_statement("1608 付了 -5000")
    assert not is_rent_payment_statement("1608 付了吗")
    assert not is_rent_payment_statement("1608 付了没有")

    q = detect_rent_status_query("1608 还欠多少")
    assert q is not None and q.kind == "unit" and q.unit_token == "1608"
    q = detect_rent_status_query("1608 交了多少")
    assert q is not None and q.kind == "unit" and q.unit_token == "1608"
    q = detect_rent_status_query("1608 交清了吗")
    assert q is not None and q.kind == "unit" and q.unit_token == "1608"


# --- first partial payment -------------------------------------------------

def test_partial_first_payment_match_card_and_confirm(make_app):
    env = make_app()
    add_unit_1608_100k(env)
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "1608 付了 40000", bot=env.bot)])

    send = env.bot.last_send()
    text = send["text"]
    assert "部分付款" in text
    assert "本月应付：₱100,000" in text
    assert "本次收到：₱40,000" in text
    assert "累计已付：₱40,000" in text
    assert "剩余：₱60,000" in text
    labels = [b.text for row in send["reply_markup"].inline_keyboard for b in row]
    assert "✓ 确认入账" in labels
    for banned in ("lease", "income", "pending", "confirmed", "1608-"):
        assert banned not in text.lower(), banned

    payload = env.store.get_conversation(OWNER_ID, OWNER_ID)["payload"]
    assert payload["amount"] == "40000.00"
    assert payload["due_amount"] == "100000.00"
    assert payload["paid_amount"] == "0.00"
    assert payload["remaining_balance"] == "60000.00"

    _confirm(env)
    month_incs = _month_incomes(env)
    assert len(month_incs) == 1
    assert month_incs[0]["amount"] == "40000.00"
    assert month_incs[0]["status"] == "confirmed"

    last = env.bot.last_edit()
    assert "已记录 1608 租金付款 ₱40,000" in last["text"]
    assert "本月应付：₱100,000" in last["text"]
    assert "累计已付：₱40,000" in last["text"]
    assert "剩余：₱60,000" in last["text"]
    assert "本月已付清" not in last["text"]


# --- accumulation to paid off ----------------------------------------------

def test_partial_multiple_payments_accumulate_to_paid_off(make_app):
    env = make_app()
    add_unit_1608_100k(env)

    for update_id, statement, paid_total, remaining in (
        (2, "1608 付了 40000", "₱40,000", "₱60,000"),
        (3, "1608 又付了 30000", "₱70,000", "₱30,000"),
        (4, "1608 又付了 30000", "₱100,000", "₱0"),
    ):
        run_updates(env, [
            make_text_update(OWNER_ID, OWNER_ID, statement,
                             message_id=update_id, update_id=update_id, bot=env.bot)
        ])
        card = env.bot.last_send()["text"]
        assert "部分付款" in card
        assert f"累计已付：{paid_total}" in card
        assert f"剩余：{remaining}" in card
        _confirm(env, update_id=update_id)

    month_incs = _month_incomes(env)
    assert [inc["amount"] for inc in month_incs] == ["40000.00", "30000.00", "30000.00"]
    assert all(inc["status"] == "confirmed" for inc in month_incs)

    final = env.bot.last_edit()
    assert "累计已付：₱100,000" in final["text"]
    assert "剩余：₱0" in final["text"]
    assert "本月已付清" in final["text"]


def test_partial_confirm_double_click_is_idempotent(make_app):
    """Same confirm card tapped twice -> exactly one income row."""
    env = make_app()
    add_unit_1608_100k(env)
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "1608 付了 40000", bot=env.bot)])
    data = _confirm_data(env)
    run_updates(
        env,
        [
            make_callback_update(OWNER_ID, OWNER_ID, data, message_id=10, update_id=2, bot=env.bot),
            make_callback_update(OWNER_ID, OWNER_ID, data, message_id=10, update_id=3, bot=env.bot),
        ],
    )
    assert env.backend.count_calls("POST", "/incomes") == 1
    assert len(_month_incomes(env)) == 1


def test_partial_pending_reported_again_confirms_existing_no_create(make_app):
    """While a partial is still pending, re-reporting the same amount surfaces
    the pending record (confirm it) instead of creating a second one."""
    env = make_app()
    add_unit_1608_100k(env)
    month = _current_month()
    env.backend.add_income(
        status="pending", lease_id=100, amount="40000.00",
        received_date=date.today().isoformat(),
        description=f"rent {month}", income_id=42,
    )
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "1608 付了 40000", bot=env.bot)])
    send = env.bot.last_send()
    assert "已登记待确认" in send["text"]
    row = [b for row2 in send["reply_markup"].inline_keyboard for b in row2]
    parsed = decode(row[0].callback_data)
    assert parsed["action"] == "cnf" and parsed["entity"] == "inc" and parsed["ref"] == "42"

    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, row[0].callback_data,
                              message_id=10, update_id=2, bot=env.bot)],
    )
    assert env.backend.count_calls("POST", "/incomes") == 0
    assert env.backend.count_calls("POST", "/incomes/42/confirm") == 1
    inc = env.backend._get_income(42)
    assert inc["status"] == "confirmed"


# --- remaining-balance queries ---------------------------------------------

def _seed_partial(env, amounts):
    month = _current_month()
    for i, amount in enumerate(amounts):
        env.backend.add_income(
            status="confirmed", lease_id=100, amount=amount,
            received_date=date.today().isoformat(),
            description=f"rent {month}", income_id=2000 + i,
        )


def test_partial_remaining_queries_read_only(make_app):
    env = make_app()
    add_unit_1608_100k(env)
    _seed_partial(env, ["40000.00"])
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "1608 还欠多少", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "部分付款" in text
    assert "本月应付：₱100,000" in text
    assert "已付：₱40,000" in text
    assert "还欠：₱60,000" in text

    run_updates(env, [
        make_text_update(OWNER_ID, OWNER_ID, "1608 交了多少",
                         message_id=2, update_id=2, bot=env.bot)
    ])
    text = env.bot.last_send()["text"]
    assert "已付：₱40,000" in text
    assert "还欠：₱60,000" in text

    run_updates(env, [
        make_text_update(OWNER_ID, OWNER_ID, "1608 交清了吗",
                         message_id=3, update_id=3, bot=env.bot)
    ])
    text = env.bot.last_send()["text"]
    assert "部分付款" in text
    assert "已交清" not in text


def test_partial_paid_off_query(make_app):
    env = make_app()
    add_unit_1608_100k(env)
    _seed_partial(env, ["40000.00", "30000.00", "30000.00"])
    run_updates(env, [
        make_text_update(OWNER_ID, OWNER_ID, "1608 交清了吗", bot=env.bot),
        make_text_update(OWNER_ID, OWNER_ID, "1608 还欠多少",
                         message_id=2, update_id=2, bot=env.bot),
    ])
    texts = "".join(env.bot.all_texts())
    assert "已交清" in texts
    assert "还欠：" not in texts
    assert "部分付款" not in texts
    assert all(method == "GET" for method, _p, _b in env.backend.calls)
    assert env.backend.count_calls("POST", "/payments/match") == 0
    assert env.backend.count_calls("POST", "/incomes") == 0


# --- overpayment / invalid amount / duplicate ------------------------------

def test_partial_overpayment_explained_zero_writes(make_app):
    env = make_app()
    add_unit_1608_100k(env)
    _seed_partial(env, ["40000.00", "30000.00"])
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "1608 又付了 50000", bot=env.bot)])
    send = env.bot.last_send()
    text = send["text"]
    assert "超额付款暂不支持" in text
    assert "剩余 ₱30,000" in text
    assert "₱50,000" in text
    assert "预付" in text or "下月" in text
    assert send["reply_markup"] is None
    assert env.backend.count_calls("POST", "/incomes") == 0
    assert len(_month_incomes(env)) == 2  # unchanged


def test_partial_invalid_amount_zero_writes(make_app):
    env = make_app()
    add_unit_1608_100k(env)
    run_updates(env, [
        make_text_update(OWNER_ID, OWNER_ID, "1608 付了 0", bot=env.bot),
        make_text_update(OWNER_ID, OWNER_ID, "1608 付了 -5000",
                         message_id=2, update_id=2, bot=env.bot),
    ])
    texts = "".join(env.bot.all_texts())
    assert "金额无效" in texts
    assert env.backend.count_calls("POST", "/incomes") == 0
    assert env.backend.count_calls("POST", "/payments/match") == 2
    assert _month_incomes(env) == []


def test_partial_full_payment_after_settled_is_duplicate_no_write(make_app):
    env = make_app()
    add_unit_1608_100k(env)
    _seed_partial(env, ["100000.00"])
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "1608 付了 100000", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "已经入账" in text
    assert env.backend.count_calls("POST", "/incomes") == 0


def test_partial_secretary_register_uses_partial_card(make_app):
    """Secretary partial statement registers ONE pending income and hands the
    Owner a partial confirm card (due / received / cumulative / remaining)."""
    env = make_app()
    add_unit_1608_100k(env)
    run_updates(env, [
        make_text_update(SECRETARY_ID, SECRETARY_ID, "1608 付了 40000", bot=env.bot)
    ])
    assert env.backend.count_calls("POST", "/incomes") == 1
    assert env.backend.incomes[-1]["status"] == "pending"
    assert env.backend.incomes[-1]["amount"] == "40000.00"

    sends = env.bot.sends()
    secretary_reply = sends[-2]
    assert secretary_reply["chat_id"] == SECRETARY_ID
    assert "₱40,000" in secretary_reply["text"]

    owner_card = sends[-1]
    assert owner_card["chat_id"] == OWNER_ID
    text = owner_card["text"]
    assert "秘书登记了一笔租金" in text
    assert "部分付款" in text
    assert "本月应付：₱100,000" in text
    assert "本次收到：₱40,000" in text
    assert "累计已付：₱40,000" in text
    assert "剩余：₱60,000" in text
