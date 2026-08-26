from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator


MONEY_QUANT = Decimal("0.01")
MONEY_MAX_DIGITS = 14
MONEY_DECIMAL_PLACES = 2


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class AuditFields(ORMModel):
    created_at: datetime
    updated_at: datetime
    created_by: int | None = None
    updated_by: int | None = None


def money_field(**kwargs) -> Any:
    """Shared Numeric(14,2) validation for money fields."""
    return Field(max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES, **kwargs)


def quantize_money(value: Decimal) -> Decimal:
    """Quantize a Decimal to 2dp with ROUND_HALF_UP (banker-friendly)."""
    return Decimal(value).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def validate_money_decimal(value: Any) -> Decimal:
    """Strict money validator: rejects floats, accepts str/int/Decimal → Decimal(2dp).

    Fail-closed: float inputs raise ValueError to prevent accidental float→Decimal
    precision loss from sneaking in through numeric coercion paths.
    """
    if isinstance(value, float):
        raise ValueError(
            "money fields must be Decimal/str/int, not float (precision risk)"
        )
    if value is None:
        raise ValueError("money value cannot be None")
    try:
        if isinstance(value, Decimal):
            d = value
        elif isinstance(value, (int, str)):
            d = Decimal(str(value))
        else:
            raise ValueError(f"unsupported money type: {type(value).__name__}")
    except (ValueError, ArithmeticError) as exc:
        raise ValueError(f"money value not parseable as Decimal: {exc}") from exc
    if not d.is_finite():
        raise ValueError("money value must be finite")
    sign, digits, exp = d.as_tuple()
    ndigits = len(digits)
    if exp > 0:
        ndigits = ndigits + exp
    if ndigits > MONEY_MAX_DIGITS:
        raise ValueError(
            f"money value exceeds max digits: {ndigits} > {MONEY_MAX_DIGITS}"
        )
    if -exp > MONEY_DECIMAL_PLACES:
        raise ValueError(
            f"money value exceeds decimal places: {-exp} > {MONEY_DECIMAL_PLACES}"
        )
    return quantize_money(d)


def money_validator(*fields: str, pre: bool = True) -> classmethod:
    """Factory returning a pydantic v2 field_validator for money Decimal fields.

    Usage::

        class Foo(BaseModel):
            amount: Decimal = money_field()
            _v_amount = money_validator("amount")
    """
    def _validator(cls, v: Any) -> Decimal:
        return validate_money_decimal(v)
    return field_validator(*fields, mode="before" if pre else "after")(_validator)


class MoneyBase(ORMModel):
    """Convenience base: auto-applies strict money validator to every
    ``Decimal``-typed field (fail-closed on float inputs, 14,2 bounds)."""

    @field_validator("*", mode="before")
    @classmethod
    def _strict_decimal_money(cls, v: Any, info) -> Any:
        field = info.field_name
        try:
            annotation = cls.model_fields[field].annotation
        except (KeyError, AttributeError):
            return v
        if annotation is Decimal or getattr(annotation, "__origin__", None) is Decimal:
            return validate_money_decimal(v)
        return v


class MessageResponse(BaseModel):
    detail: str


T = TypeVar("T")


class Paginated(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int
