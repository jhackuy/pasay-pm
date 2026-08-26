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
    lease_id: int
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
    property_id: int | None = None
    status: ExpenseStatus
    receipt_attachment_id: int | None = None
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
    property_id: int | None = None
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
    # PASAY-EXPENSE-OPERATION-003B: additional facts surfaced on reads.
    rejection_reason: str | None = None
    reapproval_reason: str | None = None
    version: int | None = None
    parent_expense_id: int | None = None


class PaymentClaimIn(BaseModel):
    """Create/verify/fail/reverse a payment claim."""
    claimed_amount: Decimal | None = Field(default=None, gt=0)
    verification_note: str | None = None
    evidence_ids: list[int] | None = None
    idempotency_key: str | None = Field(default=None, max_length=255)
    # verify-specific
    verified_amount: Decimal | None = Field(default=None, gt=0)
    result: str | None = None
    reason: str | None = Field(default=None, max_length=500)


class PaymentClaimOut(BaseModel):
    id: int
    expense_id: int
    claimed_amount: str
    claimed_by: int | None = None
    claimed_at: datetime | None = None
    status: str
    evidence_ids: list = []
    verification_note: str | None = None
    verified_amount: str | None = None
    verified_by: int | None = None
    verified_at: datetime | None = None
    mismatch: bool = False
    mismatch_reason: str | None = None
    failure_reason: str | None = None


class ExpensePaymentInfo(BaseModel):
    required_amount: str
    verified_paid: str
    remaining: str
    fully_paid: bool
    pending_claims: int
    has_mismatch: bool
    verified_claim_count: int = 0
    pending_claim_count: int = 0
    claims: list[dict] = []


class ExpenseDetailOut(ExpenseRead):
    """Mini App full detail view (PASAY-EXPENSE-OPERATION-003B section 15):
    Expense / Approval / Payment / Claims / Evidence / Verification /
    Actions / Timeline — all derived from the real rows."""
    payment: ExpensePaymentInfo
    claims: list[PaymentClaimOut] = []
    evidence: dict | None = None
    timeline: list[dict] = []
    reviewed: dict | None = None


# ---------------------------------------------------------------------------
# PASAY-MILESTONE-002 — Rent Payment Claim schemas
# ---------------------------------------------------------------------------


class RentClaimCreate(BaseModel):
    """POST /leases/{lease_id}/rents/claims — report a claimed payment."""

    period: str = Field(min_length=7, max_length=7, pattern=r"^\d{4}-\d{2}$")
    claimed_amount: Decimal = Field(gt=0)
    received_date: date | None = None
    verification_note: str | None = None
    evidence_ids: list[int] | None = None
    idempotency_key: str | None = Field(default=None, max_length=255)


class RentClaimVerify(BaseModel):
    """PATCH /rents/claims/{id}/verify."""

    verified_amount: Decimal | None = Field(default=None, gt=0)
    result: str | None = None
    reason: str | None = Field(default=None, max_length=500)


class RentClaimFail(BaseModel):
    """PATCH /rents/claims/{id}/fail."""

    reason: str = Field(min_length=1, max_length=500)


class RentClaimReverse(BaseModel):
    """PATCH /rents/claims/{id}/reverse."""

    reason: str = Field(min_length=1, max_length=500)


class RentClaimOut(BaseModel):
    id: int
    lease_id: int
    period: str
    income_id: int | None = None
    claimed_amount: str
    claimed_by: int | None = None
    claimed_at: datetime | None = None
    received_date: date | None = None
    status: str
    evidence_ids: list = []
    verification_note: str | None = None
    verified_amount: str | None = None
    verified_by: int | None = None
    verified_at: datetime | None = None
    mismatch: bool = False
    mismatch_reason: str | None = None
    failure_reason: str | None = None


class RentPeriodPaymentInfo(BaseModel):
    required_amount: str
    verified_paid: str
    remaining: str
    overpaid: str
    fully_paid: bool
    partially_paid: bool
    pending_claim_count: int = 0
    verified_claim_count: int = 0
    failed_claim_count: int = 0
    reversed_claim_count: int = 0
    pending_claimed_total: str = "0.00"
    has_mismatch: bool = False
    overclaimed_total: str = "0.00"


class RentDetailOut(BaseModel):
    """Detail view for one lease period: claims, evidence, truth summary."""

    lease_id: int
    period: str
    truth: RentPeriodPaymentInfo
    claims: list[RentClaimOut] = []
    evidence: dict | None = None
    timeline: list[dict] = []
