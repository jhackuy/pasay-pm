"""Money helpers — Decimal only.

AGENTS.md §4: Money = NUMERIC(14, 2) / Decimal. NEVER float.

API:
- parse_money(value) -> Decimal: parse from str/int/Decimal, reject float.
- quantize_money(value) -> Decimal: round to 2 decimal places (half-up).
- format_money(value) -> str: format as fixed-point with 2 decimals.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Union

_TWO_PLACES = Decimal("0.01")
MoneyInput = Union[str, int, Decimal]


class MoneyError(ValueError):
    """Raised when money input is invalid (including float, bool)."""


def parse_money(value: MoneyInput) -> Decimal:
    """Parse a money value into a Decimal quantized to 2 decimal places.

    Rejects float and bool with MoneyError (AGENTS.md §4: never float).
    """
    if isinstance(value, bool):
        raise MoneyError("bool is not allowed for money values")
    if isinstance(value, float):
        raise MoneyError(
            "float is not allowed for money values; "
            "use str, int, or Decimal (AGENTS.md §4)"
        )
    if isinstance(value, Decimal):
        return quantize_money(value)
    if isinstance(value, int):
        return quantize_money(Decimal(value))
    if isinstance(value, str):
        try:
            return quantize_money(Decimal(value))
        except InvalidOperation as exc:
            raise MoneyError(f"invalid money string: {value!r}") from exc
    raise MoneyError(f"unsupported money input type: {type(value).__name__}")


def quantize_money(value: Decimal) -> Decimal:
    """Quantize a Decimal to 2 decimal places (half-up)."""
    if not isinstance(value, Decimal):
        raise MoneyError(
            f"quantize_money requires Decimal, got {type(value).__name__}"
        )
    return value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


def format_money(value: Decimal) -> str:
    """Format a Decimal as fixed-point with exactly 2 decimal places."""
    if not isinstance(value, Decimal):
        raise MoneyError(
            f"format_money requires Decimal, got {type(value).__name__}"
        )
    return str(quantize_money(value))