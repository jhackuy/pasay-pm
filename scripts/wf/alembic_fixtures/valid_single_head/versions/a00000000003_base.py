"""Fixture 1: valid_single_head / a00000000003_base.py"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a00000000003"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
