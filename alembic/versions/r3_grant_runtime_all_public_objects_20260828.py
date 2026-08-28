"""r3-grant-runtime-all-public-objects-20260828 (M006 RETURN3-E STEP9 blocker)

Run 33156321120 STEP3 failed because original r3 used PL/pgSQL DO blocks
with RAISE NOTICE for evidence capture; those caused a PostgreSQL runtime
error at migration gate.  This revision keeps ONLY the minimal pure SQL
GRANT / ALTER DEFAULT / REVOKE statements (all naturally idempotent).

Root cause:
  1. r2 explicitly GRANTed CRUD on 10 tables only (9 bs_* +
     telegram_webhook_updates).  All pre-existing public business tables
     (users, memberships, leases, expenses, properties, units,
     operational_tasks, notifications, scheduled_jobs, ...) retained ZERO
     runtime TABLE privileges because pasay-deploy-phase1.yml STEP0 only
     does `GRANT ... ON ALL SEQUENCES`, never `ON ALL TABLES`.
  2. STEP0 grants SEQUENCES BEFORE alembic runs, so any sequence created
     by a migration was not covered unless an ALTER DEFAULT PRIVILEGES
     for SEQUENCES existed (r2 only set it for TABLES).

Fix — pure idempotent SQL only:
  * GRANT USAGE ON SCHEMA public TO pasay_runtime
  * GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public
  * GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public
  * ALTER DEFAULT PRIVILEGES FOR ROLE neondb_owner IN SCHEMA public:
        TABLES    → SELECT,INSERT,UPDATE,DELETE to pasay_runtime
        SEQUENCES → USAGE,SELECT to pasay_runtime
        FUNCTIONS → EXECUTE to pasay_runtime

Symmetric downgrade REVOKEs all four categories in reverse order.
"""
from alembic import op
import sqlalchemy as sa


revision = "r3_grant_runtime_all_public_objects_20260828"
down_revision = "r2_grant_runtime_tables_20260828"
branch_labels = None
depends_on = None


_RUNTIME_ROLE = "pasay_runtime"
_CREATOR_ROLE = "neondb_owner"
_SCHEMA = "public"


def upgrade() -> None:
    op.execute(f"GRANT USAGE ON SCHEMA {_SCHEMA} TO {_RUNTIME_ROLE};")

    op.execute(
        f"""
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON ALL TABLES IN SCHEMA {_SCHEMA}
            TO {_RUNTIME_ROLE};
        """
    )

    op.execute(
        f"""
        GRANT USAGE, SELECT
            ON ALL SEQUENCES IN SCHEMA {_SCHEMA}
            TO {_RUNTIME_ROLE};
        """
    )

    op.execute(
        f"""
        ALTER DEFAULT PRIVILEGES FOR ROLE {_CREATOR_ROLE}
            IN SCHEMA {_SCHEMA}
            GRANT SELECT, INSERT, UPDATE, DELETE
            ON TABLES
            TO {_RUNTIME_ROLE};
        """
    )
    op.execute(
        f"""
        ALTER DEFAULT PRIVILEGES FOR ROLE {_CREATOR_ROLE}
            IN SCHEMA {_SCHEMA}
            GRANT USAGE, SELECT
            ON SEQUENCES
            TO {_RUNTIME_ROLE};
        """
    )
    op.execute(
        f"""
        ALTER DEFAULT PRIVILEGES FOR ROLE {_CREATOR_ROLE}
            IN SCHEMA {_SCHEMA}
            GRANT EXECUTE
            ON FUNCTIONS
            TO {_RUNTIME_ROLE};
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        ALTER DEFAULT PRIVILEGES FOR ROLE {_CREATOR_ROLE}
            IN SCHEMA {_SCHEMA}
            REVOKE EXECUTE
            ON FUNCTIONS
            FROM {_RUNTIME_ROLE};
        """
    )
    op.execute(
        f"""
        ALTER DEFAULT PRIVILEGES FOR ROLE {_CREATOR_ROLE}
            IN SCHEMA {_SCHEMA}
            REVOKE USAGE, SELECT
            ON SEQUENCES
            FROM {_RUNTIME_ROLE};
        """
    )
    op.execute(
        f"""
        ALTER DEFAULT PRIVILEGES FOR ROLE {_CREATOR_ROLE}
            IN SCHEMA {_SCHEMA}
            REVOKE SELECT, INSERT, UPDATE, DELETE
            ON TABLES
            FROM {_RUNTIME_ROLE};
        """
    )

    op.execute(
        f"""
        REVOKE USAGE, SELECT
            ON ALL SEQUENCES IN SCHEMA {_SCHEMA}
            FROM {_RUNTIME_ROLE};
        """
    )

    op.execute(
        f"""
        REVOKE SELECT, INSERT, UPDATE, DELETE
            ON ALL TABLES IN SCHEMA {_SCHEMA}
            FROM {_RUNTIME_ROLE};
        """
    )

    op.execute(f"REVOKE USAGE ON SCHEMA {_SCHEMA} FROM {_RUNTIME_ROLE};")
