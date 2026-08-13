"""BOT-V1-USABLE-001 P0-5: AI fallback lane (structured intent, never /help)."""
from __future__ import annotations

from conftest import OWNER_ID, SECRETARY_ID, make_callback_update, make_text_update, run_updates
from pasay_bot.keyboards import decode, encode, new_nonce, now_ts


def _add_1680(env):
    """Test fixture: a unit the AI lane can resolve (DEV-BAY-1680)."""
    env.backend.units.append(
        {
            "id": 7, "property_id": 1, "unit_number": "DEV-BAY-1680",
            "floor": "16", "size_sqm": "45.00", "monthly_rent": "70000.00",
            "status": "occupied", "is_active": True,
        }
    )
    env.backend.leases.append(
        {
            "id": 9, "unit_id": 7, "tenant_id": 2, "start_date": "2026-03-01",
            "end_date": "2026-12-31", "accounting_start_date": None,
            "monthly_rent": "70000.00", "deposit": "140000.00",
            "status": "active", "due_day": 5, "notes": None,
        }
    )


def _buttons(kb):
    if kb is None:
        return []
    return [b for row in kb.inline_keyboard for b in row]


def _labels(kb):
    return [b.text for b in _buttons(kb)]


def _parse_payload(intent, **kw):
    payload = {
        "intent": intent,
        "message": "",
        "unit": "",
        "unit_id": None,
        "amount": None,
        "category": "",
        "month": "",
        "missing": [],
        "options": [],
        "provider": "fake",
        "model": "fake",
        "fallback": False,
    }
    payload.update(kw)
    return payload


def test_ai_create_expense_missing_amount_asks_then_confirms(make_app):
    """★ '帮我记一笔1680的水费' -> AI says create_expense (no amount) -> bot
    asks once -> user replies 2500 -> deterministic confirm card."""
    env = make_app()
    _add_1680(env)
    env.backend.nl_parse_payload = _parse_payload(
        "create_expense", unit="1680", unit_id=7, category="水费",
        missing=["amount"], message="知道了，是 1680 的水费。金额是多少？",
    )
    run_updates(
        env,
        [make_text_update(OWNER_ID, OWNER_ID, "帮我记一笔1680的水费", bot=env.bot)],
    )
    ask = env.bot.last_edit()["text"]
    assert "1680" in ask
    assert "金额是多少" in ask
    conv = env.store.get_conversation(OWNER_ID, OWNER_ID)
    assert conv is not None and conv["state"] == "ai_expense_partial"

    run_updates(
        env,
        [make_text_update(OWNER_ID, OWNER_ID, "2500", message_id=3, update_id=3, bot=env.bot)],
    )
    card = env.bot.last_send()["text"]
    assert "确认支出" in card
    assert "水费" in card
    assert "₱2,500" in card
    assert "/help" not in card


def test_ai_create_expense_complete_renders_confirm(make_app):
    """★ AI parses unit+category+amount -> deterministic confirm card directly."""
    env = make_app()
    _add_1680(env)
    env.backend.nl_parse_payload = _parse_payload(
        "create_expense", unit="1680", unit_id=7, category="电费",
        amount="3800", missing=[],
    )
    run_updates(
        env,
        [make_text_update(OWNER_ID, OWNER_ID, "帮我记一笔1680电费", bot=env.bot)],
    )
    card = env.bot.last_edit()["text"]
    assert "确认支出" in card
    assert "电费" in card
    assert "₱3,800" in card


def test_ai_ambiguous_unit_category_shows_two_choices(make_app):
    """★ '1680水费' (genuinely ambiguous) -> 2 explicit choices; both taps are
    deterministic (record -> ask amount / query -> expense history)."""
    env = make_app()
    _add_1680(env)
    env.backend.add_expense(expense_id=5, category="水费", amount="2500.00",
                            payee="Utility", unit_id=7)
    env.backend.nl_parse_payload = _parse_payload(
        "ambiguous", unit="1680", unit_id=7, category="水费", options=["记录水费", "查询1680记录"],
    )
    run_updates(
        env,
        [make_text_update(OWNER_ID, OWNER_ID, "1680水费", bot=env.bot)],
    )
    choice = env.bot.last_send()
    labels = _labels(choice["reply_markup"])
    assert "记录水费" in labels
    assert "查询1680记录" in labels
    assert "/help" not in choice["text"]
    conv = env.store.get_conversation(OWNER_ID, OWNER_ID)
    assert conv is not None and conv["state"] == "ai_choice"
    nonce = conv["nonce"]

    # tap [记录水费] -> asks the amount (deterministic)
    run_updates(
        env,
        [make_callback_update(
            OWNER_ID, OWNER_ID, encode("aic", "ai", "0", nonce=nonce, ts=now_ts()),
            bot=env.bot,
        )],
    )
    assert "金额是多少" in env.bot.last_edit()["text"]

    # a fresh ambiguous card -> tap [查询1680记录] -> unit expense history
    env.store.delete_conversation(OWNER_ID, OWNER_ID)
    env.backend.nl_parse_payload = _parse_payload(
        "ambiguous", unit="1680", unit_id=7, category="水费", options=["记录水费", "查询1680记录"],
    )
    run_updates(
        env,
        [make_text_update(OWNER_ID, OWNER_ID, "1680水费", message_id=5, update_id=5, bot=env.bot)],
    )
    conv = env.store.get_conversation(OWNER_ID, OWNER_ID)
    nonce = conv["nonce"]
    run_updates(
        env,
        [make_callback_update(
            OWNER_ID, OWNER_ID, encode("aic", "ai", "1", nonce=nonce, ts=now_ts()),
            update_id=6, bot=env.bot,
        )],
    )
    assert "最近支出" in env.bot.last_edit()["text"]
    assert "水费" in env.bot.last_edit()["text"]


def test_ai_create_income_routes_to_deterministic_matcher(make_app):
    """★ AI confirms create_income -> the existing deterministic matcher
    resolves the receivable (never AI-selected fields)."""
    env = make_app()
    _add_1680(env)
    env.backend.nl_parse_payload = _parse_payload("create_income", unit="1680")
    run_updates(
        env,
        [make_text_update(OWNER_ID, OWNER_ID, "帮我把1680的房租记上", bot=env.bot)],
    )
    assert env.backend.count_calls("POST", "/payments/match") == 1
    texts = "".join(env.bot.all_texts())
    assert "/help" not in texts
    assert "找到对应租金" in texts or "部分付款" in texts or "租金" in texts


def test_ai_query_answers_via_copilot(make_app):
    """★ AI says query -> grounded read-only copilot answer (no menu)."""
    env = make_app()
    env.backend.nl_parse_payload = _parse_payload("query")
    run_updates(
        env,
        [make_text_update(OWNER_ID, OWNER_ID, "帮我分析一下这个月的运营情况", bot=env.bot)],
    )
    text = env.bot.last_edit()["text"]
    assert "已收到 ₱190,000" in text
    assert "/help" not in text


def test_ai_down_never_help(make_app):
    """★ Provider down -> friendly understandable reply; never /help."""
    env = make_app()
    env.backend.nl_parse_status = 500
    run_updates(
        env,
        [make_text_update(OWNER_ID, OWNER_ID, "今天天气怎么样帮我看看租务", bot=env.bot)],
    )
    text = env.bot.last_edit()["text"]
    assert "我还没完全理解" in text
    assert "/help" not in "".join(env.bot.all_texts())


def test_ai_fallback_never_called_for_fixed_menu_or_statements(make_app):
    """★ Fixed buttons and deterministic statements never call the AI lane."""
    env = make_app()
    env.backend.nl_parse_status = 500  # would fail loudly if called
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "💰 收租", bot=env.bot)])
    run_updates(
        env,
        [make_text_update(OWNER_ID, OWNER_ID, "支出16B水费2500", message_id=2, update_id=2, bot=env.bot)],
    )
    assert env.backend.count_calls("POST", "/operations/copilot/nl-parse") == 0


def test_expense_ambiguity_is_deterministic_no_llm(make_app):
    """★ '1680水费' -> deterministic record-vs-query choices (no LLM call)."""
    env = make_app()
    _add_1680(env)
    env.backend.nl_parse_status = 500  # would fail loudly if called
    run_updates(
        env,
        [make_text_update(OWNER_ID, OWNER_ID, "1680水费", bot=env.bot)],
    )
    assert env.backend.count_calls("POST", "/operations/copilot/nl-parse") == 0
    choice = env.bot.last_send()
    labels = _labels(choice["reply_markup"])
    assert "记录水费" in labels
    assert "查询1680记录" in labels
    conv = env.store.get_conversation(OWNER_ID, OWNER_ID)
    assert conv is not None and conv["state"] == "ai_choice"
