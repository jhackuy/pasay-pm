"""SLICE2-RENT-006 - correction statements.

Covers: detector recognition of zh/en correction phrases (never "unknown"),
negated-value normalization before the matcher, Owner confirm + Secretary
register flows showing the corrected unit (1708, never 1608), the pending
re-report path (confirm the existing record, zero creates), and read-only
status queries answering the corrected unit.
"""
from datetime import date

from conftest import OWNER_ID, SECRETARY_ID, make_callback_update, make_text_update, run_updates
from pasay_bot.handlers.nl_bridge import (
    _normalize_correction,
    detect_rent_status_query,
    is_rent_payment_statement,
)
from pasay_bot.keyboards import decode


def add_units_1608_1708(env):
    """Unit 1608 (Bayshore, 70k, John Dela Cruz) + unit 1708 (Pasay Premier,
    65k, John Smith), both Jan-Jul confirmed so the current month is open."""
    env.backend.tenants.append({
        "id": 88, "full_name": "John Dela Cruz", "phone": None,
        "email": None, "is_active": True,
    })
    env.backend.tenants.append({
        "id": 89, "full_name": "John Smith", "phone": None,
        "email": None, "is_active": True,
    })
    env.backend.units.append({
        "id": 100, "property_id": 2, "unit_number": "1608", "floor": "16",
        "size_sqm": "40.00", "monthly_rent": "70000.00",
        "status": "occupied", "is_active": True,
    })
    env.backend.units.append({
        "id": 110, "property_id": 1, "unit_number": "1708", "floor": "17",
        "size_sqm": "40.00", "monthly_rent": "65000.00",
        "status": "occupied", "is_active": True,
    })
    env.backend.leases.append({
        "id": 100, "unit_id": 100, "tenant_id": 88, "start_date": "2026-01-01",
        "end_date": "2026-12-31", "accounting_start_date": None,
        "monthly_rent": "70000.00", "deposit": "140000.00",
        "status": "active", "due_day": 5, "notes": None,
    })
    env.backend.leases.append({
        "id": 110, "unit_id": 110, "tenant_id": 89, "start_date": "2026-01-01",
        "end_date": "2026-12-31", "accounting_start_date": None,
        "monthly_rent": "65000.00", "deposit": "130000.00",
        "status": "active", "due_day": 5, "notes": None,
    })
    for m in range(1, 8):
        env.backend.add_income(
            status="confirmed", lease_id=100, amount="70000.00",
            received_date=f"2026-{m:02d}-10",
            description=f"rent 2026-{m:02d}", income_id=1000 + m,
        )
        env.backend.add_income(
            status="confirmed", lease_id=110, amount="65000.00",
            received_date=f"2026-{m:02d}-10",
            description=f"rent 2026-{m:02d}", income_id=2000 + m,
        )


def _confirm_data(env):
    call = env.bot.sends()[-1]
    kb = call["reply_markup"]
    row = [b for row2 in kb.inline_keyboard for b in row2]
    return next(
        b.callback_data for b in row
        if decode(b.callback_data) is not None
        and decode(b.callback_data)["action"] == "cnf"
    )


def _match_request_body(env):
    for method, path, body in env.backend.calls:
        if method == "POST" and path == "/payments/match":
            return body
    return None


# --- detector --------------------------------------------------------------

def test_correction_detector_positives_and_negatives():
    positives = [
        "不是 1608，是 1708",
        "不是1608，是1708",
        "不是 1608 是 1708",
        "是 1708，不是 1608",
        "是 1708 不是 1608",
        "not 1608, it's 1708",
        "not 1608 it is 1708",
        "it's 1708, not 1608",
        "it is 1708 not 1608",
        "不是 608，是 1708",
    ]
    for text in positives:
        assert is_rent_payment_statement(text), text

    negatives = [
        "1608 交了没有",
        "1608 还欠多少",
        "不是 1608",
        "1608",
        "收租",
        "这个月谁没交",
        "1608 交了没有吗？",
        "",
    ]
    for text in negatives:
        assert not is_rent_payment_statement(text), text


def test_correction_normalizes_negated_value():
    assert _normalize_correction("不是 1608，是 1708") == "不是 1708，是 1708"
    assert _normalize_correction("不是 608，是 1708") == "不是 1708，是 1708"
    assert _normalize_correction("是 1708，不是 1608") == "是 1708，不是 1708"
    assert _normalize_correction("not 1608, it's 1708") == "not 1708, it's 1708"
    assert _normalize_correction("it's 1708 not 1608") == "it's 1708 not 1708"
    # non-correction text is never rewritten
    assert _normalize_correction("1608租金收到了") == "1608租金收到了"
    assert _normalize_correction("1608 交了没有") == "1608 交了没有"


def test_correction_short_tokens_never_rewrite_or_crash():
    """Single digits / month words are not unit corrections: no rewrite, no
    statement misroute, and the normalizer must never crash on them."""
    for text in ("不是 8，是 9", "这个月是 8 月不是 9 月"):
        assert _normalize_correction(text) == text
        assert not is_rent_payment_statement(text)
        assert detect_rent_status_query(text) is None


def test_status_query_uses_corrected_unit_token():
    q = detect_rent_status_query("不是 1608，是 1708 交了没有")
    assert q is not None and q.kind == "unit" and q.unit_token == "1708"
    q = detect_rent_status_query("不是 608，是 1708 还欠多少")
    assert q is not None and q.kind == "unit" and q.unit_token == "1708"


# --- Owner correction -> match card -> confirm -----------------------------

def test_owner_correction_matches_1708_and_confirms(make_app):
    env = make_app()
    add_units_1608_1708(env)
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "不是 1608，是 1708", bot=env.bot)])

    assert env.backend.count_calls("POST", "/payments/match") == 1
    assert _match_request_body(env) == {"text": "不是 1708，是 1708"}

    send = env.bot.last_send()
    text = send["text"]
    assert "Pasay Premier Residences" in text
    assert "1708" in text
    assert "1608" not in text
    assert "Bayshore" not in text
    assert "8月租金" in text
    assert "应收：₱65,000" in text
    assert "✓ 金额一致" in text

    conv = env.store.get_conversation(OWNER_ID, OWNER_ID)
    assert conv["payload"]["lease_id"] == 110
    assert conv["payload"]["unit_number"] == "1708"
    assert conv["payload"]["amount"] == "65000.00"

    data = _confirm_data(env)
    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, data, message_id=10, update_id=2, bot=env.bot)],
    )
    inc = env.backend.incomes[-1]
    assert inc["lease_id"] == 110
    assert inc["status"] == "confirmed"
    assert inc["description"] == f"rent {date.today().strftime('%Y-%m')}"
    last = env.bot.last_edit()
    assert "1708" in last["text"]
    assert "1608" not in last["text"]


# --- Secretary English correction -> pending -> Owner card -----------------

def test_secretary_correction_registers_pending_for_1708(make_app):
    env = make_app()
    add_units_1608_1708(env)
    run_updates(
        env,
        [make_text_update(SECRETARY_ID, SECRETARY_ID, "not 1608, it's 1708", bot=env.bot)],
    )
    assert _match_request_body(env) == {"text": "not 1708, it's 1708"}
    assert env.backend.count_calls("POST", "/incomes") == 1
    inc = env.backend.incomes[-1]
    assert inc["lease_id"] == 110
    assert inc["status"] == "pending"
    assert inc["amount"] == "65000.00"

    sends = env.bot.sends()
    secretary_reply, owner_card = sends[-2], sends[-1]
    assert "1708" in secretary_reply["text"]
    assert "1608" not in secretary_reply["text"]
    assert owner_card["chat_id"] == OWNER_ID
    assert "1708" in owner_card["text"]
    assert "1608" not in owner_card["text"]
    assert "秘书登记了一笔租金" in owner_card["text"]


# --- correction + pending partial -> confirm existing, zero creates --------

def test_correction_with_pending_partial_confirms_existing_no_create(make_app):
    env = make_app()
    add_units_1608_1708(env)
    month = date.today().strftime("%Y-%m")
    env.backend.add_income(
        status="pending", lease_id=110, amount="40000.00",
        received_date=date.today().isoformat(),
        description=f"rent {month}", income_id=42,
    )
    run_updates(
        env,
        [make_text_update(OWNER_ID, OWNER_ID, "不是 1608，是 1708 付了 40000", bot=env.bot)],
    )
    assert _match_request_body(env) == {"text": "不是 1708，是 1708 付了 40000"}
    send = env.bot.last_send()
    assert "已登记待确认" in send["text"]
    assert "1708" in send["text"]
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
    assert env.backend._get_income(42)["status"] == "confirmed"


# --- read-only status query honors the correction --------------------------

def test_status_query_correction_answers_1708_read_only(make_app):
    env = make_app()
    add_units_1608_1708(env)
    run_updates(
        env,
        [make_text_update(OWNER_ID, OWNER_ID, "不是 1608，是 1708 交了没有", bot=env.bot)],
    )
    text = env.bot.last_send()["text"]
    assert "Unit 1708" in text
    assert "John Smith" in text
    assert "Unit 1608" not in text
    assert "John Dela Cruz" not in text
    assert all(method == "GET" for method, _path, _body in env.backend.calls)
    assert env.backend.count_calls("POST", "/payments/match") == 0
