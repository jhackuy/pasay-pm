"""V1.3 Slice 2 — Entry D: read-only multi-candidate selector.

Multi-match unit/tenant queries now render one inline button per candidate
(property · unit · tenant). Tapping a button re-renders ONLY that candidate's
status card (byte-identical to a single hit) with zero API calls and zero
writes. Covers: both ambiguity axes, repeat-tap idempotency, expired/invalid
callbacks, unknown-user refusal, no internal-id leakage, and card isolation
across multiple selector messages.
"""
from pasay_bot.keyboards import (
    ACTION_RENT_STATUS_SELECT,
    decode,
    encode,
)
from conftest import OWNER_ID, UNKNOWN_ID, make_callback_update, make_text_update, run_updates


def add_prefixed_unit(env, unit_number, unit_id, property_id, tenant_id, rent,
                      tenant_name=None):
    """A unit in the prefixed building style (DEV-BAY-1805) with an active
    lease and Jan–Jul confirmed income; the current month stays open."""
    env.backend.units.append({
        "id": unit_id, "property_id": property_id, "unit_number": unit_number,
        "floor": "16", "size_sqm": "40.00", "monthly_rent": rent,
        "status": "occupied", "is_active": True,
    })
    env.backend.leases.append({
        "id": unit_id, "unit_id": unit_id, "tenant_id": tenant_id,
        "start_date": "2026-01-01", "end_date": "2026-12-31",
        "accounting_start_date": None, "monthly_rent": rent,
        "deposit": "0.00", "status": "active", "due_day": 5, "notes": None,
    })
    if tenant_name is not None:
        env.backend.tenants.append({
            "id": tenant_id, "full_name": tenant_name, "phone": None,
            "email": None, "is_active": True,
        })
    for m in range(1, 8):
        env.backend.add_income(
            status="confirmed", lease_id=unit_id, amount=rent,
            received_date=f"2026-{m:02d}-10",
            description=f"rent 2026-{m:02d}", income_id=unit_id * 100 + m,
        )


def add_unit_1608(env):
    """Unit 1608 (property 2) with an active John Dela Cruz lease and Jan–Jul
    confirmed income; the current month is left open (unpaid)."""
    env.backend.tenants.append({
        "id": 88, "full_name": "John Dela Cruz", "phone": None,
        "email": None, "is_active": True,
    })
    env.backend.units.append({
        "id": 100, "property_id": 2, "unit_number": "1608", "floor": "16",
        "size_sqm": "40.00", "monthly_rent": "70000.00",
        "status": "occupied", "is_active": True,
    })
    env.backend.leases.append({
        "id": 100, "unit_id": 100, "tenant_id": 88, "start_date": "2026-01-01",
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


def add_second_john(env):
    """A second tenant named John with an active lease (ambiguity case)."""
    env.backend.tenants.append({
        "id": 9, "full_name": "John Smith", "phone": None, "email": None,
        "is_active": True,
    })
    env.backend.units.append({
        "id": 110, "property_id": 1, "unit_number": "1708", "floor": "17",
        "size_sqm": "40.00", "monthly_rent": "65000.00",
        "status": "occupied", "is_active": True,
    })
    env.backend.leases.append({
        "id": 110, "unit_id": 110, "tenant_id": 9, "start_date": "2026-01-01",
        "end_date": "2026-12-31", "accounting_start_date": None,
        "monthly_rent": "65000.00", "deposit": "130000.00",
        "status": "active", "due_day": 5, "notes": None,
    })


def add_two_1805_units(env):
    """The canonical shared-suffix ambiguity: DEV-BAY-1805 + DEV-SOL-1805."""
    env.backend.tenants[1]["full_name"] = "John Dela Cruz"
    add_prefixed_unit(env, "DEV-BAY-1805", 130, 2, 2, "70000.00")
    add_prefixed_unit(env, "DEV-SOL-1805", 140, 1, 9, "48000.00",
                      tenant_name="Paolo Cruz")


def _button_data(send, index):
    return send["reply_markup"].inline_keyboard[index][0].callback_data


def _labels(send):
    kb = send["reply_markup"].inline_keyboard
    return [btn.text for row in kb for btn in row]


def _post_count(backend):
    return sum(1 for m, _p, _b in backend.calls if m == "POST")


# --- unit multi-hit -> buttons -> click each candidate ----------------------

def test_unit_multi_hit_selector_click_each_candidate(make_app):
    env = make_app()
    add_two_1805_units(env)
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "1805 交了没有", bot=env.bot)])
    send = env.bot.last_send()
    assert "找到多个匹配项" in send["text"]
    labels = _labels(send)
    assert len(labels) == 2
    assert any("Bayshore & Tower" in l and "DEV-BAY-1805" in l and "John Dela Cruz" in l for l in labels)
    assert any("Pasay Premier Residences" in l and "DEV-SOL-1805" in l and "Paolo Cruz" in l for l in labels)

    data_a = _button_data(send, 0)
    data_b = _button_data(send, 1)
    n_api = len(env.backend.calls)

    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, data_a, bot=env.bot)])
    card_a = env.bot.last_edit()["text"]
    assert "Unit DEV-BAY-1805" in card_a and "John Dela Cruz" in card_a
    assert "DEV-SOL-1805" not in card_a and "Paolo" not in card_a

    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, data_b,
                                           message_id=10, update_id=2, bot=env.bot)])
    card_b = env.bot.last_edit()["text"]
    assert "Unit DEV-SOL-1805" in card_b and "Paolo Cruz" in card_b
    assert "DEV-BAY-1805" not in card_b and "John Dela Cruz" not in card_b

    assert len(env.backend.calls) == n_api
    assert _post_count(env.backend) == 0


def test_tenant_multi_hit_selector_click_each_candidate(make_app):
    env = make_app()
    add_unit_1608(env)
    add_second_john(env)
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "John 交了吗", bot=env.bot)])
    send = env.bot.last_send()
    labels = _labels(send)
    assert len(labels) == 2
    assert any("1608" in l and "John Dela Cruz" in l for l in labels)
    assert any("1708" in l and "John Smith" in l for l in labels)

    n_api = len(env.backend.calls)
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID,
                                           _button_data(send, 0), bot=env.bot)])
    card_a = env.bot.last_edit()["text"]
    assert "Unit 1608" in card_a and "John Dela Cruz" in card_a
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID,
                                           _button_data(send, 1),
                                           message_id=10, update_id=2, bot=env.bot)])
    card_b = env.bot.last_edit()["text"]
    assert "Unit 1708" in card_b and "John Smith" in card_b
    assert len(env.backend.calls) == n_api
    assert _post_count(env.backend) == 0


# --- idempotency / expired / invalid ---------------------------------------

def test_repeat_tap_is_idempotent(make_app):
    env = make_app()
    add_two_1805_units(env)
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "1805 交了没有", bot=env.bot)])
    data = _button_data(env.bot.last_send(), 0)
    n_api = len(env.backend.calls)
    run_updates(env, [
        make_callback_update(OWNER_ID, OWNER_ID, data, bot=env.bot),
        make_callback_update(OWNER_ID, OWNER_ID, data,
                             message_id=10, update_id=2, bot=env.bot),
    ])
    edits = env.bot.edits()
    assert edits[-1]["text"] == edits[-2]["text"]
    assert env.bot.last_answer()["text"] == "✅ 已显示该房源状态"
    assert len(env.backend.calls) == n_api


def test_expired_callback_friendly_no_internal_state(make_app):
    env = make_app()
    add_two_1805_units(env)
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "1805 交了没有", bot=env.bot)])
    parsed = decode(_button_data(env.bot.last_send(), 0))
    stale = encode(ACTION_RENT_STATUS_SELECT, "sel", parsed["ref"],
                   nonce=parsed["nonce"], ts=1)
    n_api = len(env.backend.calls)
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, stale, bot=env.bot)])
    joined = "".join(env.bot.all_texts())
    assert "已经过期" in joined
    for banned in ("traceback", "exception", "nonce", "payload",
                   "lease_id", "income_id", "unit_id"):
        assert banned not in joined.lower(), banned
    assert len(env.backend.calls) == n_api


def test_unknown_selector_nonce_is_friendly(make_app):
    env = make_app()
    add_two_1805_units(env)
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "1805 交了没有", bot=env.bot)])
    parsed = decode(_button_data(env.bot.last_send(), 0))
    ghost = encode(ACTION_RENT_STATUS_SELECT, "sel", parsed["ref"],
                   nonce="0" * 16, ts=parsed["ts"])
    n_api = len(env.backend.calls)
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, ghost, bot=env.bot)])
    assert "已经过期" in "".join(env.bot.all_texts())
    assert len(env.backend.calls) == n_api


def test_out_of_range_candidate_is_invalid(make_app):
    env = make_app()
    add_two_1805_units(env)
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "1805 交了没有", bot=env.bot)])
    parsed = decode(_button_data(env.bot.last_send(), 0))
    bad = encode(ACTION_RENT_STATUS_SELECT, "sel", "99",
                 nonce=parsed["nonce"], ts=parsed["ts"])
    n_api = len(env.backend.calls)
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, bad, bot=env.bot)])
    assert "无效操作" in "".join(env.bot.all_texts())
    assert len(env.backend.calls) == n_api


# --- RBAC / safety ----------------------------------------------------------

def test_unknown_user_selector_denied_zero_api_calls(make_app):
    env = make_app()
    add_two_1805_units(env)
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "1805 交了没有", bot=env.bot)])
    data = _button_data(env.bot.last_send(), 0)
    env.backend.calls.clear()
    run_updates(env, [make_callback_update(UNKNOWN_ID, UNKNOWN_ID, data, bot=env.bot)])
    assert "无权限" in "".join(env.bot.all_texts())
    assert env.backend.calls == []


def test_selector_never_exposes_internal_ids(make_app):
    env = make_app()
    add_two_1805_units(env)
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "1805 交了没有", bot=env.bot)])
    send = env.bot.last_send()
    joined = send["text"] + " " + " ".join(_labels(send))
    for banned in ("130", "140", "lease", "income", "tenant_id", "unit_id"):
        assert banned not in joined.lower(), banned

    parsed = decode(_button_data(send, 0))
    payload = env.store.get_rent_status_selector(parsed["nonce"], OWNER_ID, OWNER_ID)
    assert payload is not None and len(payload) == 2
    allowed = {
        "tenant_name", "unit_number", "property_name", "monthly_rent",
        "paid", "outstanding", "overdue_days", "overdue_months", "month",
    }
    for row in payload:
        assert set(row.keys()) <= allowed, row

    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID,
                                           _button_data(send, 0), bot=env.bot)])
    card = env.bot.last_edit()["text"].lower()
    for banned in ("130", "140", "lease", "income", "tenant_id", "unit_id"):
        assert banned not in card, banned


# --- selector isolation across messages ------------------------------------

def test_older_selector_card_not_hijacked_by_newer_query(make_app):
    env = make_app()
    add_two_1805_units(env)
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "1805 交了没有", bot=env.bot)])
    first_data = _button_data(env.bot.last_send(), 0)
    # A second, newer selector card in the same chat must not invalidate the
    # first card's buttons (each card is keyed by its own nonce).
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "1805 交了没有",
                                       message_id=2, update_id=2, bot=env.bot)])
    assert len(_labels(env.bot.last_send())) == 2

    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, first_data, bot=env.bot)])
    card = env.bot.last_edit()["text"]
    assert "Unit DEV-BAY-1805" in card and "John Dela Cruz" in card
    assert "DEV-SOL-1805" not in card
