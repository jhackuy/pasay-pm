"""Button determinism + latency acceptance (SLICE4-BUTTONS).

Hard rules under test:
- fixed bottom Reply Keyboard buttons exact-match and route deterministically
  BEFORE any NL/NLU/LLM path (proof: nl_bridge is monkeypatched to fail);
- inline callbacks produce exactly ONE Telegram answer per click (the first
  feedback / processing toast) and never fail silently - post-ack errors are
  rendered durably onto the tapped message;
- approve / reject / confirm mutations are idempotent, RBAC-guarded and give
  explicit human feedback on backend errors / expiry / unknown data;
- unknown callbacks fail loudly instead of being swallowed;
- handler latency instrumentation records code-path timings only (no LLM).
"""
from __future__ import annotations

import pytest

from conftest import (
    OWNER_ID,
    SECRETARY_ID,
    UNKNOWN_ID,
    make_callback_update,
    make_text_update,
    run_updates,
)

from pasay_bot.handlers import nl_bridge
from pasay_bot.keyboards import encode, new_nonce, now_ts
from pasay_bot.render.i18n import t


def _seed_expense(env, expense_id=5):
    return env.backend.add_expense(
        expense_id=expense_id, category="维修", amount="5000.00",
        payee="Fix-It Co", unit_id=1, receipt_attachment_id=7,
    )


def _reject_data(expense_id=5, ts=None):
    return encode("exr", str(expense_id), "", nonce=new_nonce(), ts=ts if ts is not None else now_ts())


def _approve_data(expense_id=5):
    return encode("exa", str(expense_id), "", nonce=new_nonce(), ts=now_ts())


def _confirm_income_data(income_id=1):
    return encode("cnf", "inc", str(income_id), nonce=new_nonce(), ts=now_ts())


def _answers(env):
    return [a["text"] or "" for a in env.bot.answers()]


def _latency(env):
    return env.app.bot_data["latency"]


# --- fixed bottom Reply Keyboard: exact routing -----------------------------

@pytest.mark.parametrize(
    ("user_id", "label", "marker"),
    [
        (OWNER_ID, "🏠 房源", "房源概况"),
        (OWNER_ID, "✅ 待办", "逾期租金"),
        (OWNER_ID, "💰 财务", "财务"),
        (OWNER_ID, "☰ 更多", "本月租金"),
        (SECRETARY_ID, "🏠 Properties", "Property Overview"),
        (SECRETARY_ID, "👥 Tenants", "Tenant status"),
        (SECRETARY_ID, "💵 Rent", "Select unpaid unit"),
        (SECRETARY_ID, "✅ Tasks", "Nothing to do"),
        (SECRETARY_ID, "🔧 Maintenance", "Maintenance jobs"),
        (SECRETARY_ID, "📋 Records", "Finance"),
        (SECRETARY_ID, "⚠️ Overdue", "Overdue Rent"),
    ],
)
def test_fixed_menu_button_exact_routes_without_nl(make_app, user_id, label, marker):
    env = make_app()
    run_updates(env, [make_text_update(user_id, user_id, label, bot=env.bot)])
    locale = "zh" if user_id == OWNER_ID else "en"
    if label in ("👥 Tenants", "🔧 Maintenance"):
        # local hint routes: sent directly, no backend page load
        assert marker in env.bot.last_send()["text"]
        return
    # backend-loading routes: durable "processing" message first, page then
    # rendered onto that same message (message mutation, no junk messages)
    assert t("common.working", locale) in env.bot.last_send()["text"]
    assert marker in env.bot.last_edit()["text"]


def test_fixed_menu_button_never_enters_nl_bridge(make_app, monkeypatch):
    """Proof test: nl_bridge.handle_nl raises; every fixed button must still
    render its deterministic page, so the button path provably never reaches
    NL/NLU/LLM processing."""
    calls = []

    async def boom(*args, **kwargs):
        calls.append(True)
        raise AssertionError("NL bridge must not run")

    monkeypatch.setattr(nl_bridge, "handle_nl", boom)
    env = make_app()
    for user_id, label, marker in [
        (OWNER_ID, "🏠 房源", "房源概况"),
        (OWNER_ID, "✅ 待办", "逾期租金"),
        (OWNER_ID, "💰 财务", "财务"),
        (OWNER_ID, "☰ 更多", "本月租金"),
        (SECRETARY_ID, "🏠 Properties", "Property Overview"),
        (SECRETARY_ID, "👥 Tenants", "Tenant status"),
        (SECRETARY_ID, "💵 Rent", "Select unpaid unit"),
        (SECRETARY_ID, "✅ Tasks", "Nothing to do"),
        (SECRETARY_ID, "🔧 Maintenance", "Maintenance jobs"),
        (SECRETARY_ID, "📋 Records", "Finance"),
        (SECRETARY_ID, "⚠️ Overdue", "Overdue Rent"),
    ]:
        run_updates(env, [make_text_update(user_id, user_id, label, bot=env.bot)])
        locale = "zh" if user_id == OWNER_ID else "en"
        if label in ("👥 Tenants", "🔧 Maintenance"):
            assert marker in env.bot.last_send()["text"]
        else:
            assert t("common.working", locale) in env.bot.last_send()["text"]
            assert marker in env.bot.last_edit()["text"]
    assert calls == []


@pytest.mark.parametrize(
    ("user_id", "label", "marker"),
    [
        (OWNER_ID, "\U0001f3e0 \u623f\u6e90", "\u623f\u6e90\u6982\u51b5"),
        (OWNER_ID, "\u2705 \u5f85\u529e", "\u903e\u671f\u79df\u91d1"),
        (OWNER_ID, "\U0001f4b0 \u8d22\u52a1", "\u8d22\u52a1"),
        (OWNER_ID, "\u2630 \u66f4\u591a", "\u672c\u6708\u79df\u91d1"),
        (SECRETARY_ID, "\U0001f3e0 Properties", "Property Overview"),
        (SECRETARY_ID, "\U0001f4b5 Rent", "Select unpaid unit"),
        (SECRETARY_ID, "\u2705 Tasks", "Nothing to do"),
        (SECRETARY_ID, "\U0001f4cb Records", "Finance"),
        (SECRETARY_ID, "\u26a0\ufe0f Overdue", "Overdue Rent"),
    ],
)
def test_fixed_menu_slow_routes_status_message_is_editable(
    make_app, user_id, label, marker
):
    """Live UX regression (OWNER-UX-FAILURE-LIVE-TRACE-001): the 'processing'
    status message must be sent WITHOUT a reply keyboard. Telegram rejects
    editMessageText on messages carrying a non-inline ReplyKeyboardMarkup
    (400 'Message can't be edited'); FakeBot now enforces that real semantic,
    so the old code leaves the user stuck on the status, while the fix renders
    the page in place onto the same message."""
    env = make_app()
    run_updates(env, [make_text_update(user_id, user_id, label, bot=env.bot)])
    sends = env.bot.sends()
    edits = env.bot.edits()
    assert len(sends) == 1  # exactly one durable status message, no junk
    assert sends[0]["reply_markup"] is None  # editable by Telegram
    assert edits and marker in edits[-1]["text"]


def test_finance_button_live_ux_failure_repro_edits_status_in_place(make_app):
    """Exact repro of the captured live failure: Owner taps '\U0001f4b0
    \u8d22\u52a1' and the finance page must be rendered onto the status
    message (never a stuck '\u23f3 \u5904\u7406\u4e2d\u2026')."""
    env = make_app()
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "\U0001f4b0 \u8d22\u52a1", bot=env.bot)])
    status = env.bot.sends()[0]
    assert status["text"] == t("common.working", "zh")
    assert status["reply_markup"] is None
    edit = env.bot.last_edit()
    assert edit["message_id"] == status["message_id"]  # same message mutated
    assert "\u8d22\u52a1" in edit["text"]


def test_free_text_still_reaches_nl_bridge(make_app, monkeypatch):
    """Regression: only fixed button labels are pre-routed; genuine free text
    still flows through the conversation/NL path unchanged."""
    calls = []

    async def spy(*args, **kwargs):
        calls.append(True)

    monkeypatch.setattr(nl_bridge, "handle_nl", spy)
    env = make_app()
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "今天天气怎么样", bot=env.bot)])
    assert calls == [True]


# --- inline callbacks: ack first, no silent failure -------------------------

def test_callback_ack_is_first_bot_call(make_app):
    env = make_app()
    _seed_expense(env)
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, _reject_data(), bot=env.bot)])
    assert env.bot.calls and env.bot.calls[0]["type"] == "answer_callback_query"


def test_callback_single_answer_per_click(make_app):
    """Telegram accepts exactly ONE answerCallbackQuery per click; the bot
    must never rely on a second answer for user-visible feedback."""
    env = make_app()
    _seed_expense(env)
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, _reject_data(), bot=env.bot)])
    assert len(env.bot.answers()) == 1
    assert "处理中" in (env.bot.answers()[0]["text"] or "")
    assert "已拒绝" in env.bot.last_edit()["text"]


def test_reject_mutates_card_and_writes_once(make_app):
    env = make_app()
    _seed_expense(env)
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, _reject_data(), bot=env.bot)])
    assert env.backend.count_calls("POST", "/expenses/5/reject") == 1
    assert env.backend._get_expense(5)["status"] == "rejected"
    edit = env.bot.last_edit()
    assert "已拒绝" in edit["text"]
    assert len(env.bot.sends()) == 0  # message mutation, no junk message


def test_approve_mutates_card_and_writes_once(make_app):
    env = make_app()
    _seed_expense(env)
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, _approve_data(), bot=env.bot)])
    assert env.backend.count_calls("POST", "/expenses/5/approve") == 1
    assert env.backend._get_expense(5)["status"] == "approved"
    assert "已批准" in env.bot.last_edit()["text"]
    assert len(env.bot.answers()) == 1
    assert "处理中" in (env.bot.answers()[0]["text"] or "")


def test_reject_expired_card_shows_visible_state(make_app):
    env = make_app(callback_ttl=900)
    _seed_expense(env)
    old_ts = now_ts() - 901
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, _reject_data(ts=old_ts), bot=env.bot)])
    # expiry is the FIRST answer (explicit toast) AND the card changes too
    assert len(env.bot.answers()) == 1
    assert "过期" in env.bot.last_answer()["text"]
    edit = env.bot.last_edit()
    assert "过期" in edit["text"]
    assert env.backend.count_calls("POST", "/expenses/5/reject") == 0


def test_reject_backend_error_gives_explicit_feedback(make_app):
    env = make_app()
    _seed_expense(env)
    env.backend.fail_status["/expenses/5/reject"] = 500
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, _reject_data(), bot=env.bot)])
    # single answer (processing ack) + durable error ON the card: never silent
    assert len(env.bot.answers()) == 1
    assert "处理中" in (env.bot.answers()[0]["text"] or "")
    assert "forced 500" in env.bot.last_edit()["text"]
    assert len(env.bot.sends()) == 0
    assert env.backend._get_expense(5)["status"] == "pending"


def test_reject_unexpected_exception_fail_closed(make_app):
    env = make_app()
    _seed_expense(env)
    env.backend.raise_on_paths.add("/expenses/5/reject")
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, _reject_data(), bot=env.bot)])
    # an unhandled exception still yields a human-visible reply
    assert t("common.unexpected", "zh") in env.bot.last_edit()["text"]


def test_reject_unknown_callback_fails_loudly(make_app):
    env = make_app()
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, "v1:zzz:1", bot=env.bot)])
    assert env.bot.last_answer()["text"] == t("common.invalid", "zh")


def test_reject_non_owner_refused_with_explicit_toast(make_app):
    env = make_app()
    _seed_expense(env)
    run_updates(env, [make_callback_update(SECRETARY_ID, SECRETARY_ID, _reject_data(), bot=env.bot)])
    assert env.bot.last_answer()["text"] == t("expense.owner_only", "en")
    assert env.backend.count_calls("POST", "/expenses/5/reject") == 0
    assert env.backend._get_expense(5)["status"] == "pending"


def test_reject_already_processed_no_second_write(make_app):
    env = make_app()
    _seed_expense(env)
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, _reject_data(), bot=env.bot)])
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, _reject_data(), update_id=7, bot=env.bot)])
    assert env.backend.count_calls("POST", "/expenses/5/reject") == 1
    # the tapped card is re-rendered to the current (rejected) state
    assert "已拒绝" in env.bot.last_edit()["text"]
    # exactly one answer per click, both carrying the processing ack
    assert len(env.bot.answers()) == 2
    assert all("处理中" in (a["text"] or "") for a in env.bot.answers())


def test_slow_backend_shows_processing_before_result(make_app):
    env = make_app()
    _seed_expense(env)
    env.backend.delay_seconds = 0.15
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, _reject_data(), bot=env.bot)])
    # one answer carries the processing status; the result arrives as a
    # durable card edit (real Telegram drops any second answer)
    answers = _answers(env)
    assert answers == [t("common.working", "zh")]
    assert "已拒绝" in env.bot.last_edit()["text"]


def test_confirm_callback_ack_first_and_writes_once(make_app):
    env = make_app()
    env.backend.add_income(status="pending", lease_id=1, amount="55000.00")
    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, _confirm_income_data(1), bot=env.bot)],
    )
    assert env.bot.calls[0]["type"] == "answer_callback_query"
    assert env.backend.count_calls("POST", "/incomes/1/confirm") == 1
    assert env.backend._get_income(1)["status"] == "confirmed"
    assert _answers(env) == [t("common.working", "zh")]
    assert "收租成功" in env.bot.last_edit()["text"]


def test_inline_callback_never_enters_nl_bridge(make_app, monkeypatch):
    """Inline buttons are UI commands, never NL: reject/confirm/home still
    work while nl_bridge is forced to fail."""
    calls = []

    async def boom(*args, **kwargs):
        calls.append(True)
        raise AssertionError("NL bridge must not run for a button")

    monkeypatch.setattr(nl_bridge, "handle_nl", boom)
    env = make_app()
    _seed_expense(env)
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, _reject_data(), bot=env.bot)])
    assert env.backend._get_expense(5)["status"] == "rejected"
    assert calls == []


def test_home_button_returns_to_dashboard(make_app):
    env = make_app()
    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, encode("nav", "home"), bot=env.bot)],
    )
    assert "本月" in env.bot.last_edit()["text"]
    assert len(env.bot.answers()) == 1
    assert "处理中" in (env.bot.answers()[0]["text"] or "")


def test_retry_button_reloads_same_page(make_app):
    """error_keyboard's [重试] maps to the same deterministic nav route."""
    env = make_app()
    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, encode("nav", "properties"), bot=env.bot)],
    )
    assert "房源概况" in env.bot.last_edit()["text"]
    assert len(env.bot.answers()) == 1


# --- latency instrumentation (code-path only, no LLM) -----------------------

def test_callback_latency_recorded(make_app):
    env = make_app()
    _seed_expense(env)
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, _reject_data(), bot=env.bot)])
    sample = _latency(env).last("callback")
    assert sample is not None
    assert sample["label"] == "exr"
    assert isinstance(sample["elapsed_ms"], (int, float))
    assert sample["elapsed_ms"] >= 0


def test_menu_button_latency_recorded(make_app):
    env = make_app()
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "🏠 房源", bot=env.bot)])
    sample = _latency(env).last("menu_button")
    assert sample is not None
    assert sample["label"] == "properties"
    assert isinstance(sample["elapsed_ms"], (int, float))


def test_latency_sample_has_no_llm_fields(make_app):
    env = make_app()
    _seed_expense(env)
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, _reject_data(), bot=env.bot)])
    sample = _latency(env).last("callback")
    allowed = {"kind", "label", "elapsed_ms", "outcome", "detail", "ts"}
    assert set(sample) == allowed
    for banned in ("model", "llm", "prompt", "tokens", "provider", "nlu"):
        assert banned not in sample["label"]
        assert banned not in str(sample["detail"]).lower()
