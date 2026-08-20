"""PASAY-TASK-002 — Membership P0: Organization / Membership / Secretary Invite.

Revision ID: z9a8b7c6d5e4
Revises: e2a114b2f9d0
Create Date: 2026-08-20

Scope (from PASAY-TASK-002 contract):
1. ``organizations`` — minimal org entity (name + display_name + audit cols).
2. ``memberships`` — User<->Org links with role (OWNER/SECRETARY), state
   (ACTIVE/REMOVED), audit timestamps, and a partial unique index enforcing
   at most one ACTIVE membership per (org, user).
3. ``secretary_invites`` — one-time, single-consumption, expirable Secretary
   invitation codes produced by ACTIVE OWNER and consumed by an identified
   HUMAN User's telegram identity. Accepted invites are atomically linked to
   the resulting Membership (``created_membership_id`` UNIQUE) so an invite
   can never produce two rows.
4. Append new ``AuditAction`` enum value allowlist rows for the fresh
   Membership lifecycle events (appended; never renumbered/renamed).

Rollback:
    alembic downgrade e2a114b2f9d0
"""
from typing import Union

from alembic import op
import sqlalchemy as sa


revision: str = "z9a8b7c6d5e4"
down_revision: Union[str, None] = "e2a114b2f9d0"
branch_labels = None
depends_on = None


AUDIT_ACTION_APPENDS = (
    "org_created",
    "org_first_owner_activated",
    "secretary_invited",
    "secretary_invite_accepted",
    "secretary_invite_cancelled",
    "secretary_removed",
)


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=True),
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("length(btrim(name)) > 0", name="ck_organizations_name_nonblank"),
    )
    op.create_index("ix_organizations_name_trgm", "organizations", ["name"])

    op.create_table(
        "memberships",
        sa.Column("organization_id", sa.BigInteger(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("invited_by_membership_id", sa.BigInteger(), sa.ForeignKey("memberships.id"), nullable=True),
        sa.Column("joined_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("removed_by_membership_id", sa.BigInteger(), sa.ForeignKey("memberships.id"), nullable=True),
        sa.Column("removal_reason", sa.Text(), nullable=True),
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("role IN ('OWNER','SECRETARY')", name="ck_memberships_role"),
        sa.CheckConstraint("state IN ('ACTIVE','REMOVED')", name="ck_memberships_state"),
        sa.CheckConstraint(
            "(state = 'ACTIVE' AND removed_at IS NULL) OR "
            "(state = 'REMOVED' AND removed_at IS NOT NULL)",
            name="ck_memberships_state_removed_at",
        ),
        sa.UniqueConstraint(
            "organization_id", "user_id", "role",
            name="uq_memberships_org_user_role",
        ),
    )
    op.create_index("ix_memberships_organization_id", "memberships", ["organization_id"])
    op.create_index("ix_memberships_user_id", "memberships", ["user_id"])
    op.create_index("ix_memberships_role", "memberships", ["role"])
    op.create_index("ix_memberships_state", "memberships", ["state"])
    op.create_index(
        "uq_memberships_active_user_org",
        "memberships",
        ["organization_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("state = 'ACTIVE'"),
    )

    op.create_table(
        "secretary_invites",
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("organization_id", sa.BigInteger(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("created_by_membership_id", sa.BigInteger(), sa.ForeignKey("memberships.id"), nullable=False),
        sa.Column("invited_name_hint", sa.String(200), nullable=True),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_by_user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_by_membership_id", sa.BigInteger(), sa.ForeignKey("memberships.id"), nullable=True),
        sa.Column("created_membership_id", sa.BigInteger(), sa.ForeignKey("memberships.id"), nullable=True, unique=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "state IN ('PENDING','ACCEPTED','CANCELLED','EXPIRED')",
            name="ck_secretary_invites_state",
        ),
        sa.CheckConstraint(
            "(state = 'PENDING' AND accepted_at IS NULL AND cancelled_at IS NULL) OR "
            "(state = 'ACCEPTED' AND accepted_at IS NOT NULL) OR "
            "(state = 'CANCELLED' AND cancelled_at IS NOT NULL) OR "
            "(state = 'EXPIRED')",
            name="ck_secretary_invites_state_timestamps",
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_secretary_invites_expires_after_created"),
    )
    op.create_index("ix_secretary_invites_code", "secretary_invites", ["code"], unique=True)
    op.create_index("ix_secretary_invites_organization_id", "secretary_invites", ["organization_id"])
    op.create_index("ix_secretary_invites_created_by_membership_id", "secretary_invites", ["created_by_membership_id"])
    op.create_index("ix_secretary_invites_state", "secretary_invites", ["state"])
    op.create_index("ix_secretary_invites_accepted_by_user_id", "secretary_invites", ["accepted_by_user_id"])
    op.create_index(
        "uq_secretary_invites_code_active",
        "secretary_invites",
        ["code"],
        unique=True,
        postgresql_where=sa.text("state = 'PENDING'"),
    )

    conn = op.get_bind()
    for value in AUDIT_ACTION_APPENDS:
        conn.execute(
            sa.text(
                "ALTER TABLE audit_logs DROP CONSTRAINT IF EXISTS ck_audit_logs_action"
            )
        )
        allowed = (
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
            "org_created,org_first_owner_activated,secretary_invited,"
            "secretary_invite_accepted,secretary_invite_cancelled,"
            "secretary_removed"
        )
        conn.execute(sa.text(
            "ALTER TABLE audit_logs ADD CONSTRAINT ck_audit_logs_action CHECK "
            "(action IN (%s))" % ",".join("'%s'" % v for v in allowed.split(","))
        ))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text(
        "ALTER TABLE audit_logs DROP CONSTRAINT IF EXISTS ck_audit_logs_action"
    ))
    base_allowed = (
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
        "expense_resubmitted,expense_rejected"
    )
    conn.execute(sa.text(
        "ALTER TABLE audit_logs ADD CONSTRAINT ck_audit_logs_action CHECK "
        "(action IN (%s))" % ",".join("'%s'" % v for v in base_allowed.split(","))
    ))

    op.drop_table("secretary_invites")
    op.drop_table("memberships")
    op.drop_table("organizations")
