"""V1.2.2 OPS COPILOT Phase A+B: copilot_runs + copilot_action_proposals

Revision ID: 7a1b2c3d4e5f
Revises: 3c9a2f7b1e4d
Create Date: 2026-08-10

Adds:
- ``copilot_runs``: one row per deterministic context build (audit log of
  what the Copilot was shown; DB is the source of truth).
- ``copilot_action_proposals``: user-confirmed action intents. DB-level
  idempotency (unique idempotency_key) and CHECK-constrained action_type /
  target_type / status allowlists. Nothing in Phase A+B may set
  ``executed_at`` — execution is Phase C.

ROLLBACK:
    alembic downgrade 3c9a2f7b1e4d   (or: alembic downgrade -1)
  (drops the two tables; no other data is touched.)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '7a1b2c3d4e5f'
down_revision: Union[str, None] = '3c9a2f7b1e4d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _audit_cols():
    return [
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_by', sa.BigInteger(), nullable=True),
        sa.Column('updated_by', sa.BigInteger(), nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        'copilot_runs',
        sa.Column('actor_user_id', sa.BigInteger(), nullable=False),
        sa.Column('intent', sa.String(length=100), nullable=False),
        sa.Column('context_snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('status', sa.String(length=50), nullable=False),
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        *_audit_cols(),
        sa.CheckConstraint(
            "status IN ('COMPLETED','FAILED')",
            name='ck_copilot_runs_status',
        ),
        sa.ForeignKeyConstraint(['actor_user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_copilot_runs_actor_intent', 'copilot_runs', ['actor_user_id', 'intent'], unique=False
    )
    op.create_index('ix_copilot_runs_actor_user_id', 'copilot_runs', ['actor_user_id'], unique=False)

    op.create_table(
        'copilot_action_proposals',
        sa.Column('actor_user_id', sa.BigInteger(), nullable=False),
        sa.Column('action_type', sa.String(length=50), nullable=False),
        sa.Column('target_type', sa.String(length=50), nullable=False),
        sa.Column('target_id', sa.BigInteger(), nullable=False),
        sa.Column('payload_json', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('status', sa.String(length=50), nullable=False),
        sa.Column('idempotency_key', sa.String(length=128), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('executed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        *_audit_cols(),
        sa.CheckConstraint(
            "action_type IN ('summarize','analyze','explain','risk_scan',"
            "'create_task','assign_task','snooze_task','follow_up')",
            name='ck_copilot_action_proposals_action_type',
        ),
        sa.CheckConstraint(
            "target_type IN ('property','lease','task','expense','income','settlement')",
            name='ck_copilot_action_proposals_target_type',
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','CONFIRMED','EXECUTED','CANCELLED','EXPIRED')",
            name='ck_copilot_action_proposals_status',
        ),
        sa.ForeignKeyConstraint(['actor_user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'uq_copilot_action_proposals_idempotency',
        'copilot_action_proposals',
        ['idempotency_key'],
        unique=True,
    )
    op.create_index(
        'ix_copilot_action_proposals_actor_status',
        'copilot_action_proposals',
        ['actor_user_id', 'status'],
        unique=False,
    )
    op.create_index(
        'ix_copilot_action_proposals_actor_user_id',
        'copilot_action_proposals',
        ['actor_user_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        'ix_copilot_action_proposals_actor_user_id',
        table_name='copilot_action_proposals',
    )
    op.drop_index(
        'ix_copilot_action_proposals_actor_status',
        table_name='copilot_action_proposals',
    )
    op.drop_index(
        'uq_copilot_action_proposals_idempotency',
        table_name='copilot_action_proposals',
    )
    op.drop_table('copilot_action_proposals')

    op.drop_index('ix_copilot_runs_actor_user_id', table_name='copilot_runs')
    op.drop_index('ix_copilot_runs_actor_intent', table_name='copilot_runs')
    op.drop_table('copilot_runs')
