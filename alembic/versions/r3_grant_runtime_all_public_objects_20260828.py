"""r3-grant-runtime-all-public-objects-20260828 (M006 RETURN3-E STEP9 blocker)

After r2_grant_runtime_tables_20260828 explicitly granted CRUD on the 9
bs_* StateStore tables + telegram_webhook_updates, STEP9 synthetic /start
smoke still raises InsufficientPrivilege (SQLSTATE 42501) from iter=7+.

Root cause (confirmed by elimination — Neon MCP list_tools failure prevented
direct in-DB capture, so this is diagnostic-plus-fix):
  1. r2 GRANTed on 10 tables explicitly but left ALL other pre-existing
     public tables (users, memberships, leases, expenses, properties,
     units, tasks, notifications, etc.) with NO runtime grants at all.
  2. The pasay-deploy-phase1.yml STEP0 only grants USAGE,SELECT ON ALL
     SEQUENCES — never TABLES.  Every new table created outside that
     STEP0 path inherits ZERO runtime table privileges unless a later
     migration or ALTER DEFAULT PRIVILEGES catches it.
  3. r2 ALTER DEFAULT PRIVILEGES covered TABLES for neondb_owner but the
     runtime still needed explicit one-shot GRANTs on every existing
     table + GRANTs on ALL sequences (STEP0 runs BEFORE migrations, so
     bs_* / telegram_webhook_updates sequences were never covered).
  4. SCHEMA public USAGE and FUNCTION EXECUTE defaults may also be
     missing piecemeal depending on initial role provisioning.

Fix scope — MINIMAL idempotent closure:
  * GRANT ALL REQUIRED PRIVILEGES on ALL existing public TABLES
    (SELECT,INSERT,UPDATE,DELETE) to pasay_runtime in one shot.
  * GRANT USAGE,SELECT on ALL existing public SEQUENCES to pasay_runtime
    (covers any serial PK / identity column the runtime writes to
    through API or StateStore).
  * GRANT USAGE on SCHEMA public explicitly (redundant with r2, safe).
  * ALTER DEFAULT PRIVILEGES FOR ROLE neondb_owner IN SCHEMA public:
      - TABLES: SELECT,INSERT,UPDATE,DELETE to pasay_runtime
      - SEQUENCES: USAGE,SELECT to pasay_runtime
      - FUNCTIONS: EXECUTE to pasay_runtime
  * BEFORE/AFTER evidence captured to PostgreSQL client NOTICEs during
    upgrade (visible in alembic upgrade head output / step3 sanitized
    log) — independent of whether Neon MCP list_tools works this run.

Idempotent: every GRANT here is safe to re-apply (PostgreSQL GRANT is
naturally idempotent; duplicate grants produce no error and no change).
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


def _notice(msg: str) -> None:
    """Emit a PostgreSQL client NOTICE — visible in alembic step3 output."""
    safe = msg.replace("'", "''")
    op.execute(f"DO $$ BEGIN RAISE NOTICE '[r3] {safe}'; END $$;")


def _capture_snapshot(label: str) -> None:
    """Capture privilege evidence rows via NOTICE.

    Prints one NOTICE line per (grantee, table_name, privilege_type) tuple
    for TABLE grants; one line per (grantee, sequence_name, privilege_type)
    for SEQUENCE grants; one line summarising DEFAULT PRIVILEGES.
    """
    _notice(f"=== {label}: TABLE grants for {_RUNTIME_ROLE} ===")
    op.execute(
        f"""
        DO $$
        DECLARE r RECORD;
        BEGIN
          FOR r IN
            SELECT table_name, privilege_type
            FROM information_schema.role_table_grants
            WHERE grantee = '{_RUNTIME_ROLE}'
              AND table_schema = '{_SCHEMA}'
            ORDER BY table_name, privilege_type
          LOOP
            RAISE NOTICE '[r3][{label}] TABLE %  %', r.table_name, r.privilege_type;
          END LOOP;
        END $$;
        """
    )
    _notice(f"=== {label}: SEQUENCE grants for {_RUNTIME_ROLE} ===")
    op.execute(
        f"""
        DO $$
        DECLARE r RECORD;
        BEGIN
          FOR r IN
            SELECT sequence_name, privilege_type
            FROM information_schema.role_sequence_grants
            WHERE grantee = '{_RUNTIME_ROLE}'
              AND sequence_schema = '{_SCHEMA}'
            ORDER BY sequence_name, privilege_type
          LOOP
            RAISE NOTICE '[r3][{label}] SEQ  %  %', r.sequence_name, r.privilege_type;
          END LOOP;
        END $$;
        """
    )
    _notice(f"=== {label}: tables with any runtime grant = COUNT ===")
    op.execute(
        f"""
        DO $$
        DECLARE
          v_tables INTEGER;
          v_sequences INTEGER;
        BEGIN
          SELECT count(DISTINCT table_name) INTO v_tables
          FROM information_schema.role_table_grants
          WHERE grantee = '{_RUNTIME_ROLE}' AND table_schema = '{_SCHEMA}';
          SELECT count(DISTINCT sequence_name) INTO v_sequences
          FROM information_schema.role_sequence_grants
          WHERE grantee = '{_RUNTIME_ROLE}' AND sequence_schema = '{_SCHEMA}';
          RAISE NOTICE '[r3][{label}] SUMMARY: tables_with_grants=%, sequences_with_grants=%',
            v_tables, v_sequences;
        END $$;
        """
    )


def upgrade() -> None:
    _notice("START upgrade r3_grant_runtime_all_public_objects_20260828")

    # ── BEFORE evidence ──────────────────────────────────────────────────
    _capture_snapshot("BEFORE")

    # ── 1. SCHEMA USAGE (redundant with r2; safe repeat) ────────────────
    op.execute(f"GRANT USAGE ON SCHEMA {_SCHEMA} TO {_RUNTIME_ROLE};")
    _notice("GRANT USAGE ON SCHEMA public applied")

    # ── 2. ONE-SHOT grant on ALL existing public TABLES ─────────────────
    #    Covers every pre-existing business table (users, memberships,
    #    leases, expenses, properties, units, operational_tasks,
    #    notifications, scheduled_jobs, etc.) that the runtime role
    #    touches through the FastAPI API layer or PTB bot API client.
    #    r2 only covered the 9 bs_* + telegram_webhook_updates.
    op.execute(
        f"""
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON ALL TABLES IN SCHEMA {_SCHEMA}
            TO {_RUNTIME_ROLE};
        """
    )
    _notice("GRANT CRUD ON ALL TABLES IN SCHEMA public applied")

    # ── 3. ONE-SHOT grant on ALL existing public SEQUENCES ──────────────
    #    STEP0 grants USAGE,SELECT ON ALL SEQUENCES, but STEP0 runs
    #    BEFORE alembic migrations. Any sequence created by a migration
    #    therefore receives NO grant unless caught by ALTER DEFAULT
    #    PRIVILEGES. r2 did ALTER DEFAULT only for TABLES, not SEQUENCES.
    #    This closes the gap for both STEP0-visible and migration-created
    #    sequences (e.g. identity columns on business tables).
    op.execute(
        f"""
        GRANT USAGE, SELECT
            ON ALL SEQUENCES IN SCHEMA {_SCHEMA}
            TO {_RUNTIME_ROLE};
        """
    )
    _notice("GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public applied")

    # ── 4. ALTER DEFAULT PRIVILEGES for neondb_owner creator role ──────
    #    Covers ANY FUTURE object created by neondb_owner in public
    #    schema.  r2 already set TABLE defaults.  This adds SEQUENCE and
    #    FUNCTION defaults, and is idempotent (ALTER DEFAULT PRIVILEGES
    #    for the same role/schema duplicates safely).
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
    _notice("ALTER DEFAULT PRIVILEGES (TABLES+SEQUENCES+FUNCTIONS) applied")

    # ── AFTER evidence ───────────────────────────────────────────────────
    _capture_snapshot("AFTER")
    _notice("END upgrade r3_grant_runtime_all_public_objects_20260828 — SUCCESS")


def downgrade() -> None:
    """Symmetric REVOKE — the opposite of upgrade()."""
    _notice("START downgrade r3_grant_runtime_all_public_objects_20260828")

    # 1. REVOKE ALTER DEFAULT PRIVILEGES (reverse order)
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
    _notice("downgrade: ALTER DEFAULT PRIVILEGES revoked")

    # 2. REVOKE one-shot grants on all sequences
    op.execute(
        f"""
        REVOKE USAGE, SELECT
            ON ALL SEQUENCES IN SCHEMA {_SCHEMA}
            FROM {_RUNTIME_ROLE};
        """
    )
    _notice("downgrade: SEQUENCE grants revoked")

    # 3. REVOKE one-shot grants on all tables
    op.execute(
        f"""
        REVOKE SELECT, INSERT, UPDATE, DELETE
            ON ALL TABLES IN SCHEMA {_SCHEMA}
            FROM {_RUNTIME_ROLE};
        """
    )
    _notice("downgrade: TABLE grants revoked")

    # 4. REVOKE SCHEMA USAGE
    op.execute(f"REVOKE USAGE ON SCHEMA {_SCHEMA} FROM {_RUNTIME_ROLE};")
    _notice("downgrade: SCHEMA USAGE revoked")

    _notice("END downgrade r3_grant_runtime_all_public_objects_20260828 — SUCCESS")
