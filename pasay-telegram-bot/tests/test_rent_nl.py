"""V1.3 Slice 2 — Entry B natural-language rent collection (exact payment).

Covers: NL statement detection, match request, confirm card render, Owner
confirm (idempotent, message mutation, single financial record), duplicate
friendly message (no second write), Secretary role hiding the confirm
button, ambiguous / none / pending flows.
"""
from datetime import date

from conftest import OWNER_ID, SECRETARY_ID, make_callback_update, make_text_update, run_updates
from pasay_bot.handlers.nl_bridge import is_rent_payment_statement
from pasay_bot.keyboards import decode


def add_unit_1608(env):
    """A unit/lease matching the acceptance examples, with Jan–Jul already
    paid so August is the single outstanding bill."""
    env.backend.units.append({
        "id": 100, "property_id": 2, "unit_number": "1608", "floor": "16",
        "size_sqm": "40.00", "monthly_rent": "70000.00",
        "status": "occupied", "is_active": True,
    })
    env.backend.leases.append({
        "id": 100, "unit_id": 100, "tenant_id": 2, "start_date": "2026-01-01",
        "end_date": "2026-12-31", "accounting_start_date": None,
        "monthly_rent": "70000.00", "deposit": "140000.00",
        "status": "active", "due_day": 5, "notes": None,
    })
    for m in range(1, 8):
        env.backend.add_income(
            status="confirmed", lease_id=100, amount="70000.00",
            received_date=f"2026-{m:02d}-10",
            description=f"rent 2026-{m:02d}", income_id=1000 + m,
        )


def _match_confirm_data(env):
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


# --- statement detector -----------------------------------------------------

def test_statement_detector():
    assert is_rent_payment_statement("1608租金收到了")
    assert is_rent_payment_statement("John的70000到了")
    assert is_rent_payment_statement("昨天收到1608房租")
    assert is_rent_payment_statement("rent received for 1608")
    assert not is_rent_payment_statement("收租")
    assert not is_rent_payment_statement("租金收到了吗？")
    assert not is_rent_payment_statement("这个月谁没交租")
    assert not is_rent_payment_statement("1608")
    assert not is_rent_payment_statement("")


# --- Entry B happy path -----------------------------------------------------

def test_nl_statement_renders_match_card(make_app):
    env = make_app()
    add_unit_1608(env)
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "1608租金收到了", bot=env.bot)])

    assert env.backend.count_calls("POST", "/payments/match") == 1
    assert _match_request_body(env) == {"text": "1608租金收到了"}

    send = env.bot.sends()[-1]
    text = send["text"]
    assert "找到对应租金" in text
    assert "Bayshore" in text and "1608" in text
    assert "8月租金" in text
    assert "应收：₱70,000" in text
    assert "✓ 金额一致" in text
    assert "✓ 唯一未结账单" in text
    assert "✓ 未发现重复" in text

    labels = [b.text for row in send["reply_markup"].inline_keyboard for b in row]
    assert "✓ 确认入账" in labels

    conv = env.store.get_conversation(OWNER_ID, OWNER_ID)
    assert conv["state"] == "rent_confirm"
    assert conv["payload"]["period"] == date.today().strftime("%Y-%m")
    assert conv["payload"]["received_date"] == date.today().isoformat()
    assert conv["payload"]["amount"] == "70000.00"


def test_nl_tenant_amount_phrase(make_app):
    env = make_app()
    env.backend.rent_match_response = {
        "received_date": date.today().isoformat(),
        "candidates": [{
            "kind": "open", "confidence": "high",
            "lease_id": 100, "unit_id": 100, "unit_number": "1608",
            "property_id": 2, "property_name": "Bayshore & Tower",
            "tenant_id": 2, "tenant_name": "John Dela Cruz",
            "period": date.today().strftime("%Y-%m"), "due_date": None,
            "amount": "70000.00", "open_count": 1, "remaining_balance": "0.00",
            "income_id": None, "income_status": None,
        }],
    }
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "John的70000到了", bot=env.bot)])
    assert _match_request_body(env) == {"text": "John的70000到了"}
    assert "找到对应租金" in env.bot.sends()[-1]["text"]


def test_nl_confirm_mutates_card_once(make_app):
    """★ Entry B: [✓ 确认入账] -> one income row, original card mutated in
    place (no '成功'/'已处理' message spam), balance shown."""
    env = make_app()
    add_unit_1608(env)
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "1608租金收到了", bot=env.bot)])
    data = _match_confirm_data(env)

    # original card edited in place; the confirm mutation sends nothing new
    sends_before = len(env.bot.sends())
    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, data, message_id=10, update_id=2, bot=env.bot)],
    )
    assert len(env.bot.sends()) == sends_before

    assert env.backend.count_calls("POST", "/incomes") == 1
    income = env.backend.incomes[-1]
    assert env.backend.count_calls("POST", f"/incomes/{income['id']}/confirm") == 1
    assert income["status"] == "confirmed"
    assert income["description"] == f"rent {date.today().strftime('%Y-%m')}"

    last = env.bot.edits()[-1]
    assert last["message_id"] == 10
    assert "租金已入账" in last["text"]
    assert "Bayshore" in last["text"] and "1608" in last["text"]
    assert "8月租金" in last["text"]
    assert "₱70,000" in last["text"]
    assert "余额：₱0" in last["text"]
    assert "确认入账" not in last["text"]
    kb = last["reply_markup"]
    labels = [b.text for row in kb.inline_keyboard for b in row] if kb else []
    assert "✓ 确认入账" not in labels  # confirmed card must not keep a stale confirm button
    assert "↩️ 撤销" in labels  # reverse stays available for the Owner
    assert len(env.bot.answers()) == 1
    assert "处理中" in (env.bot.last_answer()["text"] or "")


def test_nl_confirm_double_click_idempotent(make_app):
    """Second click after success never creates a second financial record."""
    env = make_app()
    add_unit_1608(env)
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "1608租金收到了", bot=env.bot)])
    data = _match_confirm_data(env)
    run_updates(
        env,
        [
            make_callback_update(OWNER_ID, OWNER_ID, data, message_id=10, update_id=2, bot=env.bot),
            make_callback_update(OWNER_ID, OWNER_ID, data, message_id=10, update_id=3, bot=env.bot),
        ],
    )
    assert env.backend.count_calls("POST", "/incomes") == 1
    assert len(env.backend.incomes) == 8  # 7 seeded Jan-Jul + exactly one new row


# --- duplicate / no-write paths ---------------------------------------------

def test_nl_duplicate_friendly_no_second_record(make_app):
    """★ Test 5 (duplicate): already booked -> friendly message, zero writes,
    never a 409 / IntegrityError / duplicate key on screen."""
    env = make_app()
    env.backend.rent_match_response = {
        "received_date": date.today().isoformat(),
        "candidates": [{
            "kind": "duplicate", "confidence": "high",
            "lease_id": 100, "unit_id": 100, "unit_number": "1608",
            "property_id": 2, "property_name": "Bayshore & Tower",
            "tenant_id": 2, "tenant_name": "John Dela Cruz",
            "period": date.today().strftime("%Y-%m"), "due_date": None,
            "amount": "70000.00", "open_count": 0, "remaining_balance": "0.00",
            "income_id": 999, "income_status": "confirmed",
        }],
    }
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "1608租金收到了", bot=env.bot)])

    text = env.bot.sends()[-1]["text"]
    assert "这笔付款已经入账，无需重复处理" in text
    assert "409" not in text and "IntegrityError" not in text and "duplicate" not in text.lower()
    assert env.backend.count_calls("POST", "/incomes") == 0
    assert env.backend.count_calls("POST", "/incomes/999/confirm") == 0


def test_nl_pending_confirms_existing_without_create(make_app):
    """A pending income already exists -> confirm THAT record; no create."""
    env = make_app()
    env.backend.rent_match_response = {
        "received_date": date.today().isoformat(),
        "candidates": [{
            "kind": "pending", "confidence": "high",
            "lease_id": 100, "unit_id": 100, "unit_number": "1608",
            "property_id": 2, "property_name": "Bayshore & Tower",
            "tenant_id": 2, "tenant_name": "John Dela Cruz",
            "period": date.today().strftime("%Y-%m"), "due_date": None,
            "amount": "70000.00", "open_count": 0, "remaining_balance": "0.00",
            "income_id": 42, "income_status": "pending",
        }],
    }
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "1608租金收到了", bot=env.bot)])
    send = env.bot.sends()[-1]
    assert "已登记待确认" in send["text"]
    row = [b for row2 in send["reply_markup"].inline_keyboard for b in row2]
    parsed = decode(row[0].callback_data)
    assert parsed["action"] == "cnf" and parsed["entity"] == "inc" and parsed["ref"] == "42"

    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, row[0].callback_data, message_id=10, update_id=2, bot=env.bot)],
    )
    assert env.backend.count_calls("POST", "/incomes") == 0
    assert env.backend.count_calls("POST", "/incomes/42/confirm") == 1


# --- role-aware / ambiguous / none ------------------------------------------

def test_nl_secretary_statement_registers_pending_for_owner(make_app):
    """V1.3 Slice 2: a Secretary statement registers ONE pending income and
    routes the Owner-only confirm card to the Owner's chat — the Secretary
    never receives a confirm button."""
    env = make_app()
    add_unit_1608(env)
    run_updates(env, [make_text_update(SECRETARY_ID, SECRETARY_ID, "1608租金收到了", bot=env.bot)])

    assert env.backend.count_calls("POST", "/incomes") == 1
    assert env.backend.incomes[-1]["status"] == "pending"

    sends = env.bot.sends()
    secretary_reply = sends[-2]
    assert secretary_reply["chat_id"] == SECRETARY_ID
    assert "Rent payment matched" in secretary_reply["text"]
    assert secretary_reply["reply_markup"] is None

    owner_card = sends[-1]
    assert owner_card["chat_id"] == OWNER_ID
    assert "秘书登记了一笔租金" in owner_card["text"]
    kb = owner_card["reply_markup"]
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert "✓ 确认入账" in labels
    assert env.store.get_conversation(SECRETARY_ID, SECRETARY_ID) is None


def test_nl_ambiguous_lists_candidates_without_menu(make_app):
    env = make_app()
    env.backend.rent_match_response = {
        "received_date": date.today().isoformat(),
        "candidates": [
            {
                "kind": "open", "confidence": "low",
                "lease_id": 100, "unit_id": 100, "unit_number": "1608",
                "property_id": 2, "property_name": "Bayshore & Tower",
                "tenant_id": 2, "tenant_name": "John Dela Cruz",
                "period": "2026-07", "due_date": None,
                "amount": "70000.00", "open_count": 2, "remaining_balance": "70000.00",
                "income_id": None, "income_status": None,
            },
            {
                "kind": "open", "confidence": "low",
                "lease_id": 100, "unit_id": 100, "unit_number": "1608",
                "property_id": 2, "property_name": "Bayshore & Tower",
                "tenant_id": 2, "tenant_name": "John Dela Cruz",
                "period": "2026-08", "due_date": None,
                "amount": "70000.00", "open_count": 2, "remaining_balance": "0.00",
                "income_id": None, "income_status": None,
            },
        ],
    }
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "1608租金收到了", bot=env.bot)])
    send = env.bot.sends()[-1]
    assert "几笔可能的账单" in send["text"]
    assert "2026-07" not in send["text"]  # raw period never shown
    assert "7月租金" in send["text"]
    assert send["reply_markup"] is None
    assert "收租菜单" not in send["text"] and "待办中心" not in send["text"]


def test_nl_no_match_friendly(make_app):
    env = make_app()
    env.backend.rent_match_response = {"received_date": date.today().isoformat(), "candidates": []}
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "1608租金收到了", bot=env.bot)])
    assert "没有找到对应的未结账单" in env.bot.sends()[-1]["text"]
