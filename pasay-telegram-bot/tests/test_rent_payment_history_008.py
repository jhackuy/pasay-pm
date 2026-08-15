"""P1-PASAY-NIGHTLY-PRODUCT-HARDENING-008 C: deterministic rent
payment-history queries (0 LLM on the matched path).

Covers: detection (zh/en positives and negatives, never stolen from the plain
status lane or expense statements), Owner zh / Secretary en answers, count /
cumulative / latest-date math with partial payments counted individually and
pending/reversed rows excluded, this-month window, tenant variant, multi-match
read-only selector (tap renders the chosen history card, zero writes), and the
A3 payable-purpose recovery for an incomplete expense (E7/E8 shape).
"""
from datetime import date

from conftest import OWNER_ID, SECRETARY_ID, UNKNOWN_ID, make_callback_update, make_text_update, run_updates
from pasay_bot.handlers.nl_bridge import detect_rent_payment_history_query
from pasay_bot.render import cards


def add_unit(env, unit_number="1608", unit_id=100, tenant_id=88,
             tenant_name="Paolo Cruz", rent="70000.00"):
    """One active lease unit (prefixed style optional)."""
    env.backend.tenants.append({
        "id": tenant_id, "full_name": tenant_name, "phone": None,
        "email": None, "is_active": True,
    })
    env.backend.units.append({
        "id": unit_id, "property_id": 2, "unit_number": unit_number,
        "floor": "16", "size_sqm": "40.00", "monthly_rent": rent,
        "status": "occupied", "is_active": True,
    })
    env.backend.leases.append({
        "id": unit_id, "unit_id": unit_id, "tenant_id": tenant_id,
        "start_date": "2026-01-01", "end_date": "2026-12-31",
        "accounting_start_date": None, "monthly_rent": rent,
        "deposit": "0.00", "status": "active", "due_day": 5, "notes": None,
    })
    return unit_id


def seed_mixed_history(env, lease_id):
    """3 confirmed (one partial), 1 pending, 1 reversed on one lease."""
    env.backend.add_income(status="confirmed", lease_id=lease_id,
                           amount="70000.00", received_date="2026-08-05",
                           description="rent 2026-08", income_id=1)
    env.backend.add_income(status="confirmed", lease_id=lease_id,
                           amount="30000.00", received_date="2026-08-20",
                           description="rent 2026-08 partial", income_id=2)
    env.backend.add_income(status="confirmed", lease_id=lease_id,
                           amount="70000.00", received_date="2026-07-10",
                           description="rent 2026-07", income_id=3)
    env.backend.add_income(status="pending", lease_id=lease_id,
                           amount="70000.00", received_date="2026-09-01",
                           description="rent 2026-09", income_id=4)
    env.backend.add_income(status="reversed", lease_id=lease_id,
                           amount="70000.00", received_date="2026-06-10",
                           description="rent 2026-06", income_id=5)


# --- detector ---------------------------------------------------------------

def test_detector_positives():
    for q in (
        "1608 交了几次", "1608 累计交了多少", "1608 最近什么时候交的",
        "Paolo 最近什么时候交租", "这个月 1608 交了几次", "1608 一共交了",
        "when did 1608 last pay", "how many times has 1608 paid",
        "how much has 1608 paid in total", "1608 交租记录",
    ):
        r = detect_rent_payment_history_query(q)
        assert r is not None and r.kind == "payment_history", q


def test_detector_negatives_stay_on_their_lanes():
    # plain status questions, expense statements, menu words -> not history
    for q in (
        "1608 交了没有", "1608 交了多少", "1608 水费", "这个月谁还没交",
        "收租", "1608 还欠多少", "John 交了吗", "has 1608 paid?",
        "how much does 1608 owe?", "",
    ):
        assert detect_rent_payment_history_query(q) is None, q


def test_detector_month_window():
    assert detect_rent_payment_history_query("这个月 1608 交了几次").month_window == "this_month"
    assert detect_rent_payment_history_query("1608 交了几次").month_window == ""
    assert detect_rent_payment_history_query("this month, how many times has 1608 paid").month_window == "this_month"


def test_detector_requires_entity():
    assert detect_rent_payment_history_query("交了几次") is None
    assert detect_rent_payment_history_query("累计交了多少") is None


# --- unit answer: count / cumulative / latest -------------------------------

def test_owner_zh_count_cumulative_latest(make_app):
    env = make_app()
    lease_id = add_unit(env)
    seed_mixed_history(env, lease_id)
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "1608 交了几次", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "共交 3 次" in text          # pending + reversed excluded
    assert "₱170,000" in text          # 70000 + 30000 + 70000
    assert "2026-08-20" in text        # latest confirmed received_date
    assert env.backend.count_calls("POST", "/incomes") == 0


def test_secretary_en_count_cumulative_latest(make_app):
    env = make_app()
    lease_id = add_unit(env)
    seed_mixed_history(env, lease_id)
    run_updates(env, [make_text_update(SECRETARY_ID, SECRETARY_ID, "1608 交了几次", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "Paid 3 time(s)" in text
    assert "₱170,000" in text
    assert "2026-08-20" in text


def test_partial_payment_counts_as_one_payment(make_app):
    env = make_app()
    lease_id = add_unit(env)
    env.backend.add_income(status="confirmed", lease_id=lease_id,
                           amount="30000.00", received_date="2026-08-05",
                           description="rent 2026-08 part 1", income_id=1)
    env.backend.add_income(status="confirmed", lease_id=lease_id,
                           amount="40000.00", received_date="2026-08-12",
                           description="rent 2026-08 part 2", income_id=2)
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "1608 交了几次", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "共交 2 次" in text
    assert "₱70,000" in text


def test_this_month_window_counts_only_current_period(make_app):
    env = make_app()
    lease_id = add_unit(env)
    month = date.today().strftime("%Y-%m")
    env.backend.add_income(status="confirmed", lease_id=lease_id,
                           amount="70000.00", received_date=date.today().isoformat(),
                           description=f"rent {month}", income_id=1)
    env.backend.add_income(status="confirmed", lease_id=lease_id,
                           amount="70000.00", received_date="2026-05-10",
                           description="rent 2026-05", income_id=2)
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "这个月 1608 交了几次", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "共交 1 次" in text
    assert "₱70,000" in text
    assert "2026-05" not in text


def test_no_payments_yet(make_app):
    env = make_app()
    add_unit(env)
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "1608 交了几次", bot=env.bot)])
    assert "暂无交租记录" in env.bot.last_send()["text"]


def test_unit_no_match_friendly(make_app):
    env = make_app()
    add_unit(env)
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "9999 交了几次", bot=env.bot)])
    assert "没有找到编号为 9999 的房源" in env.bot.last_send()["text"]


def test_prefixed_unit_token(make_app):
    env = make_app()
    lease_id = add_unit(env, unit_number="DEV-BAY-1608")
    env.backend.add_income(status="confirmed", lease_id=lease_id,
                           amount="70000.00", received_date="2026-08-05",
                           description="rent 2026-08", income_id=1)
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "1608 交了几次", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "Unit DEV-BAY-1608" in text
    assert "共交 1 次" in text


# --- tenant variant ---------------------------------------------------------

def test_tenant_latest_payment(make_app):
    env = make_app()
    lease_id = add_unit(env, tenant_name="Paolo Cruz")
    seed_mixed_history(env, lease_id)
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "Paolo 最近什么时候交租", bot=env.bot)])
    text = env.bot.last_send()["text"]
    assert "Paolo Cruz" in text
    assert "共交 3 次" in text
    assert "2026-08-20" in text


def test_tenant_no_match_friendly(make_app):
    env = make_app()
    add_unit(env)
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "Nobody 最近什么时候交租", bot=env.bot)])
    assert "没有找到叫 Nobody 的租客" in env.bot.last_send()["text"]


# --- multi-match -> read-only selector -> tap renders chosen history ---------

def test_multi_match_selector_and_tap(make_app):
    env = make_app()
    lease_a = add_unit(env, unit_number="DEV-BAY-1805", unit_id=130, tenant_id=88,
                       tenant_name="John Dela Cruz")
    env.backend.add_income(status="confirmed", lease_id=lease_a,
                           amount="70000.00", received_date="2026-08-05",
                           description="rent 2026-08", income_id=1)
    lease_b = add_unit(env, unit_number="DEV-SOL-1805", unit_id=140, tenant_id=9,
                       tenant_name="Paolo Cruz", rent="48000.00")
    env.backend.add_income(status="confirmed", lease_id=lease_b,
                           amount="48000.00", received_date="2026-07-10",
                           description="rent 2026-07", income_id=2)
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "1805 交了几次", bot=env.bot)])
    send = env.bot.last_send()
    assert "找到多个匹配项" in send["text"]
    kb = send["reply_markup"].inline_keyboard
    labels = [btn.text for row in kb for btn in row]
    assert len(labels) == 2
    assert any("DEV-BAY-1805" in lbl and "John Dela Cruz" in lbl for lbl in labels)
    assert any("DEV-SOL-1805" in lbl and "Paolo Cruz" in lbl for lbl in labels)
    callbacks = "".join(btn.callback_data for row in kb for btn in row)
    assert "rhs" in callbacks  # history-select action, not a write/confirm
    n_api = len(env.backend.calls)

    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, kb[0][0].callback_data, bot=env.bot)])
    card_a = env.bot.last_edit()["text"]
    assert "Unit DEV-BAY-1805" in card_a and "共交 1 次" in card_a
    assert "Paolo" not in card_a
    run_updates(env, [make_callback_update(OWNER_ID, OWNER_ID, kb[1][0].callback_data,
                                           message_id=10, update_id=2, bot=env.bot)])
    card_b = env.bot.last_edit()["text"]
    assert "Unit DEV-SOL-1805" in card_b and "共交 1 次" in card_b
    assert "John" not in card_b
    assert len(env.backend.calls) == n_api
    assert env.backend.count_calls("POST", "/incomes") == 0


# --- permission -------------------------------------------------------------

def test_unknown_user_refused(make_app):
    env = make_app()
    add_unit(env)
    run_updates(env, [make_text_update(UNKNOWN_ID, UNKNOWN_ID, "1608 交了几次", bot=env.bot)])
    assert "无权限" in env.bot.last_send()["text"]


# --- A3: payable-purpose recovery for E7/E8 shape (render level) -------------

def test_payable_line_recovers_payee_for_incomplete_record():
    row = {
        "kind": "payable_expense", "expense_id": 7, "unit": "DEV-BAY-1680",
        "purpose": "", "category": "??", "payee": "Repair",
        "amount": "7000.00", "status": "approved", "expense_date": "2026-08-15",
    }
    text = cards._payable_expense_line(row, "zh")
    assert "Repair" in text
    assert "??" not in text


def test_payable_line_neutral_label_when_no_truthful_purpose():
    row = {
        "kind": "payable_expense", "expense_id": 9, "unit": "",
        "purpose": "", "category": "??", "payee": "-",
        "amount": "7000.00", "status": "approved", "expense_date": "2026-08-15",
    }
    text = cards._payable_expense_line(row, "zh")
    assert "其他" in text or "Other" in text
    assert "??" not in text


def test_expense_approval_card_unspecified_purpose_zh():
    exp = _fake_expense(category="??", description=None, payee="Repair")
    text = cards.expense_approval_card(exp, "zh")
    assert "Repair" in text and "??" not in text
    exp2 = _fake_expense(category="??", description=None, payee="-")
    text2 = cards.expense_approval_card(exp2, "zh")
    assert "未指定用途" in text2 and "??" not in text2


def _fake_expense(category, description, payee):
    class _E:
        pass
    e = _E()
    e.category = category
    e.description = description
    e.payee = payee
    e.amount = "7000.00"
    e.expense_date = date(2026, 8, 15)
    e.due_date = None
    e.receipt_attachment_id = None
    e.status = "approved"
    return e
