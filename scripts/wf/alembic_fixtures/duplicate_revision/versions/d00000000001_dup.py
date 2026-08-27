"""Fixture 4: duplicate_revision / d00000000001_dup.py

Same `revision` id assigned in two different files (d00000000001_dup.py and
d00000000001_dup_again.py). Alembic would not detect this at load time but
it MUST be a graph error — the same revision cannot exist twice.

Expected gate verdict: FAIL (duplicate revision id).
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
