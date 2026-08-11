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


# --- V1.2.2 Phase C2 — CONFIRMED-action copilot (recommend + execute) ---
# Render-safe schemas shared with the bot (mirrored dataclasses in the bot's
# api_client.py). The bot must never display the raw ``proposal_id``; the
# ``card`` / ``result`` blocks carry the human-renderable data.

class CopilotRecommendIn(BaseModel):
    """The bot posts an intent + resolved refs; ALL critical fields
    (assignee / due / target) are resolved backend-side by the canonical
    builder. Free text (``note``) is DATA only — never a mutation."""

    intent: str = Field(min_length=1, max_length=200)
    source_type: str | None = Field(default=None, max_length=50)
    source_id: int | None = None
    task_ref: int | None = None
    reason_code: str | None = Field(default=None, max_length=50)
    assignee_user_id: int | None = None
    due_at: datetime | None = None
    preset: str | None = Field(default=None, max_length=32)
    note: str | None = Field(default=None, max_length=2000)


class CopilotProposalCard(BaseModel):
    """Confirmation-card data for the UX (role-aware, no raw proposal id)."""

    action_type: str
    target_type: str
    target_id: int
    target_label: str = ""
    reason_code: str | None = None
    assignee_user_id: int | None = None
    assignee_name: str | None = None
    due_at: datetime | None = None
    note: str | None = None
    display_context: dict = Field(default_factory=dict)


class CopilotRecommendOut(BaseModel):
    """Canonical PENDING proposal + card. ``proposal_id`` is for the backend /
    bot wiring only — the bot must NOT display it."""

    proposal_id: int
    action_type: str
    status: str
    target_type: str
    target_id: int
    idempotency_key: str
    expires_at: datetime | None = None
    card: CopilotProposalCard
    detail: str
    created: bool = True


class CopilotExecuteResult(BaseModel):
    """Rendering-friendly execution outcome for the bot (role-aware text)."""

    action_type: str
    target_type: str
    target_id: int
    task_id: int | None = None
    assignee_user_id: int | None = None
    due_at: datetime | None = None
    executed_at: datetime | None = None
    status: str
    replay: bool = False
    detail: str


class CopilotExecuteOut(BaseModel):
    """POST /copilot/proposals/{id}/execute response: the proposal (now
    EXECUTED) plus the render block for the bot."""

    proposal: CopilotProposalRead
    result: CopilotExecuteResult
