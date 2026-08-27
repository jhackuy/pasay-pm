"""Fixture 4: duplicate_revision / d00000000002_base.py"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d00000000002"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
