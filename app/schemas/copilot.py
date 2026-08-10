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
