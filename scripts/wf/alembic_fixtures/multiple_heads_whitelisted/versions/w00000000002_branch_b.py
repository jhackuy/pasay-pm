"""Fixture 6: multiple_heads_whitelisted / w00000000002_branch_b.py

Branch B; whitelisted as additional head via test allow-list file.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "w00000000002"
down_revision: Union[str, None] = "w00000000003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
