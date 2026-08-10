"""V1.1 rent-entry conversation tests: smart-default confirmation card,
the [✏️修改] sub-flow (amount / date / method), validation, cancel, expiry,
and last-used payment method persistence."""
from datetime import date

from conftest import OWNER_ID, make_callback_update, make_text_update, run_updates
from pasay_bot.keyboards import decode, encode, new_nonce, now_ts


def _confirm_data(env):
    """callback_data of the confirm button on the latest confirmation card."""
    call = env.bot.edits()[-1]
    kb = call["reply_markup"]
    row = [b for row2 in kb.inline_keyboard for b in row2]
    return next(
        b.callback_data
        for b in row
        if decode(b.callback_data) is not None
        and decode(b.callback_data)["action"] == "cnf"
    )


def _start_rent(env, unit_id=1, user_id=OWNER_ID, chat_id=OWNER_ID):
    run_updates(
        env,
        [make_callback_update(user_id, chat_id, encode("rn", "go", str(unit_id)), bot=env.bot)],
    )


def _edit_menu(env):
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, encode("ed", "menu"), bot=env.bot)])


def test_flow_defaults_to_confirm_card(make_app):
    """★ B4: [💵收租] -> pick unit -> confirmation card with smart defaults."""
    env = make_app()
    _start_rent(env)
    conv = env.store.get_conversation(OWNER_ID, OWNER_ID)
    assert conv["state"] == "rent_confirm"
    payload = conv["payload"]
    assert payload["amount"] == "55000.00"          # current receivable
    assert payload["received_date"] == date.today().isoformat()  # today
    assert payload["method"] == "Bank"              # last-used default
    assert payload["period"] == date.today().strftime("%Y-%m")
    confirm_text = env.bot.edits()[-1]["text"]
    assert "确认收租" in confirm_text
    assert "金额：<b>₱55,000</b>" in confirm_text
    assert "方式：Bank" in confirm_text
    kb = env.bot.edits()[-1]["reply_markup"]
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert "✏️ 修改收租信息" in labels  # edit-first, not re-typing


def test_edit_amount_returns_to_confirm(make_app):
    env = make_app()
    _start_rent(env)
    _edit_menu(env)
    run_updates(
        env,
        [
            make_callback_update(OWNER_ID, OWNER_ID, encode("ed", "amount"), update_id=3, bot=env.bot),
            make_text_update(OWNER_ID, OWNER_ID, "60000", message_id=4, update_id=4, bot=env.bot),
        ],
    )
    conv = env.store.get_conversation(OWNER_ID, OWNER_ID)
    assert conv["state"] == "rent_confirm"
    assert conv["payload"]["amount"] == "60000.00"
    assert "金额：<b>₱60,000</b>" in env.bot.edits()[-1]["text"]


def test_amount_invalid_reasks(make_app):
    env = make_app()
    _start_rent(env)
    _edit_menu(env)
    run_updates(
        env,
        [
            make_callback_update(OWNER_ID, OWNER_ID, encode("ed", "amount"), update_id=3, bot=env.bot),
            make_text_update(OWNER_ID, OWNER_ID, "abc", message_id=4, update_id=4, bot=env.bot),
        ],
    )
    assert env.store.get_conversation(OWNER_ID, OWNER_ID)["state"] == "rent_edit_amount"
    assert "金额无效" in env.bot.sends()[-1]["text"]


def test_negative_and_oversized_amount_rejected(make_app):
    env = make_app()
    _start_rent(env)
    _edit_menu(env)
    updates = [
        make_callback_update(OWNER_ID, OWNER_ID, encode("ed", "amount"), update_id=3, bot=env.bot),
        make_text_update(OWNER_ID, OWNER_ID, "-5", message_id=4, update_id=4, bot=env.bot),
        make_text_update(OWNER_ID, OWNER_ID, "99999999999999.99", message_id=5, update_id=5, bot=env.bot),
        make_text_update(OWNER_ID, OWNER_ID, "55000.123", message_id=6, update_id=6, bot=env.bot),
    ]
    run_updates(env, updates)
    assert env.store.get_conversation(OWNER_ID, OWNER_ID)["state"] == "rent_edit_amount"


def test_date_invalid_reasks(make_app):
    env = make_app()
    _start_rent(env)
    _edit_menu(env)
    run_updates(
        env,
        [
            make_callback_update(OWNER_ID, OWNER_ID, encode("ed", "date"), update_id=3, bot=env.bot),
            make_text_update(OWNER_ID, OWNER_ID, "not-a-date", message_id=4, update_id=4, bot=env.bot),
        ],
    )
    assert env.store.get_conversation(OWNER_ID, OWNER_ID)["state"] == "rent_edit_date"
    assert "日期格式无效" in env.bot.sends()[-1]["text"]


def test_edit_date_today_button(make_app):
    env = make_app()
    _start_rent(env)
    _edit_menu(env)
    run_updates(
        env,
        [
            make_callback_update(OWNER_ID, OWNER_ID, encode("ed", "date"), update_id=3, bot=env.bot),
            make_callback_update(OWNER_ID, OWNER_ID, encode("ed", "today"), update_id=4, bot=env.bot),
        ],
    )
    conv = env.store.get_conversation(OWNER_ID, OWNER_ID)
    assert conv["state"] == "rent_confirm"
    assert conv["payload"]["received_date"] == date.today().isoformat()
    assert "日期：" + date.today().isoformat() in env.bot.edits()[-1]["text"]


def test_edit_method_returns_to_confirm(make_app):
    env = make_app()
    _start_rent(env)
    _edit_menu(env)
    run_updates(
        env,
        [
            make_callback_update(OWNER_ID, OWNER_ID, encode("ed", "method"), update_id=3, bot=env.bot),
            make_callback_update(OWNER_ID, OWNER_ID, encode("mt", "gcash"), update_id=4, bot=env.bot),
        ],
    )
    conv = env.store.get_conversation(OWNER_ID, OWNER_ID)
    assert conv["state"] == "rent_confirm"
    assert conv["payload"]["method"] == "GCash"
    assert "方式：GCash" in env.bot.edits()[-1]["text"]


def test_edit_back_returns_to_confirm(make_app):
    env = make_app()
    _start_rent(env)
    _edit_menu(env)
    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, encode("ed", "back"), update_id=3, bot=env.bot)],
    )
    conv = env.store.get_conversation(OWNER_ID, OWNER_ID)
    assert conv["state"] == "rent_confirm"
    assert "确认收租" in env.bot.edits()[-1]["text"]


def test_cancel_during_edit(make_app):
    env = make_app()
    _start_rent(env)
    _edit_menu(env)
    run_updates(
        env,
        [
            make_callback_update(OWNER_ID, OWNER_ID, encode("ed", "amount"), update_id=3, bot=env.bot),
            make_text_update(OWNER_ID, OWNER_ID, "取消", message_id=4, update_id=4, bot=env.bot),
        ],
    )
    assert env.store.get_conversation(OWNER_ID, OWNER_ID) is None
    assert "已取消" in env.bot.all_texts()[-1]


def test_cancel_command_during_conversation(make_app):
    env = make_app()
    _start_rent(env)
    run_updates(
        env,
        [make_text_update(OWNER_ID, OWNER_ID, "/cancel", message_id=4, update_id=4, bot=env.bot)],
    )
    assert env.store.get_conversation(OWNER_ID, OWNER_ID) is None


def test_conversation_expired_falls_back_to_nl(make_app):
    env = make_app()
    env.store.save_conversation(OWNER_ID, OWNER_ID, "rent_edit_amount",
                                {"unit_id": 1, "amount": "55000.00"}, ttl_seconds=-1)
    run_updates(
        env,
        [make_text_update(OWNER_ID, OWNER_ID, "55000", message_id=5, update_id=5, bot=env.bot)],
    )
    # expired conversation -> treated as free text -> unknown fallback
    assert "请用下方按钮" in env.bot.sends()[-1]["text"]


def test_method_invalid_entity_rejected(make_app):
    env = make_app()
    _start_rent(env)
    _edit_menu(env)
    run_updates(
        env,
        [
            make_callback_update(OWNER_ID, OWNER_ID, encode("ed", "method"), update_id=3, bot=env.bot),
            make_callback_update(OWNER_ID, OWNER_ID, encode("mt", "bogus"), update_id=4, bot=env.bot),
        ],
    )
    assert env.store.get_conversation(OWNER_ID, OWNER_ID)["state"] == "rent_edit_method"
    assert env.bot.last_answer()["text"] == "⚠️ 无效操作"


def test_default_method_bank_for_new_user(make_app):
    """★ B4: brand-new user falls back to the Bank default."""
    env = make_app()
    _start_rent(env)
    conv = env.store.get_conversation(OWNER_ID, OWNER_ID)
    assert conv["payload"]["method"] == "Bank"


def test_last_used_method_remembered(make_app):
    """★ B4: the last-used payment method becomes the next flow's default."""
    env = make_app()
    # flow 1: unit 1 with default Bank -> confirm
    _start_rent(env, unit_id=1)
    data = _confirm_data(env)
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, data, bot=env.bot)])
    assert env.backend.incomes[0]["status"] == "confirmed"
    assert env.store.get_user_default_method(OWNER_ID) == "Bank"

    # flow 2: unit 3, switch method to GCash -> confirm
    _start_rent(env, unit_id=3)
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, encode("ed", "menu"), update_id=3, bot=env.bot)])
    run_updates(
        env,
        [
            make_callback_update(OWNER_ID, OWNER_ID, encode("ed", "method"), update_id=4, bot=env.bot),
            make_callback_update(OWNER_ID, OWNER_ID, encode("mt", "gcash"), update_id=5, bot=env.bot),
        ],
    )
    data2 = _confirm_data(env)
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, data2, update_id=6, bot=env.bot)])
    assert env.store.get_user_default_method(OWNER_ID) == "GCash"

    # flow 3: same store, unit 3 no longer has an income (cleared), so the
    # next flow re-uses the stored GCash default.
    env.backend.incomes.clear()
    _start_rent(env, unit_id=3)
    conv = env.store.get_conversation(OWNER_ID, OWNER_ID)
    assert conv["payload"]["method"] == "GCash"
