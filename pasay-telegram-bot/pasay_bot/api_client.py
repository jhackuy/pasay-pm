"""Typed Pasay PM API client (httpx, Bearer auth).

All financial writes go through this client — the bot never writes to
PostgreSQL directly. Timeouts / 4xx are surfaced as typed exceptions so the
handlers can implement the "uncertain write" reconciliation path.

Issue #119 P0 STALE-CONNECTION RESILIENCE (Owner override 2026-09-09):
the bot's single long-lived ``httpx.AsyncClient`` keeps a small keep-alive
pool (see ``httpx.Limits`` below). A peer that drops its TLS connection
without a response (``Server disconnected without sending a response`` =
``httpx.RemoteProtocolError``) is silent on the next pooled read. For
idempotent READ requests (GET / HEAD only) we now classify the
transport-level stale/disconnect failure, recreate the client once with a
fresh connection pool, and retry the SAME request exactly once. WRITES
(POST / PATCH / PUT / DELETE) are NEVER auto-retried because the prior
write may already have committed server-side — the bot must follow the
existing "uncertain write" reconciliation path instead.
"""
from __future__ import annotations

import logging
import time as _time_module
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from contextvars import ContextVar

import httpx

# Issue #119 P0 WARM-PATH TRACE: module-level logger so per-V1-request
# observability lines (``pasay_v1_request ...``) surface under the
# ``pasay_bot.api_client`` namespace. Operators can filter with
# ``grep 'pasay_v1_request'``.
_logger = logging.getLogger("pasay_bot.api_client")


# Methods that are SAFE to silently retry on a stale/disconnected pooled
# HTTP connection. RFC 9110 §9.2.2 idempotency: GET, HEAD, PUT (with a
# natural idempotency contract) and DELETE are idempotent. We restrict to
# GET (and HEAD) here because the bot only has GET-read business needs;
# PUT/DELETE are not used by any of the six frozen menu routes nor by
# the SYSTEM jobs path, so retrying them would add risk for zero gain.
#
# Everything else (POST / PATCH / PUT / DELETE / custom verbs) is a write
# (or its commit state is otherwise uncertain), and an automatic retry
# could double-apply a financial mutation. The caller owns reconciliation
# via ``PasayApiTimeoutError`` / 409.
_READ_METHODS = frozenset({"GET", "HEAD"})

# Transport-level exceptions that signal "the pooled connection died, the
# server has nothing for us to read". We DO NOT retry on
# ``TimeoutException`` — a timeout means the server MAY have processed
# the request but the response was lost, which on a write is exactly the
# uncertain-write case the existing reconciliation path handles.
#
# Note: ``httpx.ReadError`` and ``httpx.RemoteProtocolError`` are the two
# exact exceptions produced by ``Server disconnected without sending a
# response`` (the Owner's production fingerprint). We also include
# ``CloseError`` (peer closed the idle keep-alive socket) and the broader
# ``ConnectError`` (TCP/TLS refused after the keep-alive reset) so any
# pool-reset variant is covered.
_STALE_CONNECTION_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.CloseError,
)

# ``ConnectError`` lives on ``httpx.TransportError`` (same parent as
# ``RemoteProtocolError``) and is added separately because it is NOT a
# subclass of any of the four above; it covers the case where the
# fresh client falls back to a fresh TCP connect that itself refuses.
if hasattr(httpx, "ConnectError"):
    _STALE_CONNECTION_EXCEPTIONS = _STALE_CONNECTION_EXCEPTIONS + (
        httpx.ConnectError,
    )

MAX_TELEGRAM_USER_ID = 2**63 - 1


def _time_ms() -> float:
    """Wall-clock milliseconds for callback phase profiling (007A A)."""
    return _time_module.monotonic() * 1000


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return Decimal("0")


def _to_date(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


@dataclass
class Property:
    id: int
    name: str
    address: str
    city: str
    total_units: int = 0
    is_active: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "Property":
        return cls(
            id=int(d.get("id", 0)),
            name=d.get("name") or "",
            address=d.get("address") or "",
            city=d.get("city") or "",
            total_units=int(d.get("total_units") or 0),
            is_active=bool(d.get("is_active", True)),
        )


@dataclass
class Unit:
    id: int
    property_id: int
    unit_number: str
    floor: Optional[str] = None
    size_sqm: Optional[Decimal] = None
    monthly_rent: Decimal = Decimal("0")
    status: str = "vacant"
    is_active: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "Unit":
        return cls(
            id=int(d.get("id", 0)),
            property_id=int(d.get("property_id") or 0),
            unit_number=d.get("unit_number") or "",
            floor=d.get("floor"),
            size_sqm=_to_decimal(d.get("size_sqm")) if d.get("size_sqm") is not None else None,
            monthly_rent=_to_decimal(d.get("monthly_rent")),
            status=d.get("status") or "vacant",
            is_active=bool(d.get("is_active", True)),
        )

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "property_id": self.property_id,
            "unit_number": self.unit_number,
            "floor": self.floor,
            "size_sqm": str(self.size_sqm) if self.size_sqm is not None else None,
            "monthly_rent": str(self.monthly_rent),
            "status": self.status,
            "is_active": self.is_active,
        }


@dataclass
class Lease:
    id: int
    unit_id: int
    tenant_id: int
    start_date: date
    end_date: date
    accounting_start_date: Optional[date] = None
    monthly_rent: Decimal = Decimal("0")
    deposit: Decimal = Decimal("0")
    status: str = "active"
    due_day: Optional[int] = None
    notes: Optional[str] = None

    @classmethod
    def from_dict(cls, d: dict) -> "Lease":
        return cls(
            id=int(d.get("id", 0)),
            unit_id=int(d.get("unit_id") or 0),
            tenant_id=int(d.get("tenant_id") or 0),
            start_date=_to_date(d.get("start_date")) or date.today(),
            end_date=_to_date(d.get("end_date")) or date.today(),
            accounting_start_date=_to_date(d.get("accounting_start_date")),
            monthly_rent=_to_decimal(d.get("monthly_rent")),
            deposit=_to_decimal(d.get("deposit")),
            status=d.get("status") or "active",
            due_day=d.get("due_day"),
            notes=d.get("notes"),
        )


@dataclass
class Tenant:
    id: int
    full_name: str
    phone: Optional[str] = None
    email: Optional[str] = None
    secondary_phone: Optional[str] = None
    contact_status: Optional[str] = None
    id_registered: bool = False
    emergency_name: Optional[str] = None
    emergency_phone: Optional[str] = None

    @classmethod
    def from_dict(cls, d: dict) -> "Tenant":
        return cls(
            id=int(d.get("id", 0)),
            full_name=d.get("full_name") or "",
            phone=d.get("phone"),
            email=d.get("email"),
            secondary_phone=d.get("secondary_phone"),
            contact_status=d.get("contact_status"),
            id_registered=bool(d.get("id_registered")),
            emergency_name=d.get("emergency_name"),
            emergency_phone=d.get("emergency_phone"),
        )

    @property
    def available_phone(self) -> Optional[str]:
        return self.phone or self.secondary_phone


@dataclass
class Income:
    id: int
    lease_id: Optional[int] = None
    amount: Decimal = Decimal("0")
    received_date: date = date.today()
    payment_method: Optional[str] = None
    idempotency_key: Optional[str] = None
    status: str = "pending"
    description: Optional[str] = None
    confirmed_by: Optional[int] = None
    confirmed_at: Optional[str] = None

    @classmethod
    def from_dict(cls, d: dict) -> "Income":
        return cls(
            id=int(d.get("id", 0)),
            lease_id=d.get("lease_id"),
            amount=_to_decimal(d.get("amount")),
            received_date=_to_date(d.get("received_date")) or date.today(),
            payment_method=d.get("payment_method"),
            idempotency_key=d.get("idempotency_key"),
            status=d.get("status") or "pending",
            description=d.get("description"),
            confirmed_by=d.get("confirmed_by"),
            confirmed_at=d.get("confirmed_at"),
        )

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "lease_id": self.lease_id,
            "amount": str(self.amount),
            "received_date": self.received_date.isoformat(),
            "payment_method": self.payment_method,
            "idempotency_key": self.idempotency_key,
            "status": self.status,
            "description": self.description,
            "confirmed_by": self.confirmed_by,
            "confirmed_at": self.confirmed_at,
        }


@dataclass
class RentMatchCandidate:
    """One candidate from POST /payments/match (Slice 2, Entry B).

    ``kind``/``confidence`` carry enum VALUES (open/pending/duplicate and
    high/medium/low); the bot renders them as human text and never shows the
    raw values."""

    kind: str = "open"
    confidence: str = "low"
    lease_id: int = 0
    unit_id: int = 0
    unit_number: str = ""
    property_id: int = 0
    property_name: str = ""
    tenant_id: int = 0
    tenant_name: str = ""
    period: str = ""
    due_date: Optional[date] = None
    amount: Decimal = Decimal("0")
    open_count: int = 0
    due_amount: Decimal = Decimal("0")
    paid_amount: Decimal = Decimal("0")
    remaining_balance: Decimal = Decimal("0")
    income_id: Optional[int] = None
    income_status: Optional[str] = None

    @classmethod
    def from_dict(cls, d: dict) -> "RentMatchCandidate":
        return cls(
            kind=d.get("kind") or "open",
            confidence=d.get("confidence") or "low",
            lease_id=int(d.get("lease_id") or 0),
            unit_id=int(d.get("unit_id") or 0),
            unit_number=d.get("unit_number") or "",
            property_id=int(d.get("property_id") or 0),
            property_name=d.get("property_name") or "",
            tenant_id=int(d.get("tenant_id") or 0),
            tenant_name=d.get("tenant_name") or "",
            period=d.get("period") or "",
            due_date=_to_date(d.get("due_date")),
            amount=_to_decimal(d.get("amount")),
            open_count=int(d.get("open_count") or 0),
            due_amount=_to_decimal(d.get("due_amount")),
            paid_amount=_to_decimal(d.get("paid_amount")),
            remaining_balance=_to_decimal(d.get("remaining_balance")),
            income_id=int(d["income_id"]) if d.get("income_id") is not None else None,
            income_status=d.get("income_status"),
        )


@dataclass
class RentMatchResult:
    received_date: date = date.today()
    candidates: list[RentMatchCandidate] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.candidates is None:
            self.candidates = []

    @property
    def best(self) -> Optional[RentMatchCandidate]:
        return self.candidates[0] if self.candidates else None

    @classmethod
    def from_dict(cls, d: dict) -> "RentMatchResult":
        return cls(
            received_date=_to_date(d.get("received_date")) or date.today(),
            candidates=[RentMatchCandidate.from_dict(c) for c in (d.get("candidates") or [])],
        )


@dataclass
class Expense:
    """Expense record (V1.3 expense approval). ``status`` is one of the backend
    values (pending/approved/rejected/paid/reversed/payment_claimed/partially_paid);
    UI text is derived in render/cards.py — never shown raw.

    PASAY-EXPENSE-OPERATION-003B: also carries the derived payment truth
    (verified_paid / remaining / claims) so the bot can never render a PENDING
    claim as paid (§14 / E16) and can show the real remaining balance (§4)."""

    id: int
    expense_date: date = date.today()
    due_date: Optional[date] = None
    category: str = ""
    amount: Decimal = Decimal("0")
    payee: str = ""
    description: Optional[str] = None
    unit_id: Optional[int] = None
    status: str = "pending"
    receipt_attachment_id: Optional[int] = None
    approved_by: Optional[int] = None
    approved_at: Optional[str] = None
    payer_user_id: Optional[int] = None
    # 003B payment truth (from GET /expenses/{id}/detail). Defaults keep a
    # bare /expenses/{id} read safe when no detail payload is present.
    verified_paid: Decimal = Decimal("0")
    remaining: Decimal = Decimal("0")
    fully_paid: bool = False
    pending_claims: int = 0
    claims: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "Expense":
        payment = d.get("payment") or {}
        return cls(
            id=int(d.get("id") or 0),
            expense_date=_to_date(d.get("expense_date")) or date.today(),
            due_date=_to_date(d.get("due_date")),
            category=d.get("category") or "",
            amount=_to_decimal(d.get("amount")),
            payee=d.get("payee") or "",
            description=d.get("description"),
            unit_id=int(d["unit_id"]) if d.get("unit_id") is not None else None,
            status=d.get("status") or "pending",
            receipt_attachment_id=(
                int(d["receipt_attachment_id"])
                if d.get("receipt_attachment_id") is not None else None
            ),
            approved_by=int(d["approved_by"]) if d.get("approved_by") is not None else None,
            approved_at=d.get("approved_at"),
            payer_user_id=(
                int(d["payer_user_id"]) if d.get("payer_user_id") is not None else None
            ),
            verified_paid=_to_decimal(payment.get("verified_paid")) if payment.get("verified_paid") else Decimal("0"),
            remaining=_to_decimal(payment.get("remaining")) if payment.get("remaining") else Decimal(_to_decimal(d.get("amount"))),
            fully_paid=bool(payment.get("fully_paid")),
            pending_claims=int(payment.get("pending_claims") or 0),
            claims=[dict(c) for c in (payment.get("claims") or [])],
        )

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "expense_date": self.expense_date.isoformat(),
            "due_date": self.due_date.isoformat() if self.due_date else None,
            "category": self.category,
            "amount": str(self.amount),
            "payee": self.payee,
            "description": self.description,
            "unit_id": self.unit_id,
            "status": self.status,
            "receipt_attachment_id": self.receipt_attachment_id,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "payer_user_id": self.payer_user_id,
        }


@dataclass
class FinancialSummary:
    month: str = ""
    expected_rent_total: Decimal = Decimal("0")
    collected_rent: Decimal = Decimal("0")
    outstanding_rent: Decimal = Decimal("0")
    total_income: Decimal = Decimal("0")
    total_expense: Decimal = Decimal("0")
    net_income: Decimal = Decimal("0")
    units_count: int = 0
    occupied_units: int = 0
    vacant_units: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> "FinancialSummary":
        return cls(
            month=d.get("month") or "",
            expected_rent_total=_to_decimal(d.get("expected_rent_total")),
            collected_rent=_to_decimal(d.get("collected_rent")),
            outstanding_rent=_to_decimal(d.get("outstanding_rent")),
            total_income=_to_decimal(d.get("total_income")),
            total_expense=_to_decimal(d.get("total_expense")),
            net_income=_to_decimal(d.get("net_income")),
            units_count=int(d.get("units_count") or 0),
            occupied_units=int(d.get("occupied_units") or 0),
            vacant_units=int(d.get("vacant_units") or 0),
        )


@dataclass
class OverdueRent:
    lease_id: int
    unit_id: int
    tenant_id: int
    unit: str
    tenant: str
    overdue_months: int = 0
    amount_per_month: Decimal = Decimal("0")
    total_outstanding: Decimal = Decimal("0")
    oldest_due_date: date = date.today()
    overdue_days: int = 0
    overdue_periods: list = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.overdue_periods is None:
            self.overdue_periods = []

    @classmethod
    def from_dict(cls, d: dict) -> "OverdueRent":
        return cls(
            lease_id=int(d.get("lease_id") or 0),
            unit_id=int(d.get("unit_id") or 0),
            tenant_id=int(d.get("tenant_id") or 0),
            unit=d.get("unit") or "",
            tenant=d.get("tenant") or "",
            overdue_months=int(d.get("overdue_months") or 0),
            amount_per_month=_to_decimal(d.get("amount_per_month")),
            total_outstanding=_to_decimal(d.get("total_outstanding")),
            oldest_due_date=_to_date(d.get("oldest_due_date")) or date.today(),
            overdue_days=int(d.get("overdue_days") or d.get("days_overdue") or 0),
            overdue_periods=d.get("overdue_periods") or [],
        )


@dataclass
class ReportTask:
    id: int
    title: str
    unit_id: Optional[int] = None
    unit: Optional[str] = None
    status: str = "open"
    priority: str = "medium"
    due_date: Optional[date] = None
    assigned_to: Optional[int] = None
    recurring: bool = False
    interval_months: Optional[int] = None
    next_due_date: Optional[date] = None

    @classmethod
    def from_dict(cls, d: dict) -> "ReportTask":
        return cls(
            id=int(d.get("id") or 0),
            title=d.get("title") or "",
            unit_id=int(d["unit_id"]) if d.get("unit_id") is not None else None,
            unit=d.get("unit"),
            status=d.get("status") or "open",
            priority=d.get("priority") or "medium",
            due_date=_to_date(d.get("due_date")),
            assigned_to=int(d["assigned_to"]) if d.get("assigned_to") is not None else None,
            recurring=bool(d.get("recurring", False)),
            interval_months=d.get("interval_months"),
            next_due_date=_to_date(d.get("next_due_date")),
        )


@dataclass
class OperationalTask:
    id: int
    task_type: str = ""
    title: str = ""
    description: Optional[str] = None
    property_id: Optional[int] = None
    property_code: Optional[str] = None
    tenant_id: Optional[int] = None
    lease_id: Optional[int] = None
    source_type: str = ""
    source_id: Optional[int] = None
    source_event: Optional[str] = None
    assigned_user_id: Optional[int] = None
    priority: str = "medium"
    status: str = "PENDING"
    due_at: Optional[str] = None
    snoozed_until: Optional[str] = None
    next_action: Optional[str] = None
    next_check_at: Optional[str] = None
    context: Optional[str] = None
    completion_condition: Optional[str] = None
    completed_at: Optional[str] = None
    details: Optional[dict] = None

    @classmethod
    def from_dict(cls, d: dict) -> "OperationalTask":
        return cls(
            id=int(d.get("id") or 0),
            task_type=d.get("task_type") or "",
            title=d.get("title") or "",
            description=d.get("description"),
            property_id=int(d["property_id"]) if d.get("property_id") is not None else None,
            property_code=d.get("property_code"),
            tenant_id=int(d["tenant_id"]) if d.get("tenant_id") is not None else None,
            lease_id=int(d["lease_id"]) if d.get("lease_id") is not None else None,
            source_type=d.get("source_type") or "",
            source_id=int(d["source_id"]) if d.get("source_id") is not None else None,
            source_event=d.get("source_event"),
            assigned_user_id=int(d["assigned_user_id"]) if d.get("assigned_user_id") is not None else None,
            priority=d.get("priority") or "medium",
            status=d.get("status") or "PENDING",
            due_at=d.get("due_at"),
            snoozed_until=d.get("snoozed_until"),
            next_action=d.get("next_action"),
            next_check_at=d.get("next_check_at"),
            context=d.get("context"),
            completion_condition=d.get("completion_condition"),
            completed_at=d.get("completed_at"),
            details=d.get("details") or {},
        )


@dataclass
class CopilotTodayItem:
    """One item in the read-only TODAY brief (C1). Only human text is exposed
    to the end user; backend entity refs stay internal."""

    item_ref: str = ""
    reason_why_important: str = ""
    suggested_action: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "CopilotTodayItem":
        return cls(
            item_ref=d.get("item_ref") or "",
            reason_why_important=d.get("reason_why_important") or "",
            suggested_action=d.get("suggested_action") or "",
        )


@dataclass
class CopilotToday:
    """Read-only TODAY brief from POST /operations/copilot/today (C1)."""

    top_items: list[CopilotTodayItem]
    summary: str = ""
    context_schema_version: str = "1.0"
    provider: str = ""
    model: str = ""
    latency_ms: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> "CopilotToday":
        return cls(
            top_items=[CopilotTodayItem.from_dict(i) for i in (d.get("top_items") or [])],
            summary=d.get("summary") or "",
            context_schema_version=d.get("context_schema_version") or "1.0",
            provider=d.get("provider") or "",
            model=d.get("model") or "",
            latency_ms=int(d.get("latency_ms") or 0),
        )


@dataclass
class CopilotWhy:
    """Per-item WHY enrichment (C1.1). ``fallback`` True when the provider was
    down and the deterministic reason was returned instead."""

    item_ref: str = ""
    explanation: str = ""
    recommendation: str = ""
    provider: str = ""
    model: str = ""
    fallback: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> "CopilotWhy":
        return cls(
            item_ref=d.get("item_ref") or "",
            explanation=d.get("explanation") or "",
            recommendation=d.get("recommendation") or "",
            provider=d.get("provider") or "",
            model=d.get("model") or "",
            fallback=bool(d.get("fallback")),
        )


@dataclass
class CopilotAsk:
    """Q&A answer (C1.1). ``fallback`` True when provider-down gave the friendly
    deterministic response."""

    answer: str = ""
    provider: str = ""
    model: str = ""
    fallback: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> "CopilotAsk":
        return cls(
            answer=d.get("answer") or "",
            provider=d.get("provider") or "",
            model=d.get("model") or "",
            fallback=bool(d.get("fallback")),
        )


@dataclass
class NlIntentResult:
    """Structured NL intent from the backend AI fallback lane (P0-5).

    The bot never displays these fields raw; it maps ``intent`` + validated
    entities into its own deterministic business paths.
    """

    intent: str = ""
    message: str = ""
    unit: str = ""
    unit_id: Optional[int] = None
    amount: Optional[Decimal] = None
    category: str = ""
    month: str = ""
    missing: list[str] = field(default_factory=list)
    options: list[str] = field(default_factory=list)
    provider: str = ""
    model: str = "deterministic"
    fallback: bool = False
    flags: list[str] = field(default_factory=list)
    latency_ms: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> "NlIntentResult":
        try:
            amount = _to_decimal(d.get("amount")) if d.get("amount") not in (None, "") else None
        except Exception:
            amount = None
        return cls(
            intent=d.get("intent") or "",
            message=d.get("message") or "",
            unit=d.get("unit") or "",
            unit_id=int(d["unit_id"]) if d.get("unit_id") is not None else None,
            amount=amount,
            category=d.get("category") or "",
            month=d.get("month") or "",
            missing=list(d.get("missing") or []),
            options=list(d.get("options") or []),
            provider=d.get("provider") or "",
            model=d.get("model") or "deterministic",
            fallback=bool(d.get("fallback")),
            flags=list(d.get("flags") or []),
            latency_ms=int(d.get("latency_ms") or 0),
        )


@dataclass
class CopilotRecommendCard:
    """Confirmation-card data from POST /operations/copilot/recommend (C2).
    Render-safe: the bot must NOT display the raw proposal_id."""

    action_type: str = ""
    target_type: str = ""
    target_id: int = 0
    target_label: str = ""
    reason_code: Optional[str] = None
    assignee_user_id: Optional[int] = None
    assignee_name: Optional[str] = None
    due_at: Optional[str] = None
    note: Optional[str] = None
    display_context: dict = None  # type: ignore[assignment]

    @classmethod
    def from_dict(cls, d: dict) -> "CopilotRecommendCard":
        return cls(
            action_type=d.get("action_type") or "",
            target_type=d.get("target_type") or "",
            target_id=int(d.get("target_id") or 0),
            target_label=d.get("target_label") or "",
            reason_code=d.get("reason_code"),
            assignee_user_id=(
                int(d["assignee_user_id"]) if d.get("assignee_user_id") is not None else None
            ),
            assignee_name=d.get("assignee_name"),
            due_at=d.get("due_at"),
            note=d.get("note"),
            display_context=d.get("display_context") or {},
        )


@dataclass
class CopilotRecommend:
    """Canonical PENDING proposal + card from /copilot/recommend (C2)."""

    proposal_id: int = 0
    action_type: str = ""
    status: str = ""
    target_type: str = ""
    target_id: int = 0
    idempotency_key: str = ""
    expires_at: Optional[str] = None
    card: Optional[CopilotRecommendCard] = None
    detail: str = ""
    created: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "CopilotRecommend":
        card = d.get("card") or {}
        return cls(
            proposal_id=int(d.get("proposal_id") or 0),
            action_type=d.get("action_type") or "",
            status=d.get("status") or "",
            target_type=d.get("target_type") or "",
            target_id=int(d.get("target_id") or 0),
            idempotency_key=d.get("idempotency_key") or "",
            expires_at=d.get("expires_at"),
            card=CopilotRecommendCard.from_dict(card) if card else None,
            detail=d.get("detail") or "",
            created=bool(d.get("created", True)),
        )


@dataclass
class CopilotExecute:
    """Result of POST /operations/copilot/proposals/{id}/execute (C2)."""

    action_type: str = ""
    target_type: str = ""
    target_id: int = 0
    task_id: Optional[int] = None
    assignee_user_id: Optional[int] = None
    due_at: Optional[str] = None
    executed_at: Optional[str] = None
    status: str = ""
    replay: bool = False
    detail: str = ""
    proposal_id: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> "CopilotExecute":
        result = d.get("result") or {}
        proposal = d.get("proposal") or {}
        return cls(
            action_type=result.get("action_type") or "",
            target_type=result.get("target_type") or "",
            target_id=int(result.get("target_id") or 0),
            task_id=int(result["task_id"]) if result.get("task_id") is not None else None,
            assignee_user_id=(
                int(result["assignee_user_id"])
                if result.get("assignee_user_id") is not None else None
            ),
            due_at=result.get("due_at"),
            executed_at=result.get("executed_at"),
            status=result.get("status") or "",
            replay=bool(result.get("replay")),
            detail=result.get("detail") or "",
            proposal_id=int(proposal.get("id") or 0),
        )


@dataclass
class TaskFollowupDelivery:
    task: OperationalTask
    delivery_state: str = ""
    detail: str = ""
    telegram_message_id: Optional[int] = None

    @classmethod
    def from_dict(cls, d: dict) -> "TaskFollowupDelivery":
        return cls(
            task=OperationalTask.from_dict(d.get("task") or {}),
            delivery_state=d.get("delivery_state") or "",
            detail=d.get("detail") or "",
            telegram_message_id=(
                int(d["telegram_message_id"])
                if d.get("telegram_message_id") is not None else None
            ),
        )


@dataclass
class RepairProposal:
    """One versioned solution candidate (PENDING/APPROVED/REJECTED/SUPERSEDED).
    Fully decoupled from the Repair Operation — rejecting a proposal never
    rejects the repair."""

    id: int
    repair_id: int = 0
    version: int = 1
    vendor: Optional[str] = None
    source: Optional[str] = None
    description: Optional[str] = None
    amount: Decimal = Decimal("0")
    submitted_by: Optional[int] = None
    submitted_at: Optional[str] = None
    status: str = "PENDING"
    decision_by: Optional[int] = None
    decision_at: Optional[str] = None
    rejection_reason: Optional[str] = None
    expense_id: Optional[int] = None

    @classmethod
    def from_dict(cls, d: dict) -> "RepairProposal":
        return cls(
            id=int(d.get("id") or 0),
            repair_id=int(d.get("repair_id") or 0),
            version=int(d.get("version") or 1),
            vendor=d.get("vendor"),
            source=d.get("source"),
            description=d.get("description"),
            amount=_to_decimal(d.get("amount")),
            submitted_by=d.get("submitted_by"),
            submitted_at=d.get("submitted_at"),
            status=d.get("status") or "PENDING",
            decision_by=d.get("decision_by"),
            decision_at=d.get("decision_at"),
            rejection_reason=d.get("rejection_reason"),
            expense_id=int(d["expense_id"]) if d.get("expense_id") is not None else None,
        )


@dataclass
class RepairAction:
    """One idempotent AI-employee action (the single most important next step
    a human must do now). status PENDING/IN_PROGRESS/COMPLETED/CANCELLED."""

    id: int
    repair_id: int = 0
    action_kind: str = ""
    title: str = ""
    description: Optional[str] = None
    status: str = "PENDING"
    assigned_user_id: Optional[int] = None
    due_at: Optional[str] = None
    next_check_at: Optional[str] = None
    dedupe_key: str = ""
    source_event: Optional[str] = None
    resolved_at: Optional[str] = None
    resolved_by: Optional[int] = None
    created_at: Optional[str] = None

    @classmethod
    def from_dict(cls, d: dict) -> "RepairAction":
        return cls(
            id=int(d.get("id") or 0),
            repair_id=int(d.get("repair_id") or 0),
            action_kind=d.get("action_kind") or "",
            title=d.get("title") or "",
            description=d.get("description"),
            status=d.get("status") or "PENDING",
            assigned_user_id=int(d["assigned_user_id"]) if d.get("assigned_user_id") is not None else None,
            due_at=d.get("due_at"),
            next_check_at=d.get("next_check_at"),
            dedupe_key=d.get("dedupe_key") or "",
            source_event=d.get("source_event"),
            resolved_at=d.get("resolved_at"),
            resolved_by=int(d["resolved_by"]) if d.get("resolved_by") is not None else None,
            created_at=d.get("created_at"),
        )


@dataclass
class RepairOperation:
    """The REAL-world repair problem (008A). ``status`` is one of
    OPEN/IN_PROGRESS/WAITING_HUMAN/WAITING_VENDOR/WAITING_APPROVAL/
    WAITING_PAYMENT/VERIFYING/CLOSED/CANCELLED. The derived fields
    ``next_action``/``waiting_on``/``blocked_reason`` are the single source
    both Telegram and the Mini App read — never chat copy."""

    id: int
    merchant_id: Optional[int] = None
    property_id: Optional[int] = None
    unit_id: Optional[int] = None
    issue: str = ""
    issue_description: Optional[str] = None
    created_source: str = "manual"
    reported_by: Optional[int] = None
    assignee_user_id: Optional[int] = None
    status: str = "OPEN"
    next_action: Optional[str] = None
    waiting_on: Optional[str] = None
    blocked_reason: Optional[str] = None
    next_check_at: Optional[str] = None
    closure_criteria: Optional[str] = None
    verified_by: Optional[int] = None
    verified_at: Optional[str] = None
    verification_result: Optional[str] = None
    closed_at: Optional[str] = None
    closure_reason: Optional[str] = None
    operational_task_id: Optional[int] = None
    created_at: Optional[str] = None

    proposals: list = None  # type: ignore[assignment]
    actions: list = None  # type: ignore[assignment]
    expense_ids: list = None  # type: ignore[assignment]
    timeline: list = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.proposals is None:
            self.proposals = []
        if self.actions is None:
            self.actions = []
        if self.expense_ids is None:
            self.expense_ids = []
        if self.timeline is None:
            self.timeline = []

    @classmethod
    def from_dict(cls, d: dict) -> "RepairOperation":
        return cls(
            id=int(d.get("id") or 0),
            merchant_id=d.get("merchant_id"),
            property_id=d.get("property_id"),
            unit_id=d.get("unit_id"),
            issue=d.get("issue") or "",
            issue_description=d.get("issue_description"),
            created_source=d.get("created_source") or "manual",
            reported_by=d.get("reported_by"),
            assignee_user_id=d.get("assignee_user_id"),
            status=d.get("status") or "OPEN",
            next_action=d.get("next_action"),
            waiting_on=d.get("waiting_on"),
            blocked_reason=d.get("blocked_reason"),
            next_check_at=d.get("next_check_at"),
            closure_criteria=d.get("closure_criteria"),
            verified_by=d.get("verified_by"),
            verified_at=d.get("verified_at"),
            verification_result=d.get("verification_result"),
            closed_at=d.get("closed_at"),
            closure_reason=d.get("closure_reason"),
            operational_task_id=d.get("operational_task_id"),
            created_at=d.get("created_at"),
            proposals=[RepairProposal.from_dict(p) for p in (d.get("proposals") or [])],
            actions=[RepairAction.from_dict(a) for a in (d.get("actions") or [])],
            expense_ids=list(d.get("expense_ids") or []),
            timeline=list(d.get("timeline") or []),
        )


class PasayApiError(Exception):
    """API failure with an optional backend ``error_code``.
    The backend surfaces fail-closed copilot rejections as a structured 409
    ``{"message": ..., "error_code": ...}``. The bot maps ``error_code`` to
    human strings (never showing the raw code).
    """

    def __init__(
        self,
        status_code: Optional[int],
        detail: str,
        error_code: Optional[str] = None,
    ):
        self.status_code = status_code
        self.detail = detail
        self.error_code = error_code
        super().__init__(f"Pasay API error {status_code}: {detail}")

class PasayApiAuthError(PasayApiError):
    """401 — invalid/missing API key."""


class PasayApiPermissionError(PasayApiError):
    """403 — API key role insufficient (backend enforcement)."""


class PasayApiConflictError(PasayApiError):
    """409 — e.g. "Only pending income can be confirmed" -> already handled."""


class PasayApiTimeoutError(PasayApiError):
    """Request outcome is UNKNOWN (may have been applied server-side)."""

    def __init__(self, message: str = "Pasay API timed out"):
        super().__init__(None, message)


def _extract_detail(resp: httpx.Response) -> tuple[str, Optional[str]]:
    """Return ``(detail, error_code)`` from an error response body.

    Structured 409s carry ``{"message": ..., "error_code": ...}`` under
    ``detail``; plain-string details return ``error_code=None``.
    """
    try:
        body = resp.json()
    except Exception:
        return (resp.text or "").strip() or f"HTTP {resp.status_code}", None
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, dict):
            return (
                str(detail.get("message") or detail),
                str(detail.get("error_code") or None) or None,
            )
        return str(detail or body), None
    return str(body), None

class PasayApiClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 10.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        *,
        system_org_id: Optional[int] = None,
        enable_http2: Optional[bool] = None,
    ):
        """Construct a typed PASAY API client.

        Issue #119 P0 (independent review follow-up): ``system_org_id``
        is the canonical ``trusted_organization_id`` for the SYSTEM
        scheduled-job endpoints (``/api/v1/operations/digest`` and
        ``/api/v1/operations/quick/tasks``). The V1 endpoints are
        server-side bound to a single org per SYSTEM credential; the
        client just needs to know the right id to pass in the query.
        A value of 0 (or ``None``) is treated as "no canonical org" —
        the bot-side ``PasayApiClient.get_digest`` and
        ``get_quick_tasks`` will then NOT inject any ``org_id`` and
        the server will reject the request. The value MUST be set
        for the SYSTEM job path (``PASSAY_SYSTEM_ORG_ID`` Worker
        env).

        Issue #119 P0 LATENCY: HTTP/2 is enabled by default when the
        ``h2`` package is available (the production image installs
        ``httpx[http2]``). Cloudflare's edge terminates HTTP/2 on the
        Worker-to-Container hop and on the V1 backend; multiplexing
        concurrent V1 reads (e.g. the Home/five-quick-view paths)
        onto ONE HTTP/2 stream cuts the per-tap p50 backend-fetch
        latency in production. ``enable_http2=False`` forces the
        legacy HTTP/1.1 transport (tests + opt-out).

        Issue #119 P0 STALE-CONNECTION RESILIENCE: the configured
        ``timeout``, ``enable_http2``, and the user-supplied
        ``transport`` are remembered so a transport-level stale
        connection can be re-opened (fresh keep-alive pool, same
        auth/org headers, same per-request timeout, same HTTP/2
        mode, same transport if injected for tests). The bot owns
        exactly one logical HTTP session; the pool can be re-opened
        transparently for idempotent READ retries.
        """
        self._telegram_user_id: ContextVar[int | None] = ContextVar(
            f"telegram_user_id_{id(self)}", default=None)
        self.base_url = base_url.rstrip("/")
        # Normalise system_org_id: only positive ints are honoured.
        if isinstance(system_org_id, bool) or not isinstance(system_org_id, int):
            self.system_org_id: Optional[int] = (
                int(system_org_id) if isinstance(system_org_id, int) and system_org_id > 0 else None
            )
        else:
            self.system_org_id = system_org_id if system_org_id > 0 else None
        # HTTP/2 default-on when the optional ``h2`` package is importable.
        # ``enable_http2`` may override (e.g. a future regression test
        # wants to force HTTP/1.1 to compare).
        if enable_http2 is None:
            try:
                import h2  # noqa: F401  -- probe-only import
                enable_http2 = True
            except ImportError:
                enable_http2 = False
        # Sized keepalive pool: small in absolute terms (Cloudflare Container
        # is single-process; PTB also runs as a single worker). Cap the
        # keepalive conn pool to 16 and the overall concurrency to 32 so a
        # pathological burst can never exhaust the FDs.
        _limits = httpx.Limits(
            max_connections=32,
            max_keepalive_connections=16,
            keepalive_expiry=30.0,
        )
        # Stash the auth/org headers, timeout, limits, http2 flag, and the
        # optional test transport so a stale-connection retry can reopen
        # the client with identical semantics. NEVER log the raw token.
        self._api_key = api_key
        self._timeout = float(timeout)
        self._limits = _limits
        self._enable_http2 = bool(enable_http2)
        self._transport = transport
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            timeout=httpx.Timeout(timeout),
            transport=transport,
            limits=_limits,
            http2=bool(enable_http2),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _build_fresh_client(self) -> httpx.AsyncClient:
        """Reopen a fresh ``httpx.AsyncClient`` with identical auth/limits/etc.

        Issue #119 P0 STALE-CONNECTION RESILIENCE (Owner override
        2026-09-09): when a pooled keep-alive connection silently dies
        (peer sends FIN/RST without bytes), httpx raises
        ``RemoteProtocolError`` (the Owner's
        ``Server disconnected without sending a response``). Reusing
        the same pool would keep retrying the dead socket. Reopening
        the client drops the stale keep-alive map; the next request
        goes over a fresh TCP/TLS connection that the peer has not
        yet seen. Auth/org headers, timeout, http2 flag, limits, and
        the optional injected test transport are preserved exactly.
        """
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self._api_key}"}
            if self._api_key
            else {},
            timeout=httpx.Timeout(self._timeout),
            transport=self._transport,
            limits=self._limits,
            http2=self._enable_http2,
        )

    async def _reopen_client(self) -> None:
        """Best-effort close the old pool + install a fresh client.

        The old ``aclose()`` may itself raise (e.g. when the failing
        socket is mid-read). Swallow that — we are explicitly
        abandoning the old pool. The fresh client is the only path
        the next request will use.
        """
        old = self._client
        try:
            await old.aclose()
        except Exception:  # noqa: BLE001 - abandonment is intentional
            pass
        self._client = self._build_fresh_client()

    @staticmethod
    def _is_stale_connection_error(exc: BaseException) -> bool:
        """Classify a transport exception as a stale/disconnected pool entry.

        Only transport-level exceptions are eligible. ``TimeoutException``
        is intentionally excluded — a timeout means the request MAY have
        reached the server; the safe path for an uncertain write is the
        existing ``PasayApiTimeoutError`` reconciliation, never an
        automatic retry of the same call.
        """
        if isinstance(exc, httpx.TimeoutException):
            return False
        return isinstance(exc, _STALE_CONNECTION_EXCEPTIONS)

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        user_id = self._telegram_user_id.get()
        if user_id is not None:
            headers = dict(kwargs.pop("headers", {}) or {})
            headers["X-Telegram-User-Id"] = str(user_id)
            kwargs["headers"] = headers
        _pstart = _time_ms()
        # Issue #119 P0 WARM-PATH TRACE: pull the per-update trace_id off
        # the ContextVar so this V1 request can be correlated with the
        # Worker ingress / Container dispatch / PTB handler that triggered
        # it. The fallback ``""`` keeps the log line shape stable for
        # callers that ran outside the webhook path (legacy tests).
        _trace_id = ""
        try:
            from app.services.telegram_webhook import current_trace_id  # type: ignore
            _trace_id = current_trace_id() or ""
        except Exception:  # noqa: BLE001 - instrumentation never breaks the request
            _trace_id = ""
        # Path-only: the URL builder already attached the base_url; we only
        # log the relative path + the sanitised query string (V1 endpoints
        # never carry the bearer in the path — the bearer is in the
        # ``Authorization`` header which we never log).
        _log_path = str(path)
        try:
            if "params" in kwargs and kwargs["params"]:
                from urllib.parse import urlencode
                _log_path = f"{_log_path}?{urlencode(kwargs['params'], doseq=True)}"
        except Exception:  # noqa: BLE001 - path logging is best-effort
            pass
        # Issue #119 P0 STALE-CONNECTION RESILIENCE (Owner override
        # 2026-09-09): for IDEMPOTENT reads only, allow exactly one
        # transparent retry after a transport-level stale/disconnected
        # pooled connection. The retry happens on a FRESH
        # ``httpx.AsyncClient`` (new keep-alive pool, same auth/org
        # headers, same per-request timeout, same HTTP/2 setting).
        # WRITES (POST / PATCH / PUT / DELETE / anything else) MUST
        # NEVER be auto-retried — the prior write may already have
        # committed and the existing ``PasayApiTimeoutError`` / 409
        # reconciliation path is the only safe way to handle that.
        method_upper = str(method).upper()
        _can_retry_once = method_upper in _READ_METHODS
        _attempt = 0
        _reopened = False
        while True:
            _attempt += 1
            try:
                resp = await self._client.request(method, path, **kwargs)
            except httpx.TimeoutException as exc:
                # Issue #119 P0 WARM-PATH TRACE: log the per-call latency
                # with the trace_id so a timeout on the V1 leg is visible
                # next to the PTB handler that triggered it. A timeout
                # is NEVER auto-retried — the server-side outcome of a
                # write is unknown and the caller owns reconciliation.
                try:
                    _elapsed = _time_ms() - _pstart
                    _logger.info(
                        "pasay_v1_request trace_id=%s method=%s path=%s "
                        "status=timeout elapsed_ms=%.2f error=%s",
                        _trace_id, method_upper, _log_path, _elapsed,
                        type(exc).__name__,
                    )
                except Exception:  # noqa: BLE001
                    pass
                raise PasayApiTimeoutError() from exc
            except httpx.HTTPError as exc:
                # Issue #119 P0 STALE-CONNECTION RESILIENCE: a
                # transport-level "server disconnected" on a pooled
                # keep-alive socket is recoverable for IDEMPOTENT
                # reads. Reopen the client (fresh pool) and retry
                # exactly once with the SAME method/path/body/headers.
                if (
                    _can_retry_once
                    and not _reopened
                    and self._is_stale_connection_error(exc)
                ):
                    try:
                        await self._reopen_client()
                    except Exception as reopen_exc:  # noqa: BLE001
                        try:
                            _elapsed = _time_ms() - _pstart
                            _logger.info(
                                "pasay_v1_request trace_id=%s method=%s "
                                "path=%s status=retry_aborted "
                                "elapsed_ms=%.2f error=%s reopen_error=%s",
                                _trace_id, method_upper, _log_path, _elapsed,
                                type(exc).__name__, type(reopen_exc).__name__,
                            )
                        except Exception:  # noqa: BLE001
                            pass
                        raise PasayApiError(
                            None, f"network error: {exc}"
                        ) from exc
                    _reopened = True
                    try:
                        _elapsed = _time_ms() - _pstart
                        _logger.info(
                            "pasay_v1_request trace_id=%s method=%s path=%s "
                            "status=retrying_once elapsed_ms=%.2f error=%s",
                            _trace_id, method_upper, _log_path, _elapsed,
                            type(exc).__name__,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    continue
                try:
                    _elapsed = _time_ms() - _pstart
                    _logger.info(
                        "pasay_v1_request trace_id=%s method=%s path=%s "
                        "status=error elapsed_ms=%.2f error=%s",
                        _trace_id, method_upper, _log_path, _elapsed,
                        type(exc).__name__,
                    )
                except Exception:  # noqa: BLE001
                    pass
                raise PasayApiError(None, f"network error: {exc}") from exc
            # We have a real Response object — break out of the retry
            # loop and process the body below.
            break
        # If the loop exits via ``continue`` it always re-enters ``while``;
        # the only way to reach here is via ``break`` after a successful
        # httpx.Response. The PhaseProbe add_backend accounting is done
        # once per HTTP round-trip below — but because the loop may have
        # run TWICE for a stale-retry, we record the cumulative cost
        # of every attempt on the probe.
        try:
            from pasay_bot.state.latency import current_phase

            probe = current_phase()
            if probe is not None:
                probe.add_backend(_time_ms() - _pstart)
        except Exception:  # noqa: BLE001 - instrumentation never breaks requests
            pass
        # Issue #119 P0 WARM-PATH TRACE: one structured log line per V1
        # request so operator grep can compute the per-call V1 hop latency
        # and join it with the Worker / Container records on ``trace_id``.
        try:
            _elapsed = _time_ms() - _pstart
            _logger.info(
                "pasay_v1_request trace_id=%s method=%s path=%s status=%d "
                "elapsed_ms=%.2f retried=%s",
                _trace_id, method_upper, _log_path,
                int(resp.status_code), _elapsed,
                "true" if _reopened else "false",
            )
        except Exception:  # noqa: BLE001 - observability never breaks the request
            pass
        if resp.status_code >= 400:
            detail, error_code = _extract_detail(resp)
            if resp.status_code == 401:
                raise PasayApiAuthError(resp.status_code, detail, error_code)
            if resp.status_code == 403:
                raise PasayApiPermissionError(resp.status_code, detail, error_code)
            if resp.status_code == 409:
                raise PasayApiConflictError(resp.status_code, detail, error_code)
            raise PasayApiError(resp.status_code, detail, error_code)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def bind_telegram_user(self, effective_user_id: int) -> None:
        """Bind authentication to effective_user.id for the current async task."""
        if (
            isinstance(effective_user_id, bool)
            or not isinstance(effective_user_id, int)
            or effective_user_id <= 0
            or effective_user_id > MAX_TELEGRAM_USER_ID
        ):
            raise ValueError("effective_user.id must be a positive integer")
        self._telegram_user_id.set(effective_user_id)

    def clear_telegram_user(self) -> None:
        """Clear any identity inherited from an earlier sequential update."""
        self._telegram_user_id.set(None)

    # --- read endpoints ---
    async def get_properties(self) -> list[Property]:
        data = await self._request("GET", "/properties")
        return [Property.from_dict(d) for d in data]

    async def get_units(self) -> list[Unit]:
        data = await self._request("GET", "/units")
        return [Unit.from_dict(d) for d in data]

    async def get_unit(self, unit_id: int) -> Unit:
        data = await self._request("GET", f"/units/{unit_id}")
        return Unit.from_dict(data)

    async def create_unit(
        self,
        *,
        property_id: int,
        unit_number: str,
        monthly_rent: Any,
        status: str = "vacant",
        floor: Optional[str] = None,
        size_sqm: Optional[Any] = None,
        unit_state: Optional[str] = None,
    ) -> Unit:
        """POST /units — Telegram-first Unit CRUD (AI-OPS-FOUNDATION-001 §14)."""
        payload: dict[str, Any] = {
            "property_id": int(property_id),
            "unit_number": unit_number,
            "monthly_rent": str(_to_decimal(monthly_rent)),
            "status": status,
        }
        if floor is not None:
            payload["floor"] = floor
        if size_sqm is not None:
            payload["size_sqm"] = str(_to_decimal(size_sqm))
        if unit_state is not None:
            payload["unit_state"] = unit_state
        data = await self._request("POST", "/units", json=payload, timeout=15.0)
        return Unit.from_dict(data)

    async def update_unit(
        self,
        unit_id: int,
        *,
        monthly_rent: Optional[Any] = None,
        status: Optional[str] = None,
        unit_state: Optional[str] = None,
    ) -> Unit:
        """PATCH /units/{id} — Telegram-first edits (rent / lifecycle state)."""
        payload: dict[str, Any] = {}
        if monthly_rent is not None:
            payload["monthly_rent"] = str(_to_decimal(monthly_rent))
        if status is not None:
            payload["status"] = status
        if unit_state is not None:
            payload["unit_state"] = unit_state
        data = await self._request("PATCH", f"/units/{unit_id}", json=payload, timeout=15.0)
        return Unit.from_dict(data)

    async def get_leases(self) -> list[Lease]:
        data = await self._request("GET", "/leases")
        return [Lease.from_dict(d) for d in data]

    async def get_tenants(self) -> list[Tenant]:
        data = await self._request("GET", "/tenants")
        return [Tenant.from_dict(d) for d in data]

    async def update_tenant(self, tenant_id: int, **fields) -> dict:
        """PASAY-AI-EMPLOYEE-FOUNDATION-007: low-risk tenant write (e.g.
        supplying a missing phone). ``/tenants/{id}`` responds with the safe
        public shape (ID number redacted)."""
        return await self._request("PATCH", f"/tenants/{tenant_id}", json=fields)

    async def resume_action(self, *, field: str, value: str, lease_id: int | None = None,
                            unit_id: int | None = None, task_id: int | None = None) -> dict:
        """§2/§8 self-healing: supply the missing data and get the blocked
        action returned so the bot auto-executes it (no re-click)."""
        payload = {"field": field, "value": value}
        if lease_id is not None:
            payload["lease_id"] = lease_id
        if unit_id is not None:
            payload["unit_id"] = unit_id
        if task_id is not None:
            payload["task_id"] = task_id
        return await self._request("POST", "/operations/resume", json=payload)

    async def get_action_pack(self, unit_id: int) -> dict:
        """§13: the full Rent Action Pack (phone / scripts / assignable).
        Returns the dict; frontends read ``assignable`` before assigning."""
        return await self._request("GET", f"/operations/action-pack?unit_id={unit_id}")

    async def get_action_route(self, action_type: str) -> dict:
        return await self._request("GET", f"/operations/route?action_type={action_type}")

    async def record_payment_promise(self, *, lease_id: int, amount: float | None,
                                     promised_date: str, note: str = "") -> dict:
        return await self._request(
            "POST", "/operations/promise",
            json={"lease_id": lease_id, "amount": amount,
                  "promised_date": promised_date, "note": note},
        )

    async def get_conflict_report(self, unit_id: int) -> dict:
        return await self._request("GET", f"/operations/conflict-report?unit_id={unit_id}")

    async def get_income(self, income_id: int) -> Income:
        data = await self._request("GET", f"/incomes/{income_id}")
        return Income.from_dict(data)

    async def list_incomes(self) -> list[Income]:
        data = await self._request("GET", "/incomes")
        return [Income.from_dict(d) for d in data]

    # --- expenses (read + approval/rejection) ---
    async def list_expenses(self) -> list[Expense]:
        data = await self._request("GET", "/expenses")
        return [Expense.from_dict(d) for d in data]

    async def get_expense(self, expense_id: int) -> Expense:
        data = await self._request("GET", f"/expenses/{expense_id}")
        return Expense.from_dict(data)

    async def create_expense(
        self,
        *,
        category: str,
        amount: Any,
        expense_date: str,
        unit_id: Optional[int] = None,
        payee: str = "",
        description: Optional[str] = None,
        status: str = "pending",
        payer_user_id: Optional[int] = None,
    ) -> Expense:
        """POST /expenses — BOT-V1-USABLE-001 P0-2. Secretary records PENDING
        expenses (Owner approval stays the backend's deterministic path);
        only an admin key may create approved expenses directly.

        AI-OPS-FOUNDATION-001 §4/§8: ``payer_user_id`` records the actual
        payer so the approved expense's payment task routes to them, not
        always the Owner."""
        payload: dict[str, Any] = {
            "category": category,
            "amount": str(_to_decimal(amount)),
            "expense_date": str(expense_date)[:10],
            "payee": payee or "-",
            "status": status,
        }
        if unit_id is not None:
            payload["unit_id"] = int(unit_id)
        if description:
            payload["description"] = description
        if payer_user_id is not None:
            payload["payer_user_id"] = int(payer_user_id)
        data = await self._request("POST", "/expenses", json=payload)
        return Expense.from_dict(data)

    async def approve_expense(self, expense_id: int) -> Expense:
        data = await self._request("POST", f"/expenses/{expense_id}/approve")
        return Expense.from_dict(data)

    async def reject_expense(self, expense_id: int) -> Expense:
        data = await self._request("POST", f"/expenses/{expense_id}/reject")
        return Expense.from_dict(data)

    async def pay_expense(self, expense_id: int) -> Expense:
        """POST /expenses/{id}/pay: an approved expense becomes PAID only via a
        VERIFIED payment claim (003B §3/§7) — Owner is the final verifier.
        Idempotent: a second call on an already-PAID expense returns the same
        record (no duplicate write)."""
        data = await self._request("POST", f"/expenses/{expense_id}/pay")
        return Expense.from_dict(data)

    async def get_expense_detail(self, expense_id: int) -> Expense:
        """GET /expenses/{id}/detail: full truth (payment verified_paid /
        remaining / claims / timeline). The bot reads the SAME authoritative
        payload as the Mini App (003B §19)."""
        data = await self._request("GET", f"/expenses/{expense_id}/detail")
        return Expense.from_dict(data)

    async def create_expense_claim(self, expense_id: int, *, amount, idempotency_key=None,
                                   verification_note=None, evidence_ids=None) -> dict:
        """POST /expenses/{id}/claims — Secretary reports a payment -> PENDING
        claim (awaiting verification). Idempotent via idempotency_key."""
        payload: dict[str, Any] = {"claimed_amount": str(_to_decimal(amount))}
        if verification_note:
            payload["verification_note"] = verification_note
        if evidence_ids:
            payload["evidence_ids"] = [int(x) for x in evidence_ids]
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        data = await self._request("POST", f"/expenses/{expense_id}/claims", json=payload)
        return dict(data)

    async def verify_expense_claim(self, expense_id: int, claim_id: int, *,
                                   result: str | None = None,
                                   verified_amount: str | None = None) -> dict:
        """POST .../claims/{id}/verify — Owner/verifier confirms the payment.
        Only then does the amount enter the verified aggregate (E3/E4)."""
        payload: dict[str, Any] = {}
        if result:
            payload["result"] = result
        if verified_amount:
            payload["verified_amount"] = str(_to_decimal(verified_amount))
        data = await self._request(
            "POST", f"/expenses/{expense_id}/claims/{claim_id}/verify", json=payload)
        return dict(data)

    async def fail_expense_claim(self, expense_id: int, claim_id: int, *,
                                 reason: str | None = None) -> dict:
        """POST .../claims/{id}/fail — verification failed; amount never enters
        the aggregate (E7)."""
        payload: dict[str, Any] = {}
        if reason:
            payload["reason"] = reason
        data = await self._request(
            "POST", f"/expenses/{expense_id}/claims/{claim_id}/fail", json=payload)
        return dict(data)

    async def get_expense_claims(self, expense_id: int) -> list[dict]:
        """GET /expenses/{id}/claims — every claim for the expense."""
        data = await self._request("GET", f"/expenses/{expense_id}/claims")
        return [dict(r) for r in (data or [])]

    async def get_expense_duplicates(self, expense_id: int) -> list[dict]:
        """GET /operations/quick/expense-duplicates?expense_id=...

        Advisory possible-duplicate matcher (PASAY-V2-EXPENSE-PAYABLE-TASK-006
        §7/§8): returns OTHER highly similar PAID expenses (same unit, amount,
        purpose/category, relevant date window). Amount alone is never a match.
        Empty when nothing similar is found."""
        data = await self._request(
            "GET", f"/operations/quick/expense-duplicates", params={"expense_id": expense_id}
        )
        return [dict(r) for r in (data or [])]

    async def find_income(
        self,
        *,
        lease_id: Optional[int],
        amount: Any,
        received_date: str,
        payment_method: str,
        idempotency_key: Optional[str] = None,
    ) -> Optional[Income]:
        """Reconcile an uncertain create: return the first income matching the
        exact create payload (lease_id, received_date, amount, method). Used to
        reuse a write that may have landed during a timeout / crash instead of
        creating a second income (F1/F3).

        SLICE2-RENT-005: when ``idempotency_key`` is given, matching is STRICT
        on that key (the true replay identity of the same request). The shape
        fallback only reuses still-PENDING rows: a CONFIRMED row with the same
        shape but a different key is a different (genuine second partial)
        payment, never this request's replay."""
        want_amount = _to_decimal(amount)
        want_date = str(received_date)[:10]
        for inc in await self.list_incomes():
            if idempotency_key is not None:
                if (inc.idempotency_key or "") == idempotency_key:
                    return inc
            if inc.status == "pending" and (
                inc.lease_id == lease_id
                and inc.amount == want_amount
                and inc.received_date.isoformat() == want_date
                and (inc.payment_method or "") == payment_method
            ):
                return inc
        return None

    async def match_rent_payment(self, text: str, amount: Any = None) -> RentMatchResult:
        """POST /payments/match (Slice 2, Entry B): resolve a natural-language
        payment statement to open receivables. Read-only — never writes."""
        body: dict[str, Any] = {"text": text}
        if amount is not None:
            body["amount"] = str(_to_decimal(amount))
        data = await self._request("POST", "/payments/match", json=body)
        return RentMatchResult.from_dict(data)

    async def get_financial_summary(self, month: str) -> FinancialSummary:
        data = await self._request("GET", "/reports/financial-summary", params={"month": month})
        return FinancialSummary.from_dict(data)

    async def get_overdue_rents(self) -> list[OverdueRent]:
        data = await self._request("GET", "/reports/overdue-rents")
        return [OverdueRent.from_dict(d) for d in data]

    async def get_operational_tasks(
        self, *, status: Optional[str] = None, scope: Optional[str] = None,
    ) -> list[OperationalTask]:
        """V1.2 operations center: backend filters per-role (agents only see
        their own assigned tasks). ``scope="owner"`` applies the Owner
        attention filter (AI-OPS-FOUNDATION-001 §5)."""
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        if scope:
            params["scope"] = scope
        data = await self._request("GET", "/operations/tasks", params=params)
        return [OperationalTask.from_dict(d) for d in data]

    async def get_operational_task(self, task_id: int) -> OperationalTask:
        data = await self._request("GET", f"/operations/tasks/{task_id}")
        return OperationalTask.from_dict(data)

    async def complete_operational_task(self, task_id: int) -> OperationalTask:
        data = await self._request("POST", f"/operations/tasks/{task_id}/complete")
        return OperationalTask.from_dict(data["task"])

    async def snooze_operational_task(
        self, task_id: int, *, until: Optional[str] = None, preset: Optional[str] = None,
    ) -> OperationalTask:
        payload: dict[str, Any] = {}
        if until:
            payload["until"] = until
        if preset:
            payload["preset"] = preset
        data = await self._request("POST", f"/operations/tasks/{task_id}/snooze", json=payload)
        return OperationalTask.from_dict(data["task"])

    async def cancel_operational_task(self, task_id: int) -> OperationalTask:
        data = await self._request("POST", f"/operations/tasks/{task_id}/cancel")
        return OperationalTask.from_dict(data["task"])

    async def acknowledge_operational_task(self, task_id: int) -> OperationalTask:
        """CONVERGENCE-003 §1.5: ✅ Acknowledge — PENDING -> IN_PROGRESS, stops
        same-day proactive reminders. Idempotent (repeat tap returns the
        current task)."""
        data = await self._request("POST", f"/operations/tasks/{task_id}/acknowledge")
        return OperationalTask.from_dict(data["task"])

    async def get_owner_dm_chat_id(self) -> str:
        """ZERO-LEARNING-004 §4: resolve the canonical HUMAN Owner's Telegram
        private-chat id for a REAL Remind-Owner DM. Raises PasayApiError when
        no Owner Telegram destination is configured (the caller must then NOT
        report the reminder as delivered)."""
        data = await self._request("GET", "/operations/remind-owner-target")
        chat_id = str((data or {}).get("telegram_chat_id") or "").strip()
        if not chat_id:
            raise PasayApiError(None, "No Owner Telegram destination configured")
        return chat_id

    async def get_secretary_dm_chat_id(self) -> tuple[str, int | None]:
        """TELEGRAM-OPS-REAL-WORLD-CLOSURE-005 §2.2: resolve the canonical HUMAN
        Secretary's Telegram private-chat id + principal id for a REAL
        ``📞 催租`` assign-to-Secretary DM. Raises PasayApiError when no
        Secretary Telegram destination is configured (the caller must then NOT
        mark the follow-up as assigned)."""
        data = await self._request("GET", "/operations/secretary-target")
        chat_id = str((data or {}).get("telegram_chat_id") or "").strip()
        principal_id = (data or {}).get("principal_id")
        if not chat_id:
            raise PasayApiError(None, "No Secretary Telegram destination configured")
        return chat_id, (int(principal_id) if principal_id is not None else None)

    # --- PASAY-V2-FOUNDATION-001: conversation-driven task create/update ---
    async def create_operational_task(
        self,
        *,
        task_type: str,
        title: str,
        property_id: Optional[int] = None,
        description: Optional[str] = None,
        priority: str = "medium",
        status: Optional[str] = None,
        due_at: Optional[str] = None,
        next_action: Optional[str] = None,
        next_check_at: Optional[str] = None,
        context: Optional[str] = None,
        completion_condition: Optional[str] = None,
        source_event: Optional[str] = None,
        assigned_user_id: Optional[int] = None,
        dedupe_key: Optional[str] = None,
        details: Optional[dict] = None,
    ) -> OperationalTask:
        """POST /operations/tasks: create a task from a conversation event."""
        body: dict[str, Any] = {
            "task_type": task_type,
            "title": title,
            "priority": priority,
        }
        if property_id is not None:
            body["property_id"] = property_id
        if description is not None:
            body["description"] = description
        if status is not None:
            body["status"] = status
        if due_at is not None:
            body["due_at"] = due_at
        if next_action is not None:
            body["next_action"] = next_action
        if next_check_at is not None:
            body["next_check_at"] = next_check_at
        if context is not None:
            body["context"] = context
        if completion_condition is not None:
            body["completion_condition"] = completion_condition
        if source_event is not None:
            body["source_event"] = source_event
        if assigned_user_id is not None:
            body["assigned_user_id"] = assigned_user_id
        if dedupe_key is not None:
            body["dedupe_key"] = dedupe_key
        if details is not None:
            body["details"] = details
        data = await self._request("POST", "/operations/tasks", json=body, timeout=15.0)
        return OperationalTask.from_dict(data["task"])

    async def update_operational_task(
        self,
        task_id: int,
        *,
        title: Optional[str] = None,
        status: Optional[str] = None,
        due_at: Optional[str] = None,
        next_action: Optional[str] = None,
        next_check_at: Optional[str] = None,
        context: Optional[str] = None,
        completion_condition: Optional[str] = None,
        details: Optional[dict] = None,
    ) -> OperationalTask:
        """PATCH /operations/tasks/{id}: conversation-driven partial update.

        ``details`` (AI-OPS-FOUNDATION-001 §8) carries structured promise /
        follow-up state that the backend merges into the task's JSONB."""
        body: dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if status is not None:
            body["status"] = status
        if due_at is not None:
            body["due_at"] = due_at
        if next_action is not None:
            body["next_action"] = next_action
        if next_check_at is not None:
            body["next_check_at"] = next_check_at
        if context is not None:
            body["context"] = context
        if completion_condition is not None:
            body["completion_condition"] = completion_condition
        if details is not None:
            body["details"] = details
        data = await self._request(
            "PATCH", f"/operations/tasks/{task_id}", json=body, timeout=15.0
        )
        return OperationalTask.from_dict(data["task"])

    async def deliver_task_followup(
        self,
        task_id: int,
        *,
        assignee_user_id: int,
        message: str,
        reply_markup: Optional[dict] = None,
    ) -> TaskFollowupDelivery:
        body: dict[str, Any] = {
            "assignee_user_id": int(assignee_user_id),
            "message": message,
        }
        if reply_markup is not None:
            body["reply_markup"] = reply_markup
        data = await self._request(
            "POST",
            f"/operations/tasks/{task_id}/followup-delivery",
            json=body,
            timeout=30.0,
        )
        return TaskFollowupDelivery.from_dict(data)

    async def get_quick_tasks(
        self,
        scope: Optional[str] = None,
        *,
        system_org_id: Optional[int] = None,
    ) -> list[dict]:
        """GET /operations/quick/tasks: deterministic active-task quick view.

        Issue #119 P0 (independent review follow-up): for the SYSTEM
        scheduled-job client, ``system_org_id`` (or the
        ``system_org_id`` bound to the client) is sent as the
        ``?org_id=...`` query parameter so the server can enforce the
        single-org binding. ``scope="owner"`` applies the Owner
        attention filter (HUMAN only — SYSTEM callers must not pass
        ``scope="owner"``).

        The V1 SYSTEM response is a structured dict
        ``{"items": [...], "total": ..., "limit": ..., "offset": ...}``;
        this method returns the flat ``items`` list so the bot's
        ``_send_next_check_reminders`` loop continues to iterate over
        ``task.get("next_check_at")`` rows unchanged.
        """
        params: dict[str, Any] = {}
        if scope:
            params["scope"] = scope
        canonical_org = system_org_id if system_org_id is not None else self.system_org_id
        if canonical_org is not None and canonical_org > 0:
            params["org_id"] = int(canonical_org)
        data = await self._request(
            "GET", "/operations/quick/tasks", params=params,
        )
        if isinstance(data, dict):
            # V1 SYSTEM contract: structured response with an
            # ``items`` list. Return the flat list so the bot code
            # does not need to change.
            return list(data.get("items") or [])
        if isinstance(data, list):
            # Legacy contract: flat list. Pass through unchanged.
            return data
        return []

    async def get_quick_properties(self) -> list[dict]:
        """GET /operations/quick/properties: deterministic property status."""
        data = await self._request("GET", "/operations/quick/properties")
        return data if isinstance(data, list) else []

    async def get_quick_rent(self) -> dict:
        """GET /operations/quick/rent: overdue + outstanding."""
        data = await self._request("GET", "/operations/quick/rent")
        return data or {}

    # --- Issue #119 P0 latency: ``*_safe`` wrappers for parallel gathers ----
    # The fixed bottom-menu paths use ``asyncio.gather(..., return_exceptions=True)``
    # to fire several V1 reads concurrently. The ``*_safe`` variants below
    # NEVER raise — they translate ``PasayApiError`` into the exception object
    # so callers can branch on ``isinstance(...)`` without try/except noise,
    # while keeping the existing typed ``get_quick_rent`` / ``get_units`` etc.
    # available for callers that prefer the raise-fast path.

    async def get_quick_rent_safe(self) -> dict:
        try:
            return await self.get_quick_rent()
        except PasayApiError as exc:
            return exc

    async def get_units_safe(self) -> list:
        try:
            return await self.get_units()
        except PasayApiError as exc:
            return exc

    async def get_leases_safe(self) -> list:
        try:
            return await self.get_leases()
        except PasayApiError as exc:
            return exc

    async def get_operational_tasks_safe(self, **kwargs) -> list:
        try:
            return await self.get_operational_tasks(**kwargs)
        except PasayApiError as exc:
            return exc

    async def get_quick_expense(self) -> dict:
        """GET /operations/quick/expense: month total + this month's expense
        records (PAID included) + pending approval/unresolved."""
        data = await self._request("GET", "/operations/quick/expense")
        return data or {}

    async def get_unit_timeline(self, unit_id: int) -> dict:
        """GET /operations/quick/unit-timeline: the unit's digital file
        (AI-OPS-FOUNDATION-001 §15)."""
        data = await self._request(
            "GET", "/operations/quick/unit-timeline", params={"unit_id": unit_id}
        )
        return data or {"unit": None, "events": []}

    # --- AI-OPS-FOUNDATION-001 §17: viewings --------------------------------
    async def create_viewing(self, *, unit_id: int, scheduled_at: str,
                             notes: Optional[str] = None) -> dict:
        """POST /viewings: persist a scheduled viewing as a business event."""
        body: dict[str, Any] = {"unit_id": int(unit_id), "scheduled_at": scheduled_at}
        if notes:
            body["notes"] = notes
        return await self._request("POST", "/viewings", json=body, timeout=15.0)

    async def get_digest(
        self,
        *,
        system_org_id: Optional[int] = None,
    ) -> dict:
        """GET /operations/digest: daily Active Tasks Digest.

        Issue #119 P0 (independent review follow-up): for the SYSTEM
        scheduled-job client, the canonical org id (the
        ``trusted_organization_id`` of the SYSTEM credential) is
        automatically sent as the ``?org_id=...`` query parameter
        when the client is constructed with ``system_org_id`` set.
        The server is the single source of truth — the client
        merely forwards the configured id.

        The bot's ``base_url`` already carries the ``/api/v1`` prefix
        (see ``pasay_bot.config.DEFAULT_PASAY_API_BASE``), so the
        path is the relative form ``/operations/digest`` — identical
        to the legacy call site.
        """
        canonical_org = system_org_id if system_org_id is not None else self.system_org_id
        params: dict[str, Any] = {}
        if canonical_org is not None and canonical_org > 0:
            params["org_id"] = int(canonical_org)
        data = await self._request(
            "GET", "/operations/digest", params=params,
        )
        return data or {}

    async def get_operations_summary(self, scope: Optional[str] = None) -> dict:
        params: dict[str, Any] = {}
        if scope:
            params["scope"] = scope
        data = await self._request("GET", "/operations/summary", params=params)
        return {
            "overdue": int(data.get("overdue") or 0),
            "due_today": int(data.get("due_today") or 0),
            "due_7_days": int(data.get("due_7_days") or 0),
            "pending_total": int(data.get("pending_total") or 0),
        }

    async def copilot_today(self, provider: Optional[str] = None) -> CopilotToday:
        """POST /operations/copilot/today (C1/C1.1, read-only). By default this
        is the deterministic-first fast path (no LLM, ~ms); pass ``provider`` to
        force the LLM enrichment path (eval/measurement). This endpoint is LLM-
        free by default so the client default timeout is fine; the explicit
        provider path can be slow so it gets a long per-request timeout."""
        body: dict[str, Any] = {}
        if provider:
            body["provider"] = provider
        data = await self._request(
            "POST", "/operations/copilot/today", json=body,
            timeout=120.0 if provider else 15.0,
        )
        return CopilotToday.from_dict(data)

    async def copilot_why(self, item_ref: str) -> CopilotWhy:
        """POST /operations/copilot/why (C1.1, on-demand LLM explain). The
        EXPLAIN provider is fast (non-reasoning) by default; provider-down
        returns a deterministic HTTP-200 fallback. Use a generous timeout."""
        data = await self._request(
            "POST", "/operations/copilot/why", json={"item_ref": item_ref}, timeout=120.0
        )
        return CopilotWhy.from_dict(data)

    async def copilot_ask(self, question: str) -> CopilotAsk:
        """POST /operations/copilot/ask (C1.1, on-demand Q&A). Provider-down
        returns a friendly deterministic fallback. Use a generous timeout."""
        data = await self._request(
            "POST", "/operations/copilot/ask", json={"question": question}, timeout=120.0
        )
        return CopilotAsk.from_dict(data)

    async def parse_nl_intent(self, text: str) -> NlIntentResult:
        """POST /operations/copilot/nl-parse (BOT-V1-USABLE-001 P0-5, AI
        fallback). Read-only structured intent parsing; the bot maps the
        intent to its own deterministic business paths."""
        data = await self._request(
            "POST", "/operations/copilot/nl-parse",
            json={"text": text}, timeout=45.0,
        )
        return NlIntentResult.from_dict(data)

    async def copilot_recommend(
        self,
        intent: str,
        *,
        source_type: Optional[str] = None,
        source_id: Optional[int] = None,
        task_ref: Optional[int] = None,
        reason_code: Optional[str] = None,
        assignee_user_id: Optional[int] = None,
        due_at: Optional[str] = None,
        preset: Optional[str] = None,
        note: Optional[str] = None,
    ) -> CopilotRecommend:
        """POST /operations/copilot/recommend (C2): intent + resolved refs ->
        canonical PENDING proposal card. Deterministic (no LLM)."""
        body: dict[str, Any] = {"intent": intent}
        if source_type is not None:
            body["source_type"] = source_type
        if source_id is not None:
            body["source_id"] = source_id
        if task_ref is not None:
            body["task_ref"] = task_ref
        if reason_code is not None:
            body["reason_code"] = reason_code
        if assignee_user_id is not None:
            body["assignee_user_id"] = assignee_user_id
        if due_at is not None:
            body["due_at"] = due_at
        if preset is not None:
            body["preset"] = preset
        if note is not None:
            body["note"] = note
        data = await self._request(
            "POST", "/operations/copilot/recommend", json=body, timeout=15.0
        )
        return CopilotRecommend.from_dict(data)

    async def copilot_execute(self, proposal_id: int) -> CopilotExecute:
        """POST /operations/copilot/proposals/{id}/execute (C2): CONFIRMED ->
        EXECUTED with execute-time revalidation. Replay-safe (``replay=True``
        on bot retries; never a second business effect)."""
        data = await self._request(
            "POST", f"/operations/copilot/proposals/{proposal_id}/execute", timeout=30.0
        )
        return CopilotExecute.from_dict(data)

    async def copilot_confirm(self, proposal_id: int) -> dict:
        """POST /operations/copilot/proposals/{id}/confirm (C2): the owner's
        [✅ 确认安排] tap transitions PENDING -> CONFIRMED. Idempotent replay
        when already CONFIRMED; a structured 409 surfaces fail-closed
        revalidation (stale target / expired / permissions)."""
        return await self._request(
            "POST", f"/operations/copilot/proposals/{proposal_id}/confirm", timeout=30.0
        )

    async def copilot_cancel(self, proposal_id: int) -> dict:
        """POST /operations/copilot/proposals/{id}/cancel (C2): [暂不处理]
        cancels a PENDING proposal (idempotent replay when already CANCELLED)."""
        return await self._request(
            "POST", f"/operations/copilot/proposals/{proposal_id}/cancel", timeout=30.0
        )

    async def get_me(self) -> dict:
        """POST /auth — the API key's backend user (used by the [我自己]
        assignee pick so the owner can assign to themselves)."""
        return await self._request("POST", "/auth", timeout=15.0)

    # --- AI-OPS-FOUNDATION-001 §11/§12: universal evidence index ------------
    async def create_evidence(
        self,
        *,
        external_file_id: str,
        external_message_id: Optional[int] = None,
        media_type: Optional[str] = None,
        mime_type: Optional[str] = None,
        filename: Optional[str] = None,
        size_bytes: Optional[int] = None,
        category: Optional[str] = None,
        property_id: Optional[int] = None,
        unit_id: Optional[int] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[int] = None,
        storage_provider: str = "telegram_channel",
    ) -> dict:
        """POST /evidence: index one archived media record. The bytes live in
        the storage layer (Telegram private archive); the backend keeps the
        authoritative index/relationships."""
        body: dict[str, Any] = {
            "storage_provider": storage_provider,
            "external_file_id": external_file_id,
        }
        for key, value in (
            ("external_message_id", external_message_id),
            ("media_type", media_type),
            ("mime_type", mime_type),
            ("filename", filename),
            ("size_bytes", size_bytes),
            ("category", category),
            ("property_id", property_id),
            ("unit_id", unit_id),
            ("entity_type", entity_type),
            ("entity_id", entity_id),
        ):
            if value is not None:
                body[key] = value
        return await self._request("POST", "/evidence", json=body, timeout=15.0)

    async def list_evidence(
        self,
        *,
        unit_id: Optional[int] = None,
        property_id: Optional[int] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[int] = None,
        category: Optional[str] = None,
    ) -> list[dict]:
        """GET /evidence with filters; newest first."""
        params: dict[str, Any] = {}
        if unit_id is not None:
            params["unit_id"] = unit_id
        if property_id is not None:
            params["property_id"] = property_id
        if entity_type is not None:
            params["entity_type"] = entity_type
        if entity_id is not None:
            params["entity_id"] = entity_id
        if category is not None:
            params["category"] = category
        data = await self._request("GET", "/evidence", params=params)
        return [dict(r) for r in (data or [])]

    async def get_tasks(
        self, *, status: Optional[str] = None, overdue: bool = False,
        within_days: Optional[int] = None,
    ) -> list[ReportTask]:
        """Task report: optional status filter, overdue flag, due-within window."""
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        if overdue:
            params["overdue"] = "true"
        if within_days is not None:
            params["within_days"] = str(within_days)
        data = await self._request("GET", "/reports/tasks", params=params)
        return [ReportTask.from_dict(d) for d in data]

    # --- write endpoints (pending -> confirm -> reverse, never direct DB) ---
    async def create_income(
        self,
        *,
        lease_id: Optional[int],
        amount: Any,
        received_date: str,
        payment_method: str,
        description: str,
        status: str = "pending",
        idempotency_key: Optional[str] = None,
    ) -> Income:
        payload = {
            "status": status,
            "lease_id": lease_id,
            "amount": str(_to_decimal(amount)),
            "received_date": str(received_date)[:10],
            "payment_method": payment_method,
            "description": description,
        }
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        data = await self._request("POST", "/incomes", json=payload)
        return Income.from_dict(data)

    async def confirm_income(self, income_id: int) -> Income:
        data = await self._request("POST", f"/incomes/{income_id}/confirm")
        return Income.from_dict(data)

    async def reverse_income(self, income_id: int) -> Income:
        data = await self._request("POST", f"/incomes/{income_id}/reverse")
        return Income.from_dict(data)

    # --- REPAIR-AI-EMPLOYEE-WORKFLOW-008A: Repair Operation fast path --------
    async def list_repairs(self) -> list[RepairOperation]:
        """GET /repairs — all Repair Operations (backend filters per role)."""
        data = await self._request("GET", "/repairs")
        payload = data.get("items", data) if isinstance(data, dict) else data
        return [RepairOperation.from_dict(d) for d in (payload or [])]

    async def get_repair(self, repair_id: int) -> RepairOperation:
        """GET /repairs/{id} — full Repair Operation detail (Issue/Operation,
        Proposals, Expenses, Actions, Verification). Telegram and the Mini App
        read REAL business state from here — never chat copy."""
        data = await self._request("GET", f"/repairs/{repair_id}")
        return RepairOperation.from_dict(data)

    async def create_repair(
        self,
        *,
        issue: str,
        issue_description: Optional[str] = None,
        property_id: Optional[int] = None,
        unit_id: Optional[int] = None,
        merchant_id: Optional[int] = None,
        closure_criteria: Optional[str] = None,
        assignee_user_id: Optional[int] = None,
        created_source: str = "telegram",
    ) -> RepairOperation:
        """POST /repairs — create a new OPEN Repair Operation (the real problem)."""
        body: dict[str, Any] = {"issue": issue, "created_source": created_source}
        if issue_description:
            body["issue_description"] = issue_description
        if property_id is not None:
            body["property_id"] = property_id
        if unit_id is not None:
            body["unit_id"] = unit_id
        if merchant_id is not None:
            body["merchant_id"] = merchant_id
        if closure_criteria:
            body["closure_criteria"] = closure_criteria
        if assignee_user_id is not None:
            body["assignee_user_id"] = assignee_user_id
        data = await self._request("POST", "/repairs", json=body, timeout=15.0)
        return RepairOperation.from_dict(data)

    async def submit_repair_proposal(
        self,
        repair_id: int,
        *,
        amount: Any,
        vendor: Optional[str] = None,
        source: Optional[str] = None,
        description: Optional[str] = None,
        submit_as_expense: bool = False,
    ) -> RepairOperation:
        """POST /repairs/{id}/proposals — submit the NEXT versioned quote (V1,
        V2, ...). If ``submit_as_expense`` a linked Expense is created too."""
        body: dict[str, Any] = {"amount": str(_to_decimal(amount))}
        if vendor:
            body["vendor"] = vendor
        if source:
            body["source"] = source
        if description:
            body["description"] = description
        body["submit_as_expense"] = bool(submit_as_expense)
        data = await self._request(
            "POST", f"/repairs/{repair_id}/proposals", json=body, timeout=15.0
        )
        return RepairOperation.from_dict(data)

    async def decide_repair_proposal(
        self,
        repair_id: int,
        *,
        decision: str,
        reason: Optional[str] = None,
        version: Optional[int] = None,
        proposal_id: Optional[int] = None,
        expense_id: Optional[int] = None,
    ) -> RepairOperation:
        """POST /repairs/{id}/decide — Owner APPROVE/REJECT a proposal version.
        Rejecting a proposal does NOT close the repair (it stays alive for a
        requote); approving moves it to WAITING_PAYMENT."""
        body: dict[str, Any] = {"decision": decision}
        if reason:
            body["reason"] = reason
        if version is not None:
            body["version"] = version
        if proposal_id is not None:
            body["proposal_id"] = proposal_id
        if expense_id is not None:
            body["expense_id"] = expense_id
        data = await self._request(
            "POST", f"/repairs/{repair_id}/decide", json=body, timeout=15.0
        )
        return RepairOperation.from_dict(data)

    async def pay_repair_expense(self, repair_id: int, expense_id: int) -> RepairOperation:
        """POST /repairs/{id}/pay-expense — mark the linked expense PAID. The
        repair advances at most to VERIFYING and is NEVER closed by payment."""
        data = await self._request(
            "POST", f"/repairs/{repair_id}/pay-expense", json={"expense_id": expense_id},
            timeout=15.0,
        )
        return RepairOperation.from_dict(data)

    async def record_repair_result(
        self,
        repair_id: int,
        *,
        verification_result: Optional[str] = None,
        source: Optional[str] = None,
        evidence_ids: Optional[list[int]] = None,
    ) -> RepairOperation:
        """POST /repairs/{id}/record-result — a human confirms the REAL repair
        work is done; the repair moves to VERIFYING (not closed)."""
        body: dict[str, Any] = {}
        if verification_result:
            body["verification_result"] = verification_result
        if source:
            body["source"] = source
        if evidence_ids:
            body["evidence_ids"] = evidence_ids
        data = await self._request(
            "POST", f"/repairs/{repair_id}/record-result", json=body, timeout=15.0
        )
        return RepairOperation.from_dict(data)

    async def verify_and_close_repair(
        self,
        repair_id: int,
        *,
        verification_result: Optional[str] = None,
        closure_signal: str = "HUMAN_CONFIRMED",
        source: Optional[str] = None,
    ) -> RepairOperation:
        """POST /repairs/{id}/verify — explicit verification CLOSES the repair
        (the only path into CLOSED)."""
        body: dict[str, Any] = {"closure_signal": closure_signal}
        if verification_result:
            body["verification_result"] = verification_result
        if source:
            body["source"] = source
        data = await self._request(
            "POST", f"/repairs/{repair_id}/verify", json=body, timeout=15.0
        )
        return RepairOperation.from_dict(data)
