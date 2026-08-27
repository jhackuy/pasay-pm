"""Fixture 4: duplicate_revision / d00000000001_dup_again.py

Same revision as `d00000000001_dup.py` — defines the SAME `revision` literal,
creating a duplicate-revision collision. Gate must FAIL.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d00000000001"
down_revision: Union[str, None] = "d00000000002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
