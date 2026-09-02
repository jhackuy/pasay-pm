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


# ---- frozen 7-stage pipeline (Issue #112 §"Lease Renewal") ------------


class RenewalScanRequest(BaseModel):
    """Body for ``POST /api/v1/renewals/scan``.

    ``window_days`` is the size of the lookahead window: every ACTIVE
    lease whose ``end_date`` is between today (inclusive) and today +
    ``window_days`` (inclusive) becomes a candidate renewal at
    ``DETECT_EXPIRY``.
    """

    model_config = ConfigDict(extra="forbid")

    window_days: int = Field(gt=0, le=365)


class RenewalContactRequest(BaseModel):
    """Body for ``POST /api/v1/renewals/proposals/{id}/contact``."""

    model_config = ConfigDict(extra="forbid")

    channel: Optional[str] = Field(default=None, max_length=32)
    note: Optional[str] = Field(default=None, max_length=500)


class RenewalResponseRequest(BaseModel):
    """Body for ``POST /api/v1/renewals/proposals/{id}/respond``.

    ``response`` is one of ``RENEW``, ``TERMINATE``, ``DEFER`` (see
    ``RenewalTenantResponse``).
    """

    model_config = ConfigDict(extra="forbid")

    response: str = Field(min_length=1, max_length=16)
    note: Optional[str] = Field(default=None, max_length=500)


class RenewalOwnerDecisionRequest(BaseModel):
    """Body for ``POST /api/v1/renewals/proposals/{id}/owner-decide``.

    ``decision`` is one of ``RENEW``, ``TERMINATE``, ``DEFER`` (see
    ``RenewalOwnerDecision``).
    """

    model_config = ConfigDict(extra="forbid")

    decision: str = Field(min_length=1, max_length=16)
    note: Optional[str] = Field(default=None, max_length=500)


class RenewalVerifyRequest(BaseModel):
    """Body for ``POST /api/v1/renewals/proposals/{id}/verify``."""

    model_config = ConfigDict(extra="forbid")

    note: Optional[str] = Field(default=None, max_length=500)


class RenewalCloseRequest(BaseModel):
    """Body for ``POST /api/v1/renewals/proposals/{id}/close``."""

    model_config = ConfigDict(extra="forbid")

    note: Optional[str] = Field(default=None, max_length=500)


class RenewalScanRead(BaseModel):
    """Read-only summary of a single renewal emitted by a scan.

    Returned by ``POST /api/v1/renewals/scan`` so the caller (cron /
    Telegram adapter / API client) can show the actual work it
    accomplished.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    state: str
    source_lease_id: int
    proposed_start_date: date
    proposed_end_date: date
    scan_window_days: Optional[int] = None
    is_new: bool


class RenewalScanResponse(BaseModel):
    """Response shape for ``POST /api/v1/renewals/scan``."""

    window_days: int
    replayed: bool
    count: int
    renewals: list[RenewalScanRead]


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
    idempotency_key: str
    # Frozen 7-stage pipeline (Issue #112 §"Lease Renewal") fields.
    scan_window_days: Optional[int] = None
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
