"""Fixture 5: multiple_heads_unintended / u00000000002_branch_b.py

Branch B: also diverges from u00000000003_base, creating an UNINTENDED
multi-head graph. Without an allow-list entry for u00000000002, the gate must
FAIL.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "u00000000002"
down_revision: Union[str, None] = "u00000000003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
