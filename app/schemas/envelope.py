"""Pydantic v2 model for the single PASAY-QUEUE-ENVELOPE-V1 contract.

Mirror of ``cloudflare-worker/src/envelope.ts``.
Two schemas MUST stay byte-symmetric on ``version`` / ``kind`` / ``event_id`` /
``occurred_at`` / ``payload`` structure.

Used ONLY by the internal ``/internal/ingest`` endpoint.
Exposed publicly; Telegram never hits this directly.

ND_RETURN FIX2 #4a — shared UTC timestamp validator:
  occurred_at (both envelope kinds) and scheduled_at (inside scheduled_job
  payload) all go through one validator that rejects: unparsable strings,
  naive (tz-unaware) datetimes, and datetimes whose UTC offset is anything
  other than 00:00 (strict UTC).  Failures surface as ValidationError during
  parse_envelope(), BEFORE any DB claim, so the Container endpoint returns
  HTTP 400 (terminal) instead of HTTP 503 (retry).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Literal, Union

from pydantic import BaseModel, Field, ValidationError, field_validator
from pydantic_core import PydanticCustomError

ENVELOPE_VERSION: str = "1"


ZERO = timedelta(0)


def _validate_utc_iso8601(value: Any) -> str:
    """Shared strict UTC ISO-8601 validator (ND_RETURN FIX2 #4a).

    Raises pydantic_core.PydanticCustomError for any string that is not:
      (a) a parseable ISO-8601 timestamp,
      (b) timezone-aware (not naive), and
      (c) anchored at UTC (offset == timedelta(0)).

    Accepts trailing ``Z`` (converted to ``+00:00`` for fromisoformat).
    PydanticCustomError ensures ``ValidationError.errors()[*]['ctx']``
    contains only JSON-serializable values (strings) so FastAPI can
    serialize the 400 response without a secondary TypeError.
    """
    if not isinstance(value, str) or not value:
        raise PydanticCustomError(
            "timestamp_empty",
            "timestamp must be a non-empty ISO-8601 string",
            {"input_type": type(value).__name__ if value is not None else "null"},
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise PydanticCustomError(
            "timestamp_not_iso8601",
            "timestamp not a parseable ISO-8601 datetime string: {reason}",
            {"reason": str(exc), "raw": value},
        ) from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise PydanticCustomError(
            "timestamp_naive",
            "timestamp must be timezone-aware (suffix Z or +00:00 required; naive datetime forbidden)",
            {"raw": value},
        )
    if parsed.tzinfo.utcoffset(parsed) != ZERO:
        raise PydanticCustomError(
            "timestamp_not_utc",
            "timestamp must be strict UTC (offset must equal 00:00 or suffix Z; non-zero offsets like +08:00 forbidden)",
            {"raw": value, "observed_offset": str(parsed.tzinfo.utcoffset(parsed))},
        )
    return value


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
    def _occurred_at_utc(cls, v: str) -> str:
        return _validate_utc_iso8601(v)


class ScheduledJobPayload(BaseModel):
    job_name: str = Field(min_length=1, max_length=128)
    scheduled_at: str
    params: dict[str, Any] | None = None

    @field_validator("scheduled_at")
    @classmethod
    def _scheduled_at_utc(cls, v: str) -> str:
        return _validate_utc_iso8601(v)


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

    @field_validator("occurred_at")
    @classmethod
    def _occurred_at_utc(cls, v: str) -> str:
        return _validate_utc_iso8601(v)


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
