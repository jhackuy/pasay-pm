"""V1.2 PROACTIVE OPERATIONS: operational_tasks / recurring_rules / notification_outbox

Revision ID: 3c9a2f7b1e4d
Revises: 1f1955f798cb
Create Date: 2026-08-10

Adds:
- ``users.telegram_chat_id`` (nullable) — where the notifier delivers.
- ``operational_tasks``: business-event reminders with a DB-level partial
  unique index (one PENDING task per dedupe_key) + VARCHAR+CHECK enums.
- ``recurring_rules``: rule-driven task generation.
- ``notification_outbox``: at-least-once outbox (dedupe_key unique).

NOTE on enums: the codebase stores enums as VARCHAR; unlike the legacy
tables (plain VARCHAR without CHECK), the new tables add explicit CHECK
constraints so bad status/type values are rejected by PostgreSQL itself.

ROLLBACK:
    alembic downgrade 1f1955f798cb   (or: alembic downgrade -1)
  (drops the three tables + the users column; no data is lost elsewhere).

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '3c9a2f7b1e4d'
down_revision: Union[str, None] = '1f1955f798cb'
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
    op.add_column('users', sa.Column('telegram_chat_id', sa.String(length=64), nullable=True))
    op.create_index('ix_users_telegram_chat_id', 'users', ['telegram_chat_id'], unique=False)

    op.create_table(
        'operational_tasks',
        sa.Column('task_type', sa.String(length=50), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('property_id', sa.BigInteger(), nullable=True),
        sa.Column('tenant_id', sa.BigInteger(), nullable=True),
        sa.Column('lease_id', sa.BigInteger(), nullable=True),
        sa.Column('source_type', sa.String(length=50), nullable=False),
        sa.Column('source_id', sa.BigInteger(), nullable=True),
        sa.Column('assigned_user_id', sa.BigInteger(), nullable=True),
        sa.Column('priority', sa.String(length=20), nullable=False),
        sa.Column('status', sa.String(length=50), nullable=False),
        sa.Column('due_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('remind_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('snoozed_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_by', sa.BigInteger(), nullable=True),
        sa.Column('dedupe_key', sa.String(length=255), nullable=True),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        *_audit_cols(),
        sa.CheckConstraint(
            "priority IN ('low','medium','high','critical')",
            name='ck_operational_tasks_priority',
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','COMPLETED','CANCELLED')",
            name='ck_operational_tasks_status',
        ),
        sa.CheckConstraint(
            "task_type IN ('RENT_DUE','RENT_OVERDUE','LEASE_EXPIRING',"
            "'PROPERTY_FEE_DUE','AC_MAINTENANCE','APPROVAL_PENDING',"
            "'PAYMENT_PENDING','SETTLEMENT_PENDING')",
            name='ck_operational_tasks_task_type',
        ),
        sa.ForeignKeyConstraint(['assigned_user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['lease_id'], ['leases.id']),
        sa.ForeignKeyConstraint(['property_id'], ['properties.id']),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'uq_operational_tasks_active_dedupe', 'operational_tasks', ['dedupe_key'],
        unique=True, postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.create_index('ix_operational_tasks_task_type_status', 'operational_tasks', ['task_type', 'status'], unique=False)
    op.create_index('ix_operational_tasks_due_at', 'operational_tasks', ['due_at'], unique=False)
    op.create_index('ix_operational_tasks_status_due_at', 'operational_tasks', ['status', 'due_at'], unique=False)
    op.create_index('ix_operational_tasks_assigned_status', 'operational_tasks', ['assigned_user_id', 'status'], unique=False)
    op.create_index('ix_operational_tasks_property_id', 'operational_tasks', ['property_id'], unique=False)
    op.create_index('ix_operational_tasks_tenant_id', 'operational_tasks', ['tenant_id'], unique=False)
    op.create_index('ix_operational_tasks_lease_id', 'operational_tasks', ['lease_id'], unique=False)
    op.create_index('ix_operational_tasks_source_id', 'operational_tasks', ['source_id'], unique=False)

    op.create_table(
        'recurring_rules',
        sa.Column('rule_type', sa.String(length=50), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('property_id', sa.BigInteger(), nullable=True),
        sa.Column('recurrence', sa.String(length=20), nullable=False),
        sa.Column('interval_months', sa.Integer(), nullable=True),
        sa.Column('next_run_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('assigned_user_id', sa.BigInteger(), nullable=True),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        *_audit_cols(),
        sa.CheckConstraint(
            "recurrence IN ('monthly','quarterly','yearly','fixed_interval')",
            name='ck_recurring_rules_recurrence',
        ),
        sa.CheckConstraint(
            "rule_type IN ('RENT_DUE','RENT_OVERDUE','LEASE_EXPIRING',"
            "'PROPERTY_FEE_DUE','AC_MAINTENANCE','APPROVAL_PENDING',"
            "'PAYMENT_PENDING','SETTLEMENT_PENDING')",
            name='ck_recurring_rules_rule_type',
        ),
        sa.ForeignKeyConstraint(['assigned_user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['property_id'], ['properties.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_recurring_rules_enabled_next_run', 'recurring_rules', ['enabled', 'next_run_at'], unique=False)
    op.create_index('ix_recurring_rules_property_id', 'recurring_rules', ['property_id'], unique=False)

    op.create_table(
        'notification_outbox',
        sa.Column('task_id', sa.BigInteger(), nullable=True),
        sa.Column('channel', sa.String(length=20), nullable=False),
        sa.Column('recipient', sa.String(length=200), nullable=False),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('status', sa.String(length=50), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('dedupe_key', sa.String(length=255), nullable=True),
        sa.Column('telegram_message_id', sa.BigInteger(), nullable=True),
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        *_audit_cols(),
        sa.CheckConstraint(
            "status IN ('PENDING','SENT','FAILED','DROPPED')",
            name='ck_notification_outbox_status',
        ),
        sa.ForeignKeyConstraint(['task_id'], ['operational_tasks.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('uq_notification_outbox_dedupe', 'notification_outbox', ['dedupe_key'], unique=True)
    op.create_index('ix_notification_outbox_status_next_attempt', 'notification_outbox', ['status', 'next_attempt_at'], unique=False)
    op.create_index('ix_notification_outbox_task_id', 'notification_outbox', ['task_id'], unique=False)
    op.create_index('ix_notification_outbox_recipient', 'notification_outbox', ['recipient'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_notification_outbox_recipient', table_name='notification_outbox')
    op.drop_index('ix_notification_outbox_task_id', table_name='notification_outbox')
    op.drop_index('ix_notification_outbox_status_next_attempt', table_name='notification_outbox')
    op.drop_index('uq_notification_outbox_dedupe', table_name='notification_outbox')
    op.drop_table('notification_outbox')

    op.drop_index('ix_recurring_rules_property_id', table_name='recurring_rules')
    op.drop_index('ix_recurring_rules_enabled_next_run', table_name='recurring_rules')
    op.drop_table('recurring_rules')

    op.drop_index('ix_operational_tasks_source_id', table_name='operational_tasks')
    op.drop_index('ix_operational_tasks_lease_id', table_name='operational_tasks')
    op.drop_index('ix_operational_tasks_tenant_id', table_name='operational_tasks')
    op.drop_index('ix_operational_tasks_property_id', table_name='operational_tasks')
    op.drop_index('ix_operational_tasks_assigned_status', table_name='operational_tasks')
    op.drop_index('ix_operational_tasks_status_due_at', table_name='operational_tasks')
    op.drop_index('ix_operational_tasks_due_at', table_name='operational_tasks')
    op.drop_index('ix_operational_tasks_task_type_status', table_name='operational_tasks')
    op.drop_index('uq_operational_tasks_active_dedupe', table_name='operational_tasks')
    op.drop_table('operational_tasks')

    op.drop_index('ix_users_telegram_chat_id', table_name='users')
    op.drop_column('users', 'telegram_chat_id')
