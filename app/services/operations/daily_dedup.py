"""Persistent daily reminder dedupe (TELEGRAM-OPS-UX-CONVERGENCE-003 §1.3/§1.4).

Product rule (frozen):
    the same ``business task/object + recipient/chat + local date + reminder
    type`` is proactively reminded AT MOST ONCE per Philippines natural day.

The scheduler may keep scanning at high frequency; high-frequency scanning
must never mean high-frequency sending. This module provides the durable,
atomic guard used at enqueue time:

- ``philippines_local_date(now)``  — the Philippines operational date
  (Asia/Manila, UTC+8, no DST) for the daily dedupe boundary, so a UTC date
  flip can never cause two sends on one PH day.
- ``claim_daily_dedup(db, ...)``   — atomic INSERT ... ON CONFLICT DO NOTHING
  against ``uq_reminder_daily_dedup_key``. Exactly one concurrent worker
  wins the row; every other pass (and every pass after a runtime restart)
  sees the conflict and skips. The DB is the only source of truth — never
  Python memory, never a race-prone ``SELECT exists -> send``.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.operations import ReminderDailyDedup

PH_TZ = ZoneInfo("Asia/Manila")

# Dedupe key prefix. The key embeds the BUSINESS dedupe_key (stable identity
# across task rows), the notification recipient, the PH local date and the
# reminder type — the exact product rule identity.
DEDUP_PREFIX = "reminder:"


def philippines_local_date(now: datetime | None = None) -> str:
    """'YYYY-MM-DD' of the Philippines operational day (Asia/Manila, UTC+8)."""
    now = now or datetime.now(PH_TZ)
    return now.astimezone(PH_TZ).date().isoformat()


def daily_dedup_key(
    business_key: str | None,
    recipient: str,
    local_date: str,
    reminder_type: str,
) -> str:
    """``reminder:{business_key}:{recipient}:{local_date}:{reminder_type}``.

    ``business_key`` is the task's ``dedupe_key`` (e.g. ``lease:3:RENT_DUE:
    2026-08``) — the stable business object identity. When a task row carries
    no dedupe_key (manual/human-confirmed tasks) the task id is used instead.
    """
    return (
        f"{DEDUP_PREFIX}{business_key or 'task'}:"
        f"{recipient}:{local_date}:{reminder_type}"
    )


def claim_daily_dedup(
    db: Session,
    *,
    business_key: str | None,
    task_id: int | None,
    recipient: str,
    reminder_type: str,
    now: datetime | None = None,
) -> bool:
    """Atomically claim today's reminder slot; True = this call may send.

    The dedupe row is inserted in the SAME transaction as the caller's outbox
    enqueue: a concurrent worker (or a later pass after a restart) hits the
    unique index conflict and returns False without ever enqueueing.
    """
    local_date = philippines_local_date(now)
    key = daily_dedup_key(business_key, recipient, local_date, reminder_type)
    stmt = (
        pg_insert(ReminderDailyDedup)
        .values(
            dedupe_key=key,
            task_id=task_id,
            recipient=recipient,
            local_date=local_date,
            reminder_type=reminder_type,
        )
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
        .returning(ReminderDailyDedup.id)
    )
    row = db.execute(stmt).first()
    return row is not None
