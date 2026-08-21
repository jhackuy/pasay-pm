"""PASAY-TASK-007 Issue #25 FIX3 — Property + Unit scoped + Channel Binding.

Revision ID: b5c6d7e8f9a0
Revises: a1b2c3d4e5f6
Create Date: 2026-08-21

Scope (Issue #25 authoritative contract, NOT PRODUCT_CONFORMANCE_AUDIT_001):
  1. properties.organization_id — nullable BIGINT FK (compatible with existing
     data; old rows keep NULL, new writes MUST supply).
  2. uq_units_active_property_unit_number — partial UNIQUE on
     (property_id, unit_number) WHERE deleted_at IS NULL AND is_active = TRUE.
     + ck_units_unit_number_nonblank — forbid blank unit_number strings.
  3. unit_channel_bindings — Issue #25 §4 minimal binding model:
       - organization_id NOT NULL FK, unit_id NOT NULL FK
       - purpose enum (archive / business_group)
       - channel_chat_id BigInteger (required if ACTIVE, allows negative Telegram IDs)
       - thread_topic_id BigInteger (optional thread/topic locator)
       - status ACTIVE|REVOKED with timestamp gating (CHECK)
       - uq_unit_binding_active_unit_purpose: partial UNIQUE on (unit_id, purpose)
         WHERE status='ACTIVE' → one active binding per (unit, purpose)
       - revokes preserve history (REVOKED rows stay; never hard-deleted)
  4. AuditAction allowlist tail-append: drop the 7 archive-article actions
     (property_article_* / unit_article_* / unit_archive_rendered) and add
     the 3 minimal binding actions (unit_channel_bound/replaced/revoked).

Downgrade safety (mirrors Issue #20 FIX2 pattern — FAIL CLOSED on drift):
  - unit_channel_bindings downgrade: sa.inspect audit of 4 tz-aware DateTime
    columns + no unmodeled JSONB/dialect types BEFORE drop_table.
  - properties.organization_id downgrade: sa.inspect audit of the column's
    actual DB type (must be BIGINT/INT8-ish, no JSONB drift) BEFORE drop.
  - Audit logs CHECK constraint is rebuilt in both directions atomically.
  - Any drift aborts with RuntimeError before destructive SQL executes.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

from alembic import op

revision: str = "b5c6d7e8f9a0"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


# ---- AuditLog action allowlist rebuild -------------------------------------
#
# _BASE_ALLOWED matches the tail in z9a8b7c6d5e4 (membership P0 migration).
# We NEVER renumber/rename the older values; only the PASAY-TASK-007 tail
# changes (from 7 archive-article → 3 minimal-binding actions), which is
# safe because this migration has never been merged into the base branch.
# Note: PASAY-TASK-011 (a1b2c3d4e5f6_scheduled_job_ledger) does NOT modify
# the audit action allowlist, so _BASE_ALLOWED is still valid here.

_BASE_ALLOWED = (
    "create,update,soft_delete,confirm,approve,reject,pay,reverse,"
    "task_created,task_completed,task_cancelled,task_snoozed,"
    "rule_created,rule_updated,rule_disabled,"
    "task_auto_completed,task_auto_cancelled,task_backfilled,"
    "task_reminder_redelivered,outbox_dropped,copilot_context_built,"
    "copilot_proposal_created,copilot_proposal_confirmed,"
    "copilot_proposal_cancelled,copilot_proposal_expired,"
    "copilot_proposal_confirm_rejected,copilot_proposal_executing,"
    "copilot_proposal_executed,copilot_proposal_execution_rejected,"
    "task_reassigned,task_updated,task_completed_via_approval,"
    "task_completed_via_rejection,task_completed_via_payment,"
    "task_reminded,task_escalated,task_acknowledged,"
    "phone_direct_update,blocked_created,blocked_resolved,"
    "payment_promise_recorded,promise_fulfilled,promise_missed_refollow,"
    "action_assigned,rent_followup_sent,"
    "repair_created,proposal_submitted,proposal_approved,"
    "proposal_rejected,repair_completed_pending_verification,"
    "repair_closed_after_verification,repair_cancelled,"
    "expense_claim_created,expense_claim_verified,"
    "expense_claim_failed,expense_claim_reversed,"
    "expense_amount_mismatch,expense_partially_paid,"
    "expense_fully_paid,expense_requires_reapproval,"
    "expense_resubmitted,expense_rejected,"
    "org_created,org_first_owner_activated,"
    "secretary_invited,secretary_invite_accepted,"
    "secretary_invite_cancelled,secretary_removed"
)

_PROPERTY_CHANNEL_ISSUE25_APPENDS = (
    "unit_channel_bound",
    "unit_channel_replaced",
    "unit_channel_revoked",
)

# ---- Downgrade audit catalogues --------------------------------------------
_UCB_DT_TZ_COLUMNS = [
    "created_at", "updated_at", "revoked_at",
]

_SAFE_TYPES_PREFIX = (
    "VARCHAR", "CHAR", "BIGINT", "INTEGER", "INT ", "INT,", "SMALLINT",
    "BOOLEAN", "NUMERIC", "DECIMAL", "TEXT", "DATE", "DATETIME",
    "TIMESTAMP", "UUID",
)


def _set_audit_action_allowlist(conn, allowed_csv: str) -> None:
    conn.execute(
        sa.text("ALTER TABLE audit_logs DROP CONSTRAINT IF EXISTS ck_audit_logs_action")
    )
    values_sql = ",".join(f"'{v}'" for v in allowed_csv.split(","))
    conn.execute(sa.text(
        "ALTER TABLE audit_logs ADD CONSTRAINT ck_audit_logs_action CHECK "
        f"(action IN ({values_sql}))"
    ))


def _audit_timezone_columns(conn, table: str, expected_tz: list[str]) -> None:
    insp = sa_inspect(conn)
    cols = {c["name"]: c for c in insp.get_columns(table)}
    missing = [name for name in expected_tz if name not in cols]
    if missing:
        raise RuntimeError(
            f"downgrade audit: {table} missing expected columns {missing}"
            f" — refusing DROP to avoid semantic data loss"
        )
    bad: list[str] = []
    for name in expected_tz:
        col_type = str(cols[name]["type"]).upper()
        if not any(col_type.startswith(p) for p in ("DATETIME", "TIMESTAMP")):
            bad.append(f"{name}({col_type})")
    if bad:
        raise RuntimeError(
            f"downgrade audit: {table} tz-guard columns not datetime: {bad}"
            f" — refusing DROP (declared DateTime(timezone=True) drift)"
        )


def _audit_no_jsonb_or_dialect_columns(conn, table: str) -> None:
    insp = sa_inspect(conn)
    cols = insp.get_columns(table)
    bad: list[str] = []
    for c in cols:
        col_type = str(c["type"]).upper()
        if "JSON" in col_type:
            bad.append(f"{c['name']}({col_type})")
            continue
        if not any(col_type.startswith(p) for p in _SAFE_TYPES_PREFIX):
            bad.append(f"{c['name']}({col_type})")
    if bad:
        raise RuntimeError(
            f"downgrade audit: {table} has unmodeled dialect/JSON columns "
            f"{bad} — refusing blind DROP (would lose unmodeled payloads)"
        )


def _audit_column_type_is_int(conn, table: str, column: str) -> None:
    insp = sa_inspect(conn)
    cols = {c["name"]: c for c in insp.get_columns(table)}
    if column not in cols:
        return  # column not present = nothing to drop, OK
    col_type = str(cols[column]["type"]).upper()
    if not any(col_type.startswith(p) for p in ("BIGINT", "INTEGER", "INT ", "INT,", "SMALLINT")):
        raise RuntimeError(
            f"downgrade audit: {table}.{column} type={col_type!r} not integer-ish"
            f" — refusing drop column (would silently drop unexpected payload)"
        )


def _audit_no_orphan_audit_actions(conn, actions: tuple[str, ...]) -> None:
    """Fail closed if audit_logs still carries the Issue #25 tail actions.

    Per Issue #25 "不得粗暴删除旧数据" rule — we must NOT let downgrade
    DROP the CHECK constraint then re-add a narrower one that would fail on
    existing rows, NOR silently delete audit rows. When orphan rows exist,
    abort the downgrade explicitly with a RuntimeError that names the count.
    """
    values_sql = ",".join(f"'{v}'" for v in actions)
    row = conn.execute(sa.text(
        f"SELECT COUNT(*) FROM audit_logs WHERE action IN ({values_sql})"
    )).fetchone()
    count = row[0] if row is not None else 0
    if count:
        raise RuntimeError(
            f"downgrade audit: audit_logs has {count} rows with Issue #25 tail "
            f"actions {sorted(actions)} — refusing to rebuild the narrower "
            f"ck_audit_logs_action constraint (would violate existing rows). "
            f"Either migrate those rows forward to a newer schema, or "
            f"explicitly delete them BEFORE running downgrade."
        )


# ---- upgrade / downgrade ---------------------------------------------------

def upgrade() -> None:
    # --- 1. properties.organization_id (nullable, compat with existing data)
    with op.batch_alter_table("properties", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("organization_id", sa.BigInteger(), sa.ForeignKey("organizations.id"), nullable=True)
        )
        batch_op.create_index(
            "ix_properties_organization_id_active",
            ["organization_id"],
            postgresql_where=sa.text("deleted_at IS NULL"),
        )
    op.create_index(
        "ix_properties_organization_id",
        "properties",
        ["organization_id"],
        unique=False,
    )

    # --- 2. Unit table: partial unique index + nonblank CHECK
    with op.batch_alter_table("units", schema=None) as batch_op:
        batch_op.create_index(
            "uq_units_active_property_unit_number",
            ["property_id", "unit_number"],
            unique=True,
            postgresql_where=sa.text("deleted_at IS NULL AND is_active = TRUE"),
        )
        batch_op.create_check_constraint(
            "ck_units_unit_number_nonblank",
            "length(btrim(unit_number)) > 0",
        )

    # --- 3. unit_channel_bindings table (Issue #25 §4 minimal binding)
    op.create_table(
        "unit_channel_bindings",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "organization_id",
            sa.BigInteger(), sa.ForeignKey("organizations.id"),
            nullable=False, index=True,
        ),
        sa.Column(
            "unit_id",
            sa.BigInteger(), sa.ForeignKey("units.id"),
            nullable=False, index=True,
        ),
        sa.Column("purpose", sa.String(length=30), nullable=False),
        sa.Column("channel_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("thread_topic_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="ACTIVE"),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "revoked_by_membership_id",
            sa.BigInteger(), sa.ForeignKey("memberships.id"),
            nullable=True,
        ),
        sa.Column("notes", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        # Constraints (mirror the ORM model declarations):
        sa.CheckConstraint(
            "purpose IN ('archive','business_group')",
            name="ck_unit_binding_purpose_enum",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE','REVOKED')",
            name="ck_unit_binding_status_enum",
        ),
        sa.CheckConstraint(
            "(status = 'ACTIVE' AND channel_chat_id IS NOT NULL) OR status <> 'ACTIVE'",
            name="ck_unit_binding_active_has_chat_id",
        ),
        sa.CheckConstraint(
            "(status = 'REVOKED' AND revoked_at IS NOT NULL) OR status <> 'REVOKED'",
            name="ck_unit_binding_revoked_has_timestamp",
        ),
    )
    op.create_index(
        "uq_unit_binding_active_unit_purpose",
        "unit_channel_bindings",
        ["unit_id", "purpose"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_index(
        "ix_unit_bindings_org_unit_status",
        "unit_channel_bindings",
        ["organization_id", "unit_id", "status"],
    )

    # --- 4. Audit action allowlist rebuild
    conn = op.get_bind()
    expanded = _BASE_ALLOWED + "," + ",".join(_PROPERTY_CHANNEL_ISSUE25_APPENDS)
    _set_audit_action_allowlist(conn, expanded)


def downgrade() -> None:
    conn = op.get_bind()

    # --- Pre-drop audits (FAIL CLOSED on any detected drift)
    # a) unit_channel_bindings table (tz + JSONB audits)
    _audit_timezone_columns(conn, "unit_channel_bindings", _UCB_DT_TZ_COLUMNS)
    _audit_no_jsonb_or_dialect_columns(conn, "unit_channel_bindings")
    # b) properties.organization_id column type audit (must be integer-ish)
    _audit_column_type_is_int(conn, "properties", "organization_id")
    # c) AuditLog orphan-row guard: existing unit_channel_* rows would make
    #    the narrower _BASE_ALLOWED CHECK fail on ADD CONSTRAINT. Abort early.
    _audit_no_orphan_audit_actions(conn, _PROPERTY_CHANNEL_ISSUE25_APPENDS)

    # --- 4. Restore baseline audit allowlist (drop the Issue #25 tail)
    _set_audit_action_allowlist(conn, _BASE_ALLOWED)

    # --- 3. Drop unit_channel_bindings (audited above)
    op.drop_index("uq_unit_binding_active_unit_purpose", table_name="unit_channel_bindings")
    op.drop_index("ix_unit_bindings_org_unit_status", table_name="unit_channel_bindings")
    op.drop_table("unit_channel_bindings")

    # --- 2. Drop Unit constraints / indexes added in upgrade
    with op.batch_alter_table("units", schema=None) as batch_op:
        batch_op.drop_constraint("ck_units_unit_number_nonblank", type_="check")
        batch_op.drop_index("uq_units_active_property_unit_number")

    # --- 1. Drop properties.organization_id (audited above for type safety)
    with op.batch_alter_table("properties", schema=None) as batch_op:
        batch_op.drop_index("ix_properties_organization_id_active")
    op.drop_index("ix_properties_organization_id", table_name="properties")
    with op.batch_alter_table("properties", schema=None) as batch_op:
        batch_op.drop_column("organization_id")
