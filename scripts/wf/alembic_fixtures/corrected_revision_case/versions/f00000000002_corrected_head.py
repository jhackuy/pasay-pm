"""Fixture: corrected_revision_case / f00000000002_corrected_head.py

Counterpart of filename_as_down_revision: this migration uses the **real**
revision id `f1a2b3c4d5e6` as its `down_revision` (NOT the filename stem).

Expected gate verdict: PASS — graph is valid, single head, reachable base.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f00000000002"
down_revision: Union[str, None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
