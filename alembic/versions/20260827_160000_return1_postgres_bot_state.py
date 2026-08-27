"""return1-postgres-bot-state (PASAY-DEPLOY-PHASE1-RETURN1)

Cloudflare Container local disk is EPHEMERAL. The SQLite StateStore at
/opt/pasay-pm or /app/state loses durability truths (daily_marks,
reminder_deliveries, followup_deliveries) after container sleep/restart.

Create 9 Neon/Postgres tables mirroring the SQLite StateStore schema so
PASAY_RUNTIME_MODE=cloudflare-container can use durable Postgres state
while Windows/dev continues to use the local SQLite store.

Scope (per Owner RETURN1 §C): ONLY StateStore tables. No business schema
changes. No new migrations for leases/expenses/etc.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
# NOTE: revision id must be <= 32 chars. The stock alembic_version table uses
# `version_num character varying(32) NOT NULL` (non-upgradable without a
# pre-bootstrap migration which we cannot run before alembic itself boots).
revision = "ret1_postgres_bot_state_20260828"
down_revision = "m4d000000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS bs_conversations (
          chat_id        TEXT NOT NULL,
          user_id        TEXT NOT NULL,
          state          TEXT NOT NULL,
          payload_json   TEXT NOT NULL DEFAULT '{}',
          nonce          TEXT NOT NULL DEFAULT '',
          updated_at     TEXT NOT NULL,
          expires_at     TEXT NOT NULL,
          PRIMARY KEY (chat_id, user_id)
        );
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS bs_idempotency_keys (
          key          TEXT PRIMARY KEY,
          kind         TEXT NOT NULL,
          resource     TEXT NOT NULL DEFAULT '',
          status       TEXT NOT NULL,
          result_json  TEXT,
          created_at   TEXT NOT NULL,
          expires_at   TEXT NOT NULL
        );
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS bs_user_defaults (
          user_id        TEXT PRIMARY KEY,
          payment_method TEXT NOT NULL DEFAULT 'Bank',
          updated_at     TEXT NOT NULL
        );
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS bs_rent_status_selectors (
          nonce        TEXT PRIMARY KEY,
          chat_id      TEXT NOT NULL,
          user_id      TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          created_at   TEXT NOT NULL,
          expires_at   TEXT NOT NULL
        );
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS bs_v2_context (
          chat_id      TEXT NOT NULL,
          user_id      TEXT NOT NULL,
          payload_json TEXT NOT NULL DEFAULT '{}',
          updated_at   TEXT NOT NULL,
          expires_at   TEXT NOT NULL,
          PRIMARY KEY (chat_id, user_id)
        );
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS bs_known_groups (
          chat_id    TEXT PRIMARY KEY,
          title      TEXT NOT NULL DEFAULT '',
          first_seen TEXT NOT NULL
        );
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS bs_daily_marks (
          key        TEXT PRIMARY KEY,
          created_at TEXT NOT NULL
        );
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS bs_reminder_deliveries (
          expense_id   TEXT NOT NULL,
          date         TEXT NOT NULL,
          target_user  TEXT NOT NULL DEFAULT '',
          destination  TEXT NOT NULL DEFAULT '',
          sent_at      TEXT NOT NULL,
          message_id   TEXT NOT NULL DEFAULT '',
          PRIMARY KEY (expense_id, date)
        );
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS bs_followup_deliveries (
          task_id      TEXT PRIMARY KEY,
          unit_id      TEXT NOT NULL DEFAULT '',
          date         TEXT NOT NULL DEFAULT '',
          target_user  TEXT NOT NULL DEFAULT '',
          destination  TEXT NOT NULL DEFAULT '',
          sent_at      TEXT NOT NULL,
          message_id   TEXT NOT NULL DEFAULT ''
        );
        """
    )


def downgrade() -> None:
    for tbl in reversed([
        "bs_conversations",
        "bs_idempotency_keys",
        "bs_user_defaults",
        "bs_rent_status_selectors",
        "bs_v2_context",
        "bs_known_groups",
        "bs_daily_marks",
        "bs_reminder_deliveries",
        "bs_followup_deliveries",
    ]):
        op.execute(f"DROP TABLE IF EXISTS {tbl};")
