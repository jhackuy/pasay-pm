"""PASAY-M004-FIX2 — MoveOutInspection notes 列 + Evidence soft-delete 列.

Revision ID: a1b2c3d4e5f0
Revises: f4b5d6c7e8a9
Create Date: 2026-08-23

UPGRADE SCOPE:
1. ``move_out_inspections``: 新增 notes TEXT nullable 列。
2. ``evidence``: 新增 deleted_at TIMESTAMPTZ nullable 列（对齐 SoftDeleteMixin + gate 过滤 soft-deleted）。

DOWNGRADE SAFETY (逆序还原):
- DROP move_out_inspections.notes 列。
- DROP evidence.deleted_at 列。
"""
from typing import Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1b2c3d4e5f0"
down_revision: Union[str, None] = "f4b5d6c7e8a9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "move_out_inspections",
        sa.Column("notes", sa.Text(), nullable=True),
    )
    op.add_column(
        "evidence",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("evidence", "deleted_at")
    op.drop_column("move_out_inspections", "notes")
