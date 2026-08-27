"""Fixture 2: filename_as_down_revision (Issue #65 regression case).

Reproduces the exact bug from M006: the developer wrote
`down_revision = "f1a2b3c4d5e6_telegram_webhook_inbound_updates"` — the
**filename stem** of an existing migration rather than its real revision id
`f1a2b3c4d5e6`.

Expected gate verdict: FAIL (dangling predecessor; alembic itself raises
KeyError on graph traversal).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f00000000001"
down_revision: Union[str, None] = "f1a2b3c4d5e6_telegram_webhook_inbound_updates"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
