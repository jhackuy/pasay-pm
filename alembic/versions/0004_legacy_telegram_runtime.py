"""Create legacy Telegram runtime tables required by deployed container.

Issue #119 production black-box root cause after PR #134:
``process_telegram_update_payload`` fails before PTB dispatch because
``telegram_webhook_updates`` is absent from the production migration chain.
The scheduled-job ledger is created here as well because the same deployed
legacy runtime references it for outbound job idempotency.

Revision ID: 0004_legacy_telegram_runtime
Revises: 0003_units_cap
Create Date: 2026-09-06
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0004_legacy_telegram_runtime"
down_revision = "0003_units_cap"
branch_labels = None
depends_on = None


LEDGER_TABLE_COMMENT = (
    "OWNED_BY_ALEMBIC_REV=a1b2c3d4e5f6;"
    "SCHEMA_REV=2;"
    "DIGEST=cols:event_id[256PK]+job_name[128NN]+occurred_at[TZNN]+"
    "consumed_at[TZNNDEFNOW]+payload[JSONB]|TZ:pg|dialect:jsonb-pg;"
    "SOURCE=alembic-upgrade-a1b2c3d4e5f6;"
    "LEDGER_TYPE=scheduled-job-idempotency;"
)


def upgrade() -> None:
    op.create_table(
        "telegram_webhook_updates",
        sa.Column("update_id", sa.BigInteger(), primary_key=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("update_type", sa.String(length=50), nullable=True),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("delivery_count", sa.BigInteger(), nullable=False),
        sa.Column("attempt_count", sa.BigInteger(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_error_type", sa.String(length=200), nullable=True),
        sa.Column("handler_result_summary", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('claimed','done','failed','retryable')",
            name="ck_telegram_webhook_updates_state",
        ),
    )
    op.create_index(
        "ix_telegram_webhook_updates_chat_id",
        "telegram_webhook_updates",
        ["chat_id"],
    )
    op.create_index(
        "ix_telegram_webhook_updates_user_id",
        "telegram_webhook_updates",
        ["user_id"],
    )
    op.create_index(
        "ix_telegram_webhook_updates_state_created",
        "telegram_webhook_updates",
        ["state", "created_at"],
    )

    op.create_table(
        "pasay_scheduled_job_ledger",
        sa.Column("event_id", sa.String(length=256), primary_key=True),
        sa.Column("job_name", sa.String(length=128), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "consumed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.String()),
            nullable=True,
        ),
        comment=LEDGER_TABLE_COMMENT,
    )


def downgrade() -> None:
    op.drop_table("pasay_scheduled_job_ledger")
    op.drop_index(
        "ix_telegram_webhook_updates_state_created",
        table_name="telegram_webhook_updates",
    )
    op.drop_index(
        "ix_telegram_webhook_updates_user_id",
        table_name="telegram_webhook_updates",
    )
    op.drop_index(
        "ix_telegram_webhook_updates_chat_id",
        table_name="telegram_webhook_updates",
    )
    op.drop_table("telegram_webhook_updates")
