"""Fixture 7: merge_predecessor_tuple / mg00000000001_branch_a.py

Branch A head.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "mg00000000001"
down_revision: Union[str, None] = "mg00000000004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
