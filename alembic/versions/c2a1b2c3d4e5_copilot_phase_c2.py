"""V1.2.2 Phase C2 — CONFIRMED-action copilot (FOLLOWUP task type + action code)

Revision ID: c2a1b2c3d4e5
Revises: ab1a2b3c4d5e
Create Date: 2026-08-11

Changes (CHECK-constraint allowlists kept in sync with the models):
1. ``operational_tasks`` / ``recurring_rules``: add the ``FOLLOWUP`` task type
   to ``ck_operational_tasks_task_type`` / ``ck_recurring_rules_rule_type`` so
   human-confirmed copilot follow-up tasks (text/tracking only) can be stored.
2. ``copilot_action_proposals``: add ``create_followup_task`` to
   ``ck_copilot_action_proposals_action_type`` (the canonical EXECUTABLE
   follow-up code; the executor allowlist is exactly
   create_followup_task / assign_task / snooze_task).

ROLLBACK:
    alembic downgrade ab1a2b3c4d5e
  (restores the previous CHECK allowlists; no data is touched.)
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'c2a1b2c3d4e5'
down_revision: Union[str, None] = 'ab1a2b3c4d5e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TASK_TYPES = (
    "'RENT_DUE','RENT_OVERDUE','LEASE_EXPIRING','PROPERTY_FEE_DUE',"
    "'AC_MAINTENANCE','APPROVAL_PENDING','PAYMENT_PENDING',"
    "'SETTLEMENT_PENDING','FOLLOWUP'"
)
_LEGACY_TASK_TYPES = (
    "'RENT_DUE','RENT_OVERDUE','LEASE_EXPIRING','PROPERTY_FEE_DUE',"
    "'AC_MAINTENANCE','APPROVAL_PENDING','PAYMENT_PENDING','SETTLEMENT_PENDING'"
)
_ACTION_TYPES = (
    "'summarize','analyze','explain','risk_scan',"
    "'create_task','assign_task','snooze_task','follow_up',"
    "'create_followup_task'"
)
_LEGACY_ACTION_TYPES = (
    "'summarize','analyze','explain','risk_scan',"
    "'create_task','assign_task','snooze_task','follow_up'"
)


def upgrade() -> None:
    op.drop_constraint('ck_operational_tasks_task_type', 'operational_tasks', type_='check')
    op.create_check_constraint(
        'ck_operational_tasks_task_type',
        'operational_tasks',
        f"task_type IN ({_TASK_TYPES})",
    )
    op.drop_constraint('ck_recurring_rules_rule_type', 'recurring_rules', type_='check')
    op.create_check_constraint(
        'ck_recurring_rules_rule_type',
        'recurring_rules',
        f"rule_type IN ({_TASK_TYPES})",
    )
    op.drop_constraint(
        'ck_copilot_action_proposals_action_type',
        'copilot_action_proposals',
        type_='check',
    )
    op.create_check_constraint(
        'ck_copilot_action_proposals_action_type',
        'copilot_action_proposals',
        f"action_type IN ({_ACTION_TYPES})",
    )


def downgrade() -> None:
    op.drop_constraint(
        'ck_copilot_action_proposals_action_type',
        'copilot_action_proposals',
        type_='check',
    )
    op.create_check_constraint(
        'ck_copilot_action_proposals_action_type',
        'copilot_action_proposals',
        f"action_type IN ({_LEGACY_ACTION_TYPES})",
    )
    op.drop_constraint('ck_recurring_rules_rule_type', 'recurring_rules', type_='check')
    op.create_check_constraint(
        'ck_recurring_rules_rule_type',
        'recurring_rules',
        f"rule_type IN ({_LEGACY_TASK_TYPES})",
    )
    op.drop_constraint('ck_operational_tasks_task_type', 'operational_tasks', type_='check')
    op.create_check_constraint(
        'ck_operational_tasks_task_type',
        'operational_tasks',
        f"task_type IN ({_LEGACY_TASK_TYPES})",
    )
