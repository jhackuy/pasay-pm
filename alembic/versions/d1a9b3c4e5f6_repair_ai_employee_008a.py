"""REPAIR-AI-EMPLOYEE-WORKFLOW-008A — AI Employee Repair Operation model.

Revision ID: d1a9b3c4e5f6
Revises: c4d5e6f7a8b9
Create Date: 2026-08-17

Adds the first-class Repair Operation (real-world problem) — decoupled from
the legacy AC_MAINTENANCE operational_tasks which are only a bridge for
back-compat — plus its versioned solution proposals and idempotent action
stream:

- ``repair_operations``: one business status per repair
  (OPEN/IN_PROGRESS/WAITING_HUMAN/WAITING_VENDOR/WAITING_APPROVAL/
  WAITING_PAYMENT/VERIFYING/CLOSED/CANCELLED) + derived AI-employee state
  (next_action / waiting_on / blocked_reason) + verification gate + closure
  record. CLOSED is ONLY reachable through verification.
- ``repair_proposals``: versioned solution candidates (PENDING/APPROVED/
  REJECTED/SUPERSEDED); (repair_id, version) unique so V1/V2 history is
  preserved. Rejecting a proposal never rejects the repair.
- ``repair_actions``: idempotent AI-continuation steps. A DB partial unique
  index on ``(repair_id, dedupe_key)`` while ACTIVE makes repeated worker
  ticks / retries unable to create duplicate "requote" actions (008A §4).

ROLLBACK:
    alembic downgrade c4d5e6f7a8b9
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'd1a9b3c4e5f6'
down_revision: Union[str, None] = 'c4d5e6f7a8b9'
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
        'repair_operations',
        sa.Column('merchant_id', sa.BigInteger(), nullable=True),
        sa.Column('property_id', sa.BigInteger(), nullable=True),
        sa.Column('unit_id', sa.BigInteger(), nullable=True),
        sa.Column('issue', sa.String(length=200), nullable=False),
        sa.Column('issue_description', sa.Text(), nullable=True),
        sa.Column('created_source', sa.String(length=50), nullable=False),
        sa.Column('reported_by', sa.BigInteger(), nullable=True),
        sa.Column('assignee_user_id', sa.BigInteger(), nullable=True),
        sa.Column('status', sa.String(length=50), nullable=False),
        sa.Column('next_action', sa.String(length=400), nullable=True),
        sa.Column('waiting_on', sa.String(length=50), nullable=True),
        sa.Column('blocked_reason', sa.Text(), nullable=True),
        sa.Column('next_check_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('closure_criteria', sa.Text(), nullable=True),
        sa.Column('verified_by', sa.BigInteger(), nullable=True),
        sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('verification_result', sa.Text(), nullable=True),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('closure_reason', sa.Text(), nullable=True),
        sa.Column('evidence', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('operational_task_id', sa.BigInteger(), nullable=True),
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        *_audit_cols(),
        sa.CheckConstraint(
            "status IN ('OPEN','IN_PROGRESS','WAITING_HUMAN','WAITING_VENDOR',"
            "'WAITING_APPROVAL','WAITING_PAYMENT','VERIFYING','CLOSED','CANCELLED')",
            name='ck_repair_operations_status',
        ),
        sa.ForeignKeyConstraint(['assignee_user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['property_id'], ['properties.id']),
        sa.ForeignKeyConstraint(['unit_id'], ['units.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_repair_operations_status', 'repair_operations', ['status'], unique=False)
    op.create_index('ix_repair_operations_property_id', 'repair_operations', ['property_id'], unique=False)
    op.create_index('ix_repair_operations_unit_id', 'repair_operations', ['unit_id'], unique=False)
    op.create_index(
        'ix_repair_operations_assignee_status', 'repair_operations',
        ['assignee_user_id', 'status'], unique=False,
    )
    op.create_index(
        'ix_repair_operations_merchant_id', 'repair_operations', ['merchant_id'], unique=False,
    )
    op.create_index(
        'ix_repair_operations_operational_task_id', 'repair_operations',
        ['operational_task_id'], unique=False,
    )
    op.create_index(
        'ix_repair_operations_next_check_at', 'repair_operations',
        ['next_check_at'], unique=False,
    )

    op.create_table(
        'repair_proposals',
        sa.Column('repair_id', sa.BigInteger(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('vendor', sa.String(length=200), nullable=True),
        sa.Column('source', sa.String(length=50), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('amount', sa.Numeric(14, 2), nullable=False),
        sa.Column('submitted_by', sa.BigInteger(), nullable=True),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status', sa.String(length=50), nullable=False),
        sa.Column('decision_by', sa.BigInteger(), nullable=True),
        sa.Column('decision_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('rejection_reason', sa.Text(), nullable=True),
        sa.Column('expense_id', sa.BigInteger(), nullable=True),
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        *_audit_cols(),
        sa.CheckConstraint(
            "status IN ('PENDING','APPROVED','REJECTED','SUPERSEDED')",
            name='ck_repair_proposals_status',
        ),
        sa.ForeignKeyConstraint(['expense_id'], ['expenses.id']),
        sa.ForeignKeyConstraint(['repair_id'], ['repair_operations.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'uq_repair_proposals_repair_version', 'repair_proposals',
        ['repair_id', 'version'], unique=True,
    )
    op.create_index('ix_repair_proposals_repair_id', 'repair_proposals', ['repair_id'], unique=False)
    op.create_index('ix_repair_proposals_expense_id', 'repair_proposals', ['expense_id'], unique=False)

    op.create_table(
        'repair_actions',
        sa.Column('repair_id', sa.BigInteger(), nullable=False),
        sa.Column('action_kind', sa.String(length=50), nullable=False),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=50), nullable=False),
        sa.Column('assigned_user_id', sa.BigInteger(), nullable=True),
        sa.Column('due_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('next_check_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('dedupe_key', sa.String(length=255), nullable=False),
        sa.Column('source_event', sa.String(length=160), nullable=True),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolved_by', sa.BigInteger(), nullable=True),
        sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        *_audit_cols(),
        sa.CheckConstraint(
            "status IN ('PENDING','IN_PROGRESS','COMPLETED','CANCELLED')",
            name='ck_repair_actions_status',
        ),
        sa.ForeignKeyConstraint(['assigned_user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['repair_id'], ['repair_operations.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'uq_repair_actions_active_dedupe', 'repair_actions', ['repair_id', 'dedupe_key'],
        unique=True, postgresql_where=sa.text("status IN ('PENDING','IN_PROGRESS')"),
    )
    op.create_index('ix_repair_actions_repair_id', 'repair_actions', ['repair_id'], unique=False)
    op.create_index(
        'ix_repair_actions_assignee_status', 'repair_actions',
        ['assigned_user_id', 'status'], unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_repair_actions_assignee_status', table_name='repair_actions')
    op.drop_index('ix_repair_actions_repair_id', table_name='repair_actions')
    op.drop_index('uq_repair_actions_active_dedupe', table_name='repair_actions')
    op.drop_table('repair_actions')

    op.drop_index('ix_repair_proposals_expense_id', table_name='repair_proposals')
    op.drop_index('ix_repair_proposals_repair_id', table_name='repair_proposals')
    op.drop_index('uq_repair_proposals_repair_version', table_name='repair_proposals')
    op.drop_table('repair_proposals')

    op.drop_index('ix_repair_operations_next_check_at', table_name='repair_operations')
    op.drop_index('ix_repair_operations_operational_task_id', table_name='repair_operations')
    op.drop_index('ix_repair_operations_merchant_id', table_name='repair_operations')
    op.drop_index('ix_repair_operations_assignee_status', table_name='repair_operations')
    op.drop_index('ix_repair_operations_unit_id', table_name='repair_operations')
    op.drop_index('ix_repair_operations_property_id', table_name='repair_operations')
    op.drop_index('ix_repair_operations_status', table_name='repair_operations')
    op.drop_table('repair_operations')
