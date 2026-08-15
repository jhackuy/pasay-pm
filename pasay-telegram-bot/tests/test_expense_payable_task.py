"""PASAY-V2-EXPENSE-PAYABLE-TASK-006 — Owner payment tasks.

Covers the canonical ``PENDING -> APPROVED -> PAID`` rule and its bot UX:
- every APPROVED (unpaid) expense is an Owner actionable Task;
- a PAID expense never appears as a payable Task;
- Tasks no longer shows the empty state while payable expenses exist;
- each payable expense exposes a stable identity (#E{id}) so same-day /
  same-amount records stay distinguishable;
- the Owner can pay one specific expense through a deterministic flow;
- a receipt is optional (paying without one succeeds);
- payment moves the SAME expense to PAID (no new Expense created);
- repeated payment on the same expense is idempotent;
- a possible-duplicate warning appears for highly similar separate records;
- amount alone never triggers the warning.

These are bot-behavior tests against the FakeBackend (tests/conftest.py),
which mirrors the backend quick/tasks + /pay + duplicate semantics."""
from __future__ import annotations

from conftest import (
    OWNER_ID,
    make_callback_update,
    make_text_update,
    run_updates,
)
from pasay_bot.keyboards import decode, encode, new_nonce, now_ts


def _buttons(kb):
    if kb is None:
        return []
    return [b for row in kb.inline_keyboard for b in row]


def _add_approved(env, expense_id, amount="7000.00", category="维修",
                  unit_id=1, expense_date="2026-08-15", status="approved",
                  **kw):
    return env.backend.add_expense(
        expense_id=expense_id, category=category, amount=amount,
        payee="Fix-It Co", unit_id=unit_id, status=status,
        expense_date=expense_date, **kw,
    )


def _open_tasks_quickview(env, user_id=OWNER_ID):
    run_updates(env, [make_text_update(user_id, user_id, "✅ Tasks", bot=env.bot)])
    return env.bot.last_send()


def _pay_data(expense_id, ref=""):
    return encode("exp", str(expense_id), ref, nonce=new_nonce(), ts=now_ts())


def _pay_confirm_data(expense_id, ref=""):
    return encode("expc", str(expense_id), ref, nonce=new_nonce(), ts=now_ts())


def _pay_cb(user_id, data, update_id, bot=None):
    return make_callback_update(user_id, user_id, data, update_id=update_id, bot=bot)


def _distinct_pay_actions(env):
    """Distinct Pay callback targets (#E{id}) on the last Tasks Quick View."""
    kb = env.bot.last_send()["reply_markup"]
    if kb is None or not hasattr(kb, "inline_keyboard"):
        return set()
    out = set()
    for b in _buttons(kb):
        d = decode(b.callback_data)
        if d and d["action"] == "exp" and d["entity"].isdigit():
            out.add(int(d["entity"]))
    return out


# --- Test A: APPROVED becomes an Owner Task; PAID does not -----------------

def test_approved_expense_appears_as_payable_task_paid_does_not(make_app):
    env = make_app()
    _add_approved(env, 1027, status="approved")   # payable
    _add_approved(env, 1028, status="paid")       # NOT payable
    _open_tasks_quickview(env)
    text = env.bot.last_send()["text"]
    assert "#E1027" in text
    assert "#E1028" not in text

    # The payable row is actionable via a Pay button pointing at #E1027.
    assert 1027 in _distinct_pay_actions(env)
    assert 1028 not in _distinct_pay_actions(env)


# --- Test B: visible identity for same-day / same-amount expenses ---------

def test_same_day_same_amount_expenses_are_distinguishable(make_app):
    env = make_app()
    _add_approved(env, 1027, unit_id=1)   # DEV-BAY-1680 area
    _add_approved(env, 1031, unit_id=1)
    _open_tasks_quickview(env)
    text = env.bot.last_send()["text"]
    assert "#E1027" in text
    assert "#E1031" in text
    assert 1027 in _distinct_pay_actions(env)
    assert 1031 in _distinct_pay_actions(env)


# --- Test C: payment completes the SAME expense and removes the Task ------

def test_payment_completes_same_expense_and_removes_task(make_app):
    env = make_app()
    _add_approved(env, 1027, status="approved")
    assert env.backend._get_expense(1027)["status"] == "approved"

    # Open the pay flow and finalize using the deterministic button flow.
    run_updates(env, [_pay_cb(OWNER_ID, _pay_data(1027), 1, bot=env.bot)])
    # No duplicates by default -> Confirm finalizes.
    run_updates(env, [_pay_cb(OWNER_ID, _pay_confirm_data(1027), 2, bot=env.bot)])

    assert env.backend._get_expense(1027)["status"] == "paid"
    assert env.backend.count_calls("POST", "/expenses/1027/pay") == 1
    # No new expense was created by paying.
    ids = [e["id"] for e in env.backend.expenses]
    assert ids.count(1027) == 1

    # The result card is PAID and the payable row disappears on next refresh.
    result_text = "".join(env.bot.all_texts())
    assert "已付款" in result_text
    _open_tasks_quickview(env)
    assert "#E1027" not in env.bot.last_send()["text"]
    assert 1027 not in _distinct_pay_actions(env)
    # It remains in Expense history (still one record).
    assert len(env.backend.expenses) == 1


# --- Test D: receipt is optional; pay without one succeeds ----------------

def test_pay_without_receipt_succeeds(make_app):
    env = make_app()
    _add_approved(env, 1027, status="approved", receipt_attachment_id=None)
    run_updates(env, [_pay_cb(OWNER_ID, _pay_data(1027), 1, bot=env.bot)])
    run_updates(env, [_pay_cb(OWNER_ID, _pay_confirm_data(1027), 2, bot=env.bot)])
    assert env.backend._get_expense(1027)["status"] == "paid"
    assert env.backend.count_calls("POST", "/expenses/1027/pay") == 1


# --- Test E: same-expense idempotency -------------------------------------

def test_repeat_payment_on_same_expense_is_idempotent(make_app):
    env = make_app()
    _add_approved(env, 1027, status="approved")
    run_updates(env, [_pay_cb(OWNER_ID, _pay_data(1027), 1, bot=env.bot)])
    run_updates(env, [_pay_cb(OWNER_ID, _pay_confirm_data(1027), 2, bot=env.bot)])
    assert env.backend._get_expense(1027)["status"] == "paid"
    before = list(env.backend.expenses)
    # Pay again -> idempotent "already paid", no second write, no new record.
    run_updates(env, [_pay_cb(OWNER_ID, _pay_confirm_data(1027), 3, bot=env.bot)])
    assert env.backend._get_expense(1027)["status"] == "paid"
    assert env.backend.count_calls("POST", "/expenses/1027/pay") == 1
    assert env.backend.expenses == before
    assert len(env.backend.expenses) == 1


# --- Test F: possible-duplicate warning (strong fields) -------------------

def test_possible_duplicate_warning_shows_both_ids_and_does_not_reject(make_app):
    env = make_app()
    _add_approved(env, 1031, status="approved")          # current / payable
    # Existing highly-similar PAID record (#E1027).
    env.backend.add_expense(expense_id=1027, category="维修", amount="7000.00",
                            payee="Fix-It Co", unit_id=1,
                            expense_date="2026-08-15", status="paid")
    env.backend.expense_duplicates = [
        {
            "expense_id": 1027, "status": "paid", "unit": "16B",
            "purpose": "维修", "amount": "7000.00", "expense_date": "2026-08-15",
        }
    ]
    run_updates(env, [_pay_cb(OWNER_ID, _pay_data(1031), 1, bot=env.bot)])
    text = env.bot.last_edit()["text"]
    assert "可能重复" in text
    assert "#E1027" in text
    # The current expense was NOT deleted or rejected (advisory only).
    assert env.backend._get_expense(1031)["status"] == "approved"
    # The warning card must NOT silently finalize the payment either.
    assert env.backend.count_calls("POST", "/expenses/1031/pay") == 0

    # The Owner confirms on the warning card -> the SAME expense becomes PAID.
    run_updates(env, [_pay_cb(OWNER_ID, _pay_confirm_data(1031), 2, bot=env.bot)])
    assert env.backend._get_expense(1031)["status"] == "paid"
    assert env.backend.count_calls("POST", "/expenses/1031/pay") == 1


# --- Test G: amount alone is never a duplicate signal ---------------------

def test_amount_alone_never_triggers_duplicate_warning(make_app):
    env = make_app()
    # Different unit/purpose, same ₱7,000 amount, already PAID.
    env.backend.add_expense(expense_id=2044, category="水费", amount="7000.00",
                            payee="Water Co", unit_id=3,
                            expense_date="2026-08-15", status="paid")
    _add_approved(env, 1027, status="approved", amount="7000.00", category="维修", unit_id=1)
    env.backend.expense_duplicates = []  # backend returns no match (unit/purpose differ)
    run_updates(env, [_pay_cb(OWNER_ID, _pay_data(1027), 1, bot=env.bot)])
    text = env.bot.last_edit()["text"]
    assert "可能重复" not in text
    assert "#E2044" not in text
