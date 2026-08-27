"""Fixture 7: merge_predecessor_tuple / mg00000000003_merge_head.py

The MERGE migration: `down_revision` is a tuple of both branch heads
(mg00000000001, mg00000000002). This is the legitimate merge/branch use case.

Expected gate verdict: PASS (with allow-list covering mg00000000001 and
mg00000000002 — but for the merge case, after this file is added the graph
collapses to single head mg00000000003, so no allow-list is needed).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "mg00000000003"
down_revision: Union[str, Sequence[str], None] = ("mg00000000001", "mg00000000002")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
