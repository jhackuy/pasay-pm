"""PASAY reference implementation — money helpers.

Issue #99 / PR #100 /oc continuation. Staged under
``.opencode-qualification/reference/`` pending Owner-side promotion to
``app/core/money.py`` once ``opencode.json`` is updated out-of-band.

Hard invariants enforced by this module:
    * Money values are ALWAYS ``decimal.Decimal`` — never ``float``.
    * All inputs from external sources (DB strings, JSON, Telegram messages,
      request payloads) are parsed via ``parse_money`` which rejects ``float``
      inputs at runtime.
    * ``quantize`` to NUMERIC(14,2) before any persistence boundary.
    * No silent rounding for partial-payments; rounding mode is ROUND_HALF_UP
      only and is exposed via ``money_quantize`` so callers can override.

This is a reference implementation. Promotion to ``app/core/money.py``
requires no behavioral change — only a path move and an import swap in
callers. Unit tests in ``tests/unit/test_money_reference.py`` will gate the
promotion.
"""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from typing import Iterable, Union

# NUMERIC(14,2) at the DB layer → 12 integer digits + 2 fractional digits.
MONEY_QUANTUM = Decimal("0.01")
MONEY_MAX = Decimal("999999999999.99")
MONEY_MIN = -MONEY_MAX

# Accept "1234.5", "1,234.5", "1234.56", "$1,234.56", "USD 1234.56" etc.
# Reject scientific notation and floats-as-strings produced by json.dumps(1.0).
_NON_NUMERIC_CHARS = re.compile(r"[^\d.\-]")

# Marker for sentinel that is intentionally NOT a Decimal subclass.
_FORBIDDEN_FLOAT_TYPES = (float,)


class MoneyError(ValueError):
    """Raised when a money value cannot be parsed, quantised, or compared safely."""


def _coerce_to_str(value: object) -> str:
    if isinstance(value, bool):
        # bool is int subclass; reject explicitly to avoid True == 1 money bugs.
        raise MoneyError("bool is not a valid money source")
    if isinstance(value, _FORBIDDEN_FLOAT_TYPES):
        raise MoneyError(
            "float is forbidden as a money source per AGENTS.md §4 / "
            "DATA_CONTRACT.md NUMERIC(14,2) invariant; "
            "use Decimal('1.50') or string '1.50'"
        )
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (int,)):
        return str(value)
    if isinstance(value, str):
        return value
    raise MoneyError(f"unsupported money source type: {type(value).__name__}")


def parse_money(value: Union[str, int, Decimal], *, allow_negative: bool = True) -> Decimal:
    """Parse a money value into ``Decimal`` quantised to NUMERIC(14,2).

    Raises ``MoneyError`` on ``float`` input, bool input, or unparseable string.
    """
    raw = _coerce_to_str(value).strip()
    if not raw:
        raise MoneyError("empty money source")
    # Strip currency symbols, thousands separators, leading/trailing spaces.
    cleaned = _NON_NUMERIC_CHARS.sub("", raw.replace(",", ""))
    if cleaned in ("", "-", "--"):
        raise MoneyError(f"unparseable money source: {value!r}")
    try:
        with localcontext() as ctx:
            ctx.rounding = ROUND_HALF_UP
            d = Decimal(cleaned)
    except (InvalidOperation, ValueError) as exc:
        raise MoneyError(f"unparseable money source: {value!r}") from exc
    if not allow_negative and d < 0:
        raise MoneyError(f"negative money not allowed: {value!r}")
    if d < MONEY_MIN or d > MONEY_MAX:
        raise MoneyError(f"money value {d} out of NUMERIC(14,2) range")
    return money_quantize(d)


def money_quantize(value: Decimal, quantum: Decimal = MONEY_QUANTUM) -> Decimal:
    """Quantise to the DB NUMERIC quantum using ROUND_HALF_UP.

    Idempotent: ``money_quantize(money_quantize(x)) == money_quantize(x)``.
    """
    if not isinstance(value, Decimal):
        raise MoneyError(
            f"money_quantize requires Decimal input, got {type(value).__name__}"
        )
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


def money_sum(values: Iterable[Union[str, int, Decimal]]) -> Decimal:
    """Sum an iterable of money values; quantise once at the end.

    Each input is parsed via :func:`parse_money` so ``float`` inputs raise.
    """
    total = Decimal("0")
    for v in values:
        total += parse_money(v)
    return money_quantize(total)


def money_multiply(
    amount: Union[str, int, Decimal],
    factor: Union[str, int, Decimal],
) -> Decimal:
    """Multiply a money amount by a ratio (e.g. tax rate, commission %).

    The factor is intentionally typed the same as the amount so it cannot
    silently slip a ``float`` through. Result is quantised to NUMERIC(14,2).
    """
    a = parse_money(amount)
    f = parse_money(factor)
    return money_quantize(a * f)


def money_compare(a: Union[str, int, Decimal], b: Union[str, int, Decimal]) -> int:
    """Return -1, 0, or 1 — never ``bool`` (which is ambiguous for ``==``)."""
    aa = parse_money(a)
    bb = parse_money(b)
    if aa < bb:
        return -1
    if aa > bb:
        return 1
    return 0


def money_is_zero(value: Union[str, int, Decimal]) -> bool:
    return parse_money(value) == Decimal("0.00")


def money_to_cents(value: Union[str, int, Decimal]) -> int:
    """Convert a NUMERIC(14,2) value to integer minor units (cents).

    Useful for payment-provider integrations that require integer minor units.
    Returns the value multiplied by 100 with banker-safe integer rounding.
    """
    d = parse_money(value)
    return int((d * Decimal(100)).to_integral_value(rounding=ROUND_HALF_UP))


def money_from_cents(cents: int) -> Decimal:
    """Inverse of :func:`money_to_cents`. Rejects non-int input."""
    if not isinstance(cents, int) or isinstance(cents, bool):
        raise MoneyError("money_from_cents requires int input")
    return money_quantize(Decimal(cents) / Decimal(100))


__all__ = [
    "MONEY_QUANTUM",
    "MONEY_MAX",
    "MONEY_MIN",
    "MoneyError",
    "parse_money",
    "money_quantize",
    "money_sum",
    "money_multiply",
    "money_compare",
    "money_is_zero",
    "money_to_cents",
    "money_from_cents",
]
