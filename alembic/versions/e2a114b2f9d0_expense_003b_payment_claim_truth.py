"""PASAY-VNEXT-EXPENSE-OPERATION-003B — Expense payment-claim truth model.

Revision ID: e2a114b2f9d0
Revises: d1a9b3c4e5f6
Create Date: 2026-08-17

Adds the authoritative payment-claim truth layer so an Expense's paid/remaining
state derives from VERIFIED claims, not a raw approved->paid flip:

- ``expenses`` gains 003B continuity columns (rejection_reason, reapproval_reason,
  version, parent_expense_id). The ``expense_status`` values are enforced by the
  application model (``pg_enum``); the DB stores varchar without a CHECK for
  these status columns (matching the existing expenses.status / audit_logs.action
  schema which have no DB CHECK), so no constraint alteration is required to add
  ``payment_claimed`` / ``partially_paid``.
- ``expense_payment_claims``: one row per reported payment with its own
  lifecycle (PENDING -> VERIFIED | FAILED | REVERSED), a deterministic
  idempotency key (DB unique partial index) for duplicate-submission
  protection, and amount-mismatch preservation.

ROLLBACK:
    alembic downgrade d1a9b3c4e5f6
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'e2a114b2f9d0'
down_revision: Union[str, None] = 'd1a9b3c4e5f6'
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
    op.add_column('expenses', sa.Column('rejection_reason', sa.Text(), nullable=True))
    op.add_column('expenses', sa.Column('reapproval_reason', sa.Text(), nullable=True))
    op.add_column('expenses', sa.Column('version', sa.BigInteger(), nullable=True))
    op.add_column('expenses', sa.Column('parent_expense_id', sa.BigInteger(), nullable=True))
    op.create_index('ix_expenses_parent_expense_id', 'expenses', ['parent_expense_id'], unique=False)

    op.create_table(
        'expense_payment_claims',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('expense_id', sa.BigInteger(), nullable=False),
        sa.Column('claimed_amount', sa.Numeric(14, 2), nullable=False),
        sa.Column('claimed_by', sa.BigInteger(), nullable=True),
        sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.String(length=50), nullable=False),
        sa.Column('evidence_ids', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('verification_note', sa.Text(), nullable=True),
        sa.Column('verified_amount', sa.Numeric(14, 2), nullable=True),
        sa.Column('verified_by', sa.BigInteger(), nullable=True),
        sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('mismatch', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('mismatch_reason', sa.Text(), nullable=True),
        sa.Column('failure_reason', sa.Text(), nullable=True),
        sa.Column('idempotency_key', sa.String(length=255), nullable=True),
        *_audit_cols(),
        sa.CheckConstraint(
            "status IN ('PENDING','VERIFIED','FAILED','REVERSED')",
            name='ck_expense_payment_claims_status',
        ),
        sa.ForeignKeyConstraint(['expense_id'], ['expenses.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'uq_expense_payment_claims_idempotency_key', 'expense_payment_claims',
        ['idempotency_key'], unique=True,
        postgresql_where=sa.text('idempotency_key IS NOT NULL'),
    )
    op.create_index(
        'ix_expense_payment_claims_expense_status', 'expense_payment_claims',
        ['expense_id', 'status'], unique=False,
    )
    op.create_index(
        'ix_expense_payment_claims_status', 'expense_payment_claims',
        ['status'], unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_expense_payment_claims_status', table_name='expense_payment_claims')
    op.drop_index('ix_expense_payment_claims_expense_status', table_name='expense_payment_claims')
    op.drop_index('uq_expense_payment_claims_idempotency_key', table_name='expense_payment_claims')
    op.drop_table('expense_payment_claims')

    op.drop_index('ix_expenses_parent_expense_id', table_name='expenses')
    op.drop_column('expenses', 'parent_expense_id')
    op.drop_column('expenses', 'version')
    op.drop_column('expenses', 'reapproval_reason')
    op.drop_column('expenses', 'rejection_reason')
