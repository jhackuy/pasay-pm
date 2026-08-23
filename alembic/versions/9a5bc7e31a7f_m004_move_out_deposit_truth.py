"""PASAY-MILESTONE-004 — Move-out inspection + deposit settlement truth model.

Revision ID: 9a5bc7e31a7f
Revises: m2a000000001
Create Date: 2026-08-22

UPGRADE SCOPE:
1. ``move_out_inspections``: authoritative move-out inspection record per
   lease (partial unique index uq_move_out_active enforces at most one
   SCHEDULED/INSPECTED row per lease_id) with SCHEDULED -> INSPECTED ->
   CONFIRMED | CANCELLED lifecycle, JSONB findings + evidence_ids,
   confirmed_by/cancelled_by actor columns, and full AuditMixin cols.
2. ``deposit_settlements``: one settlement per move-out inspection (UNIQUE
   move_out_inspection_id), with deposit_received, deductions JSONB,
   computed total_deductions/refund_amount, DRAFT -> CONFIRMED -> RECONCILED
   lifecycle, ix_deposit_settlements_lease_id lookup index.
3. ``leases``: bidirectional pointer columns
   (move_out_inspection_id/deposit_settlement_id FKs),
   moved_out_settled_at closed timestamp, and renewal_metadata JSONB for
   renewal/novation metadata.
4. ``tenants``: moved_out_at TIMESTAMPTZ nullable — when the tenant last
   vacated their active lease (truth column for future tenant history).
5. ``ck_operational_tasks_task_type`` allowlist: append MOVE_OUT_INSPECTION
   and DEPOSIT_SETTLEMENT (old 9 values -> 11).
6. ``ck_recurring_rules_rule_type`` allowlist: same two new values appended.

DOWNGRADE SAFETY (逆序还原):
- Drop new leases/tenants columns (and their FK constraints) first; then
- Drop ix_deposit_settlements_lease_id + deposit_settlements table; then
- Drop uq_move_out_active + move_out_inspections table; then
- Restore both CHECK allowlists to the previous 9-value legacy set.

ROLLBACK:
    alembic downgrade m2a000000001
"""
from typing import Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "9a5bc7e31a7f"
down_revision: Union[str, None] = "m2a000000001"
branch_labels = None
depends_on = None


_LEGACY_TASK_TYPES = (
    "'RENT_DUE','RENT_OVERDUE','LEASE_EXPIRING','PROPERTY_FEE_DUE',"
    "'AC_MAINTENANCE','APPROVAL_PENDING','PAYMENT_PENDING',"
    "'SETTLEMENT_PENDING','FOLLOWUP'"
)
_EXPANDED_TASK_TYPES = (
    "'RENT_DUE','RENT_OVERDUE','LEASE_EXPIRING','PROPERTY_FEE_DUE',"
    "'AC_MAINTENANCE','APPROVAL_PENDING','PAYMENT_PENDING',"
    "'SETTLEMENT_PENDING','FOLLOWUP',"
    "'MOVE_OUT_INSPECTION','DEPOSIT_SETTLEMENT'"
)


def _audit_cols():
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
    ]


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. move_out_inspections
    # ------------------------------------------------------------------
    op.create_table(
        "move_out_inspections",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("lease_id", sa.BigInteger(), nullable=False),
        sa.Column("unit_id", sa.BigInteger(), nullable=True),
        sa.Column("tenant_id", sa.BigInteger(), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("inspected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.String(length=50),
            nullable=False,
            server_default="SCHEDULED",
        ),
        sa.Column(
            "findings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "evidence_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("confirmed_by", sa.BigInteger(), nullable=True),
        sa.Column("cancelled_by", sa.BigInteger(), nullable=True),
        *_audit_cols(),
        sa.CheckConstraint(
            "status IN ('SCHEDULED','INSPECTED','CONFIRMED','CANCELLED')",
            name="ck_move_out_inspections_status",
        ),
        sa.ForeignKeyConstraint(["lease_id"], ["leases.id"]),
        sa.ForeignKeyConstraint(["unit_id"], ["units.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_move_out_active",
        "move_out_inspections",
        ["lease_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('SCHEDULED','INSPECTED')"),
    )
    op.create_index(
        "ix_move_out_inspections_lease_id",
        "move_out_inspections",
        ["lease_id"],
        unique=False,
    )
    op.create_index(
        "ix_move_out_inspections_status",
        "move_out_inspections",
        ["status"],
        unique=False,
    )

    # ------------------------------------------------------------------
    # 2. deposit_settlements
    # ------------------------------------------------------------------
    op.create_table(
        "deposit_settlements",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("lease_id", sa.BigInteger(), nullable=False),
        sa.Column("move_out_inspection_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "deposit_received",
            sa.Numeric(precision=14, scale=2),
            nullable=False,
        ),
        sa.Column(
            "deductions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "total_deductions",
            sa.Numeric(precision=14, scale=2),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "refund_amount",
            sa.Numeric(precision=14, scale=2),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "status",
            sa.String(length=50),
            nullable=False,
            server_default="DRAFT",
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("confirmed_by", sa.BigInteger(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        *_audit_cols(),
        sa.CheckConstraint(
            "status IN ('DRAFT','CONFIRMED','RECONCILED')",
            name="ck_deposit_settlements_status",
        ),
        sa.ForeignKeyConstraint(
            ["lease_id"], ["leases.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["move_out_inspection_id"], ["move_out_inspections.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "move_out_inspection_id",
            name="uq_deposit_settlements_move_out_inspection_id",
        ),
    )
    op.create_index(
        "ix_deposit_settlements_lease_id",
        "deposit_settlements",
        ["lease_id"],
        unique=False,
    )
    op.create_index(
        "ix_deposit_settlements_status",
        "deposit_settlements",
        ["status"],
        unique=False,
    )

    # ------------------------------------------------------------------
    # 3. leases: bidirectional FKs + settled timestamp + renewal metadata
    # ------------------------------------------------------------------
    op.add_column(
        "leases",
        sa.Column("move_out_inspection_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_leases_move_out_inspection",
        "leases",
        "move_out_inspections",
        ["move_out_inspection_id"],
        ["id"],
    )
    op.add_column(
        "leases",
        sa.Column("deposit_settlement_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_leases_deposit_settlement",
        "leases",
        "deposit_settlements",
        ["deposit_settlement_id"],
        ["id"],
    )
    op.add_column(
        "leases",
        sa.Column(
            "moved_out_settled_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "leases",
        sa.Column(
            "renewal_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )

    # ------------------------------------------------------------------
    # 4. tenants.moved_out_at
    # ------------------------------------------------------------------
    op.add_column(
        "tenants",
        sa.Column("moved_out_at", sa.DateTime(timezone=True), nullable=True),
    )

    # ------------------------------------------------------------------
    # 5. ck_operational_tasks_task_type (9 -> 11)
    # ------------------------------------------------------------------
    op.drop_constraint(
        "ck_operational_tasks_task_type", "operational_tasks", type_="check"
    )
    op.create_check_constraint(
        "ck_operational_tasks_task_type",
        "operational_tasks",
        f"task_type IN ({_EXPANDED_TASK_TYPES})",
    )

    # ------------------------------------------------------------------
    # 6. ck_recurring_rules_rule_type (9 -> 11)
    # ------------------------------------------------------------------
    op.drop_constraint(
        "ck_recurring_rules_rule_type", "recurring_rules", type_="check"
    )
    op.create_check_constraint(
        "ck_recurring_rules_rule_type",
        "recurring_rules",
        f"rule_type IN ({_EXPANDED_TASK_TYPES})",
    )


def downgrade() -> None:
    # Pre-downgrade: delete post-m2a enum rows BEFORE restoring legacy CHECK allowlists.
    op.execute(
        "DELETE FROM operational_tasks WHERE task_type IN ('MOVE_OUT_INSPECTION', 'DEPOSIT_SETTLEMENT')"
    )
    op.execute(
        "DELETE FROM recurring_rules WHERE rule_type IN ('MOVE_OUT_INSPECTION', 'DEPOSIT_SETTLEMENT')"
    )

    # ------------------------------------------------------------------
    # 6 (reverse). Restore recurring_rules rule_type allowlist (legacy 9)
    # ------------------------------------------------------------------
    op.drop_constraint(
        "ck_recurring_rules_rule_type", "recurring_rules", type_="check"
    )
    op.create_check_constraint(
        "ck_recurring_rules_rule_type",
        "recurring_rules",
        f"rule_type IN ({_LEGACY_TASK_TYPES})",
    )

    # ------------------------------------------------------------------
    # 5 (reverse). Restore operational_tasks task_type allowlist (legacy 9)
    # ------------------------------------------------------------------
    op.drop_constraint(
        "ck_operational_tasks_task_type", "operational_tasks", type_="check"
    )
    op.create_check_constraint(
        "ck_operational_tasks_task_type",
        "operational_tasks",
        f"task_type IN ({_LEGACY_TASK_TYPES})",
    )

    # ------------------------------------------------------------------
    # 4 (reverse). tenants.moved_out_at
    # ------------------------------------------------------------------
    op.drop_column("tenants", "moved_out_at")

    # ------------------------------------------------------------------
    # 3 (reverse). leases columns + FKs (drop FKs before columns)
    # ------------------------------------------------------------------
    op.drop_column("leases", "renewal_metadata")
    op.drop_column("leases", "moved_out_settled_at")
    op.drop_constraint(
        "fk_leases_deposit_settlement", "leases", type_="foreignkey"
    )
    op.drop_column("leases", "deposit_settlement_id")
    op.drop_constraint(
        "fk_leases_move_out_inspection", "leases", type_="foreignkey"
    )
    op.drop_column("leases", "move_out_inspection_id")

    # ------------------------------------------------------------------
    # 2 (reverse). deposit_settlements + its index
    # ------------------------------------------------------------------
    op.drop_index(
        "ix_deposit_settlements_status", table_name="deposit_settlements"
    )
    op.drop_index(
        "ix_deposit_settlements_lease_id", table_name="deposit_settlements"
    )
    op.drop_table("deposit_settlements")

    # ------------------------------------------------------------------
    # 1 (reverse). move_out_inspections + its indexes
    # ------------------------------------------------------------------
    op.drop_index(
        "ix_move_out_inspections_status", table_name="move_out_inspections"
    )
    op.drop_index(
        "ix_move_out_inspections_lease_id", table_name="move_out_inspections"
    )
    op.drop_index("uq_move_out_active", table_name="move_out_inspections")
    op.drop_table("move_out_inspections")
