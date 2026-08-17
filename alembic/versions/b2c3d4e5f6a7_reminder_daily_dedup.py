"""TELEGRAM-OPS-UX-CONVERGENCE-003 §1.4: persistent same-day reminder dedupe.

Revision ID: b2c3d4e5f6a7
Revises: f0a1b2c3d4e5
Create Date: 2026-08-17

One table ``reminder_daily_dedup``: unique ``dedupe_key``
(``reminder:<business>:<recipient>:<local_date>:<type>``) makes the daily
reminder enqueue atomic (INSERT ... ON CONFLICT DO NOTHING), survives runtime
restarts and is safe under concurrent workers.

ROLLBACK:
    alembic downgrade f0a1b2c3d4e5
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, None] = 'f0a1b2c3d4e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'reminder_daily_dedup',
        sa.Column('id', sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column('dedupe_key', sa.String(length=255), nullable=False),
        sa.Column('task_id', sa.BigInteger(), sa.ForeignKey('operational_tasks.id'), nullable=True),
        sa.Column('recipient', sa.String(length=200), nullable=False),
        sa.Column('local_date', sa.String(length=10), nullable=False),
        sa.Column('reminder_type', sa.String(length=50), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('created_by', sa.BigInteger(), nullable=True),
        sa.Column('updated_by', sa.BigInteger(), nullable=True),
    )
    op.create_index('uq_reminder_daily_dedup_key', 'reminder_daily_dedup', ['dedupe_key'], unique=True)
    op.create_index('ix_reminder_daily_dedup_date', 'reminder_daily_dedup', ['local_date'], unique=False)
    op.create_index('ix_reminder_daily_dedup_task_id', 'reminder_daily_dedup', ['task_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_reminder_daily_dedup_task_id', table_name='reminder_daily_dedup')
    op.drop_index('ix_reminder_daily_dedup_date', table_name='reminder_daily_dedup')
    op.drop_index('uq_reminder_daily_dedup_key', table_name='reminder_daily_dedup')
    op.drop_table('reminder_daily_dedup')
