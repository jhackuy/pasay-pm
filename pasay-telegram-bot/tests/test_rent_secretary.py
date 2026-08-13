"""V1.3 Slice 2 — Secretary Register -> Owner Confirm closed loop.

Covers the real operations chain: Secretary one-line English register (one
pending income, English reply, Chinese action card to the Owner's private
chat), Owner one-tap confirm (terminal Chinese card, no stale confirm button),
Secretary RBAC (never confirm), duplicate pending / confirmed, update replay
idempotency, the read-only [有问题] button, and no internal error leakage.
"""
from datetime import date

from conftest import (
    OWNER_ID,
    SECRETARY_ID,
    make_callback_update,
    make_text_update,
    run_updates,
)
from pasay_bot.keyboards import decode


def add_unit_1608(env):
    """A unit/lease matching the acceptance examples, with Jan-Jul already
    paid so the current month is the single outstanding bill."""
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


def _register(env, text="Received rent for 1608."):
    """Secretary reports rent; returns (secretary_reply, owner_card, income)."""
    run_updates(env, [make_text_update(SECRETARY_ID, SECRETARY_ID, text, bot=env.bot)])
    sends = env.bot.sends()
    return sends[-2], sends[-1], env.backend.incomes[-1]


def _owner_confirm_data(env):
    """callback_data of the [✓ 确认入账] button on the Owner's card."""
    kb = env.bot.sends()[-1]["reply_markup"]
    for row in kb.inline_keyboard:
        for b in row:
            parsed = decode(b.callback_data)
            if parsed and parsed["action"] == "cnf" and parsed["entity"] == "inc":
                return b.callback_data
    raise AssertionError("Owner card has no income confirm button")


def _issue_data(env):
    kb = env.bot.sends()[-1]["reply_markup"]
    for row in kb.inline_keyboard:
        for b in row:
            parsed = decode(b.callback_data)
            if parsed and parsed["action"] == "iss":
                return b.callback_data
    raise AssertionError("Owner card has no [有问题] button")


def _keyboard_labels(call):
    kb = call["reply_markup"]
    if kb is None:
        return []
    return [b.text for row in kb.inline_keyboard for b in row]


# --- happy path: Secretary register ----------------------------------------

def test_secretary_registers_pending_once_and_owner_receives_card(make_app):
    """One English statement -> exactly one pending income, English reply to
    the Secretary, Chinese confirm card to the Owner's private chat."""
    env = make_app()
    add_unit_1608(env)
    secretary_reply, owner_card, income = _register(env)

    assert env.backend.count_calls("POST", "/payments/match") == 1
    assert env.backend.count_calls("POST", "/incomes") == 1
    assert income["status"] == "pending"
    assert income["lease_id"] == 100
    assert income["description"] == f"rent {date.today().strftime('%Y-%m')}"

    # Secretary sees a short English confirmation with no re-entry questions.
    assert secretary_reply["chat_id"] == SECRETARY_ID
    assert "Rent payment matched" in secretary_reply["text"]
    assert "1608" in secretary_reply["text"]
    assert "Aug rent" in secretary_reply["text"]
    assert "₱70,000" in secretary_reply["text"]
    assert "Sent to Owner for confirmation." in secretary_reply["text"]
    assert secretary_reply["reply_markup"] is None
    assert "month" not in secretary_reply["text"].lower()

    # Owner gets a Chinese action-at-source card (chat_id == owner user id).
    assert owner_card["chat_id"] == OWNER_ID
    text = owner_card["text"]
    assert "秘书登记了一笔租金" in text
    assert "Bayshore" in text and "1608" in text
    assert "8月租金" in text
    assert "应收：₱70,000" in text
    assert "收到：₱70,000" in text
    assert "登记人：Secretary" in text
    assert "✓ 金额一致" in text
    assert "✓ 唯一未结账单" in text
    labels = _keyboard_labels(owner_card)
    assert "✓ 确认入账" in labels
    assert "有问题" in labels

    conv = env.store.get_conversation(OWNER_ID, OWNER_ID)
    assert conv is not None and conv["state"] == "rent_secretary_confirm"
    assert conv["payload"]["income_id"] == income["id"]
    assert conv["payload"]["flow"] == "secretary_register"


def test_secretary_reports_with_english_variants(make_app):
    """'Received rent for 1608.' and '1608 rent received.' both register."""
    for text in ("Received rent for 1608.", "1608 rent received."):
        env = make_app()
        add_unit_1608(env)
        run_updates(
            env, [make_text_update(SECRETARY_ID, SECRETARY_ID, text, bot=env.bot)]
        )
        assert env.backend.count_calls("POST", "/incomes") == 1
        assert env.backend.incomes[-1]["status"] == "pending"


# --- Owner confirm ----------------------------------------------------------

def test_owner_confirm_mutates_card_to_terminal_state(make_app):
    """[✓ 确认入账] -> one confirm call, terminal Chinese card, no stale
    confirm button; reverse stays (matches the existing confirmed card)."""
    env = make_app()
    add_unit_1608(env)
    _register(env)
    income = env.backend.incomes[-1]
    data = _owner_confirm_data(env)

    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, data, message_id=10, bot=env.bot)],
    )

    assert env.backend.count_calls("POST", f"/incomes/{income['id']}/confirm") == 1
    assert income["status"] == "confirmed"
    last = env.bot.edits()[-1]
    text = last["text"]
    assert "租金已入账" in text
    assert "1608" in text
    assert "8月租金" in text
    assert "₱70,000" in text
    assert "余额：₱0" in text
    assert "登记：Secretary" in text
    labels = _keyboard_labels(last)
    assert "确认入账" not in labels
    assert "↩️ 撤销" in labels
    assert len(env.bot.answers()) == 1
    assert "处理中" in (env.bot.last_answer()["text"] or "")


def test_owner_confirm_double_click_idempotent(make_app):
    """Same confirm callback replayed -> confirm called once, still terminal."""
    env = make_app()
    add_unit_1608(env)
    _register(env)
    income = env.backend.incomes[-1]
    data = _owner_confirm_data(env)
    run_updates(
        env,
        [
            make_callback_update(OWNER_ID, OWNER_ID, data, message_id=10, update_id=2, bot=env.bot),
            make_callback_update(OWNER_ID, OWNER_ID, data, message_id=10, update_id=3, bot=env.bot),
        ],
    )
    assert env.backend.count_calls("POST", f"/incomes/{income['id']}/confirm") == 1
    assert income["status"] == "confirmed"
    assert "租金已入账" in env.bot.edits()[-1]["text"]


# --- RBAC -------------------------------------------------------------------

def test_secretary_cannot_confirm_owner_card(make_app):
    """Secretary hand-crafts the Owner's confirm callback -> refused before
    any API call; the pending income stays pending."""
    env = make_app()
    add_unit_1608(env)
    _register(env)
    income = env.backend.incomes[-1]
    data = _owner_confirm_data(env)
    run_updates(
        env,
        [
            make_callback_update(SECRETARY_ID, SECRETARY_ID, data, message_id=10, bot=env.bot),
        ],
    )
    assert env.backend.count_calls("POST", f"/incomes/{income['id']}/confirm") == 0
    assert income["status"] == "pending"
    answer = (env.bot.last_answer()["text"] or "").lower()
    assert "permission" in answer or "无权限" in answer


# --- duplicates -------------------------------------------------------------

def test_duplicate_pending_no_second_income_no_second_owner_card(make_app):
    """Situation A: same statement while pending -> friendly English 'already
    waiting', no second pending row, Owner is NOT notified twice."""
    env = make_app()
    add_unit_1608(env)
    _register(env)
    sends_before = len(env.bot.sends())

    run_updates(
        env, [make_text_update(SECRETARY_ID, SECRETARY_ID, "Received rent for 1608.", bot=env.bot)]
    )

    assert env.backend.count_calls("POST", "/incomes") == 1
    text = env.bot.sends()[-1]["text"]
    assert "already waiting for Owner confirmation" in text
    assert env.bot.sends()[-1]["chat_id"] == SECRETARY_ID
    assert len(env.bot.sends()) == sends_before + 1  # only the Secretary reply


def test_duplicate_confirmed_no_new_pending(make_app):
    """Situation B: same statement after the Owner confirmed -> friendly
    English 'already recorded and confirmed', no new pending row."""
    env = make_app()
    add_unit_1608(env)
    _register(env)
    data = _owner_confirm_data(env)
    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, data, message_id=10, update_id=2, bot=env.bot)],
    )

    run_updates(
        env, [make_text_update(SECRETARY_ID, SECRETARY_ID, "Received rent for 1608.", bot=env.bot)]
    )

    assert env.backend.count_calls("POST", "/incomes") == 1
    assert sum(1 for i in env.backend.incomes if i["status"] == "pending") == 0
    text = env.bot.sends()[-1]["text"]
    assert "already been recorded and confirmed" in text


# --- replay idempotency -----------------------------------------------------

def test_duplicate_update_replay_no_second_create_or_notification(make_app):
    """The same statement redelivered while both matches still resolve to
    open-high: the bot-side idempotency guard dedupes — one pending row, one
    Owner card, no internal errors on screen."""
    env = make_app()
    add_unit_1608(env)
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
    run_updates(
        env,
        [
            make_text_update(SECRETARY_ID, SECRETARY_ID, "Received rent for 1608.", bot=env.bot, update_id=1),
            make_text_update(SECRETARY_ID, SECRETARY_ID, "Received rent for 1608.", bot=env.bot, update_id=1),
        ],
    )
    assert env.backend.count_calls("POST", "/incomes") == 1
    owner_cards = [s for s in env.bot.sends() if s["chat_id"] == OWNER_ID]
    assert len(owner_cards) == 1
    texts = " | ".join(s["text"] for s in env.bot.sends())
    assert "409" not in texts and "IntegrityError" not in texts and "idempotency" not in texts.lower()


# --- [有问题] read-only -------------------------------------------------------

def test_issue_button_is_read_only_status_hint(make_app):
    """[有问题] before confirm: friendly 'still pending' tip, zero writes,
    no message mutation. After confirm: 'already recorded' tip."""
    env = make_app()
    add_unit_1608(env)
    _register(env)
    income = env.backend.incomes[-1]
    issue = _issue_data(env)

    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, issue, message_id=10, update_id=2, bot=env.bot)],
    )
    # post-ack tip is rendered durably onto the card (never a silent drop)
    assert "这笔租金仍待确认，请直接描述问题。" in env.bot.last_edit()["text"]
    assert env.backend.count_calls("POST", "/incomes") == 1
    assert income["status"] == "pending"

    data = _owner_confirm_data(env)
    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, data, message_id=10, update_id=3, bot=env.bot)],
    )
    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, issue, message_id=10, update_id=4, bot=env.bot)],
    )
    assert "这笔租金已入账；如需调整请联系秘书。" in env.bot.last_edit()["text"]


def test_issue_button_owner_only(make_app):
    """Secretary hand-crafts the [有问题] callback -> refused, read-only."""
    env = make_app()
    add_unit_1608(env)
    _register(env)
    issue = _issue_data(env)
    run_updates(
        env,
        [make_callback_update(SECRETARY_ID, SECRETARY_ID, issue, message_id=10, bot=env.bot)],
    )
    answer = (env.bot.last_answer()["text"] or "").lower()
    assert "permission" in answer or "无权限" in answer
    assert env.backend.count_calls("POST", "/incomes") == 1
