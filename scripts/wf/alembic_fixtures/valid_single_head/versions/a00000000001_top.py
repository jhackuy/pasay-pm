"""Fixture 1: valid_single_head

Linear chain a -> b -> c with a single head. The happy path.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a00000000001"
down_revision: Union[str, None] = "a00000000002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
