"""Pydantic schemas for the Copilot context + proposal endpoints (V1.2.2)."""
from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import AuditFields


class CopilotProposalCreate(BaseModel):
    action_type: str = Field(min_length=1, max_length=50)
    target_type: str = Field(min_length=1, max_length=50)
    target_id: int
    payload: dict = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=128)
    expires_at: datetime | None = None


class CopilotProposalRead(AuditFields):
    id: int
    actor_user_id: int
    action_type: str
    target_type: str
    target_id: int
    payload_json: dict
    status: str
    idempotency_key: str
    expires_at: datetime | None = None
    confirmed_at: datetime | None = None
    executed_at: datetime | None = None


class CopilotProposalActionOut(BaseModel):
    proposal: CopilotProposalRead
    detail: str


# --- V1.2.2 Phase C1.1 — read-only TODAY / WHY / ASK (no execution) ---

class CopilotTodayIn(BaseModel):
    """Optional body for POST /operations/copilot/today.

    ``provider`` is an explicit LLM enrichment override (eval/measurement).
    The DEFAULT (no provider) is the deterministic fast path — no LLM.
    """

    provider: str | None = Field(default=None, max_length=64)
    intent_note: str | None = Field(default=None, max_length=500)


class CopilotTodayItemOut(BaseModel):
    item_ref: str
    reason_why_important: str
    suggested_action: str


class LatencyOut(BaseModel):
    """Structured timing breakdown (Deliverable D) — monotonic ms per phase."""

    context_build_ms: int = 0
    priority_ms: int = 0
    grounding_ms: int = 0
    llm_ms: int = 0
    render_ms: int = 0
    total_ms: int = 0


class CopilotTodayOut(BaseModel):
    """TODAY brief schema (UI shows at most 3 items + a short summary).

    ``enriched=False`` marks the deterministic-first fast response (no LLM);
    ``summary_version`` versions the deterministic summary format.
    """

    top_items: list[CopilotTodayItemOut]
    summary: str
    context_schema_version: str
    provider: str
    model: str
    latency_ms: int
    enriched: bool = False
    summary_version: str | None = None
    flags: list[str] = Field(default_factory=list)
    deterministic_top_refs: list[str] = Field(default_factory=list)
    latency: LatencyOut = Field(default_factory=LatencyOut)


class CopilotWhyIn(BaseModel):
    """Per-item WHY enrichment request."""

    item_ref: str = Field(min_length=1, max_length=64)
    provider: str | None = Field(default=None, max_length=64)


class CopilotWhyOut(BaseModel):
    """WHY response; ``fallback=True`` means the deterministic reason/action
    was returned because the provider was unavailable or unusable."""

    item_ref: str
    explanation: str
    recommendation: str
    grounded_refs: list[str]
    provider: str
    model: str
    latency_ms: int
    fallback: bool = False
    flags: list[str] = Field(default_factory=list)
    latency: LatencyOut = Field(default_factory=LatencyOut)


class CopilotAskIn(BaseModel):
    """On-demand Q&A request (grounded, read-only)."""

    question: str = Field(min_length=1, max_length=500)
    provider: str | None = Field(default=None, max_length=64)


class CopilotAskOut(BaseModel):
    """Q&A response; ``fallback=True`` means a friendly deterministic answer
    was returned because the provider was unavailable or unusable."""

    answer: str
    provider: str
    model: str
    latency_ms: int
    fallback: bool = False
    flags: list[str] = Field(default_factory=list)
    latency: LatencyOut = Field(default_factory=LatencyOut)
