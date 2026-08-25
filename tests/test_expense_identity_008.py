"""P1-PASAY-NIGHTLY-PRODUCT-HARDENING-008: Expense identity + test-DB isolation.

A2: the write path must reject placeholder/empty expense categories so a newly
created Expense always carries a meaningful human identity.
A3: the read-model purpose chain must recover truthful facts (e.g. the payee)
for an incomplete historical record such as E7/E8 (category `??`).
B1: the test-DB guard fails closed against the live/production databases.
"""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.schemas.financial import ExpenseCreate, ExpenseUpdate
from app.services.operations.quick import _expense_purpose


def _create(category: str = "水费", payee: str = "Fix-It Co") -> ExpenseCreate:
    return ExpenseCreate(
        expense_date=date(2026, 8, 1),
        category=category,
        amount=Decimal("100.00"),
        payee=payee,
        status="pending",
    )


# --- A2: write-path placeholder rejection -----------------------------------

@pytest.mark.parametrize(
    "bad",
    ["??", "?", "-", "--", "none", "null", "n/a", "  ", ""],
)
def test_placeholder_category_rejected_on_create(bad):
    with pytest.raises(ValidationError):
        _create(category=bad)


def test_dash_payee_still_allowed():
    # `-` is the bot's established DB-NOT-NULL "unknown vendor" sentinel; it is
    # never rendered as a purpose, so it stays a legal write value.
    _create(payee="-")


def test_placeholder_payee_rejected_on_create():
    with pytest.raises(ValidationError):
        _create(payee="??")
    with pytest.raises(ValidationError):
        _create(payee="   ")


def test_meaningful_category_and_payee_accepted():
    obj = _create(category="维修", payee="Repair")
    assert obj.category == "维修"
    assert obj.payee == "Repair"


def test_update_rejects_placeholder_category():
    with pytest.raises(ValidationError):
        ExpenseUpdate(category="??")
    with pytest.raises(ValidationError):
        ExpenseUpdate(category="   ")


def test_update_allows_null_category_and_valid_others():
    ExpenseUpdate(amount=Decimal("5.00"))
    ExpenseUpdate(category="水费", payee="DEV Meralco")


# --- A3: read-model purpose recovery for incomplete historical records -------

def test_purpose_falls_back_to_payee_for_incomplete_record():
    # E7/E8 shape: category `??`, no description, payee 'Repair'.
    expense = SimpleNamespace(category="??", description=None, payee="Repair")
    assert _expense_purpose(expense) == "Repair"


def test_purpose_prefers_category_over_payee():
    expense = SimpleNamespace(category="水费", description=None, payee="DEV Meralco")
    assert _expense_purpose(expense) == "水费"


def test_purpose_prefers_description_over_payee():
    expense = SimpleNamespace(category="??", description="Aircon repair", payee="Repair")
    assert _expense_purpose(expense) == "Aircon repair"


def test_purpose_none_when_everything_placeholder():
    expense = SimpleNamespace(category="??", description="  ", payee="-")
    assert _expense_purpose(expense) is None


# --- B1: test-DB isolation guard fails closed --------------------------------

def test_test_db_guard_allows_isolated_name_and_blocks_live():
    import re as _re
    from tests.conftest import (
        _CONFIGURED_DB,
        _FORBIDDEN_TEST_DBS,
        _ALLOWED_TEST_DB_PREFIX_RE,
        TEST_DB_NAME,
        _test_db_allowed,
    )

    # The active test DB must be an isolated name (strict pasay_*_ prefix whitelist).
    assert _test_db_allowed(TEST_DB_NAME, _CONFIGURED_DB) is True
    assert bool(_ALLOWED_TEST_DB_PREFIX_RE.fullmatch(TEST_DB_NAME)), (
        "TEST_DB_NAME=%r must match strict prefix whitelist pattern %s"
        % (TEST_DB_NAME, _ALLOWED_TEST_DB_PREFIX_RE.pattern)
    )
    # Explicitly allowed well-known families.
    for family_name in (
        "pasay_pm_r1_20260101_001",
        "pasay_gate_m004_01",
        "pasay_freeze_candidate_003",
        "pasay_closeout_20260801",
        "pasay_return2_r4_20260824_01",
        "pasay_fresh_alembic_002",
        "pasay_alembic_head_check",
    ):
        assert _test_db_allowed(family_name, _CONFIGURED_DB) is True, family_name
    # Live/production names are refused deterministically.
    for name in _FORBIDDEN_TEST_DBS:
        assert _test_db_allowed(name, _CONFIGURED_DB) is False
    assert _test_db_allowed(_CONFIGURED_DB, _CONFIGURED_DB) is False
    assert _test_db_allowed("", _CONFIGURED_DB) is False
    # Anything not matching the strict prefix whitelist must also fail closed.
    for disallowed in (
        "my_custom_db",
        "pasayprod",
        "production",
        "defaultdb",
        "pasaytest",
        "pasay_pm_r1_20260101_001!prod",
    ):
        assert _test_db_allowed(disallowed, _CONFIGURED_DB) is False, disallowed
