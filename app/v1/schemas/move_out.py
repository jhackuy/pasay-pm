"""Move-out / Settlement DTOs (Pydantic v2).

Money is ``Decimal``: Pydantic v2 serializes ``Decimal`` to a JSON
string, so the wire format never degrades into a float (AGENTS.md §4).

Every request model sets ``extra="forbid"`` so an unknown field is a
422 rather than a silently ignored instruction.

The deposit settlement amounts are derived from the OWNER's disposition
choice; ``refund_amount`` and ``additional_owed`` are constrained so
that ``FULL_REFUND`` ⇒ ``refund_amount == deposit_held``, ``NO_REFUND``
⇒ both are 0, etc. The service re-validates these invariants in
addition to the DB-level CHECK constraints.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator


def _reject_json_float(v: Any) -> Any:
    if isinstance(v, float):
        raise ValueError(
            "float is not allowed for money values; "
            "use str, int, or Decimal (AGENTS.md §4)",
        )
    return v


MoneyDecimal = Annotated[Decimal, BeforeValidator(_reject_json_float)]


# ---- create / input --------------------------------------------------


class MoveOutRequestCreate(BaseModel):
    """Open a move-out inspection request. Idempotent on (org_id, key)."""

    model_config = ConfigDict(extra="forbid")

    lease_id: int = Field(gt=0)
    planned_move_out_date: Optional[date] = None
    notes: Optional[str] = Field(default=None, max_length=2000)


class MoveOutInspectionCreate(BaseModel):
    """Record a walk-through inspection."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=4000)


class MoveOutDamageCreate(BaseModel):
    """Record a damage / charge against the deposit."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1, max_length=32)
    description: str = Field(min_length=1, max_length=2000)
    amount: MoneyDecimal = Field(ge=0, max_digits=14, decimal_places=2)
    accepted_amount: Optional[MoneyDecimal] = Field(
        default=None, ge=0, max_digits=14, decimal_places=2,
    )


class MoveOutDamageAcceptRequest(BaseModel):
    """OWNER accepts a damage item, setting ``accepted_amount``."""

    model_config = ConfigDict(extra="forbid")

    accepted_amount: MoneyDecimal = Field(
        ge=0, max_digits=14, decimal_places=2,
    )


class DepositSettlementCreate(BaseModel):
    """Record the terminal deposit settlement. OWNER-only."""

    model_config = ConfigDict(extra="forbid")

    disposition: Literal[
        "FULL_REFUND", "PARTIAL_REFUND", "NO_REFUND", "ADDITIONAL_OWED",
    ]
    deposit_held: MoneyDecimal = Field(
        ge=0, max_digits=14, decimal_places=2,
    )
    refund_amount: MoneyDecimal = Field(
        ge=0, max_digits=14, decimal_places=2,
    )
    additional_owed: MoneyDecimal = Field(
        ge=0, max_digits=14, decimal_places=2,
    )
    notes: Optional[str] = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _amounts_match_disposition(self) -> "DepositSettlementCreate":
        disp = self.disposition
        if disp == "FULL_REFUND":
            if self.refund_amount != self.deposit_held:
                raise ValueError(
                    "FULL_REFUND requires refund_amount == deposit_held",
                )
            if self.additional_owed != Decimal("0"):
                raise ValueError(
                    "FULL_REFUND requires additional_owed == 0",
                )
        elif disp == "NO_REFUND":
            if self.refund_amount != Decimal("0"):
                raise ValueError(
                    "NO_REFUND requires refund_amount == 0",
                )
            if self.additional_owed != Decimal("0"):
                raise ValueError(
                    "NO_REFUND requires additional_owed == 0",
                )
        elif disp == "ADDITIONAL_OWED":
            # additional_owed must equal deposit_held - deductions (which
            # is the caller's responsibility to compute); we only check
            # non-negative here.
            pass
        return self


class MoveOutCancelRequest(BaseModel):
    """Cancel a non-terminal move-out. Reason required."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


class MoveOutKeysArrearsRequest(BaseModel):
    """Coverage Matrix 7.6: record keys-returned + arrears ledger."""

    model_config = ConfigDict(extra="forbid")

    keys_returned: bool
    arrears_amount: Decimal = Field(
        default=Decimal("0"), ge=0, max_digits=14, decimal_places=2,
    )
    notes: str | None = Field(default=None, max_length=2000)


class MoveOutFollowUpCreate(BaseModel):
    """Create a Task projection on the move-out's linked Operation."""

    model_config = ConfigDict(extra="forbid")

    move_out_id: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=200)
    due_at: Optional[datetime] = None


# ---- read ------------------------------------------------------------


class MoveOutRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    lease_id: int
    state: str
    requested_at: datetime
    requested_by_user_id: Optional[int] = None
    planned_move_out_date: Optional[date] = None
    inspected_at: Optional[datetime] = None
    inspected_by_user_id: Optional[int] = None
    inspection_notes: Optional[str] = None
    settled_at: Optional[datetime] = None
    settlement_id: Optional[int] = None
    cancelled_at: Optional[datetime] = None
    cancel_reason: Optional[str] = None
    keys_returned: Optional[bool] = None
    arrears_amount: Optional[Decimal] = None
    keys_arrears_notes: Optional[str] = None
    archived_at: Optional[datetime] = None
    idempotency_key: str


class MoveOutInspectionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    move_out_id: int
    inspected_at: datetime
    inspected_by_user_id: Optional[int] = None
    summary: str


class MoveOutDamageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    move_out_id: int
    kind: str
    description: str
    amount: Decimal
    accepted_amount: Decimal
    recorded_by_user_id: Optional[int] = None


class DepositSettlementRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    move_out_id: int
    disposition: str
    deposit_held: Decimal
    deductions_total: Decimal
    refund_amount: Decimal
    additional_owed: Decimal
    notes: Optional[str] = None
    settled_by_user_id: Optional[int] = None
    settled_at: datetime


class MoveOutActivityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    move_out_id: int
    kind: str
    actor_user_id: Optional[int] = None
    detail: Optional[str] = None
    occurred_at: datetime


class MoveOutBalanceRead(BaseModel):
    """Read-only balance projection for a move-out."""

    move_out_id: int
    deposit_held: Decimal
    deductions_total: Decimal
    refund_amount: Decimal
    additional_owed: Decimal
    is_settled: bool


# ---- reuse rent_payment TaskRead / OperationRead via re-export -------

from app.v1.schemas.rent_payment import (  # noqa: E402, F401
    OperationRead,
    TaskRead,
)


__all__ = [
    "DepositSettlementCreate",
    "DepositSettlementRead",
    "MoveOutActivityRead",
    "MoveOutBalanceRead",
    "MoveOutCancelRequest",
    "MoveOutDamageAcceptRequest",
    "MoveOutDamageCreate",
    "MoveOutDamageRead",
    "MoveOutFollowUpCreate",
    "MoveOutKeysArrearsRequest",
    "MoveOutInspectionCreate",
    "MoveOutInspectionRead",
    "MoveOutRead",
    "MoveOutRequestCreate",
    "OperationRead",
    "TaskRead",
]
