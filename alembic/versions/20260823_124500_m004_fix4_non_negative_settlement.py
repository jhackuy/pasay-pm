"""PASAY-M004-FIX4 — DepositSettlement 非负金额 CheckConstraint.

Revision ID: m4b000000001
Revises: a1b2c3d4e5f0
Create Date: 2026-08-23

UPGRADE SCOPE:
1. ``deposit_settlements``: ck_deposit_received_non_negative (deposit_received >= 0)
2. ``deposit_settlements``: ck_total_deductions_non_negative (total_deductions >= 0)
3. ``deposit_settlements``: ck_refund_amount_non_negative (refund_amount >= 0)

DOWNGRADE SAFETY (逆序还原):
- DROP ck_refund_amount_non_negative
- DROP ck_total_deductions_non_negative
- DROP ck_deposit_received_non_negative
"""
from typing import Union

from alembic import op
import sqlalchemy as sa


revision: str = "m4b000000001"
down_revision: Union[str, None] = "a1b2c3d4e5f0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_deposit_settlements_deposit_received_non_negative",
        "deposit_settlements",
        "deposit_received >= 0",
    )
    op.create_check_constraint(
        "ck_deposit_settlements_total_deductions_non_negative",
        "deposit_settlements",
        "total_deductions >= 0",
    )
    op.create_check_constraint(
        "ck_deposit_settlements_refund_amount_non_negative",
        "deposit_settlements",
        "refund_amount >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_deposit_settlements_refund_amount_non_negative",
        "deposit_settlements",
        type_="check",
    )
    op.drop_constraint(
        "ck_deposit_settlements_total_deductions_non_negative",
        "deposit_settlements",
        type_="check",
    )
    op.drop_constraint(
        "ck_deposit_settlements_deposit_received_non_negative",
        "deposit_settlements",
        type_="check",
    )
