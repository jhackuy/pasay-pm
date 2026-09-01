"""Rent / Payment DTOs (Pydantic v2).

Money is ``Decimal``: Pydantic v2 serializes ``Decimal`` to a JSON string,
so the wire format never degrades into a float (AGENTS.md §4).

Every request model sets ``extra="forbid"`` so an unknown field is a 422
rather than a silently ignored instruction.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class RentEvidenceIn(BaseModel):
    """Proof supplied alongside a payment claim."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1, max_length=16)
    reference: str = Field(min_length=1, max_length=500)


class RentDueScheduleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_id: int = Field(gt=0)
    period_start: date
    due_date: date
    amount_due: Decimal = Field(gt=0, max_digits=14, decimal_places=2)


class RentDueScheduleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    lease_id: int
    period_start: date
    due_date: date
    amount_due: Decimal
    state: str
    created_at: datetime
    updated_at: datetime


class RentPaymentClaimCreate(BaseModel):
    """A claim is an assertion, not money truth."""

    model_config = ConfigDict(extra="forbid")

    claimed_amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    evidence: list[RentEvidenceIn] = Field(default_factory=list, max_length=20)


class RentPaymentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    due_schedule_id: int
    claimed_amount: Decimal
    verified_amount: Optional[Decimal] = None
    status: str
    claimed_by_user_id: Optional[int] = None
    claimed_at: datetime
    idempotency_key: str


class RentEvidenceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    rent_payment_id: int
    kind: str
    reference: str
    uploaded_by_user_id: Optional[int] = None
    created_at: datetime


class RentVerifyRequest(BaseModel):
    """Omit ``verified_amount`` to verify exactly the claimed amount."""

    model_config = ConfigDict(extra="forbid")

    verified_amount: Optional[Decimal] = Field(
        default=None, gt=0, max_digits=14, decimal_places=2,
    )


class RentDecisionRequest(BaseModel):
    """Rejection / reversal always requires an explicit reason."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


class RentVerificationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    rent_payment_id: int
    decision: str
    verified_amount: Optional[Decimal] = None
    verifier_user_id: Optional[int] = None
    decided_at: datetime
    reason: Optional[str] = None


class RentBalanceRead(BaseModel):
    """Remaining balance snapshot. ``is_paid`` is the only Paid signal."""

    model_config = ConfigDict(from_attributes=True)

    due_schedule_id: int
    amount_due: Decimal
    verified_total: Decimal
    remaining_balance: Decimal
    is_paid: bool


class RentFollowUpCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    due_at: Optional[datetime] = None


class TaskRead(BaseModel):
    """Projection of a human action — never the business truth."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    operation_id: int
    kind: str
    title: str
    state: str
    due_at: Optional[datetime] = None
    done_at: Optional[datetime] = None
    created_at: datetime


class OperationRead(BaseModel):
    """Business truth for the rent-collection cycle."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    kind: str
    subject_type: str
    subject_id: int
    state: str
    due_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None


class RentActivityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    due_schedule_id: Optional[int] = None
    rent_payment_id: Optional[int] = None
    kind: str
    detail: Optional[str] = None
    actor_user_id: Optional[int] = None
    occurred_at: datetime


__all__ = [
    "OperationRead",
    "RentActivityRead",
    "RentBalanceRead",
    "RentDecisionRequest",
    "RentDueScheduleCreate",
    "RentDueScheduleRead",
    "RentEvidenceIn",
    "RentEvidenceRead",
    "RentFollowUpCreate",
    "RentPaymentClaimCreate",
    "RentPaymentRead",
    "RentVerificationRead",
    "RentVerifyRequest",
    "TaskRead",
]
