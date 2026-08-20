"""Scheduled job ledger idempotency table.

Schema authority: Alembic migration ``a1b2c3d4e5f6_scheduled_job_ledger``
owns the DDL; this ORM model is the SINGLE Python-side truth source for the
same column contract.  Runtime code MUST import this model via
``from app.models import ScheduledJobLedger`` and reference
``ScheduledJobLedger.__table__`` instead of inlining a ``sa.Table(...)``
declaration inside a request handler.  Doing so would create a second
Python schema truth source that could silently drift against the Alembic
DDL — that is exactly the anti-pattern banned by ND_RETURN PASAY-TASK-011
FIX1 blocker #4 ("Ledger owned by Alembic, not runtime lazy DDL").

Contract (must exactly match ``a1b2c3d4e5f6_scheduled_job_ledger`` DDL):
  * PK: ``event_id``  — VARCHAR(256), single column.  INSERT … ON CONFLICT
    (event_id) DO NOTHING is the idempotency mechanism.
  * ``job_name``      — VARCHAR(128), NOT NULL — operator label.
  * ``occurred_at``   — TIMESTAMPTZ, NOT NULL — bucket-floored scheduled
    instant from Worker cron ``scheduled()`` handler.
  * ``consumed_at``   — TIMESTAMPTZ, NOT NULL, server_default NOW() — when
    the backend first CLAIMED the row (INSERT win).  Used for
    owner-visibility / audit.
  * ``payload``       — JSONB, NULLABLE — optional {params} JSON dict from
    the scheduled_job envelope.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ScheduledJobLedger(Base):
    __tablename__ = "pasay_scheduled_job_ledger"

    event_id: Mapped[str] = mapped_column(String(length=256), primary_key=True)
    job_name: Mapped[str] = mapped_column(String(length=128), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    consumed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    payload: Mapped[dict[str, Any] | None] = mapped_column(
        postgresql.JSONB(astext_type=String()), nullable=True,
    )
