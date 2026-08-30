"""Expense DTOs (Pydantic v2).

Money is ``Decimal``: Pydantic v2 serializes ``Decimal`` to a JSON string,
so the wire format never degrades into a float (AGENTS.md §4).

Every request model sets ``extra="forbid"`` so an unknown field is a 422
rather than a silently ignored instruction.

Category is on the claim itself; purpose is a separate Property/Unit column
(not exposed here — see Property DTOs).
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


# ---- create / input --------------------------------------------------


class ExpenseReceiptIn(BaseModel):
    """Proof supplied alongside or after a claim."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1, max_length=16)
    reference: str = Field(min_length=1, max_length=500)


class ExpenseClaimOpen(BaseModel):
    """Open a new expense claim. Idempotency-Key is required at the route."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    category: str = Field(min_length=1, max_length=32)
    claimed_amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    receipts: list[ExpenseReceiptIn] = Field(
        default_factory=list, max_length=20,
    )


class ExpenseVerifyRequest(BaseModel):
    """Verification decision. Omit ``verified_amount`` to verify exactly the claim."""

    model_config = ConfigDict(extra="forbid")

    verified_amount: Optional[Decimal] = Field(
        default=None, gt=0, max_digits=14, decimal_places=2,
    )


class ExpenseDecisionRequest(BaseModel):
    """Rejection / reversal always requires an explicit reason."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


class ExpenseFollowUpCreate(BaseModel):
    """Create a Task projection on the linked Operation."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    due_at: Optional[datetime] = None


# ---- read ------------------------------------------------------------


class ExpenseClaimRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    title: str
    category: str
    claimed_amount: Decimal
    verified_amount: Optional[Decimal] = None
    status: str
    opened_by_user_id: Optional[int] = None
    opened_at: datetime
    idempotency_key: str


class ExpenseReceiptRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    claim_id: int
    kind: str
    reference: str
    uploaded_by_user_id: Optional[int] = None
    created_at: datetime


class ExpenseVerificationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    claim_id: int
    decision: str
    verified_amount: Optional[Decimal] = None
    verifier_user_id: Optional[int] = None
    decided_at: datetime
    reason: Optional[str] = None


class ExpenseActivityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    claim_id: Optional[int] = None
    receipt_id: Optional[int] = None
    kind: str
    detail: Optional[str] = None
    actor_user_id: Optional[int] = None
    occurred_at: datetime


class ExpenseBalanceRead(BaseModel):
    """Verified vs claimed snapshot. ``is_settled`` is the only Settled signal."""

    model_config = ConfigDict(from_attributes=True)

    claim_id: int
    claimed_amount: Decimal
    verified_total: Decimal
    remaining_amount: Decimal
    is_settled: bool


# ---- reuse rent_payment TaskRead / OperationRead via re-export -------

# Re-use the canonical Task/Operation read models from rent_payment so
# the REST surface for follow-ups / operations stays a single definition.
from app.v1.schemas.rent_payment import (  # noqa: E402, F401
    OperationRead,
    TaskRead,
)


__all__ = [
    "ExpenseActivityRead",
    "ExpenseBalanceRead",
    "ExpenseClaimOpen",
    "ExpenseClaimRead",
    "ExpenseDecisionRequest",
    "ExpenseFollowUpCreate",
    "ExpenseReceiptIn",
    "ExpenseReceiptRead",
    "ExpenseVerificationRead",
    "ExpenseVerifyRequest",
    "OperationRead",
    "TaskRead",
]