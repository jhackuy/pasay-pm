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


# --- V1.2.2 Phase C1 — read-only TODAY brief (no execution) ---

class CopilotTodayIn(BaseModel):
    """Optional body for POST /operations/copilot/today."""

    provider: str | None = Field(default=None, max_length=64)
    intent_note: str | None = Field(default=None, max_length=500)


class CopilotTodayItemOut(BaseModel):
    item_ref: str
    reason_why_important: str
    suggested_action: str


class CopilotTodayOut(BaseModel):
    """TODAY brief schema (UI shows at most 3 items + a short summary)."""

    top_items: list[CopilotTodayItemOut]
    summary: str
    context_schema_version: str
    provider: str
    model: str
    latency_ms: int
