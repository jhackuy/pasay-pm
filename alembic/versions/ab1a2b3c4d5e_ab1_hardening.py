"""V1.2.2 A+B.1 hardening: actor-scoped idempotency + reminder generation + claim marker

Revision ID: ab1a2b3c4d5e
Revises: 7a1b2c3d4e5f
Create Date: 2026-08-11

Changes:
1. ``copilot_action_proposals``: replace the GLOBAL unique ``idempotency_key``
   with a composite ``UNIQUE(actor_user_id, idempotency_key)`` so two different
   actors using the same key are independent requests (same actor + same key
   stays idempotent).
2. ``operational_tasks``: add ``reminder_generation`` (int, default 0), bumped
   on every snooze / complete / cancel so the snooze-redelivery logical
   identity is ``(task, generation, window)`` instead of ``(task, window)``.
3. ``notification_outbox``: add ``claimed_at`` (nullable timestamptz), the
   durable claim marker for the notifier's atomic claim -> send -> finalize
   flow (reclaimed only after the claim lease expires).

ROLLBACK:
    alembic downgrade 7a1b2c3d4e5f
  (drops claimed_at / reminder_generation; restores the global unique
  idempotency_key index — note a downgrade fails if the composite key no
  longer uniquely identifies rows, which is expected: the down path is a
  clean-rollback migration for pre-release databases.)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'ab1a2b3c4d5e'
down_revision: Union[str, None] = '7a1b2c3d4e5f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index(
        'uq_copilot_action_proposals_idempotency',
        table_name='copilot_action_proposals',
    )
    op.create_index(
        'uq_copilot_action_proposals_actor_idempotency',
        'copilot_action_proposals',
        ['actor_user_id', 'idempotency_key'],
        unique=True,
    )
    op.add_column(
        'operational_tasks',
        sa.Column('reminder_generation', sa.Integer(), nullable=False, server_default='0'),
    )
    op.add_column(
        'notification_outbox',
        sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('notification_outbox', 'claimed_at')
    op.drop_column('operational_tasks', 'reminder_generation')
    op.drop_index(
        'uq_copilot_action_proposals_actor_idempotency',
        table_name='copilot_action_proposals',
    )
    op.create_index(
        'uq_copilot_action_proposals_idempotency',
        'copilot_action_proposals',
        ['idempotency_key'],
        unique=True,
    )
