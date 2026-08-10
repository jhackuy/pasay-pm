"""financial idempotency: incomes.idempotency_key + partial unique index

Revision ID: 1f1955f798cb
Revises: 2b4cbce5195f
Create Date: 2026-08-10

PASay V1.1 financial-safety hardening:
- incomes gains a nullable `idempotency_key` column + a partial UNIQUE index
  (only non-NULL keys are constrained; legacy rows stay NULL and untouched).
- This gives create-idempotency a DB-level atomic backstop: concurrent
  creates with the same key can only land once; losers get IntegrityError and
  re-read the winner.

PRE-APPLY DUPLICATE DETECTION (run read-only BEFORE upgrading production;
  0 rows means it is safe to add the UNIQUE index):
    SELECT idempotency_key, count(*) AS n
    FROM incomes
    WHERE idempotency_key IS NOT NULL
    GROUP BY idempotency_key
    HAVING count(*) > 1;

  The column is new and nullable with no backfill, so every pre-existing row
  has NULL and cannot collide; the query above guards against any out-of-band
  writes that populated the column before the index was created.

ROLLBACK:
    alembic downgrade 2b4cbce5195f   (or: alembic downgrade -1)
  (drops the partial unique index and the column; no data is lost).

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '1f1955f798cb'
down_revision: Union[str, None] = '2b4cbce5195f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DETECTION_SQL = """
SELECT idempotency_key, count(*) AS n
FROM incomes
WHERE idempotency_key IS NOT NULL
GROUP BY idempotency_key
HAVING count(*) > 1;
"""


def upgrade() -> None:
    op.add_column(
        'incomes',
        sa.Column('idempotency_key', sa.String(length=128), nullable=True),
    )
    op.create_index(
        'uq_incomes_idempotency_key',
        'incomes',
        ['idempotency_key'],
        unique=True,
        postgresql_where=sa.text('idempotency_key IS NOT NULL'),
    )


def downgrade() -> None:
    op.drop_index('uq_incomes_idempotency_key', table_name='incomes')
    op.drop_column('incomes', 'idempotency_key')
