"""V1.3 Slice 2 — Entry C: read-only rent status NL queries.

Covers: deterministic detection (zh/en positives and negatives, never misread
as rent-payment statements), Owner zh / Secretary en answers, exact
unit-number match, tenant ambiguity candidates (read-only, no auto-select),
no-match friendly prompts, unknown-user refusal, zero writes on every query
path, and no internal ids / error details leaked to the user.
"""
from datetime import date

from conftest import OWNER_ID, SECRETARY_ID, UNKNOWN_ID, make_text_update, run_updates
from pasay_bot.handlers.nl_bridge import (
    detect_rent_status_query,
    is_rent_payment_statement,
)


def add_unit_1608(env, with_overdue=False):
    """Unit 1608 with Jan–Jul confirmed; the current month is left open."""
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
    # Make the canonical "John" tenant for the 1608 lease.
    env.backend.tenants[1]["full_name"] = "John Dela Cruz"
    for m in range(1, 8):
        env.backend.add_income(
            status="confirmed", lease_id=100, amount="70000.00",
            received_date=f"2026-{m:02d}-10",
            description=f"rent 2026-{m:02d}", income_id=1000 + m,
        )
    if with_overdue:
        month = date.today().strftime("%Y-%m")
        env.backend.overdue_rows.append({
            "lease_id": 100, "unit_id": 100, "tenant_id": 2, "unit": "1608",
            "tenant": "John Dela Cruz", "overdue_months": 1,
            "overdue_periods": [{"month": month, "amount": "70000.00"}],
            "amount_per_month": "70000.00", "total_outstanding": "70000.00",
            "oldest_due_date": date.today().isoformat(), "overdue_days": 5,
            "outstanding": "70000.00", "days_overdue": 5,
        })


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


def set_who_unpaid_rows(env):
    """Two overdue rows: 16B owes the CURRENT month, 2C owes only older
    months — so a "this month" query must include 16B but exclude 2C."""
    month = date.today().strftime("%Y-%m")
    env.backend.overdue = [
        {
            "lease_id": 1, "unit_id": 1, "tenant_id": 1, "unit": "16B",
            "tenant": "Juan Dela Cruz", "overdue_months": 1,
            "overdue_periods": [{"month": month, "amount": "55000.00"}],
            "amount_per_month": "55000.00", "total_outstanding": "55000.00",
            "oldest_due_date": date.today().isoformat(), "overdue_days": 5,
            "outstanding": "55000.00", "days_overdue": 5,
        },
        {
            "lease_id": 2, "unit_id": 3, "tenant_id": 2, "unit": "2C",
            "tenant": "Maria <Admin>", "overdue_months": 2,
            "overdue_periods": [], "amount_per_month": "12000.00",
            "total_outstanding": "24000.00",
            "oldest_due_date": "2026-07-10", "overdue_days": 40,
            "outstanding": "24000.00", "days_overdue": 40,
        },
    ]


# --- deterministic detector -------------------------------------------------

def test_detect_who_unpaid_queries():
    for text in (
        "这个月谁还没交", "谁还没交", "谁没交", "还没交房租的",
        "这个月谁没交租", "还没交租金",
        "Who hasn't paid this month?", "who didn't pay?",
        "unpaid this month", "who has not paid?",
    ):
        q = detect_rent_status_query(text)
        assert q is not None and q.kind == "who_unpaid", text


def test_detect_unit_queries():
    cases = {
        "1608 交了没有": "1608",
        "1608 还欠多少": "1608",
        "has 1608 paid?": "1608",
        "how much does 1608 owe?": "1608",
        "16B 交了吗": "16B",
    }
    for text, token in cases.items():
        q = detect_rent_status_query(text)
        assert q is not None and q.kind == "unit" and q.unit_token == token, text


def test_detect_tenant_queries():
    for text in ("John 交了吗", "did John pay?", "how much does John owe?"):
        q = detect_rent_status_query(text)
        assert q is not None and q.kind == "tenant" and q.tenant_token == "John", text


def test_detect_negative_and_routes_untouched():
    for text in ("1608", "John", "收租", "财务", "逾期", "帮助", ""):
        assert detect_rent_status_query(text) is None, text


def test_queries_are_never_rent_payment_statements():
    for text in (
        "这个月谁还没交", "1608 交了没有", "1608 还欠多少", "John 交了吗",
        "who hasn't paid?", "has 1608 paid?", "did John pay?",
    ):
        assert not is_rent_payment_statement(text), text


# --- Owner zh / Secretary en answers ----------------------------------------

def test_owner_zh_unit_unpaid(make_app):
    env = make_app()
    add_unit_1608(env)
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "1608 交了没有", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "Unit 1608" in text
    assert "John Dela Cruz" in text
    assert "⚠️ 未交" in text
    assert "欠 ₱70,000" in text
    assert env.backend.count_calls("POST", "/incomes") == 0


def test_owner_zh_unit_paid(make_app):
    env = make_app()
    add_unit_1608(env)
    month = date.today().strftime("%Y-%m")
    env.backend.add_income(
        status="confirmed", lease_id=100, amount="70000.00",
        received_date=date.today().isoformat(),
        description=f"rent {month}",
    )
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "1608 交了没有", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "✅ 已交" in text
    assert "未交" not in text
    assert "欠" not in text


def test_owner_zh_unit_owes_with_overdue(make_app):
    env = make_app()
    add_unit_1608(env, with_overdue=True)
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "1608 还欠多少", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "⚠️ 未交" in text
    assert "欠 ₱70,000" in text
    assert "逾期 5 天" in text


def test_secretary_en_unit_answer(make_app):
    env = make_app()
    add_unit_1608(env)
    run_updates(env, [make_text_update(SECRETARY_ID, SECRETARY_ID, "has 1608 paid?", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "Unit 1608" in text
    assert "Not paid" in text
    assert "Owes ₱70,000" in text


def test_secretary_en_who_unpaid(make_app):
    env = make_app()
    set_who_unpaid_rows(env)
    run_updates(env, [
        make_text_update(SECRETARY_ID, SECRETARY_ID, "who hasn't paid this month?", bot=env.bot)
    ])
    text = env.bot.last_send()["text"]
    assert "unpaid · 1" in text
    assert "Unit 16B" in text
    assert "Juan Dela Cruz" in text
    assert "Unit 2C" not in text  # 2C owes older months only, not this month


# --- who-unpaid list --------------------------------------------------------

def test_owner_zh_who_unpaid_list(make_app):
    env = make_app()
    set_who_unpaid_rows(env)
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "这个月谁还没交", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "未交 · 1笔" in text
    assert "Unit 16B" in text
    assert "Juan Dela Cruz" in text
    assert "Unit 2C" not in text


def test_who_unpaid_all_collected(make_app):
    env = make_app()
    env.backend.overdue = []
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "这个月谁还没交", bot=env.bot)])
    assert "全部收齐" in env.bot.last_send()["text"]


# --- tenant matching / ambiguity ---------------------------------------------

def test_tenant_single_hit_answers_status(make_app):
    env = make_app()
    add_unit_1608(env)
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "John 交了吗", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "Unit 1608" in text
    assert "John Dela Cruz" in text
    assert "⚠️ 未交" in text


def test_tenant_ambiguous_lists_candidates_read_only(make_app):
    env = make_app()
    add_unit_1608(env)
    add_second_john(env)
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "John 交了吗", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "找到多个匹配项" in text
    assert "John Dela Cruz" in text and "John Smith" in text
    assert "Unit 1608" in text and "Unit 1708" in text
    # read-only: never a select/confirm action, zero writes
    assert env.bot.last_send()["reply_markup"] is None
    assert env.backend.count_calls("POST", "/incomes") == 0


def test_tenant_no_match_friendly(make_app):
    env = make_app()
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "Nobody 交了吗", bot=env.bot)])
    assert "没有找到叫 Nobody 的租客" in env.bot.last_send()["text"]


def test_unit_no_match_friendly(make_app):
    env = make_app()
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "1999 交了没有", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "没有找到编号为 1999 的房源" in text
    assert "unknown" not in text.lower()


# --- RBAC / safety ------------------------------------------------------------

def test_unknown_user_denied_before_any_api_call(make_app):
    env = make_app()
    run_updates(env, [
        make_text_update(UNKNOWN_ID, UNKNOWN_ID, "这个月谁还没交", bot=env.bot),
        make_text_update(UNKNOWN_ID, UNKNOWN_ID, "1608 交了没有",
                         message_id=2, update_id=2, bot=env.bot),
    ])
    texts = "".join(env.bot.all_texts())
    assert "无权限" in texts
    assert env.backend.calls == []


def test_queries_never_write(make_app):
    env = make_app()
    add_unit_1608(env)
    run_updates(env, [
        make_text_update(OWNER_ID, OWNER_ID, "这个月谁还没交", bot=env.bot),
        make_text_update(OWNER_ID, OWNER_ID, "1608 交了没有",
                         message_id=2, update_id=2, bot=env.bot),
        make_text_update(SECRETARY_ID, SECRETARY_ID, "did John pay?",
                         message_id=3, update_id=3, bot=env.bot),
    ])
    assert all(method == "GET" for method, _path, _body in env.backend.calls)
    assert env.backend.count_calls("POST", "/payments/match") == 0
    assert env.backend.count_calls("POST", "/incomes") == 0


def test_query_error_is_friendly_no_internal_details(make_app):
    env = make_app()
    env.backend.fail_status["/reports/overdue-rents"] = 403
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "这个月谁还没交", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "暂时无法查询租金状态" in text
    for banned in ("403", "forced", "detail", "error_code", "traceback"):
        assert banned not in text.lower(), banned


def test_status_never_leaks_internal_ids(make_app):
    env = make_app()
    add_unit_1608(env, with_overdue=True)
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "1608 还欠多少", bot=env.bot)])
    text = env.bot.last_send()["text"].lower()
    for banned in ("lease", "income", "pending", "confirmed", "403", "409", "id"):
        assert banned not in text, banned
