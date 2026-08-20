"""Telegram webhook inbound-update log + idempotency table.

This table is the webhook equivalent of the polling consumer's in-memory
``update_queue`` plus SQLite idempotency guard, but persisted to PostgreSQL
so process restarts and horizontal replicas never double-execute an update
that has already been accepted by the backend.

States:
  * ``claimed``  — we inserted the row (UPDATE_ID primary key win) and are
                   currently running handlers. A concurrent Telegram replay
                   or a duplicate worker sees this row and short-circuits.
  * ``done``     — handlers returned cleanly. Future replays return 200 OK
                   without touching domain code.
  * ``failed``   — handlers raised a non-temporary exception. Future replays
                   also short-circuit (the same malformed/business-invalid
                   payload would fail again, and Telegram retries are finite).
  * ``retryable``— a *temporary* failure (DB/Telegram transient error) was
                   observed within the retry budget. The row is left in this
                   state; Telegram's own delivery retry will eventually hit
                   the endpoint again and a fresh attempt is allowed because
                   a claimed row older than ``CLAIM_STALE_SECONDS`` is treated
                   as a stale claim from a crashed worker.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

CLAIM_STALE_SECONDS = 120  # a "claimed" row older than this is treated as crashed


class TelegramWebhookState(str, Enum):
    claimed = "claimed"
    done = "done"
    failed = "failed"
    retryable = "retryable"


class TelegramWebhookUpdate(Base):
    __tablename__ = "telegram_webhook_updates"
    __table_args__ = (
        CheckConstraint(
            "state IN ('claimed','done','failed','retryable')",
            name="ck_telegram_webhook_updates_state",
        ),
        Index("ix_telegram_webhook_updates_state_created", "state", "created_at"),
    )

    # Telegram's globally unique update identifier (strictly monotonic per bot).
    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # Effective chat / user ids (best-effort, null when the update has none).
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)

    # One of the Telegram Update types (message / callback_query / ...).
    update_type: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Lifecycle.
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, default=TelegramWebhookState.claimed.value,
    )
    # delivery_count: how many times Telegram has *delivered* this update_id
    # to the backend (i.e. how many times we have CLAIMED it via INSERT/CAS).
    # Used exclusively for cross-request budget (F7): max_attempts_cross is
    # compared against this value, so in-process backoff attempts do NOT
    # prematurely exhaust the budget.
    delivery_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    # attempt_count: TOTAL number of times process_update was (or will be)
    # invoked for this update_id across *all* cross-request attempts.
    #   = Σ per_request (1 claim + max(0, in_process_attempts - 1))
    # Purely diagnostic / owner visibility.
    attempt_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    handler_result_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    @staticmethod
    def claim_stale_cutoff(now: datetime | None = None) -> datetime:
        now = now or datetime.now(timezone.utc)
        return now - timedelta(seconds=CLAIM_STALE_SECONDS)
