"""BOT-V1-USABLE-001 P0-5: grounded NL intent parsing (deterministic lanes)."""
from __future__ import annotations

from decimal import Decimal

from app.services.copilot.nl_parse import (
    Catalog,
    _deterministic_fallback,
    _normalize_amount,
    _normalize_category,
    _normalize_month,
    _resolve_unit,
)


def _catalog():
    return Catalog(
        units=[
            {"id": 1, "unit_number": "DEV-BAY-1203", "property": "DEV - Bayshore"},
            {"id": 2, "unit_number": "DEV-BAY-1608", "property": "DEV - Bayshore"},
        ],
        categories=["水费", "电费", "维修", "物业费", "其他"],
        current_month="2026-08",
    )


def test_category_normalization():
    assert _normalize_category("水费") == "水费"
    assert _normalize_category("water") == "水费"
    assert _normalize_category("electricity") == "电费"
    assert _normalize_category("维修费") == "维修"
    assert _normalize_category("association fee") == "物业费"
    assert _normalize_category("") == ""


def test_month_normalization():
    cat = _catalog()
    assert _normalize_month("这个月", cat.current_month) == "2026-08"
    assert _normalize_month("8月", cat.current_month) == "2026-08"
    assert _normalize_month("2026年9月", cat.current_month) == "2026-09"
    assert _normalize_month("2026-07", cat.current_month) == "2026-07"


def test_amount_normalization():
    assert _normalize_amount("2500") == Decimal("2500.00")
    assert _normalize_amount("2,500") == Decimal("2500.00")
    assert _normalize_amount("-5") is None
    assert _normalize_amount("0") is None
    assert _normalize_amount("abc") is None
    assert _normalize_amount(None) is None


def test_unit_resolution_suffix_match():
    cat = _catalog()
    unit, unit_id = _resolve_unit("1608", cat)
    assert unit == "DEV-BAY-1608"
    assert unit_id == 2
    assert _resolve_unit("9999", cat) == ("9999", None)


def test_deterministic_fallback_expense():
    result = _deterministic_fallback("支出1680水费2500", _catalog())
    assert result.intent == "create_expense"
    assert result.category == "水费"
    assert result.amount == Decimal("2500.00")


def test_deterministic_fallback_income():
    result = _deterministic_fallback("1608租金收到了", _catalog())
    assert result.intent == "create_income"
    assert result.unit_id == 2


def test_deterministic_fallback_query():
    result = _deterministic_fallback("这个月收了多少钱", _catalog())
    assert result.intent == "query"


def test_deterministic_fallback_ambiguous():
    result = _deterministic_fallback("今天天气不错", _catalog())
    assert result.intent == "ambiguous"
    assert len(result.options) >= 2
