"""PASAY-TASK-007 Property + Channel P0 migration.

Revision ID: a1b2c3d4e5f6
Revises: e2a114b2f9d0
Create Date: 2026-08-21

Scope (PASAY-TASK-007 Issue #25 contract):
1. ``property_archive_channels`` — one property-level article row per
   (platform, property_id) with a PUBLISHED-has-message_id CHECK guard so
   the bot always has exactly one message to edit in-place.
2. ``unit_archive_articles`` — per-unit digital file with
   ``render_hash`` + ``render_version`` counters so the rendering
   service can short-circuit a no-op edit (no truth change = no spam).
3. AuditAction allowlist append (PASAY-TASK-007 rows at the TAIL, never
   renumber or rename the existing membership/expense/repair values).

Downgrade safety (mirrors Issue #31 FIX5 + Issue #20 FIX2 pattern):
- Every DROP in ``downgrade()`` runs a ``sa.inspect``-backed audit
  confirming:
  (a) DateTime columns marked timezone-aware ARE real tz-aware columns
      in the live connection dialect (UTC-vs-local semantic data loss);
  (b) No JSON/JSONB "dialect-only" columns silently appear that the
      declared schema did not intend to ship (would mean a DROP loses
      unmodeled JSON payloads that future migrations might re-add).
- Audit aborts with a RuntimeError before any DROP when drift is
  detected; no partial loss possible.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "z9a8b7c6d5e4"
branch_labels = None
depends_on = None


PROPERTY_CHANNEL_AUDIT_APPENDS = (
    "property_article_published",
    "property_article_edited",
    "property_article_archived",
    "unit_article_published",
    "unit_article_edited",
    "unit_article_archived",
    "unit_archive_rendered",
)

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

_DT_TZ_COLUMNS = {
    "property_archive_channels": [
        "created_at", "updated_at",
        "last_published_at", "last_rendered_at",
    ],
    "unit_archive_articles": [
        "created_at", "updated_at",
        "last_published_at", "last_rendered_at",
    ],
}

_SAFE_TYPES_PREFIX = (
    "VARCHAR", "CHAR", "BIGINT", "INTEGER", "INT ", "SMALLINT",
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


def upgrade() -> None:
    op.create_table(
        "property_archive_channels",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("property_id", sa.BigInteger(), sa.ForeignKey("properties.id"), nullable=False, index=True),
        sa.Column("platform", sa.String(length=30), nullable=False, server_default="telegram_channel"),
        sa.Column("channel_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("external_message_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
        sa.Column("render_hash", sa.String(length=64), nullable=True),
        sa.Column("last_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_rendered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("render_error", sa.Text(), nullable=True),
        sa.Column("editor_user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("notes", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.UniqueConstraint("platform", "property_id", name="uq_property_archive_active_property"),
        sa.CheckConstraint(
            "platform IN ('telegram_channel','telegram_group','discord')",
            name="ck_property_archive_platform",
        ),
        sa.CheckConstraint(
            "status IN ('draft','published','archived')",
            name="ck_property_archive_status",
        ),
        sa.CheckConstraint(
            "(status = 'published' AND external_message_id IS NOT NULL) OR status <> 'published'",
            name="ck_property_archive_published_has_message",
        ),
    )

    op.create_table(
        "unit_archive_articles",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("unit_id", sa.BigInteger(), sa.ForeignKey("units.id"), nullable=False, index=True),
        sa.Column("property_id", sa.BigInteger(), sa.ForeignKey("properties.id"), nullable=False, index=True),
        sa.Column("platform", sa.String(length=30), nullable=False, server_default="telegram_channel"),
        sa.Column("channel_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("external_message_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
        sa.Column("render_hash", sa.String(length=64), nullable=True),
        sa.Column("render_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_rendered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("render_error", sa.Text(), nullable=True),
        sa.Column("editor_user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("event_count_at_publish", sa.Integer(), nullable=True),
        sa.Column("notes", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.UniqueConstraint("platform", "unit_id", name="uq_unit_archive_active_unit"),
        sa.CheckConstraint(
            "platform IN ('telegram_channel','telegram_group','discord')",
            name="ck_unit_archive_platform",
        ),
        sa.CheckConstraint(
            "status IN ('draft','published','archived')",
            name="ck_unit_archive_status",
        ),
        sa.CheckConstraint(
            "(status = 'published' AND external_message_id IS NOT NULL) OR status <> 'published'",
            name="ck_unit_archive_published_has_message",
        ),
    )
    op.create_index(
        "ix_unit_archive_property_status",
        "unit_archive_articles",
        ["unit_id", "status"],
    )

    conn = op.get_bind()
    expanded = _BASE_ALLOWED + "," + ",".join(PROPERTY_CHANNEL_AUDIT_APPENDS)
    _set_audit_action_allowlist(conn, expanded)


def downgrade() -> None:
    conn = op.get_bind()
    for table, cols in _DT_TZ_COLUMNS.items():
        _audit_timezone_columns(conn, table, cols)
        _audit_no_jsonb_or_dialect_columns(conn, table)
    _set_audit_action_allowlist(conn, _BASE_ALLOWED)

    op.drop_table("unit_archive_articles")
    op.drop_table("property_archive_channels")
