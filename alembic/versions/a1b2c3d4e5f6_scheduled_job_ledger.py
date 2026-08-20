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
        pk_cols = sorted((existing_pk or {}).get("constrained_columns") or [])
        if pk_cols != ["event_id"]:
            raise RuntimeError(
                "pasay_scheduled_job_ledger already exists but PRIMARY KEY is not "
                f"EXACTLY the single column (event_id). Got PK columns: {pk_cols!r}. "
                "The ledger uses INSERT … ON CONFLICT (event_id) DO NOTHING and "
                "requires a single-column unique PK on event_id. Please manually "
                "reconcile the legacy table with this migration's schema."
            )
        existing_cols = {c["name"]: c for c in inspector.get_columns("pasay_scheduled_job_ledger")}
        required_names = {"event_id", "job_name", "occurred_at", "consumed_at", "payload"}
        missing = required_names - set(existing_cols.keys())
        if missing:
            raise RuntimeError(
                f"pasay_scheduled_job_ledger already exists but is missing required columns: {sorted(missing)}. "
                "Please manually reconcile the legacy table with this migration's schema."
            )
        event_id_col = existing_cols["event_id"]
        event_id_type = getattr(event_id_col.get("type", None), "python_type", None)
        if event_id_type is not None and not issubclass(event_id_type, str):
            raise RuntimeError(
                f"pasay_scheduled_job_ledger.event_id column must be a string type compatible with VARCHAR(256); got python_type={event_id_type!r}. "
                "Please manually reconcile the legacy table with this migration's schema."
            )
        occurred_at_col = existing_cols["occurred_at"]
        oa_type = occurred_at_col.get("type")
        oa_is_tz = getattr(oa_type, "timezone", None) if oa_type is not None else None
        if oa_type is not None and oa_is_tz is False:
            raise RuntimeError(
                "pasay_scheduled_job_ledger.occurred_at must be TIMESTAMPTZ (timezone-aware); "
                f"got a naive datetime column type: {oa_type!r}. "
                "Please manually reconcile the legacy table with this migration's schema."
            )
        payload_col = existing_cols.get("payload")
        if payload_col is not None:
            payload_raw_type = payload_col.get("type")
            payload_dialect_name = ""
            try:
                payload_dialect_name = payload_raw_type.compile(dialect=postgresql.dialect()) if payload_raw_type is not None else ""
            except Exception:
                payload_dialect_name = ""
            payload_is_json_compat = (
                payload_dialect_name.upper().startswith("JSON")
                or "JSON" in type(payload_raw_type).__name__.upper()
            )
            if payload_raw_type is not None and payload_raw_type._isnull is False and not payload_is_json_compat:
                raise RuntimeError(
                    f"pasay_scheduled_job_ledger.payload must be JSON/JSONB-compatible for JSONB cast; got {payload_raw_type!r}. "
                    "Please manually reconcile the legacy table with this migration's schema."
                )
        consumed_at_col = existing_cols["consumed_at"]
        if consumed_at_col.get("default") is None and consumed_at_col.get("server_default") is None:
            pass
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
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("pasay_scheduled_job_ledger"):
        return

    existing_pk = inspector.get_pk_constraint("pasay_scheduled_job_ledger")
    pk_cols = sorted((existing_pk or {}).get("constrained_columns") or [])
    if pk_cols != ["event_id"]:
        raise RuntimeError(
            "pasay_scheduled_job_ledger exists but PRIMARY KEY is not EXACTLY "
            f"the single column (event_id). Got PK columns: {pk_cols!r}. "
            "Refusing to drop a table that may contain legacy ledger data "
            "created outside this migration. Please manually reconcile before "
            "running downgrade."
        )

    existing_cols = {c["name"]: c for c in inspector.get_columns("pasay_scheduled_job_ledger")}
    required_names = {"event_id", "job_name", "occurred_at", "consumed_at", "payload"}
    present = set(existing_cols.keys())
    if present != required_names:
        extra = present - required_names
        missing = required_names - present
        raise RuntimeError(
            "pasay_scheduled_job_ledger column set does not EXACTLY match the "
            f"schema created by this migration. Extra columns: {sorted(extra)!r}. "
            f"Missing columns: {sorted(missing)!r}. Refusing to drop to preserve "
            "existing data. Please manually reconcile."
        )

    event_id_col = existing_cols["event_id"]
    event_id_type = getattr(event_id_col.get("type", None), "python_type", None)
    if event_id_type is not None and not issubclass(event_id_type, str):
        raise RuntimeError(
            f"pasay_scheduled_job_ledger.event_id column must be a string type "
            f"compatible with VARCHAR(256); got python_type={event_id_type!r}. "
            "Refusing to drop a mismatched legacy table. Please manually reconcile."
        )

    occurred_at_col = existing_cols["occurred_at"]
    oa_type = occurred_at_col.get("type")
    oa_is_tz = getattr(oa_type, "timezone", None) if oa_type is not None else None
    if oa_type is not None and oa_is_tz is False:
        raise RuntimeError(
            "pasay_scheduled_job_ledger.occurred_at must be TIMESTAMPTZ "
            f"(timezone-aware); got a naive datetime column type: {oa_type!r}. "
            "Refusing to drop a mismatched legacy table. Please manually reconcile."
        )

    op.drop_table("pasay_scheduled_job_ledger")
