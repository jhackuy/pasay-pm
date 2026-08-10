"""phase 2: tasks recurring / leases due_day / expenses due_date+unit_id

Revision ID: d7e5c461d569
Revises: 0f9a2e554ec6
Create Date: 2026-08-10 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd7e5c461d569'
down_revision: Union[str, None] = '0f9a2e554ec6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # tasks: recurring maintenance / todo fields
    op.add_column('tasks', sa.Column('recurring', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.add_column('tasks', sa.Column('interval_months', sa.Integer(), nullable=True))
    op.add_column('tasks', sa.Column('assigned_to', sa.BigInteger(), nullable=True))
    op.add_column('tasks', sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('tasks', sa.Column('last_completed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('tasks', sa.Column('next_due_date', sa.Date(), nullable=True))
    op.create_index(op.f('ix_tasks_assigned_to'), 'tasks', ['assigned_to'], unique=False)
    op.create_foreign_key('fk_tasks_assigned_to_users', 'tasks', 'users', ['assigned_to'], ['id'])

    # leases: rent due day
    op.add_column('leases', sa.Column('due_day', sa.Integer(), nullable=True))

    # expenses: bill due date + optional unit link
    op.add_column('expenses', sa.Column('due_date', sa.Date(), nullable=True))
    op.add_column('expenses', sa.Column('unit_id', sa.BigInteger(), nullable=True))
    op.create_index(op.f('ix_expenses_unit_id'), 'expenses', ['unit_id'], unique=False)
    op.create_foreign_key('fk_expenses_unit_id_units', 'expenses', 'units', ['unit_id'], ['id'])


def downgrade() -> None:
    op.drop_constraint('fk_expenses_unit_id_units', 'expenses', type_='foreignkey')
    op.drop_index(op.f('ix_expenses_unit_id'), table_name='expenses')
    op.drop_column('expenses', 'unit_id')
    op.drop_column('expenses', 'due_date')

    op.drop_column('leases', 'due_day')

    op.drop_constraint('fk_tasks_assigned_to_users', 'tasks', type_='foreignkey')
    op.drop_index(op.f('ix_tasks_assigned_to'), table_name='tasks')
    op.drop_column('tasks', 'next_due_date')
    op.drop_column('tasks', 'last_completed_at')
    op.drop_column('tasks', 'completed_at')
    op.drop_column('tasks', 'assigned_to')
    op.drop_column('tasks', 'interval_months')
    op.drop_column('tasks', 'recurring')
