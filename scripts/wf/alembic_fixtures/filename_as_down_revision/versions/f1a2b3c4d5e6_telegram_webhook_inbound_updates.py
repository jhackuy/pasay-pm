"""Fixture 2: filename_as_down_revision / f1a2b3c4d5e6_telegram_webhook_inbound_updates.py

The real, valid migration — note: filename is "f1a2b3c4d5e6_telegram_webhook_inbound_updates"
but revision is "f1a2b3c4d5e6". The buggy sibling above mistakenly uses the
filename stem as the `down_revision`.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
