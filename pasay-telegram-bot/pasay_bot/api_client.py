"""Typed Pasay PM API client (httpx, Bearer auth).

All financial writes go through this client — the bot never writes to
PostgreSQL directly. Timeouts / 4xx are surfaced as typed exceptions so the
handlers can implement the "uncertain write" reconciliation path.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from contextvars import ContextVar

import httpx

MAX_TELEGRAM_USER_ID = 2**63 - 1


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

    @classmethod
    def from_dict(cls, d: dict) -> "Tenant":
        return cls(
            id=int(d.get("id", 0)),
            full_name=d.get("full_name") or "",
            phone=d.get("phone"),
            email=d.get("email"),
        )


@dataclass
class Income:
    id: int
    lease_id: Optional[int] = None
    amount: Decimal = Decimal("0")
    received_date: date = date.today()
    payment_method: Optional[str] = None
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
    values (pending/approved/rejected/paid/reversed); UI text is derived in
    render/cards.py — never shown raw."""

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

    @classmethod
    def from_dict(cls, d: dict) -> "Expense":
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
    tenant_id: Optional[int] = None
    lease_id: Optional[int] = None
    source_type: str = ""
    source_id: Optional[int] = None
    assigned_user_id: Optional[int] = None
    priority: str = "medium"
    status: str = "PENDING"
    due_at: Optional[str] = None
    snoozed_until: Optional[str] = None
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
            tenant_id=int(d["tenant_id"]) if d.get("tenant_id") is not None else None,
            lease_id=int(d["lease_id"]) if d.get("lease_id") is not None else None,
            source_type=d.get("source_type") or "",
            source_id=int(d["source_id"]) if d.get("source_id") is not None else None,
            assigned_user_id=int(d["assigned_user_id"]) if d.get("assigned_user_id") is not None else None,
            priority=d.get("priority") or "medium",
            status=d.get("status") or "PENDING",
            due_at=d.get("due_at"),
            snoozed_until=d.get("snoozed_until"),
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
    ):
        self._telegram_user_id: ContextVar[int | None] = ContextVar(
            f"telegram_user_id_{id(self)}", default=None)
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            timeout=httpx.Timeout(timeout),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        user_id = self._telegram_user_id.get()
        if user_id is not None:
            headers = dict(kwargs.pop("headers", {}) or {})
            headers["X-Telegram-User-Id"] = str(user_id)
            kwargs["headers"] = headers
        try:
            resp = await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise PasayApiTimeoutError() from exc
        except httpx.HTTPError as exc:
            raise PasayApiError(None, f"network error: {exc}") from exc
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

    async def get_leases(self) -> list[Lease]:
        data = await self._request("GET", "/leases")
        return [Lease.from_dict(d) for d in data]

    async def get_tenants(self) -> list[Tenant]:
        data = await self._request("GET", "/tenants")
        return [Tenant.from_dict(d) for d in data]

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

    async def approve_expense(self, expense_id: int) -> Expense:
        data = await self._request("POST", f"/expenses/{expense_id}/approve")
        return Expense.from_dict(data)

    async def reject_expense(self, expense_id: int) -> Expense:
        data = await self._request("POST", f"/expenses/{expense_id}/reject")
        return Expense.from_dict(data)

    async def find_income(
        self,
        *,
        lease_id: Optional[int],
        amount: Any,
        received_date: str,
        payment_method: str,
    ) -> Optional[Income]:
        """Reconcile an uncertain create: return the first income matching the
        exact create payload (lease_id, received_date, amount, method). Used to
        reuse a write that may have landed during a timeout / crash instead of
        creating a second income (F1/F3)."""
        want_amount = _to_decimal(amount)
        want_date = str(received_date)[:10]
        for inc in await self.list_incomes():
            if (
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
        self, *, status: Optional[str] = None,
    ) -> list[OperationalTask]:
        """V1.2 operations center: backend filters per-role (agents only see
        their own assigned tasks)."""
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
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

    async def get_operations_summary(self) -> dict:
        data = await self._request("GET", "/operations/summary")
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
