"""Telegram webhook inbound update log + idempotency table.

Revision ID: f1a2b3c4d5e6
Revises: e2a114b2f9d0
Create Date: 2026-08-20 09:30:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "f1a2b3c4d5e6"
down_revision = "e2a114b2f9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "telegram_webhook_updates",
        sa.Column("update_id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("update_type", sa.String(length=50), nullable=True),
        sa.Column(
            "state",
            sa.String(length=20),
            nullable=False,
            server_default="claimed",
        ),
        sa.Column(
            "delivery_count", sa.BigInteger(), nullable=False, server_default="1",
        ),
        sa.Column(
            "attempt_count", sa.BigInteger(), nullable=False, server_default="1",
        ),
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


def downgrade() -> None:
    op.drop_index("ix_telegram_webhook_updates_state_created", table_name="telegram_webhook_updates")
    op.drop_index("ix_telegram_webhook_updates_user_id", table_name="telegram_webhook_updates")
    op.drop_index("ix_telegram_webhook_updates_chat_id", table_name="telegram_webhook_updates")
    op.drop_table("telegram_webhook_updates")
