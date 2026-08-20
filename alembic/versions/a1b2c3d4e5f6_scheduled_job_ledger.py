"""PASAY-TASK-011 FIX1 — scheduled job idempotency ledger for Queue/Container boundary.

Revision ID: a1b2c3d4e5f6
Revises: z9a8b7c6d5e4
Create Date: 2026-08-20

Scope (from ND_RETURN PASAY-TASK-011 FIX1 blocker #4):
1. ``pasay_scheduled_job_ledger`` — replaces the runtime ``CREATE TABLE IF NOT EXISTS``
   lazy-DDL path used previously by the internal ingestion router. Alembic owns the
   table definition so the migration chain remains the single DB schema authority
   (Scope E + single-head contract).
2. Columns mirror the earlier raw DDL exactly:
   - ``event_id`` VARCHAR(256) PRIMARY KEY — deterministic envelope event_id
     (insert-on-conflict-nothing idempotency for scheduled envelopes).
   - ``job_name`` VARCHAR(128) NOT NULL — observability only.
   - ``occurred_at`` TIMESTAMPTZ NOT NULL — envelope ``occurred_at`` verbatim.
   - ``consumed_at`` TIMESTAMPTZ NOT NULL DEFAULT NOW() — audit timestamp.
   - ``payload`` JSONB NULL — optional scheduled_job envelope payload copy.

Rollback:
    alembic downgrade z9a8b7c6d5e4
"""
from typing import Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "z9a8b7c6d5e4"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if inspector.has_table("pasay_scheduled_job_ledger"):
        existing_pk = inspector.get_pk_constraint("pasay_scheduled_job_ledger")
        pk_cols = (existing_pk or {}).get("constrained_columns") or []
        if "event_id" not in pk_cols:
            raise RuntimeError(
                "pasay_scheduled_job_ledger already exists but PRIMARY KEY is not (event_id). "
                "Please manually reconcile the legacy table with this migration's schema."
            )
        existing_cols = {c["name"] for c in inspector.get_columns("pasay_scheduled_job_ledger")}
        required = {"event_id", "job_name", "occurred_at", "consumed_at", "payload"}
        missing = required - existing_cols
        if missing:
            raise RuntimeError(
                f"pasay_scheduled_job_ledger already exists but is missing required columns: {sorted(missing)}. "
                "Please manually reconcile the legacy table with this migration's schema."
            )
        return

    op.create_table(
        "pasay_scheduled_job_ledger",
        sa.Column("event_id", sa.String(length=256), nullable=False),
        sa.Column("job_name", sa.String(length=128), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "consumed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_pasay_scheduled_job_ledger")),
    )


def downgrade() -> None:
    op.drop_table("pasay_scheduled_job_ledger")
