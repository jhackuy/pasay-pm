"""leases: accounting_start_date (nullable, no backfill, no server_default)

Revision ID: 2b4cbce5195f
Revises: d7e5c461d569
Create Date: 2026-08-10 16:32:01.402103

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '2b4cbce5195f'
down_revision: Union[str, None] = 'd7e5c461d569'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'leases',
        sa.Column('accounting_start_date', sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('leases', 'accounting_start_date')
