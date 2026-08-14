"""PASAY-V2-FOUNDATION-001: V2 task core fields.

Revision ID: 9a8b7c6d5e4f
Revises: e3b4c5d6e7f8

Adds:
- ``operational_tasks.status`` enum value ``IN_PROGRESS`` (CHECK constraint
  updated; VARCHAR storage means no ALTER TYPE is needed).
- ``operational_tasks.next_action`` (String(300), nullable)
- ``operational_tasks.next_check_at`` (DateTime tz, nullable, indexed)
- ``operational_tasks.context`` (Text, nullable)
- ``operational_tasks.completion_condition`` (String(300), nullable)
- ``operational_tasks.source_event`` (String(500), nullable)

All new columns are nullable so scheduler-generated and legacy rows remain
fully back-compatible.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "9a8b7c6d5e4f"
down_revision: Union[str, None] = "e3b4c5d6e7f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "operational_tasks",
        sa.Column("next_action", sa.String(length=300), nullable=True),
    )
    op.add_column(
        "operational_tasks",
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "operational_tasks",
        sa.Column("context", sa.Text(), nullable=True),
    )
    op.add_column(
        "operational_tasks",
        sa.Column("completion_condition", sa.String(length=300), nullable=True),
    )
    op.add_column(
        "operational_tasks",
        sa.Column("source_event", sa.String(length=500), nullable=True),
    )
    op.create_index(
        "ix_operational_tasks_next_check_at",
        "operational_tasks",
        ["next_check_at"],
        unique=False,
    )
    op.drop_constraint(
        "ck_operational_tasks_status", "operational_tasks", type_="check"
    )
    op.create_check_constraint(
        "ck_operational_tasks_status",
        "operational_tasks",
        "status IN ('PENDING','IN_PROGRESS','COMPLETED','CANCELLED')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_operational_tasks_status", "operational_tasks", type_="check"
    )
    op.create_check_constraint(
        "ck_operational_tasks_status",
        "operational_tasks",
        "status IN ('PENDING','COMPLETED','CANCELLED')",
    )
    op.drop_index("ix_operational_tasks_next_check_at", table_name="operational_tasks")
    op.drop_column("operational_tasks", "source_event")
    op.drop_column("operational_tasks", "completion_condition")
    op.drop_column("operational_tasks", "context")
    op.drop_column("operational_tasks", "next_check_at")
    op.drop_column("operational_tasks", "next_action")
