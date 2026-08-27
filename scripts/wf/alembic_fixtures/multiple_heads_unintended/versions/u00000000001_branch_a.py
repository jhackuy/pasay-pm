"""Fixture 5: multiple_heads_unintended / u00000000001_branch_a.py

Branch A: diverges from u00000000003_base without an explicit allow-list entry.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "u00000000001"
down_revision: Union[str, None] = "u00000000003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
