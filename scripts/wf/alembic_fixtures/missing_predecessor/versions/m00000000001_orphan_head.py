"""Fixture 3: missing_predecessor / m00000000001_orphan_head.py

`down_revision` references `m_does_not_exist_xxx` which is NOT a revision in
the repository (not even a filename stem; just a fabricated id).

Expected gate verdict: FAIL (dangling predecessor).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "m00000000001"
down_revision: Union[str, None] = "m_does_not_exist_xxx"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
