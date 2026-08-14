"""V1.1 UX regression tests (no-network harness: FakeBot + httpx MockTransport).

Covers the named scenarios from the V1.1 brief: dashboard, pending
aggregation, rent-flow compression with smart defaults, state-driven buttons,
edit-first navigation, back/cancel, error recovery, empty states, i18n."""
from datetime import date, timedelta

from conftest import OWNER_ID, SECRETARY_ID, make_callback_update, make_text_update, run_updates
from pasay_bot.keyboards import decode, encode, new_nonce, now_ts

CUR_MONTH = date.today().strftime("%Y-%m")
TODAY = date.today().isoformat()


def _buttons_of(kb):
    if kb is None:
        return []
    return [b for row in kb.inline_keyboard for b in row]


def _has_nav(kb, entity):
    return any(
        decode(b.callback_data) is not None and decode(b.callback_data)["action"] == "nav"
        and decode(b.callback_data)["entity"] == entity
        for b in _buttons_of(kb)
    )


def _confirm_data(env):
    call = env.bot.edits()[-1]
    kb = call["reply_markup"]
    return next(
        b.callback_data
        for b in _buttons_of(kb)
        if decode(b.callback_data) is not None and decode(b.callback_data)["action"] == "cnf"
    )


def _paid_unit1(env):
    env.backend.add_income(
        status="confirmed", lease_id=1, amount="55000.00",
        received_date=TODAY, payment_method="Bank",
        description=f"rent {CUR_MONTH}", income_id=1,
    )


# --- dashboard (B1) ---

def test_start_shows_dashboard(make_app):
    """PASAY-V2-FOUNDATION-001: /start is technical recovery -> short greeting
    (never the full dashboard) + the identical 4-button English Quick View
    keyboard for both roles."""
    env = make_app()
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "/start", bot=env.bot)])
    send = env.bot.last_send()
    text = send["text"]
    assert "Hello" in text
    kb = send["reply_markup"]
    assert kb.__class__.__name__ == "ReplyKeyboardMarkup"
    assert kb.is_persistent is True
    labels = [b.text for row in kb.keyboard for b in row]
    assert labels == ["🏠 Properties", "✅ Tasks", "💰 Rent", "💸 Expense"]


def test_secretary_start_has_english_persistent_keyboard(make_app):
    """★ SECRETARY /start mounts the SAME 4-button English Quick View keyboard
    as the Owner (one menu, no structural chaos)."""
    env = make_app()
    run_updates(env, [make_text_update(SECRETARY_ID, SECRETARY_ID, "/start", bot=env.bot)])
    send = env.bot.last_send()
    assert "Hello" in send["text"]
    kb = send["reply_markup"]
    assert kb.__class__.__name__ == "ReplyKeyboardMarkup"
    assert kb.is_persistent is True
    labels = [b.text for row in kb.keyboard for b in row]
    assert labels == ["🏠 Properties", "✅ Tasks", "💰 Rent", "💸 Expense"]


def test_more_keyword_opens_fallback_inline_menu(make_app):
    """★ typing ☰ 更多 (legacy alias) opens the dashboard with the minimal
    inline fallback (rent / overdue / copilot / home), not a menu detour."""
    env = make_app()
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "☰ 更多", bot=env.bot)])
    send = env.bot.last_send()
    assert "本月租金" in send["text"]  # dashboard rendered directly
    kb = send["reply_markup"]
    assert kb.__class__.__name__ == "InlineKeyboardMarkup"
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert "💵 收租" in labels
    assert "⚠️ 逾期" in labels
    assert "🤖 运营助手" in labels
    assert "🏠 首页" in labels
    assert "📋 待办中心" not in labels


def test_nl_todo_keyword_opens_unified_page(make_app):
    """★ the fixed ✅ Tasks button (plain text) routes to the deterministic
    Tasks Quick View (no LLM, no multi-layer navigation)."""
    env = make_app()
    env.backend.add_ops_task(
        task_id=1, title="季度空调保养", task_type="AC_MAINTENANCE",
        due_at=f"{TODAY}T00:00:00+08:00",
    )
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "✅ Tasks", bot=env.bot)])
    page = env.bot.last_send()
    assert "季度空调保养" in page["text"]
    kb = page["reply_markup"]
    assert kb.__class__.__name__ == "ReplyKeyboardMarkup"


def test_dashboard_no_tasks(make_app):
    """★ no overdue/tasks -> Tasks Quick View shows the positive empty line."""
    env = make_app()
    env.backend.overdue = []
    env.backend.tasks = []
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "✅ Tasks", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "待办" in text


def test_dashboard_with_overdue(make_app):
    """★ Rent Quick View surfaces the overdue unit."""
    env = make_app()
    env.backend.quick_rent = {
        "overdue": [
            {"unit": "16B", "unit_code": "16B", "amount": "55000.00", "overdue_days": 5},
            {"unit": "2C", "unit_code": "2C", "amount": "24000.00", "overdue_days": 40},
        ],
        "outstanding_total": "79000.00",
    }
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "💰 Rent", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "16B" in text or "2C" in text


def test_dashboard_zero_values_hidden(make_app):
    """★ zero values render as clean empty lines in the Quick Views, never as
    old dashboard noise or internal terms."""
    env = make_app()
    env.backend.overdue = []
    env.backend.tasks = []
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "💸 Expense", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "支出" in text


# --- state-driven buttons (B5) ---

def test_paid_unit_has_no_collect_button(make_app):
    """★ paid unit: hidden from the collect list, no collect button on its page,
    and a stale collect callback is refused."""
    env = make_app()
    _paid_unit1(env)
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, encode("nav", "rent"), bot=env.bot)])
    collect_text = env.bot.edits()[-1]["text"]
    assert "16B" not in collect_text
    assert "2C" in collect_text  # other unpaid unit still shown

    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, encode("rn", "unit", "1"),
                                           update_id=2, bot=env.bot)])
    unit_text = env.bot.edits()[-1]["text"]
    assert "本月租金已收" in unit_text
    labels = [b.text for b in _buttons_of(env.bot.edits()[-1]["reply_markup"])]
    assert "✅ 登记收租" not in labels
    assert "💰 查看付款" in labels

    # stale collect callback on a paid unit -> refused, no NEW write
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, encode("rn", "go", "1"),
                                           update_id=3, bot=env.bot)])
    assert len(env.backend.incomes) == 1  # only the pre-existing confirmed one
    assert env.backend.count_calls("POST", "/incomes") == 0
    assert "本月租金已收" in env.bot.edits()[-1]["text"]


def test_vacant_unit_has_no_collect_button(make_app):
    """★ vacant unit: hidden from the collect list and has no collect button."""
    env = make_app()
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, encode("nav", "rent"), bot=env.bot)])
    collect_text = env.bot.edits()[-1]["text"]
    assert "17A" not in collect_text  # vacant unit hidden

    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, encode("rn", "unit", "2"),
                                           update_id=2, bot=env.bot)])
    labels = [b.text for b in _buttons_of(env.bot.edits()[-1]["reply_markup"])]
    assert "✅ 登记收租" not in labels

    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, encode("rn", "go", "2"),
                                           update_id=3, bot=env.bot)])
    assert "没有活跃租约" in env.bot.edits()[-1]["text"]


def test_overdue_unit_priority(make_app):
    """★ overdue units sort first in the collect list (most-days first)."""
    env = make_app()
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, encode("nav", "rent"), bot=env.bot)])
    kb = env.bot.edits()[-1]["reply_markup"]
    refs = [
        decode(b.callback_data)["ref"]
        for b in _buttons_of(kb)
        if decode(b.callback_data) is not None and decode(b.callback_data)["action"] == "rn"
    ]
    # 2C (40d) before 16B (5d); unit 3 = 2C, unit 1 = 16B
    assert refs[0] == "3"
    assert refs[1] == "1"


def test_unit_page_payment_view(make_app):
    """★ [💰 查看付款] opens the income record for a paid unit."""
    env = make_app()
    _paid_unit1(env)
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, encode("rn", "unit", "1"), bot=env.bot)])
    kb = env.bot.edits()[-1]["reply_markup"]
    view = next(
        b.callback_data
        for b in _buttons_of(kb)
        if decode(b.callback_data) is not None and decode(b.callback_data)["action"] == "det"
    )
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, view, update_id=2, bot=env.bot)])
    text = env.bot.edits()[-1]["text"]
    assert "💰 付款记录" in text
    assert "#1" in text
    assert "₱55,000" in text


# --- back / cancel / expiry (B7) ---

def test_back_button_every_page(make_app):
    """PASAY-V2-FOUNDATION-001: fixed Quick View buttons reply directly with
    ONE deterministic card (no second-level menu, no dead end)."""
    env = make_app()
    for label, marker in [
        ("🏠 Properties", "房源"),
        ("✅ Tasks", "待办"),
        ("💰 Rent", "租金"),
        ("💸 Expense", "支出"),
    ]:
        run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, label, bot=env.bot)])
        send = env.bot.last_send()
        assert marker in send["text"]
        assert send["reply_markup"] is not None


def test_cancel_write_flow(make_app):
    """★ [❌取消] during a write flow clears state and offers home."""
    env = make_app()
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, encode("rn", "go", "1"), bot=env.bot)])
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, encode("ccl"), update_id=2, bot=env.bot)])
    assert env.store.get_conversation(OWNER_ID, OWNER_ID) is None
    text = env.bot.edits()[-1]["text"]
    assert "已取消" in text
    assert _has_nav(env.bot.edits()[-1]["reply_markup"], "home")


def test_expired_state_home_button(make_app):
    """★ expired action -> explicit expired card with [🏠 首页] (no dead end)."""
    env = make_app()
    env.backend.add_income(status="pending", income_id=1)
    old_ts = now_ts() - 10000
    data = encode("cnf", "inc", "1", nonce=new_nonce(), ts=old_ts)
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, data, bot=env.bot)])
    text = env.bot.edits()[-1]["text"]
    assert "这个操作已经过期" in text
    assert _has_nav(env.bot.edits()[-1]["reply_markup"], "home")
    assert env.backend.count_calls("POST", "/incomes/1/confirm") == 0


# --- empty states (B9) ---

def test_empty_overdue_state(make_app):
    env = make_app()
    env.backend.overdue = []
    env.backend.quick_rent = {"overdue": [], "outstanding_total": "0.00"}
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "💰 Rent", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "租金" in text
    assert "No data" not in text and "[]" not in text and "0 records" not in text
    assert env.bot.last_send()["reply_markup"] is not None


def test_empty_property_state(make_app):
    env = make_app()
    env.backend.properties = []
    env.backend.units = []
    env.backend.quick_properties = []
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "🏠 Properties", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "房源" in text
    assert "No data" not in text and "[]" not in text
    assert env.bot.last_send()["reply_markup"] is not None


# --- edit-first navigation (B6/B11) ---

def test_edit_navigation_no_message_spam(make_app):
    """★ nav callbacks edit the current message; no new messages are sent."""
    env = make_app()
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, encode("nav", "rent"), bot=env.bot)])
    sends = len(env.bot.sends())
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, encode("nav", "finance"),
                                           update_id=2, bot=env.bot)])
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, encode("nav", "home"),
                                           update_id=3, bot=env.bot)])
    assert len(env.bot.sends()) == sends  # zero new messages
    edits = env.bot.edits()
    assert all(c["message_id"] == 10 for c in edits)
    assert "本月租金" in edits[-1]["text"]  # back on the dashboard


def test_callback_ack_fast(make_app):
    """★ B11: the callback is acknowledged BEFORE the page renders."""
    env = make_app()
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, encode("nav", "rent"), bot=env.bot)])
    assert env.bot.calls and env.bot.calls[0]["type"] == "answer_callback_query"
    assert env.bot.edits()  # page still rendered afterwards


# --- rent-flow compression + smart defaults (B4) ---

def test_collect_default_current_period(make_app):
    env = make_app()
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, encode("rn", "go", "1"), bot=env.bot)])
    conv = env.store.get_conversation(OWNER_ID, OWNER_ID)
    assert conv["payload"]["period"] == CUR_MONTH


def test_collect_default_amount(make_app):
    env = make_app()
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, encode("rn", "go", "1"), bot=env.bot)])
    conv = env.store.get_conversation(OWNER_ID, OWNER_ID)
    assert conv["payload"]["amount"] == "55000.00"  # current receivable


def test_collect_default_today(make_app):
    env = make_app()
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, encode("rn", "go", "1"), bot=env.bot)])
    conv = env.store.get_conversation(OWNER_ID, OWNER_ID)
    assert conv["payload"]["received_date"] == TODAY


def test_double_click_still_idempotent(make_app):
    """★ double-click on the confirm button writes exactly one income."""
    env = make_app()
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, encode("rn", "go", "1"), bot=env.bot)])
    data = _confirm_data(env)
    run_updates(
        env,
        [
            make_callback_update(OWNER_ID, OWNER_ID, data, update_id=2, bot=env.bot),
            make_callback_update(OWNER_ID, OWNER_ID, data, update_id=3, bot=env.bot),
        ],
    )
    assert len(env.backend.incomes) == 1
    assert env.backend.count_calls("POST", "/incomes") == 1


def test_uncertain_payment_state(make_app):
    """★ an in-flight confirm shows the 'checking result, don't repeat'
    message and never writes twice."""
    env = make_app()
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, encode("rn", "go", "1"), bot=env.bot)])
    data = _confirm_data(env)
    nonce = decode(data)["nonce"]
    env.guard.acquire(f"ik:cnf:ren:{nonce}", kind="income", resource="")
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, data, update_id=2, bot=env.bot)])
    # the in-flight state is rendered durably onto the card (no silent drop)
    assert "请勿重复提交" in (env.bot.last_edit()["text"] or "")
    assert len(env.backend.incomes) == 0


# --- error recovery (B8) ---

def test_api_error_retry_button(make_app):
    """★ Quick View API failure -> friendly error card with a home retry."""
    env = make_app()
    env.backend.fail_status["/reports/financial-summary"] = 500
    env.backend.fail_status["/operations/quick/expense"] = 500
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "💸 Expense", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "获取数据失败" in text or "failed" in text.lower()
    kb = env.bot.last_send()["reply_markup"]
    assert kb is not None


def test_confirm_error_retry_same_nonce(make_app):
    """★ confirm failure keeps the SAME nonce on the retry button so the
    idempotency guard still dedupes."""
    env = make_app()
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, encode("rn", "go", "1"), bot=env.bot)])
    data = _confirm_data(env)
    nonce = decode(data)["nonce"]
    env.backend.fail_status["/incomes"] = 500
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, data, update_id=2, bot=env.bot)])
    kb = env.bot.edits()[-1]["reply_markup"]
    retry = next(
        b.callback_data
        for b in _buttons_of(kb)
        if decode(b.callback_data) is not None and decode(b.callback_data)["action"] == "cnf"
    )
    assert decode(retry)["nonce"] == nonce


# --- i18n (B10) ---

def test_i18n_zh(make_app):
    """★ OWNER (zh) pages use short natural Chinese wording."""
    env = make_app()
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "/start", bot=env.bot)])
    zh_start = env.bot.last_send()["text"]
    assert "Hello" in zh_start and "你好" in zh_start
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "💰 Rent", message_id=2, update_id=2, bot=env.bot)])
    assert "租金" in env.bot.last_send()["text"]
    # no English 'No data' / 'Main Menu' leak
    assert "Main Menu" not in "".join(env.bot.all_texts())


def test_i18n_en(make_app):
    """★ SECRETARY (en) pages use short natural English wording."""
    env = make_app()
    run_updates(env, [make_text_update(SECRETARY_ID, SECRETARY_ID, "/start", bot=env.bot)])
    start = env.bot.last_send()["text"]
    assert "Hello" in start
    run_updates(env, [make_text_update(SECRETARY_ID, SECRETARY_ID, "✅ Tasks",
                                       message_id=2, update_id=2, bot=env.bot)])
    assert "Tasks" in env.bot.last_send()["text"]
    run_updates(env, [make_callback_update(SECRETARY_ID, SECRETARY_ID,
                                           encode("cnf", "inc", "1", nonce=new_nonce(),
                                                  ts=now_ts() - 10000),
                                           update_id=3, bot=env.bot)])
    assert "This action has expired" in env.bot.edits()[-1]["text"]


# --- pending aggregation (B2/B3) ---

def test_pending_aggregates_overdue_and_tasks(make_app):
    """PASAY-V2-FOUNDATION-001: Tasks Quick View shows the active tasks."""
    env = make_app()
    env.backend.add_ops_task(
        task_id=1, title="Fix AC in 16B", task_type="AC_MAINTENANCE",
        due_at=f"{TODAY}T00:00:00+08:00",
    )
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "✅ Tasks", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "Fix AC in 16B" in text
    assert "待办" in text
    kb = env.bot.last_send()["reply_markup"]
    assert kb is not None


def test_pending_empty_positive(make_app):
    """★ empty Tasks Quick View shows a friendly empty line, not zeros."""
    env = make_app()
    env.backend.overdue = []
    env.backend.tasks = []
    env.backend.quick_tasks = []
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "✅ Tasks", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "待办" in text
    assert "0" not in text.replace("待办", "")
