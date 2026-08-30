"""Repair DTOs (Pydantic v2).

Money is ``Decimal``: Pydantic v2 serializes ``Decimal`` to a JSON string,
so the wire format never degrades into a float (AGENTS.md §4).

Every request model sets ``extra="forbid"`` so an unknown field is a 422
rather than a silently ignored instruction.

Category is on the report itself; severity is separate. Report/Quote/Work
/CompletionClaim/Verification are five separate DTOs, mirroring the five
separate tables — never collapsed.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Optional

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _reject_json_float(v: Any) -> Any:
    """Reject a JSON float at the Pydantic schema boundary (AGENTS.md §4)."""
    if isinstance(v, float):
        raise ValueError(
            "float is not allowed for money values; "
            "use str, int, or Decimal (AGENTS.md §4)",
        )
    return v


MoneyDecimal = Annotated[Decimal, BeforeValidator(_reject_json_float)]


# ---- create / input --------------------------------------------------


class RepairReportCreate(BaseModel):
    """Open a new repair report. Idempotency-Key is required at the route."""

    model_config = ConfigDict(extra="forbid")

    unit_id: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2000)
    category: str = Field(min_length=1, max_length=32)
    severity: str = Field(min_length=1, max_length=16)
    # Advisory only — never closes the report.
    linked_expense_payment_id: Optional[int] = Field(
        default=None, gt=0,
    )


class RepairTechnicianAssign(BaseModel):
    """Attach a technician (internal or external) to a report."""

    model_config = ConfigDict(extra="forbid")

    technician_name: str = Field(min_length=1, max_length=200)
    technician_source: str = Field(min_length=1, max_length=16)
    technician_eta_at: Optional[datetime] = None


class RepairQuoteSubmit(BaseModel):
    """Technician submits a quote. Idempotent on (org_id, key)."""

    model_config = ConfigDict(extra="forbid")

    amount: MoneyDecimal = Field(gt=0, max_digits=14, decimal_places=2)
    description: str = Field(min_length=1, max_length=2000)
    technician_name: str = Field(min_length=1, max_length=200)


class RepairQuoteDecision(BaseModel):
    """OWNER approves or rejects a quote. ``reason`` is required on REJECT."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=0, max_length=500)


class RepairWorkCreate(BaseModel):
    """Append a work progress event."""

    model_config = ConfigDict(extra="forbid")

    state: str = Field(min_length=1, max_length=16)
    note: str = Field(min_length=1, max_length=2000)


class RepairCompletionClaimCreate(BaseModel):
    """Technician / secretary submits a completion claim."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=2000)


class RepairVerificationCreate(BaseModel):
    """OWNER verifies (or rejects) a completion claim."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=0, max_length=500)


class RepairReversalCreate(BaseModel):
    """OWNER reverses a previous VERIFIED repair decision."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=0, max_length=500)


class RepairFollowUpCreate(BaseModel):
    """Create a Task projection on the linked Operation."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    due_at: Optional[datetime] = None


# ---- read ------------------------------------------------------------


class RepairReportRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    unit_id: int
    title: str
    description: str
    category: str
    severity: str
    state: str
    reported_by_user_id: Optional[int] = None
    reported_at: datetime
    technician_name: Optional[str] = None
    technician_source: Optional[str] = None
    technician_eta_at: Optional[datetime] = None
    quoted_amount: Optional[Decimal] = None
    idempotency_key: str
    linked_expense_payment_id: Optional[int] = None
    completed_at: Optional[datetime] = None


class RepairQuoteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    report_id: int
    amount: Decimal
    description: str
    decision: str
    technician_name: str
    submitted_by_user_id: Optional[int] = None
    decided_by_user_id: Optional[int] = None
    decided_at: Optional[datetime] = None
    reason: Optional[str] = None


class RepairWorkRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    report_id: int
    state: str
    note: str
    actor_user_id: Optional[int] = None
    occurred_at: datetime


class RepairCompletionClaimRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    report_id: int
    summary: str
    claimed_by_user_id: Optional[int] = None
    claimed_at: datetime


class RepairVerificationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    report_id: int
    decision: str
    verifier_user_id: Optional[int] = None
    decided_at: datetime
    reason: Optional[str] = None


class RepairActivityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    report_id: Optional[int] = None
    quote_id: Optional[int] = None
    work_id: Optional[int] = None
    claim_id: Optional[int] = None
    kind: str
    detail: Optional[str] = None
    actor_user_id: Optional[int] = None
    occurred_at: datetime


# ---- reuse rent_payment TaskRead / OperationRead via re-export -------

# Re-use the canonical Task/Operation read models from rent_payment so
# the REST surface for follow-ups / operations stays a single definition.
from app.v1.schemas.rent_payment import (  # noqa: E402, F401
    OperationRead,
    TaskRead,
)


__all__ = [
    "OperationRead",
    "RepairActivityRead",
    "RepairCompletionClaimCreate",
    "RepairCompletionClaimRead",
    "RepairFollowUpCreate",
    "RepairQuoteDecision",
    "RepairQuoteRead",
    "RepairQuoteSubmit",
    "RepairReportCreate",
    "RepairReportRead",
    "RepairReversalCreate",
    "RepairTechnicianAssign",
    "RepairVerificationCreate",
    "RepairVerificationRead",
    "RepairWorkCreate",
    "RepairWorkRead",
    "TaskRead",
]
