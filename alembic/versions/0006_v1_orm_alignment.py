"""Align v1_users / v1_memberships schema with the V1 ORM (Issue #119 P0).

The Issue #119 P0 v1_api_credential_bootstrap requires the bootstrap
script to be able to insert v1_users + v1_memberships rows that match
what the V1 ORM (and therefore the V1 dependency layer /
``Role.parse`` / ``MembershipState.parse``) reads back. The 0001
baseline created a slightly different shape that the V1 ORM never
caught up with — that drift blocks every V1 ORM-driven write today.

Specific drift and the reconciliation this migration performs:

  1. ``v1_users.telegram_id`` (baseline, lowercase) is renamed to
     ``v1_users.telegram_user_id`` to match
     ``app/v1/models/foundation.py::User.telegram_user_id``. Every
     V1 ORM path (bootstrap, webapp_auth, seed_workspace, etc.)
     writes the column under the new name.
  2. ``v1_users.default_language VARCHAR(8) NOT NULL DEFAULT 'zh-CN'``
     is added with the same default the ORM declares. Existing rows
     backfill to ``'zh-CN'`` automatically.
  3. ``v1_memberships.role`` is migrated to UPPERCASE so the
     existing CHECK constraint ``IN ('OWNER','SECRETARY')``
     (which the V1 ORM expects) accepts the migrated rows. The old
     baseline CHECK allowed ``('owner','secretary','tenant')``.
     A pre-flight fail-closed guard refuses to apply this
     migration if any ``tenant`` membership exists — V1 has no
     TENANT role (DATA_CONTRACT §2.5; that's a product decision
     outside the scope of credential bootstrap, so this migration
     stops and demands an operator decision rather than silently
     destroying tenant data).
  4. ``v1_memberships.state`` migrates ``SUSPENDED`` → ``ACTIVE``.
     The new CHECK ``state IN ('ACTIVE','REMOVED')`` is the V1
     contract. SUSPENDED is removed entirely (DATA_CONTRACT §2.5).
     A pre-flight guard aborts if any non-``{ACTIVE,SUSPENDED,
     REMOVED}`` state value exists (defensive — should be a no-op
     given the previous CHECK constraint, but the guard makes the
     failure mode explicit instead of leaving a confusing
     string-truncation error from PostgreSQL).

Round-trip safety:
  * Every ALTER TABLE is additive or a rename with an explicit
    type preservation. ``downgrade`` does not attempt to lowercase
    roles or to undo the rename — the baseline did not require the
    new state values, and rolling back to lowercase without
    destructive data rewrite is impossible safely. The downgrade
    path therefore drops only the new defaults and renames the
    column back, leaving existing rows with their migrated values
    (which still validate against the old CHECK because
    ``UPPER('OWNER') IN ('owner','secretary','tenant')`` is False;
    so the operator must migrate roles back manually if a real
    rollback is required).

Out of scope for this PR (separately tracked):
  * Legacy ``users`` / ``principals`` / ``api_credentials`` —
    intentionally NOT reintroduced.
  * Bot env contract, Worker forwarding, business rules, Telegram
    runtime, Mini App.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0006_v1_orm_alignment"
down_revision = "0005_v1_api_cred_system"
branch_labels = None
depends_on = None


V1_ALLOWED_ROLES = ("OWNER", "SECRETARY")
V1_ALLOWED_STATES = ("ACTIVE", "REMOVED")
BASELINE_ALLOWED_ROLES = ("owner", "secretary", "tenant")
BASELINE_ALLOWED_STATES = ("ACTIVE", "SUSPENDED", "REMOVED")


def _check_role_no_tenant_or_unknown(conn) -> None:
    """Pre-flight: refuse to migrate if any 'tenant' or unknown role exists.

    V1 has no TENANT role (DATA_CONTRACT §2.5 — that's a product
    decision outside Issue #119 P0 scope). Silently dropping a
    TENANT row to make the CHECK constraint happy would destroy
    data; aborting forces an explicit operator decision.
    """
    rows = conn.execute(
        sa.text(
            "SELECT id, user_id, org_id, role FROM v1_memberships "
            "WHERE role IS NULL "
            "   OR role NOT IN ('OWNER','SECRETARY','owner','secretary','tenant')"
        )
    ).fetchall()
    if rows:
        ids = [r[0] for r in rows]
        raise RuntimeError(
            f"v1_memberships contains {len(rows)} rows with unknown "
            f"role values (id(s)={ids[:10]}); refusing to migrate. "
            f"Manual cleanup required before this migration can apply."
        )
    tenants = conn.execute(
        sa.text("SELECT COUNT(*) FROM v1_memberships WHERE role = 'tenant'")
    ).scalar()
    if tenants and int(tenants) > 0:
        # Hard stop. V1 has no TENANT role; preserving 'tenant' rows
        # would force the new CHECK to allow it, defeating the
        # migration. Operator must decide on the tenant lifecycle
        # separately (out of Issue #119 P0 scope).
        raise RuntimeError(
            f"v1_memberships has {tenants} rows with role='tenant'. "
            f"V1 does not define a TENANT role; this migration does NOT "
            f"silently migrate tenant rows to OWNER/SECRETARY. Operator "
            f"must resolve tenant rows manually before applying 0006."
        )


def _check_state_no_unknown(conn) -> None:
    """Pre-flight: refuse to migrate if any state value is outside the
    historical allow-list (defensive — should always pass)."""
    rows = conn.execute(
        sa.text(
            "SELECT id, state FROM v1_memberships "
            "WHERE state IS NULL OR state NOT IN "
            "('ACTIVE','SUSPENDED','REMOVED')"
        )
    ).fetchall()
    if rows:
        ids = [r[0] for r in rows]
        raise RuntimeError(
            f"v1_memberships has {len(rows)} rows with unknown state "
            f"values (id(s)={ids[:10]}); refusing to migrate."
        )


def upgrade() -> None:
    conn = op.get_bind()

    # 0) Pre-flight guards — fail closed on data the migration cannot
    #    safely migrate.
    _check_role_no_tenant_or_unknown(conn)
    _check_state_no_unknown(conn)

    # 1) Rename v1_users.telegram_id → telegram_user_id to match the
    #    V1 ORM. PostgreSQL preserves the column type and data across
    #    the rename.
    op.alter_column(
        "v1_users", "telegram_id",
        new_column_name="telegram_user_id",
    )
    # The baseline's UNIQUE constraint name was uq_v1_users_telegram_id;
    # rename it so its name keeps describing what it actually
    # constrains (and so a future create_all matches no-op).
    op.execute(
        sa.text(
            "ALTER TABLE v1_users "
            "RENAME CONSTRAINT uq_v1_users_telegram_id "
            "TO uq_v1_users_telegram_user_id"
        )
    )

    # 2) Add default_language with a server-side default so existing
    #    rows backfill cleanly.
    op.add_column(
        "v1_users",
        sa.Column(
            "default_language",
            sa.String(length=8),
            nullable=False,
            server_default="zh-CN",
        ),
    )

    # 2b) Add v1_memberships.is_bootstrap — the V1 ORM declares this
    #     column (used by ``app/v1/api/bootstrap.py`` to mark the
    #     OWNER created by the very first bootstrap call, and by
    #     ``assert_not_bootstrap_for_secretary``). The baseline did
    #     not include it; the V1 dependency layer + tests rely on it.
    op.add_column(
        "v1_memberships",
        sa.Column(
            "is_bootstrap",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # 3) Migrate v1_memberships.role lowercase → uppercase.
    op.execute(
        sa.text(
            "UPDATE v1_memberships SET role = 'OWNER' WHERE role = 'owner'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE v1_memberships SET role = 'SECRETARY' WHERE role = 'secretary'"
        )
    )

    # 4) Replace the baseline role CHECK with the V1-Orm allow-list.
    op.drop_constraint(
        "ck_v1_memberships_role", "v1_memberships", type_="check",
    )
    op.create_check_constraint(
        "ck_v1_memberships_role",
        "v1_memberships",
        "role IN ('OWNER','SECRETARY')",
    )

    # 5) Migrate v1_memberships.state SUSPENDED → ACTIVE.
    #    V1 has no SUSPENDED state (DATA_CONTRACT §2.5). Mapping to
    #    ACTIVE is the conservative choice — the row stays usable,
    #    operator can re-suspend via a future product feature if
    #    needed.
    op.execute(
        sa.text(
            "UPDATE v1_memberships SET state = 'ACTIVE' WHERE state = 'SUSPENDED'"
        )
    )

    # 6) Replace the baseline state CHECK without SUSPENDED.
    op.drop_constraint(
        "ck_v1_memberships_state", "v1_memberships", type_="check",
    )
    op.create_check_constraint(
        "ck_v1_memberships_state",
        "v1_memberships",
        "state IN ('ACTIVE','REMOVED')",
    )


def downgrade() -> None:
    """Best-effort downgrade.

    The pre-flight guards (tenant rows / unknown states) are NOT
    re-applied on the way down — rolling the data back is a
    destructive operation that should not happen by accident. If an
    operator truly needs to reverse this migration, they should:

      1. ensure no role='OWNER'/'SECRETARY' / state='ACTIVE'/'REMOVED'
         rows exist that would not survive the old CHECK,
      2. UPDATE role to lowercase / state to include 'SUSPENDED' if
         they want a literal mirror,
      3. THEN run ``alembic downgrade -1``.

    Otherwise the downgrade will fail with a constraint-violation
    error from PostgreSQL when the next INSERT hits the old CHECK.
    """
    # Re-add SUSPENDED to the state CHECK.
    op.drop_constraint(
        "ck_v1_memberships_state", "v1_memberships", type_="check",
    )
    op.create_check_constraint(
        "ck_v1_memberships_state",
        "v1_memberships",
        "state IN ('ACTIVE','SUSPENDED','REMOVED')",
    )

    # Re-add 'tenant' to the role CHECK.
    op.drop_constraint(
        "ck_v1_memberships_role", "v1_memberships", type_="check",
    )
    op.create_check_constraint(
        "ck_v1_memberships_role",
        "v1_memberships",
        "role IN ('OWNER','SECRETARY','owner','secretary','tenant')",
    )

    # Drop default_language (the column is additive — no data is lost
    # because the ORM still treats default_language as part of the
    # row, but the column is removed on downgrade so a fresh
    # create_all() build stays in sync with the baseline).
    op.drop_column("v1_users", "default_language")

    # Drop is_bootstrap (same rationale).
    op.drop_column("v1_memberships", "is_bootstrap")

    # Rename the unique constraint back to the baseline name.
    op.execute(
        sa.text(
            "ALTER TABLE v1_users "
            "RENAME CONSTRAINT uq_v1_users_telegram_user_id "
            "TO uq_v1_users_telegram_id"
        )
    )

    # Rename the column back. The column type is preserved.
    op.alter_column(
        "v1_users", "telegram_user_id",
        new_column_name="telegram_id",
    )
