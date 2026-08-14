"""End-to-end handler tests through PTB's no-network Application:
rent flow, confirm, duplicate/double clicks, expiry, invalid callbacks,
permissions + bypass, timeout reconciliation, crash recovery, pending list,
and dual-key (manager/admin) enforcement."""
import asyncio
import logging
import re
import time
from datetime import date

import pytest

from conftest import (
    OWNER_ID,
    SECRETARY_ID,
    UNKNOWN_ID,
    FakeBot,
    make_callback_update,
    make_text_update,
    run_updates,
)
from telegram.error import BadRequest
from pasay_bot.handlers import callback as callback_handlers
from pasay_bot.keyboards import decode, encode, new_nonce, now_ts


def _html_balanced(text: str) -> bool:
    stack = []
    for m in re.finditer(r"</?([a-zA-Z][a-zA-Z0-9-]*)[^>]*>", text):
        raw, name = m.group(0), m.group(1).lower()
        if raw.startswith("</"):
            if not stack or stack[-1] != name:
                return False
            stack.pop()
        elif not raw.endswith("/>"):
            stack.append(name)
    return not stack


def _confirm_card_data(env):
    """callback_data of the confirm button on the rendered confirmation card."""
    return callback_data_of(env, "edit_message_text", -1)


class _NoopEditBot(FakeBot):
    """edit_message_text raises Telegram's 'Message is not modified'
    BadRequest when re-rendering identical text for the same message."""

    def __init__(self):
        super().__init__()
        self._text_by_message = {}

    async def edit_message_text(self, text=None, chat_id=None, message_id=None,
                                parse_mode=None, reply_markup=None, **kw):
        prev = self._text_by_message.get(message_id)
        self._text_by_message[message_id] = text
        if prev is not None and prev == text:
            self.calls.append({
                "type": "edit_message_text",
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": parse_mode,
                "reply_markup": reply_markup,
            })
            raise BadRequest("Message is not modified")
        return await super().edit_message_text(
            text=text, chat_id=chat_id, message_id=message_id,
            parse_mode=parse_mode, reply_markup=reply_markup, **kw,
        )


def callback_data_of(env, call_type, index=-1):
    call = env.bot.of_type(call_type)[index]
    return call["reply_markup"].inline_keyboard[0][0].callback_data


def _run_rent_to_confirm(env, user_id=OWNER_ID, chat_id=OWNER_ID):
    """V1.1 compressed flow: pick unpaid unit -> confirmation card with
    smart defaults (amount = monthly rent, date = today, method = default)."""
    updates = [
        make_callback_update(user_id, chat_id, encode("rn", "go", "1"), bot=env.bot),
    ]
    run_updates(env, updates)
    return _confirm_card_data(env)


# --- navigation / rent flow ---

def test_rent_callback(make_app):
    """★ B4/B5: [💵 收租] shows the unpaid-unit collect list — paid and vacant
    units hidden, overdue units first; selecting a unit opens the confirmation
    card with smart defaults."""
    env = make_app()
    updates = [
        make_callback_update(OWNER_ID, OWNER_ID, encode("nav", "rent"), bot=env.bot),
        make_callback_update(OWNER_ID, OWNER_ID, encode("rn", "go", "1"),
                             message_id=20, update_id=2, bot=env.bot),
    ]
    run_updates(env, updates)
    collect_text = env.bot.edits()[0]["text"]
    assert "选择未付款 Unit" in collect_text
    # 2C (40d overdue) sorts before 16B (5d); vacant 17A and paid units hidden
    assert "2C" in collect_text
    assert "16B" in collect_text
    assert "17A" not in collect_text
    confirm_text = env.bot.edits()[-1]["text"]
    assert "确认收租" in confirm_text
    assert "🏢" not in confirm_text or "Unit" in confirm_text
    # smart defaults on the confirmation card
    assert "金额：<b>₱55,000</b>" in confirm_text
    assert "日期：" + date.today().isoformat() in confirm_text
    conv = env.store.get_conversation(OWNER_ID, OWNER_ID)
    assert conv is not None and conv["state"] == "rent_confirm"
    kb = env.bot.edits()[-1]["reply_markup"]
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert "✅ 确认入账" in labels
    assert "✏️ 修改收租信息" in labels


def test_commands_and_menu(make_app):
    """★ B1: /start is the short greeting; text keywords still route to the
    deterministic read pages (finance / overdue / properties)."""
    env = make_app()
    updates = [
        make_text_update(OWNER_ID, OWNER_ID, "/start", bot=env.bot),
        make_text_update(OWNER_ID, OWNER_ID, "finance", message_id=2, update_id=2, bot=env.bot),
        make_text_update(OWNER_ID, OWNER_ID, "overdue", message_id=3, update_id=3, bot=env.bot),
        make_text_update(OWNER_ID, OWNER_ID, "properties", message_id=4, update_id=4, bot=env.bot),
    ]
    run_updates(env, updates)
    start_text = env.bot.sends()[0]["text"]
    assert "Hello" in start_text
    texts = "".join(env.bot.all_texts())
    assert "2026年8月财务" in texts
    assert "逾期租金 · 2笔" in texts
    assert "房源概况" in texts


def test_overdue_escape_and_action_buttons(make_app):
    env = make_app()
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "overdue", bot=env.bot)])
    text = env.bot.last_send()["text"]
    # tenant name with HTML-ish content must be escaped
    assert "Maria &lt;Admin&gt;" in text
    assert "Maria <Admin>" not in text
    kb = env.bot.last_send()["reply_markup"]
    buttons = [b for row in kb.inline_keyboard for b in row]
    # 2 items x (记一笔 + 详情) + home button (B7)
    assert len(buttons) == 5
    assert "🏠 首页" in [b.text for b in buttons]


# --- confirm flows ---

def test_rent_confirm(make_app):
    env = make_app()
    data = _run_rent_to_confirm(env)
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, data, bot=env.bot)])
    assert len(env.backend.incomes) == 1
    inc = env.backend.incomes[0]
    assert inc["status"] == "confirmed"
    assert inc["amount"] == "55000.00"
    assert inc["description"] == f"rent {date.today().strftime('%Y-%m')}"
    # single answer = processing ack; the done card is the durable result
    assert len(env.bot.answers()) == 1
    assert "处理中" in (env.bot.last_answer()["text"] or "")
    assert "收租成功" in env.bot.edits()[-1]["text"]
    assert "编号" not in env.bot.edits()[-1]["text"]  # no income_id on the user-facing card
    kb = env.bot.edits()[-1]["reply_markup"]
    labels = [b.text for row in kb.inline_keyboard for b in row] if kb else []
    assert "✅ 确认入账" not in labels  # no stale confirm button after confirmation
    assert "↩️ 撤销" in labels
    # conversation is retained (15-min TTL) so a second click can replay
    conv = env.store.get_conversation(OWNER_ID, OWNER_ID)
    assert conv is not None and conv["state"] == "rent_confirm"


def test_secretary_records_pending_not_confirmed(make_app):
    env = make_app()
    data = _run_rent_to_confirm(env, user_id=SECRETARY_ID, chat_id=SECRETARY_ID)
    run_updates(env, [make_callback_update(SECRETARY_ID, SECRETARY_ID, data, bot=env.bot)])
    assert len(env.backend.incomes) == 1
    inc = env.backend.incomes[0]
    assert inc["status"] == "pending"
    assert env.backend.count_calls("POST", "/incomes/1/confirm") == 0
    assert len(env.bot.answers()) == 1
    assert "Processing" in (env.bot.last_answer()["text"] or "")
    assert "Recorded, pending" in env.bot.edits()[-1]["text"]


def test_edit_noop_message_not_modified(make_app, caplog):
    """★ F12: re-clicking 登记收租 re-renders identical text; the Telegram
    'Message is not modified' BadRequest must be swallowed — no exception,
    no error log (the same double-click used to spam err.log)."""
    env = make_app(bot=_NoopEditBot())
    updates = [
        make_callback_update(OWNER_ID, OWNER_ID, encode("rn", "go", "1"), bot=env.bot),
        make_callback_update(OWNER_ID, OWNER_ID, encode("rn", "go", "1"),
                             message_id=10, update_id=2, bot=env.bot),
    ]
    with caplog.at_level(logging.ERROR):
        run_updates(env, updates)  # must complete without propagating
    edits = env.bot.edits()
    assert len(edits) == 2
    assert edits[0]["text"] == edits[1]["text"]  # identical re-render is a no-op
    assert "Message is not modified" not in caplog.text  # no error log spam
    conv = env.store.get_conversation(OWNER_ID, OWNER_ID)
    assert conv is not None and conv["state"] == "rent_confirm"


def test_edit_other_bad_request_still_raises():
    """★ F12: only the 'Message is not modified' BadRequest is swallowed;
    any other BadRequest must still propagate from _edit."""
    class _RejectEditBot(_NoopEditBot):
        async def edit_message_text(self, text=None, chat_id=None, message_id=None,
                                    parse_mode=None, reply_markup=None, **kw):
            self._text_by_message.setdefault(message_id, text)
            raise BadRequest("Bad Request: message can't be edited")

    update = make_callback_update(OWNER_ID, OWNER_ID, "x", bot=_RejectEditBot())
    with pytest.raises(BadRequest):
        asyncio.run(callback_handlers._edit(update, "text"))


def test_duplicate_rent_callback(make_app):
    """★ Same confirm card clicked twice -> only ONE income is written."""
    env = make_app()
    data = _run_rent_to_confirm(env)
    run_updates(
        env,
        [
            make_callback_update(OWNER_ID, OWNER_ID, data, update_id=10, bot=env.bot),
            make_callback_update(OWNER_ID, OWNER_ID, data, update_id=11, bot=env.bot),
        ],
    )
    assert len(env.backend.incomes) == 1
    assert env.backend.count_calls("POST", "/incomes") == 1
    assert env.backend.count_calls("POST", "/incomes/1/confirm") == 1
    assert len(env.bot.answers()) == 3  # setup click + 2 confirm clicks
    assert "处理中" in (env.bot.last_answer()["text"] or "")
    assert "收租成功" in env.bot.edits()[-1]["text"]


def test_double_confirm(make_app):
    """★ Confirm the same pending income twice -> confirm endpoint called once."""
    env = make_app()
    env.backend.add_income(status="pending", income_id=1)
    nonce = new_nonce()
    data = encode("cnf", "inc", "1", nonce=nonce, ts=now_ts())
    run_updates(
        env,
        [
            make_callback_update(OWNER_ID, OWNER_ID, data, update_id=10, bot=env.bot),
            make_callback_update(OWNER_ID, OWNER_ID, data, update_id=11, bot=env.bot),
        ],
    )
    assert env.backend.count_calls("POST", "/incomes/1/confirm") == 1
    assert len(env.backend.incomes) == 1
    assert env.backend.incomes[0]["status"] == "confirmed"
    assert len(env.bot.answers()) == 2  # two confirm clicks on the same data
    assert "处理中" in (env.bot.last_answer()["text"] or "")
    assert "收租成功" in env.bot.edits()[-1]["text"]


def test_double_confirm_backend_409_path(make_app):
    """★ Even if the local guard is bypassed, backend 409 -> 'already handled'."""
    env = make_app()
    env.backend.add_income(status="confirmed", income_id=1)
    # Hand-craft a confirm callback for the already-confirmed income with a
    # different nonce so the local guard can't short-circuit.
    data = encode("cnf", "inc", "1", nonce=new_nonce(), ts=now_ts())
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, data, bot=env.bot)])
    assert env.backend.count_calls("POST", "/incomes/1/confirm") == 1
    assert len(env.bot.answers()) == 1
    assert "处理中" in (env.bot.last_answer()["text"] or "")
    assert "收租成功" in env.bot.edits()[-1]["text"]
    assert "编号" not in env.bot.edits()[-1]["text"]
    kb = env.bot.edits()[-1]["reply_markup"]
    labels = [b.text for row in kb.inline_keyboard for b in row] if kb else []
    assert "✅ 确认入账" not in labels


def test_expired_callback(make_app):
    """★ Old card (ts beyond TTL) -> refused, no API call, no write."""
    env = make_app()
    env.backend.add_income(status="pending", income_id=1)
    old_ts = now_ts() - 10000  # 10k seconds > 900s TTL
    data = encode("cnf", "inc", "1", nonce=new_nonce(), ts=old_ts)
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, data, bot=env.bot)])
    assert env.backend.count_calls("POST", "/incomes/1/confirm") == 0
    assert env.backend.incomes[0]["status"] == "pending"
    assert "过期" in (env.bot.last_answer()["text"] or "")


def test_invalid_callback(make_app):
    env = make_app()
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, "garbage", bot=env.bot)])
    assert env.bot.last_answer()["text"] == "⚠️ 无效操作"
    assert env.backend.calls == []


def test_reverse_owner_only(make_app):
    env = make_app()
    env.backend.add_income(status="confirmed", income_id=1)
    data = encode("rv", "inc", "1", nonce=new_nonce(), ts=now_ts())
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, data, bot=env.bot)])
    assert env.backend.incomes[0]["status"] == "reversed"
    assert len(env.bot.answers()) == 1
    assert "处理中" in (env.bot.last_answer()["text"] or "")
    assert "已撤销" in env.bot.edits()[-1]["text"]


# --- permissions ---

def test_permission_bypass(make_app):
    """★ Secretary hand-crafts a confirm callback -> bot refuses, no API call.
    Backend enforcement is also simulated (403) for an agent-level key."""
    env = make_app()
    env.backend.add_income(status="pending", income_id=1)
    data = encode("cnf", "inc", "1", nonce=new_nonce(), ts=now_ts())
    run_updates(
        env,
        [make_callback_update(SECRETARY_ID, SECRETARY_ID, data, bot=env.bot)],
    )
    assert env.backend.count_calls("POST", "/incomes/1/confirm") == 0
    assert env.backend.incomes[0]["status"] == "pending"
    answer = (env.bot.last_answer()["text"] or "").lower()
    assert "permission" in answer or "无权限" in answer

    # unknown telegram user also refused
    run_updates(
        env,
        [make_callback_update(UNKNOWN_ID, UNKNOWN_ID, data, update_id=20, bot=env.bot)],
    )
    assert env.backend.count_calls("POST", "/incomes/1/confirm") == 0

    # backend enforcement: even a valid user's confirm is refused if the API
    # key lacks permission (backend 403).
    env2 = make_app(api_key="agent-key")
    env2.backend.add_income(status="pending", income_id=1)
    env2.backend.fail_status["/incomes/1/confirm"] = 403
    run_updates(
        env2,
        [make_callback_update(OWNER_ID, OWNER_ID, data, update_id=30, bot=env2.bot)],
    )
    assert env2.backend.incomes[0]["status"] == "pending"
    # backend 403 arrives after the processing ack -> durable on the card
    assert "无权限" in (env2.bot.last_edit()["text"] or "")


# --- timeout reconciliation (design §13) ---

def test_backend_timeout_before_write(make_app):
    """★ Create times out before the write lands: user is told it's uncertain,
    retry is allowed and completes exactly one income."""
    env = make_app()
    env.backend.timeout_before_write_paths.add("/incomes")
    data = _run_rent_to_confirm(env)
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, data, update_id=10, bot=env.bot)])
    assert len(env.backend.incomes) == 0
    assert env.backend.count_calls("POST", "/incomes") == 1
    assert len(env.bot.answers()) == 2  # setup click + timed-out confirm click
    assert "处理中" in (env.bot.last_answer()["text"] or "")
    edit = env.bot.last_edit()["text"] or ""
    assert "网络超时" in edit
    assert "请重试" in edit or "不确定" in edit

    # retry same card -> allowed (failed state) and completes
    env.backend.timeout_before_write_paths.clear()
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, data, update_id=11, bot=env.bot)])
    assert len(env.backend.incomes) == 1
    assert env.backend.incomes[0]["status"] == "confirmed"
    assert "收租成功" in env.bot.edits()[-1]["text"]


def test_backend_timeout_after_write(make_app):
    """★ CREATE actually landed server-side but the response timed out: the
    bot reconciles via GET /incomes matching, confirms the reused income,
    never claims 'nothing changed', and never creates a second income."""
    env = make_app()
    data = _run_rent_to_confirm(env)
    env.backend.timeout_after_write_paths.add("/incomes")

    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, data, update_id=10, bot=env.bot)])
    assert len(env.backend.incomes) == 1
    assert env.backend.incomes[0]["status"] == "confirmed"
    assert env.backend.count_calls("POST", "/incomes") == 1
    assert env.backend.count_calls("GET", "/incomes") >= 1  # reconciled via list
    assert env.backend.count_calls("POST", "/incomes/1/confirm") == 1
    assert len(env.bot.answers()) == 2  # setup click + confirm click
    assert "处理中" in (env.bot.last_answer()["text"] or "")
    assert "没有修改" not in "".join(env.bot.all_texts())
    assert "收租成功" in env.bot.edits()[-1]["text"]

    # second click on the same card -> replay, no new API calls
    before = len(env.backend.calls)
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, data, update_id=11, bot=env.bot)])
    assert len(env.backend.incomes) == 1
    assert len(env.backend.calls) == before
    assert len(env.bot.answers()) == 3
    assert "处理中" in (env.bot.last_answer()["text"] or "")
    assert "收租成功" in env.bot.edits()[-1]["text"]


def test_create_timeout_after_write_no_duplicate(make_app):
    """★ F1 regression: create lands as pending, response times out, retry on
    the same card must NEVER produce a second income."""
    env = make_app()
    data = _run_rent_to_confirm(env)
    env.backend.timeout_after_write_paths.add("/incomes")

    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, data, update_id=10, bot=env.bot)])
    assert len(env.backend.incomes) == 1
    assert env.backend.incomes[0]["status"] == "confirmed"
    assert env.backend.count_calls("POST", "/incomes") == 1

    before = len(env.backend.calls)
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, data, update_id=11, bot=env.bot)])
    assert len(env.backend.incomes) == 1  # never a second income
    assert env.backend.count_calls("POST", "/incomes") == 1
    assert len(env.backend.calls) == before
    assert len(env.bot.answers()) == 3  # setup click + two confirm clicks
    assert "处理中" in (env.bot.last_answer()["text"] or "")
    assert "收租成功" in env.bot.edits()[-1]["text"]


def test_new_card_re_records_same_period_reuses_pending(make_app):
    """★ F1: after a create timeout, a NEW card (new nonce) for the same
    unit/period reuses the landed pending income instead of duplicating it.
    (V1.1: confirmed incomes block re-entry, so the first write stays pending.)"""
    env = make_app()
    data = _run_rent_to_confirm(env, user_id=SECRETARY_ID, chat_id=SECRETARY_ID)
    env.backend.timeout_after_write_paths.add("/incomes")

    run_updates(env, [make_callback_update(SECRETARY_ID, SECRETARY_ID, data, update_id=10, bot=env.bot)])
    assert len(env.backend.incomes) == 1
    assert env.backend.incomes[0]["status"] == "pending"
    assert env.backend.count_calls("POST", "/incomes") == 1

    # OWNER opens a fresh flow (new nonce) and re-records the same unit/period
    env.backend.timeout_after_write_paths.clear()
    data2 = _run_rent_to_confirm(env)
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, data2, update_id=20, bot=env.bot)])
    assert len(env.backend.incomes) == 1  # reused, not duplicated
    assert env.backend.count_calls("POST", "/incomes") == 1
    assert env.backend.incomes[0]["status"] == "confirmed"
    assert "收租成功" in env.bot.edits()[-1]["text"]


def test_backend_timeout_pending_reconcile(make_app):
    """Confirm times out with NO server-side effect (income still pending):
    keep it pending, allow retry, never mislead the user."""
    env = make_app()
    data = _run_rent_to_confirm(env)
    env.backend.timeout_without_effect_paths.add("/incomes/1/confirm")

    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, data, update_id=10, bot=env.bot)])
    assert env.backend.incomes[0]["status"] == "pending"
    edit = env.bot.last_edit()["text"] or ""
    assert "待确认" in edit or "网络超时" in edit
    assert "收租成功" not in "".join(env.bot.all_texts())

    # retry is allowed, resumes the existing pending income, and completes
    env.backend.timeout_without_effect_paths.clear()
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, data, update_id=11, bot=env.bot)])
    assert len(env.backend.incomes) == 1  # no second income created
    assert env.backend.incomes[0]["status"] == "confirmed"
    assert "收租成功" in env.bot.edits()[-1]["text"]


# --- V1.3 Gate A: one Native Bot caller credential for Owner transitions ---

def test_confirm_and_reverse_use_native_bot_credential_and_owner_subject(make_app):
    """Confirm/reverse carry one SERVICE caller plus the clicking HUMAN id."""
    env = make_app(api_key="native-bot-key", admin_api_key="legacy-admin-key")

    data = _run_rent_to_confirm(env, user_id=SECRETARY_ID, chat_id=SECRETARY_ID)
    run_updates(env, [make_callback_update(SECRETARY_ID, SECRETARY_ID, data, bot=env.bot)])
    assert env.backend.incomes[0]["status"] == "pending"
    assert env.backend.auth_for("POST", "/incomes") == "Bearer native-bot-key"

    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, encode("cnf", "inc", "1", nonce=new_nonce(), ts=now_ts()), bot=env.bot)],
    )
    assert env.backend.incomes[0]["status"] == "confirmed"
    assert env.backend.auth_for("POST", "/incomes/1/confirm") == "Bearer native-bot-key"

    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, encode("rv", "inc", "1", nonce=new_nonce(), ts=now_ts()), update_id=30, bot=env.bot)],
    )
    assert env.backend.incomes[0]["status"] == "reversed"
    assert env.backend.auth_for("POST", "/incomes/1/reverse") == "Bearer native-bot-key"

    financial_calls = [
        (call, telegram_user_id)
        for call, telegram_user_id in zip(
            env.backend.calls, env.backend.telegram_user_calls
        )
        if call[0] == "POST" and call[1] in {
            "/incomes/1/confirm", "/incomes/1/reverse"
        }
    ]
    assert financial_calls == [
        (("POST", "/incomes/1/confirm", None), str(OWNER_ID)),
        (("POST", "/incomes/1/reverse", None), str(OWNER_ID)),
    ]


def test_reverse_does_not_require_legacy_admin_key(make_app):
    """The Native Bot credential is sufficient caller proof for Owner reverse."""
    env = make_app(api_key="native-bot-key", admin_api_key="")
    env.backend.add_income(status="confirmed", income_id=1)
    data = encode("rv", "inc", "1", nonce=new_nonce(), ts=now_ts())
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, data, bot=env.bot)])
    assert env.backend.count_calls("POST", "/incomes/1/reverse") == 1
    assert env.backend.incomes[0]["status"] == "reversed"
    assert env.backend.auth_for("POST", "/incomes/1/reverse") == "Bearer native-bot-key"

    # A full OWNER flow keeps the reverse action available without a second
    # credential; the backend still validates the bound canonical subject.
    env2 = make_app(admin_api_key="")
    data2 = _run_rent_to_confirm(env2)
    run_updates(env2, [make_callback_update(OWNER_ID, OWNER_ID, data2, bot=env2.bot)])
    kb = env2.bot.edits()[-1]["reply_markup"]
    labels = [b.text for row in kb.inline_keyboard for b in row] if kb else []
    assert "↩️ 撤销" in labels


# --- F3: crash after write -> restart -> same card retry, no duplicate ---

def test_crash_after_write_restart_no_duplicate(make_app, tmp_path):
    """★ F3: create lands, the process dies before settling, the bot restarts;
    the same card retry reuses the landed pending income instead of creating
    a second one."""
    db = str(tmp_path / "restart_state.db")
    env1 = make_app(state_db=db)
    data = _run_rent_to_confirm(env1)
    parsed = decode(data)
    key = f"ik:cnf:ren:{parsed['nonce']}"

    # simulate: create landed as pending, then the process died mid-write
    env1.backend.add_income(
        status="pending", income_id=1, lease_id=1, amount="55000.00",
        received_date=date.today().isoformat(), payment_method="Bank",
    )
    env1.guard.acquire(key, kind="income", resource="")
    env1.store._conn.execute(
        "UPDATE idempotency_keys SET created_at=? WHERE key=?",
        (str(int(time.time()) - 200), key),
    )
    env1.store._conn.commit()

    # restart on the same state db + backend
    env2 = make_app(backend=env1.backend, state_db=db)
    run_updates(env2, [make_callback_update(OWNER_ID, OWNER_ID, data, update_id=30, bot=env2.bot)])
    assert len(env2.backend.incomes) == 1
    assert env2.backend.incomes[0]["status"] == "confirmed"
    assert env2.backend.count_calls("POST", "/incomes") == 0  # never re-created
    assert "收租成功" in env2.bot.edits()[-1]["text"]


# --- F4: read paths are permission-gated ---

def test_unknown_user_read_refused(make_app):
    """★ F4: unknown users cannot read finance (text route or nav callback)."""
    env = make_app()
    run_updates(env, [make_text_update(UNKNOWN_ID, UNKNOWN_ID, "finance", bot=env.bot)])
    assert env.backend.count_calls("GET", "/reports/financial-summary") == 0
    assert "无权限" in "".join(env.bot.all_texts())

    run_updates(
        env,
        [make_callback_update(UNKNOWN_ID, UNKNOWN_ID, encode("nav", "finance"), update_id=20, bot=env.bot)],
    )
    assert env.backend.count_calls("GET", "/reports/financial-summary") == 0
    assert "无权限" in (env.bot.last_answer()["text"] or "")


def test_owner_and_secretary_can_read(make_app):
    """★ F4: OWNER and SECRETARY keep read access (zh for OWNER, en for SECRETARY)."""
    for user_id, marker in ((OWNER_ID, "财务"), (SECRETARY_ID, "Finance")):
        env = make_app()
        run_updates(env, [make_text_update(user_id, user_id, "finance", bot=env.bot)])
        assert env.backend.count_calls("GET", "/reports/financial-summary") == 1
        assert marker in "".join(env.bot.all_texts())


# --- F5: OWNER confirms SECRETARY-recorded pending via /pending ---

def test_owner_confirms_secretary_pending_via_pending_command(make_app):
    """★ F5: SECRETARY records pending; OWNER confirms it from the pending page."""
    env = make_app()
    data = _run_rent_to_confirm(env, user_id=SECRETARY_ID, chat_id=SECRETARY_ID)
    run_updates(env, [make_callback_update(SECRETARY_ID, SECRETARY_ID, data, bot=env.bot)])
    assert env.backend.incomes[0]["status"] == "pending"

    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "pending", bot=env.bot)])
    send = env.bot.last_send()
    assert "待确认收款" in send["text"]
    kb = send["reply_markup"]
    assert kb is not None
    btn = next(
        b
        for row in kb.inline_keyboard
        for b in row
        if decode(b.callback_data) is not None
        and decode(b.callback_data)["action"] == "cnf"
        and decode(b.callback_data)["entity"] == "inc"
        and decode(b.callback_data)["ref"] == "1"
    )

    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, btn.callback_data, update_id=30, bot=env.bot)],
    )
    assert env.backend.incomes[0]["status"] == "confirmed"
    assert env.backend.count_calls("POST", "/incomes/1/confirm") == 1
    assert "收租成功" in env.bot.edits()[-1]["text"]


def test_secretary_pending_list_has_no_confirm_buttons(make_app):
    """★ F5/V1.3: SECRETARY's unified Tasks page shows her tasks only — the
    Owner's decision rows (pending income etc.) never appear with confirm."""
    env = make_app()
    env.backend.add_income(status="pending", income_id=1)
    run_updates(env, [make_text_update(SECRETARY_ID, SECRETARY_ID, "pending", bot=env.bot)])
    send = env.bot.last_send()
    assert "Tasks" in send["text"]  # SECRETARY locale is en (✅ Tasks title)
    assert "Pending income" not in send["text"]  # not her decision
    kb = send["reply_markup"]
    assert kb is not None
    cnf = [
        b for row in kb.inline_keyboard for b in row
        if decode(b.callback_data) is not None
        and decode(b.callback_data)["action"] == "cnf"
    ]
    assert cnf == []


# --- F6: group-chat ownership check happens before acquire ---

def test_group_other_user_click_does_not_lock_card(make_app):
    """★ F6: in a group, another member clicking your card must not burn the
    idempotency key / lock your card."""
    env = make_app()
    GROUP = 424242
    data = _run_rent_to_confirm(env, user_id=OWNER_ID, chat_id=GROUP)
    nonce = decode(data)["nonce"]

    # secretary (has RENT_ENTRY) clicks the OWNER's card first
    run_updates(env, [make_callback_update(SECRETARY_ID, GROUP, data, update_id=20, bot=env.bot)])
    assert env.store.get_idempotency(f"ik:cnf:ren:{nonce}") is None  # never acquired

    # owner's own click still completes exactly one income
    run_updates(env, [make_callback_update(OWNER_ID, GROUP, data, update_id=21, bot=env.bot)])
    assert len(env.backend.incomes) == 1
    assert env.backend.incomes[0]["status"] == "confirmed"


# --- F7: reverse timeout still-confirmed toast ---

def test_reverse_timeout_reconcile_still_confirmed_toast(make_app):
    """★ F7: reverse times out with no effect (still confirmed) -> the toast
    must say 'not reversed, retry', never 'already processed'."""
    env = make_app()
    env.backend.add_income(status="confirmed", income_id=1)
    env.backend.timeout_without_effect_paths.add("/incomes/1/reverse")
    data = encode("rv", "inc", "1", nonce=new_nonce(), ts=now_ts())
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, data, bot=env.bot)])
    assert env.backend.incomes[0]["status"] == "confirmed"
    edit = env.bot.last_edit()["text"] or ""
    assert "未撤销" in edit
    assert "已处理" not in edit
