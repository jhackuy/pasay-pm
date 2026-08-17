from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

from app.models import ExpenseStatus, IncomeStatus
from app.schemas.common import AuditFields, money_field

# P1-PASAY-NIGHTLY-PRODUCT-HARDENING-008 A2: placeholder/empty text is never a
# meaningful expense identity. These sentinels mirror the read-path cleaners
# (app.services.operations.quick._clean_text and the bot's
# _clean_free_text) so the write path rejects at the boundary what the read
# path would otherwise have to hide. Payee keeps the established `-`
# "unknown vendor" sentinel (the bot's DB NOT NULL contract); it is never a
# displayed purpose (renderers drop it).
_PLACEHOLDER_SENTINELS = {"??", "?", "--", "none", "null", "n/a", "na", "unknown"}


def _meaningful_label(value: str, *, field_name: str, allow_dash: bool = False) -> str:
    """Normalize a human label and reject empty/whitespace/placeholder text.

    Raises ValueError (pydantic 422) when no meaningful label remains, so a
    newly created Expense can never carry `??`, whitespace-only or equivalent
    placeholder text as its identity (A2)."""
    text = " ".join(str(value).split())
    if not text:
        raise ValueError(f"{field_name} must not be empty or whitespace")
    lowered = text.lower()
    if lowered in _PLACEHOLDER_SENTINELS or (not allow_dash and lowered == "-"):
        raise ValueError(
            f"{field_name} must be a meaningful human-readable label, not a placeholder"
        )
    return text


class IncomeBase(BaseModel):
    lease_id: int | None = None
    amount: Decimal = money_field(gt=0)
    received_date: date
    payment_method: str | None = Field(default=None, max_length=50)
    idempotency_key: str | None = Field(default=None, max_length=128)
    status: IncomeStatus
    description: str | None = None


class IncomeCreate(IncomeBase):
    pass


class IncomeUpdate(BaseModel):
    lease_id: int | None = None
    amount: Decimal | None = money_field(gt=0)
    received_date: date | None = None
    payment_method: str | None = Field(default=None, max_length=50)
    description: str | None = None


class IncomeRead(IncomeBase, AuditFields):
    id: int
    confirmed_by: int | None = None
    confirmed_at: datetime | None = None


class ExpenseBase(BaseModel):
    expense_date: date
    due_date: date | None = None
    category: str = Field(min_length=1, max_length=100)
    amount: Decimal = money_field(gt=0)
    payee: str = Field(min_length=1, max_length=200)
    description: str | None = None
    unit_id: int | None = None
    status: ExpenseStatus
    receipt_attachment_id: int | None = None
    # AI-OPS-FOUNDATION-001 §4/§8: the actual payer; payment responsibility
    # routes to this user after approval instead of always the Owner.
    payer_user_id: int | None = None


class ExpenseCreate(ExpenseBase):
    @field_validator("category")
    @classmethod
    def _category_meaningful(cls, value: str) -> str:
        return _meaningful_label(value, field_name="category")

    @field_validator("payee")
    @classmethod
    def _payee_meaningful(cls, value: str) -> str:
        # `-` stays allowed: it is the bot's established DB-NOT-NULL "unknown
        # vendor" sentinel and is never rendered as a purpose.
        return _meaningful_label(value, field_name="payee", allow_dash=True)


class ExpenseUpdate(BaseModel):
    expense_date: date | None = None
    due_date: date | None = None
    category: str | None = Field(default=None, min_length=1, max_length=100)
    amount: Decimal | None = money_field(gt=0, default=None)
    payee: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    unit_id: int | None = None
    receipt_attachment_id: int | None = None
    payer_user_id: int | None = None

    @field_validator("category")
    @classmethod
    def _category_meaningful(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _meaningful_label(value, field_name="category")

    @field_validator("payee")
    @classmethod
    def _payee_meaningful(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _meaningful_label(value, field_name="payee", allow_dash=True)


class ExpenseRead(ExpenseBase, AuditFields):
    id: int
    approved_by: int | None = None
    approved_at: datetime | None = None
