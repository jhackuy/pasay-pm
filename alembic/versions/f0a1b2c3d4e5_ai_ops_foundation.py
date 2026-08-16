"""AI-OPS-FOUNDATION-001 — execution-oriented operations foundation.

Revision ID: f0a1b2c3d4e5
Revises: 9a8b7c6d5e4f
Create Date: 2026-08-16

Changes (appended only; CHECK/ENUM allowlists kept in sync with the models):
1. ``audit_action`` enum: add ``task_reminded`` (a later reminder pass refreshed
   the SAME active task instead of creating a duplicate sibling).
2. ``expenses``: add ``payer_user_id`` (BIGINT, nullable) — the human
   responsible for payment, so an approved expense's PAYMENT_PENDING task is
   routed to the ACTUAL payer, not always the Owner.
3. ``units``: add ``unit_state`` VARCHAR(30) nullable — richer lifecycle state
   (VACANT/PREPARING/LISTED/VIEWING/RESERVED/OCCUPIED/NOTICE_GIVEN/MOVE_OUT/
   INSPECTION) without breaking the legacy ``unit_status`` enum; plus
   ``unit_lifecycle_events`` for a durable, queryable unit timeline.
4. ``leases``: add deposit accounting columns (``deposit_received`` /
   ``deposit_refund`` / ``deposit_deductions`` JSONB) so the model can
   represent required / received / held / deductions / refund.
5. ``viewings``: new table — scheduled unit viewings with outcome/reason
   (vacancy pipeline real data).
6. ``evidence``: new table — the universal evidence index (Telegram private
   archive as the free-first storage layer; PostgreSQL stays the
   authoritative index/relationship store).

ROLLBACK:
    alembic downgrade 9a8b7c6d5e4f
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'f0a1b2c3d4e5'
down_revision: Union[str, None] = '9a8b7c6d5e4f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. audit_action is a VARCHAR(50) (native_enum=False) — the Python enum
    #    class already carries the new value; no DB enum change is needed.

    # 2. expenses.payer_user_id
    op.add_column('expenses', sa.Column('payer_user_id', sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        'fk_expenses_payer_user', 'expenses', 'users', ['payer_user_id'], ['id'],
    )

    # 3. units.unit_state + unit_lifecycle_events
    op.add_column('units', sa.Column('unit_state', sa.String(length=30), nullable=True))
    op.create_table(
        'unit_lifecycle_events',
        sa.Column('id', sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column('unit_id', sa.BigInteger(), sa.ForeignKey('units.id'), nullable=False, index=True),
        sa.Column('from_status', sa.String(length=30), nullable=True),
        sa.Column('to_status', sa.String(length=30), nullable=False),
        sa.Column('reason', sa.String(length=300), nullable=True),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_by', sa.BigInteger(), nullable=True),
        sa.Column('updated_by', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )
    op.create_index('ix_unit_lifecycle_events_unit_at', 'unit_lifecycle_events', ['unit_id', 'occurred_at'])

    # 4. leases deposit accounting
    op.add_column('leases', sa.Column('deposit_received', sa.Numeric(14, 2), nullable=True))
    op.add_column('leases', sa.Column('deposit_refund', sa.Numeric(14, 2), nullable=True))
    op.add_column('leases', sa.Column('deposit_deductions', postgresql.JSONB(), nullable=True))

    # 5. viewings
    op.create_table(
        'viewings',
        sa.Column('id', sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column('unit_id', sa.BigInteger(), sa.ForeignKey('units.id'), nullable=False, index=True),
        sa.Column('property_id', sa.BigInteger(), sa.ForeignKey('properties.id'), nullable=True, index=True),
        sa.Column('scheduled_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='scheduled'),
        sa.Column('outcome', sa.String(length=30), nullable=True),
        sa.Column('reason', sa.String(length=500), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_by', sa.BigInteger(), nullable=True),
        sa.Column('updated_by', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(
            "status IN ('scheduled','done','cancelled')", name='ck_viewings_status'
        ),
        sa.CheckConstraint(
            "outcome IN ('interested','not_interested','follow_up') OR outcome IS NULL",
            name='ck_viewings_outcome',
        ),
    )
    op.create_index('ix_viewings_status_at', 'viewings', ['status', 'scheduled_at'])

    # 6. evidence (universal evidence index; storage_provider is portable)
    op.create_table(
        'evidence',
        sa.Column('id', sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column('storage_provider', sa.String(length=30), nullable=False, server_default='telegram_channel'),
        sa.Column('external_file_id', sa.String(length=300), nullable=False),
        sa.Column('external_message_id', sa.BigInteger(), nullable=True),
        sa.Column('media_type', sa.String(length=30), nullable=True),
        sa.Column('mime_type', sa.String(length=100), nullable=True),
        sa.Column('filename', sa.String(length=255), nullable=True),
        sa.Column('size_bytes', sa.BigInteger(), nullable=True),
        sa.Column('checksum', sa.String(length=128), nullable=True),
        sa.Column('category', sa.String(length=50), nullable=True),
        sa.Column('uploaded_by', sa.BigInteger(), nullable=True),
        sa.Column('property_id', sa.BigInteger(), sa.ForeignKey('properties.id'), nullable=True, index=True),
        sa.Column('unit_id', sa.BigInteger(), sa.ForeignKey('units.id'), nullable=True, index=True),
        sa.Column('entity_type', sa.String(length=50), nullable=True),
        sa.Column('entity_id', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_by', sa.BigInteger(), nullable=True),
        sa.Column('updated_by', sa.BigInteger(), nullable=True),
    )
    op.create_index('ix_evidence_entity', 'evidence', ['entity_type', 'entity_id'])
    op.create_index('ix_evidence_unit_created', 'evidence', ['unit_id', 'created_at'])


def downgrade() -> None:
    op.drop_table('evidence')
    op.drop_table('viewings')
    op.drop_column('leases', 'deposit_deductions')
    op.drop_column('leases', 'deposit_refund')
    op.drop_column('leases', 'deposit_received')
    op.drop_table('unit_lifecycle_events')
    op.drop_column('units', 'unit_state')
    op.drop_constraint('fk_expenses_payer_user', 'expenses', type_='foreignkey')
    op.drop_column('expenses', 'payer_user_id')
