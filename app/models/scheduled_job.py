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

FIX13 — Ownership Marker + Legacy Data Preservation:
  The Alembic migration writes a PostgreSQL ``COMMENT ON TABLE`` machine
  token with ``OWNED_BY_ALEMBIC_REV=a1b2c3d4e5f6`` and ``SCHEMA_REV=2``.
  The ORM ``__table_args__`` comment field below MUST match the expected
  marker text EXACTLY — test_t8d asserts them byte-for-byte identical so
  a future edit that forgets to bump the Alembic marker or the ORM
  comment Fails Closed immediately (no silent drift).

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


ALEMBIC_OWNERSHIP_REV: str = "a1b2c3d4e5f6"
SCHEMA_REV: str = "2"
LEDGER_SCHEMA_DIGEST: str = (
    "cols:event_id[256PK]+job_name[128NN]+occurred_at[TZNN]+consumed_at[TZNNDEFNOW]+payload[JSONB]"
    "|TZ:pg|dialect:jsonb-pg"
)

EXPECTED_TABLE_COMMENT: str = (
    f"OWNED_BY_ALEMBIC_REV={ALEMBIC_OWNERSHIP_REV};"
    f"SCHEMA_REV={SCHEMA_REV};"
    f"DIGEST={LEDGER_SCHEMA_DIGEST};"
    f"SOURCE=alembic-upgrade-{ALEMBIC_OWNERSHIP_REV};"
    "LEDGER_TYPE=scheduled-job-idempotency;"
)


class ScheduledJobLedger(Base):
    __tablename__ = "pasay_scheduled_job_ledger"
    __table_args__ = {
        "comment": EXPECTED_TABLE_COMMENT,
    }

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
