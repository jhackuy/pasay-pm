"""Pydantic v2 model for the single PASAY-QUEUE-ENVELOPE-V1 contract.

Mirror of ``cloudflare-worker/src/envelope.ts``.
Two schemas MUST stay byte-symmetric on ``version`` / ``kind`` / ``event_id`` /
``occurred_at`` / ``payload`` structure.

Used ONLY by the internal ``/internal/ingest`` endpoint.
Exposed publicly; Telegram never hits this directly.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Union

from pydantic import BaseModel, Field, ValidationError, field_validator

ENVELOPE_VERSION: str = "1"


class EnvelopeKind(str, Enum):
    TELEGRAM_UPDATE = "telegram_update"
    SCHEDULED_JOB = "scheduled_job"


class TelegramMeta(BaseModel):
    update_id: int
    chat_id: int | None = None


class TelegramUpdateEnvelope(BaseModel):
    version: Literal["1"] = ENVELOPE_VERSION
    kind: Literal[EnvelopeKind.TELEGRAM_UPDATE] = EnvelopeKind.TELEGRAM_UPDATE
    event_id: str = Field(min_length=1, max_length=128)
    occurred_at: str
    payload: dict[str, Any]
    # Cloudflare Worker 端写入 "_telegram_meta"；Python Pydantic v2 不允许
    # 字段名以 "_" 开头，所以用 alias 双向兼容（allow_population_by_field_name
    # → by_alias=True 默认允许反序列化时接受 alias）。
    telegram_meta: TelegramMeta | None = Field(
        default=None, alias="_telegram_meta", serialization_alias="_telegram_meta"
    )

    model_config = {"populate_by_name": True}

    @field_validator("event_id")
    @classmethod
    def _event_id_prefix(cls, v: str) -> str:
        if not v.startswith("tg:"):
            raise ValueError("telegram_update event_id must start with 'tg:'")
        return v

    @field_validator("occurred_at")
    @classmethod
    def _iso8601(cls, v: str) -> str:
        datetime.fromisoformat(v.replace("Z", "+00:00"))
        return v


class ScheduledJobPayload(BaseModel):
    job_name: str = Field(min_length=1, max_length=128)
    scheduled_at: str
    params: dict[str, Any] | None = None


class ScheduledJobEnvelope(BaseModel):
    version: Literal["1"] = ENVELOPE_VERSION
    kind: Literal[EnvelopeKind.SCHEDULED_JOB] = EnvelopeKind.SCHEDULED_JOB
    event_id: str = Field(min_length=1, max_length=256)
    occurred_at: str
    payload: ScheduledJobPayload

    @field_validator("event_id")
    @classmethod
    def _event_id_prefix(cls, v: str) -> str:
        if not v.startswith("sched:"):
            raise ValueError("scheduled_job event_id must start with 'sched:'")
        return v


PasayQueueEnvelope = Union[TelegramUpdateEnvelope, ScheduledJobEnvelope]


def parse_envelope(raw: dict[str, Any]) -> PasayQueueEnvelope:
    """Strict two-step discriminator parse.

    Never raises a raw ValidationError to the outer HTTP layer; callers wrap
    into a friendly ``422 envelope_malformed`` response so the Queue consumer
    can classify the delivery as permanently failed (``terminal``) and not
    hammer the container with infinite retries.
    """
    kind = raw.get("kind") if isinstance(raw, dict) else None
    if kind == EnvelopeKind.TELEGRAM_UPDATE.value:
        return TelegramUpdateEnvelope.model_validate(raw)
    if kind == EnvelopeKind.SCHEDULED_JOB.value:
        return ScheduledJobEnvelope.model_validate(raw)
    raise ValidationError.from_exception_data(
        title="PasayQueueEnvelope",
        line_errors=[{
            "type": "literal_error",
            "loc": ("kind",),
            "input": kind,
            "ctx": {"expected": "'telegram_update' or 'scheduled_job'"},
        }],
    )


def iso_now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()
