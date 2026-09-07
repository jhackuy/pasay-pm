"""Add principal_type + purpose to v1_api_credentials (Issue #119 P0).

Issue #119 v1_api_credential_bootstrap: the V1 clean rewrite never had
a SYSTEM principal — every credential was implicitly tied to a HUMAN
membership via v1_memberships (Role.OWNER / Role.SECRETARY only). The
scheduled-job contract (JOB-SERVICE-AUTH-002) still requires a SYSTEM
credential that authenticates WITHOUT binding a Telegram id and WITHOUT
resolving to a HUMAN user.

This migration is the minimum additive change to support that
contract while keeping the clean-rewrite V1 auth model intact:

  * ``v1_api_credentials.principal_type VARCHAR(16) NOT NULL DEFAULT 'HUMAN'``
    with a CHECK constraint ``IN ('HUMAN','SYSTEM')``. Existing rows
    backfill to ``'HUMAN'`` (the default), so every previously-issued
    bearer keeps its current behavior.
  * ``v1_api_credentials.purpose VARCHAR(64) NULL``. ``NULL`` for the
    interactive manager / secretary / admin keys; ``'internal:scheduler'``
    for the scheduled-job credential. The dep layer also constrains the
    accepted SYSTEM purposes (V1_SYSTEM_PURPOSES in
    ``app.v1.models.base``), so adding more SYSTEM purposes requires an
    explicit code change in addition to a row insert.
  * ``(principal_type, purpose)`` compound index for the SYSTEM-credential
    hot path.

Round-trip safety:
  * upgrade() / downgrade() are exact inverses. ``downgrade`` drops the
    index, the check constraint, then both columns. Existing rows must
    still validate after downgrade because no column is added with a
    non-HUMAN default that would orphan anything.
  * No data is moved, copied, or backfilled outside the column default.
  * ``V1Base.metadata`` picks the new columns up on its own; no ORM-side
    schema sync is needed for the migration to be applied.

NOT touched (out of Issue #119 P0 scope):
  * No legacy ``users`` / ``principals`` / ``api_credentials`` tables.
  * No bot env contract, no Worker forwarding.
  * No business rules, no Telegram runtime, no Mini App.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0005_v1_api_cred_system"
down_revision = "0004_legacy_telegram_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) Add principal_type with a server-side default so the ALTER TABLE
    #    on a populated table backfills every existing row to 'HUMAN'.
    op.add_column(
        "v1_api_credentials",
        sa.Column(
            "principal_type",
            sa.String(length=16),
            nullable=False,
            server_default="HUMAN",
        ),
    )
    # 2) purpose is NULLable: only SYSTEM credentials carry one; HUMAN
    #    credentials deliberately leave it NULL.
    op.add_column(
        "v1_api_credentials",
        sa.Column("purpose", sa.String(length=64), nullable=True),
    )
    # 3) CHECK constraint — fail closed if a future migration tries to
    #    insert a value outside the {HUMAN, SYSTEM} allow-list.
    op.create_check_constraint(
        "ck_v1_api_credentials_principal_type",
        "v1_api_credentials",
        "principal_type IN ('HUMAN','SYSTEM')",
    )
    # 4) Compound index for the SYSTEM-credential lookup hot path
    #    (only SystemPrincipal-authenticated readers scan this index;
    #    HUMAN auth uses the existing key_hash UNIQUE index).
    op.create_index(
        "ix_v1_api_credentials_principal_type_purpose",
        "v1_api_credentials",
        ["principal_type", "purpose"],
    )


def downgrade() -> None:
    # Inverted order: drop the index, then the check, then the columns.
    op.drop_index(
        "ix_v1_api_credentials_principal_type_purpose",
        table_name="v1_api_credentials",
    )
    op.drop_constraint(
        "ck_v1_api_credentials_principal_type",
        "v1_api_credentials",
        type_="check",
    )
    op.drop_column("v1_api_credentials", "purpose")
    op.drop_column("v1_api_credentials", "principal_type")
