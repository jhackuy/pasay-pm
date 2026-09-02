"""Lease Renewal DTOs (Pydantic v2).

Money is ``Decimal``: Pydantic v2 serializes ``Decimal`` to a JSON string,
so the wire format never degrades into a float (AGENTS.md §4).

Every request model sets ``extra="forbid"`` so an unknown field is a 422
rather than a silently ignored instruction.

Approval ≠ Execution: a renewal proposal carries proposed terms; an
``approve`` decision accepts the proposal but does NOT execute it. The
separate ``execute`` endpoint is the closure gate that mutates the
source lease and creates the new lease.
"""
from __future__ import annotations

from datetime import date, datetime
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


class RenewalProposeRequest(BaseModel):
    """Propose a typed renewal of an ACTIVE lease. Idempotent on (org_id, key)."""

    model_config = ConfigDict(extra="forbid")

    source_lease_id: int = Field(gt=0)
    proposed_start_date: date
    proposed_end_date: date
    proposed_monthly_rent: MoneyDecimal = Field(
        gt=0, max_digits=14, decimal_places=2,
    )
    proposed_deposit: MoneyDecimal = Field(
        ge=0, max_digits=14, decimal_places=2,
    )


class RenewalDecisionRequest(BaseModel):
    """OWNER approves or rejects a renewal proposal. ``reason`` required on reject."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=0, max_length=500)


class RenewalExecuteRequest(BaseModel):
    """Execute an APPROVED proposal. Body is empty in current design."""

    model_config = ConfigDict(extra="forbid")


class RenewalCancelRequest(BaseModel):
    """Cancel a non-terminal proposal. ``reason`` is required."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


class RenewalFollowUpCreate(BaseModel):
    """Create a Task projection on the renewal's linked Operation."""

    model_config = ConfigDict(extra="forbid")

    renewal_id: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=200)
    due_at: Optional[datetime] = None


# ---- 7-stage pipeline (Issue #112 §"Lease Renewal") ----------------


class RenewalScanRequest(BaseModel):
    """DETECT_EXPIRY. System-side scan of upcoming lease expiries."""

    model_config = ConfigDict(extra="forbid")

    scan_window_days: int = Field(gt=0, le=365)
    as_of: Optional[date] = None
    lease_id: Optional[int] = Field(default=None, gt=0)


class RenewalScanRead(BaseModel):
    """Echo of a single renewal row produced / replayed by a scan."""

    model_config = ConfigDict(from_attributes=True)

    renewal: RenewalRead
    replayed: bool


class RenewalScanResponse(BaseModel):
    """Scan result aggregate."""

    scan_window_days: int
    detected: list[RenewalRead]
    replayed: list[RenewalRead]


class RenewalContactRequest(BaseModel):
    """DETECT_EXPIRY → CONTACT_TENANT."""

    model_config = ConfigDict(extra="forbid")

    contact_method: str = Field(min_length=1, max_length=40)
    note: Optional[str] = Field(default=None, max_length=500)


class RenewalResponseRequest(BaseModel):
    """CONTACT_TENANT → TENANT_RESPONSE."""

    model_config = ConfigDict(extra="forbid")

    tenant_response: str = Field(
        min_length=1, max_length=16,
    )
    note: Optional[str] = Field(default=None, max_length=500)


class RenewalOwnerDecisionRequest(BaseModel):
    """TENANT_RESPONSE → OWNER_DECISION. Optional term overrides."""

    model_config = ConfigDict(extra="forbid")

    owner_decision: str = Field(min_length=1, max_length=16)
    proposed_start_date: Optional[date] = None
    proposed_end_date: Optional[date] = None
    proposed_monthly_rent: Optional[MoneyDecimal] = None
    proposed_deposit: Optional[MoneyDecimal] = None
    note: Optional[str] = Field(default=None, max_length=500)


class RenewalVerifyRequest(BaseModel):
    """EXECUTE → VERIFY. Owner's post-execution confirmation."""

    model_config = ConfigDict(extra="forbid")

    note: Optional[str] = Field(default=None, max_length=500)


class RenewalCloseRequest(BaseModel):
    """VERIFY → CLOSED. Terminal."""

    model_config = ConfigDict(extra="forbid")

    note: Optional[str] = Field(default=None, max_length=500)


# ---- read ------------------------------------------------------------


class LeaseRead(BaseModel):
    """Read-only projection of a lease used in renewal responses."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    unit_id: int
    tenant_id: int
    start_date: date
    end_date: date
    monthly_rent: Decimal
    deposit: Decimal
    state: str


class RenewalRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    source_lease_id: int
    new_lease_id: Optional[int] = None
    state: str
    proposed_start_date: date
    proposed_end_date: date
    proposed_monthly_rent: Decimal
    proposed_deposit: Decimal
    proposed_by_user_id: Optional[int] = None
    proposed_at: datetime
    decided_by_user_id: Optional[int] = None
    decided_at: Optional[datetime] = None
    decision_reason: Optional[str] = None
    executed_at: Optional[datetime] = None
    idempotency_key: Optional[str] = None

    # ---- 7-stage pipeline additions (Issue #112 §"Lease Renewal") ----
    scan_window_days: Optional[int] = None
    scan_key: Optional[str] = None
    contact_method: Optional[str] = None
    contacted_at: Optional[datetime] = None
    tenant_response: Optional[str] = None
    tenant_response_at: Optional[datetime] = None
    owner_decision: Optional[str] = None
    owner_decision_at: Optional[datetime] = None
    verified_at: Optional[datetime] = None
    verified_by_user_id: Optional[int] = None
    closed_at: Optional[datetime] = None
    closed_by_user_id: Optional[int] = None


class RenewalActivityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    renewal_id: int
    kind: str
    actor_user_id: Optional[int] = None
    detail: Optional[str] = None
    occurred_at: datetime


class RenewalExecuteResponse(BaseModel):
    """Return the renewal and the new lease after EXECUTED."""

    renewal: RenewalRead
    new_lease: LeaseRead


# ---- reuse rent_payment TaskRead / OperationRead via re-export -------

from app.v1.schemas.rent_payment import (  # noqa: E402, F401
    OperationRead,
    TaskRead,
)


__all__ = [
    "LeaseRead",
    "OperationRead",
    "RenewalActivityRead",
    "RenewalCancelRequest",
    "RenewalCloseRequest",
    "RenewalContactRequest",
    "RenewalDecisionRequest",
    "RenewalExecuteRequest",
    "RenewalExecuteResponse",
    "RenewalFollowUpCreate",
    "RenewalOwnerDecisionRequest",
    "RenewalProposeRequest",
    "RenewalRead",
    "RenewalResponseRequest",
    "RenewalScanRead",
    "RenewalScanRequest",
    "RenewalScanResponse",
    "RenewalVerifyRequest",
    "TaskRead",
]
