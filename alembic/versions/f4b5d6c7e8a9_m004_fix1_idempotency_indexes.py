"""PASAY-M004-FIX1 — Expense idempotency + move_out index rename.

Revision ID: f4b5d6c7e8a9
Revises: 9a5bc7e31a7f
Create Date: 2026-08-23

UPGRADE SCOPE:
1. ``expenses``: 新增 idempotency_key VARCHAR(128) nullable 列，并创建与
   incomes 表风格一致的 PostgreSQL partial unique index
   ``uq_expenses_idempotency_key`` (WHERE idempotency_key IS NOT NULL)。
2. ``move_out_inspections``: 将 M004 baseline 9a5 创建的 partial unique index
   从旧名 ``uq_move_out_active`` 重命名为更语义化的
   ``uq_move_out_inspections_active_per_lease``（postgresql_where 语义不变：
   status IN ('SCHEDULED','INSPECTED')）。

DOWNGRADE SAFETY (逆序还原):
- 先 DROP ``uq_expenses_idempotency_key`` 索引，再 DROP expenses.idempotency_key 列；
- 再将 move_out_inspections 的索引名从 ``uq_move_out_inspections_active_per_lease``
  回滚为旧名 ``uq_move_out_active``。
"""
from typing import Union

from alembic import op
import sqlalchemy as sa


revision: str = "f4b5d6c7e8a9"
down_revision: Union[str, None] = "9a5bc7e31a7f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. expenses: idempotency_key 列 + partial unique index
    # ------------------------------------------------------------------
    op.add_column(
        "expenses",
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
    )
    op.create_index(
        "uq_expenses_idempotency_key",
        "expenses",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    # ------------------------------------------------------------------
    # 2. move_out_inspections: 重命名 partial unique index
    # ------------------------------------------------------------------
    op.execute(
        "ALTER INDEX uq_move_out_active "
        "RENAME TO uq_move_out_inspections_active_per_lease"
    )


def downgrade() -> None:
    # Pre-downgrade: delete M004-only idempotency rows BEFORE dropping idempotency_key column.
    op.execute(
        "DELETE FROM expenses WHERE idempotency_key LIKE 'deposit_settlement:%'"
    )
    op.execute(
        "DELETE FROM incomes WHERE idempotency_key LIKE 'deposit_settlement:%'"
    )

    # ------------------------------------------------------------------
    # 2 (reverse). move_out_inspections: 还原旧索引名
    # ------------------------------------------------------------------
    op.execute(
        "ALTER INDEX uq_move_out_inspections_active_per_lease "
        "RENAME TO uq_move_out_active"
    )

    # ------------------------------------------------------------------
    # 1 (reverse). expenses: 先 drop index 再 drop column
    # ------------------------------------------------------------------
    op.drop_index("uq_expenses_idempotency_key", table_name="expenses")
    op.drop_column("expenses", "idempotency_key")
