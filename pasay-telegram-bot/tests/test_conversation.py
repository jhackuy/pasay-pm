"""Rent-entry conversation state machine: amount -> date -> method -> confirm
card; invalid input; cancel; expiry. Drives handlers through PTB's no-network
Application (FakeBot + httpx MockTransport)."""
from datetime import date

from conftest import OWNER_ID, make_callback_update, make_text_update, run_updates
from pasay_bot.keyboards import decode, encode, new_nonce, now_ts


def _start_rent(env, user_id=OWNER_ID, chat_id=OWNER_ID):
    run_updates(
        env,
        [make_callback_update(user_id, chat_id, encode("rn", "go", "1"), bot=env.bot)],
    )


def _run_entry(env, amount="55000", received_date=None, user_id=OWNER_ID, chat_id=OWNER_ID):
    received_date = received_date or date.today().isoformat()
    updates = [
        make_callback_update(user_id, chat_id, encode("rn", "go", "1"), bot=env.bot),
        make_text_update(user_id, chat_id, amount, message_id=2, update_id=2, bot=env.bot),
        make_text_update(user_id, chat_id, received_date, message_id=3, update_id=3, bot=env.bot),
        make_callback_update(user_id, chat_id, encode("mt", "bank"), message_id=4, update_id=4, bot=env.bot),
    ]
    run_updates(env, updates)


def test_flow_pending_to_confirm_card(make_app):
    env = make_app()
    _run_entry(env)
    conv = env.store.get_conversation(OWNER_ID, OWNER_ID)
    assert conv["state"] == "rent_confirm"
    payload = conv["payload"]
    assert payload["amount"] == "55000.00"
    assert payload["received_date"] == date.today().isoformat()
    assert payload["method"] == "Bank"
    confirm_text = env.bot.edits()[-1]["text"]
    assert "确认收租" in confirm_text
    assert "金额：<b>₱55,000</b>" in confirm_text
    assert "方式：Bank" in confirm_text


def test_flow_default_amount_and_date(make_app):
    env = make_app()
    _run_entry(env, amount="默认", received_date="默认")
    payload = env.store.get_conversation(OWNER_ID, OWNER_ID)["payload"]
    assert payload["amount"] == "55000.00"  # monthly rent default
    assert payload["received_date"] == date.today().isoformat()


def test_amount_invalid_reasks(make_app):
    env = make_app()
    updates = [
        make_callback_update(OWNER_ID, OWNER_ID, encode("rn", "go", "1"), bot=env.bot),
        make_text_update(OWNER_ID, OWNER_ID, "abc", message_id=2, update_id=2, bot=env.bot),
    ]
    run_updates(env, updates)
    assert env.store.get_conversation(OWNER_ID, OWNER_ID)["state"] == "rent_amount"
    assert "金额无效" in env.bot.sends()[-1]["text"]


def test_negative_and_oversized_amount_rejected(make_app):
    env = make_app()
    updates = [
        make_callback_update(OWNER_ID, OWNER_ID, encode("rn", "go", "1"), bot=env.bot),
        make_text_update(OWNER_ID, OWNER_ID, "-5", message_id=2, update_id=2, bot=env.bot),
        make_text_update(OWNER_ID, OWNER_ID, "99999999999999.99", message_id=3, update_id=3, bot=env.bot),
        make_text_update(OWNER_ID, OWNER_ID, "55000.123", message_id=4, update_id=4, bot=env.bot),
    ]
    run_updates(env, updates)
    assert env.store.get_conversation(OWNER_ID, OWNER_ID)["state"] == "rent_amount"


def test_date_invalid_reasks(make_app):
    env = make_app()
    updates = [
        make_callback_update(OWNER_ID, OWNER_ID, encode("rn", "go", "1"), bot=env.bot),
        make_text_update(OWNER_ID, OWNER_ID, "55000", message_id=2, update_id=2, bot=env.bot),
        make_text_update(OWNER_ID, OWNER_ID, "not-a-date", message_id=3, update_id=3, bot=env.bot),
    ]
    run_updates(env, updates)
    assert env.store.get_conversation(OWNER_ID, OWNER_ID)["state"] == "rent_date"
    assert "日期格式无效" in env.bot.sends()[-1]["text"]


def test_cancel_during_conversation(make_app):
    env = make_app()
    updates = [
        make_callback_update(OWNER_ID, OWNER_ID, encode("rn", "go", "1"), bot=env.bot),
        make_text_update(OWNER_ID, OWNER_ID, "取消", message_id=2, update_id=2, bot=env.bot),
    ]
    run_updates(env, updates)
    assert env.store.get_conversation(OWNER_ID, OWNER_ID) is None
    assert "已取消" in env.bot.sends()[-1]["text"]


def test_cancel_command_during_conversation(make_app):
    env = make_app()
    updates = [
        make_callback_update(OWNER_ID, OWNER_ID, encode("rn", "go", "1"), bot=env.bot),
        make_text_update(OWNER_ID, OWNER_ID, "/cancel", message_id=2, update_id=2, bot=env.bot),
    ]
    run_updates(env, updates)
    assert env.store.get_conversation(OWNER_ID, OWNER_ID) is None


def test_conversation_expired_falls_back_to_nl(make_app):
    env = make_app()
    env.store.save_conversation(OWNER_ID, OWNER_ID, "rent_amount",
                                {"unit_id": 1}, ttl_seconds=-1)
    run_updates(
        env,
        [make_text_update(OWNER_ID, OWNER_ID, "55000", message_id=5, update_id=5, bot=env.bot)],
    )
    # expired conversation -> treated as free text -> unknown fallback
    assert "请用下方按钮" in env.bot.sends()[-1]["text"]


def test_method_invalid_entity_rejected(make_app):
    env = make_app()
    env.store.save_conversation(OWNER_ID, OWNER_ID, "rent_method",
                                {"amount": "55000.00"})
    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, encode("mt", "bogus"), bot=env.bot)],
    )
    assert env.store.get_conversation(OWNER_ID, OWNER_ID)["state"] == "rent_method"
    assert env.bot.last_answer()["text"] == "已过期"  # invalid -> common.invalid
