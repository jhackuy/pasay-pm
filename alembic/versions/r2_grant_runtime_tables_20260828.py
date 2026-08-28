"""r2-grant-runtime-tables-20260828 (M006 RETURN3-E STEP9 blocker)

Production runtime role pasay_runtime had SEQUENCE grants only (from STEP0
GRANT path in pasay-deploy-phase1.yml) but lacked TABLE privileges for the
nine bs_* StateStore tables introduced in ret1_postgres_bot_state_20260828,
as well as explicit write grants on telegram_webhook_updates required by
the PTB handler to advance state from claimed/retryable -> done and to
set processed_at / last_error columns.

Without these grants, the STEP9 synthetic /start Worker->Queue->Container
path repeatedly raised InsufficientPrivilege (SQLSTATE 42501) on the first
INSERT or UPDATE against bs_* / telegram_webhook_updates, exhausting
delivery retries and leaving the event stuck in state=retryable (never
reaching processing=done).

This migration is idempotent: GRANT statements are safe to re-apply.
"""
from alembic import op
import sqlalchemy as sa


revision = "r2_grant_runtime_tables_20260828"
down_revision = "ret1_postgres_bot_state_20260828"
branch_labels = None
depends_on = None


_AFFECTED_BS_TABLES = (
    "bs_conversations",
    "bs_idempotency_keys",
    "bs_user_defaults",
    "bs_rent_status_selectors",
    "bs_v2_context",
    "bs_known_groups",
    "bs_daily_marks",
    "bs_reminder_deliveries",
    "bs_followup_deliveries",
)


def upgrade() -> None:
    op.execute(
        """
        GRANT USAGE ON SCHEMA public TO pasay_runtime;
        """
    )

    for tbl in _AFFECTED_BS_TABLES:
        op.execute(
            f"""
            GRANT SELECT, INSERT, UPDATE, DELETE
            ON TABLE {tbl}
            TO pasay_runtime;
            """
        )

    op.execute(
        """
        GRANT SELECT, INSERT, UPDATE, DELETE
        ON TABLE telegram_webhook_updates
        TO pasay_runtime;
        """
    )

    op.execute(
        """
        ALTER DEFAULT PRIVILEGES FOR ROLE neondb_owner
            IN SCHEMA public
            GRANT SELECT, INSERT, UPDATE, DELETE
            ON TABLES
            TO pasay_runtime;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES FOR ROLE neondb_owner
            IN SCHEMA public
            REVOKE SELECT, INSERT, UPDATE, DELETE
            ON TABLES
            FROM pasay_runtime;
        """
    )

    op.execute(
        """
        REVOKE SELECT, INSERT, UPDATE, DELETE
        ON TABLE telegram_webhook_updates
        FROM pasay_runtime;
        """
    )

    for tbl in reversed(_AFFECTED_BS_TABLES):
        op.execute(
            f"""
            REVOKE SELECT, INSERT, UPDATE, DELETE
            ON TABLE {tbl}
            FROM pasay_runtime;
            """
        )

    op.execute(
        """
        REVOKE USAGE ON SCHEMA public FROM pasay_runtime;
        """
    )
