"""baseline

Issue #99 / PR #100 single fresh baseline for empty PostgreSQL 16.

Covers the V1 schema: Organization, User, Membership, ApiCredential,
Property, Unit, Tenant, Lease. Replaces the legacy 30-file chain.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-08-29 12:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "v1_organizations",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True,
        ),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("name", name="uq_v1_organizations_name"),
    )

    op.create_table(
        "v1_users",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True,
        ),
        sa.Column("telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("display_name", sa.String(length=120), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("telegram_id", name="uq_v1_users_telegram_id"),
    )

    op.create_table(
        "v1_memberships",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True,
        ),
        sa.Column(
            "org_id", sa.BigInteger(),
            sa.ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "user_id", sa.BigInteger(),
            sa.ForeignKey("v1_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column(
            "state", sa.String(length=16), nullable=False,
            server_default="ACTIVE",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "org_id", "user_id", name="uq_v1_memberships_org_user",
        ),
        sa.CheckConstraint(
            "role IN ('owner','secretary','tenant')",
            name="ck_v1_memberships_role",
        ),
        sa.CheckConstraint(
            "state IN ('ACTIVE','SUSPENDED','REMOVED')",
            name="ck_v1_memberships_state",
        ),
    )
    op.create_index(
        "ix_v1_memberships_org_id", "v1_memberships", ["org_id"],
    )
    op.create_index(
        "ix_v1_memberships_user_id", "v1_memberships", ["user_id"],
    )

    op.create_table(
        "v1_api_credentials",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True,
        ),
        sa.Column(
            "user_id", sa.BigInteger(),
            sa.ForeignKey("v1_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "key_hash", name="uq_v1_api_credentials_key_hash",
        ),
    )
    op.create_index(
        "ix_v1_api_credentials_user_id", "v1_api_credentials", ["user_id"],
    )

    op.create_table(
        "v1_properties",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True,
        ),
        sa.Column(
            "org_id", sa.BigInteger(),
            sa.ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column(
            "address_line1", sa.String(length=200), nullable=True,
        ),
        sa.Column(
            "address_line2", sa.String(length=200), nullable=True,
        ),
        sa.Column("city", sa.String(length=80), nullable=True),
        sa.Column("region", sa.String(length=80), nullable=True),
        sa.Column("postal_code", sa.String(length=20), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_v1_properties_org_id", "v1_properties", ["org_id"],
    )

    op.create_table(
        "v1_units",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True,
        ),
        sa.Column(
            "property_id", sa.BigInteger(),
            sa.ForeignKey("v1_properties.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "org_id", sa.BigInteger(),
            sa.ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("label", sa.String(length=80), nullable=False),
        sa.Column(
            "bedrooms", sa.Integer(), nullable=False, server_default="0",
        ),
        sa.Column(
            "bathrooms", sa.Integer(), nullable=False, server_default="0",
        ),
        sa.Column(
            "monthly_rent", sa.Numeric(14, 2), nullable=False,
        ),
        sa.Column(
            "status", sa.String(length=16), nullable=False,
            server_default="AVAILABLE",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('AVAILABLE','OCCUPIED','MAINTENANCE','RETIRED')",
            name="ck_v1_units_status",
        ),
    )
    op.create_index(
        "ix_v1_units_property_id", "v1_units", ["property_id"],
    )
    op.create_index(
        "ix_v1_units_org_id", "v1_units", ["org_id"],
    )

    op.create_table(
        "v1_tenants",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True,
        ),
        sa.Column(
            "org_id", sa.BigInteger(),
            sa.ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "user_id", sa.BigInteger(),
            sa.ForeignKey("v1_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("full_name", sa.String(length=120), nullable=False),
        sa.Column(
            "contact_phone", sa.String(length=32), nullable=True,
        ),
        sa.Column(
            "contact_email", sa.String(length=120), nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_v1_tenants_org_id", "v1_tenants", ["org_id"],
    )

    op.create_table(
        "v1_leases",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True,
        ),
        sa.Column(
            "org_id", sa.BigInteger(),
            sa.ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "unit_id", sa.BigInteger(),
            sa.ForeignKey("v1_units.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id", sa.BigInteger(),
            sa.ForeignKey("v1_tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column(
            "monthly_rent", sa.Numeric(14, 2), nullable=False,
        ),
        sa.Column(
            "deposit", sa.Numeric(14, 2), nullable=False,
            server_default="0",
        ),
        sa.Column(
            "state", sa.String(length=16), nullable=False,
            server_default="DRAFT",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "state IN ('DRAFT','ACTIVE','TERMINATED','EXPIRED')",
            name="ck_v1_leases_state",
        ),
    )
    op.create_index(
        "ix_v1_leases_org_id", "v1_leases", ["org_id"],
    )
    op.create_index(
        "ix_v1_leases_unit_id", "v1_leases", ["unit_id"],
    )
    op.create_index(
        "ix_v1_leases_tenant_id", "v1_leases", ["tenant_id"],
    )

    op.create_table(
        "v1_operations",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True,
        ),
        sa.Column(
            "org_id", sa.BigInteger(),
            sa.ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("subject_type", sa.String(length=32), nullable=False),
        sa.Column("subject_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "state", sa.String(length=16), nullable=False,
            server_default="open",
        ),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "state IN ('open','in_progress','resolved','cancelled')",
            name="ck_v1_operations_state",
        ),
        sa.UniqueConstraint(
            "org_id", "kind", "subject_type", "subject_id",
            name="uq_v1_operations_org_kind_subject",
        ),
    )
    op.create_index(
        "ix_v1_operations_org_id", "v1_operations", ["org_id"],
    )
    op.create_index(
        "ix_v1_operations_org_subject", "v1_operations",
        ["org_id", "subject_type", "subject_id"],
    )

    op.create_table(
        "v1_tasks",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True,
        ),
        sa.Column(
            "org_id", sa.BigInteger(),
            sa.ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "operation_id", sa.BigInteger(),
            sa.ForeignKey("v1_operations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column(
            "state", sa.String(length=16), nullable=False,
            server_default="open",
        ),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "state IN ('open','done','cancelled')",
            name="ck_v1_tasks_state",
        ),
    )
    op.create_index("ix_v1_tasks_org_id", "v1_tasks", ["org_id"])
    op.create_index(
        "ix_v1_tasks_operation_id", "v1_tasks", ["operation_id"],
    )
    # Operation is Truth, Task is Projection: at most ONE open task per
    # operation, enforced by the database rather than by convention.
    op.create_index(
        "uq_v1_tasks_one_open_per_operation",
        "v1_tasks",
        ["operation_id"],
        unique=True,
        postgresql_where=sa.text("state = 'open'"),
    )

    op.create_table(
        "v1_rent_due_schedules",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True,
        ),
        sa.Column(
            "org_id", sa.BigInteger(),
            sa.ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "lease_id", sa.BigInteger(),
            sa.ForeignKey("v1_leases.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("amount_due", sa.Numeric(14, 2), nullable=False),
        sa.Column(
            "state", sa.String(length=16), nullable=False,
            server_default="DUE",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "state IN ('DUE','OVERDUE','PAID','CANCELLED')",
            name="ck_v1_rent_due_schedules_state",
        ),
        sa.CheckConstraint(
            "amount_due > 0",
            name="ck_v1_rent_due_schedules_amount_positive",
        ),
        sa.CheckConstraint(
            "due_date >= period_start",
            name="ck_v1_rent_due_schedules_dates",
        ),
        sa.UniqueConstraint(
            "lease_id", "period_start",
            name="uq_v1_rent_due_schedules_lease_period",
        ),
    )
    op.create_index(
        "ix_v1_rent_due_schedules_org_id",
        "v1_rent_due_schedules", ["org_id"],
    )
    op.create_index(
        "ix_v1_rent_due_schedules_org_due_date",
        "v1_rent_due_schedules", ["org_id", "due_date"],
    )
    op.create_index(
        "ix_v1_rent_due_schedules_lease_id",
        "v1_rent_due_schedules", ["lease_id"],
    )

    op.create_table(
        "v1_rent_payments",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True,
        ),
        sa.Column(
            "org_id", sa.BigInteger(),
            sa.ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "due_schedule_id", sa.BigInteger(),
            sa.ForeignKey("v1_rent_due_schedules.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("claimed_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("verified_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column(
            "status", sa.String(length=16), nullable=False,
            server_default="PENDING",
        ),
        sa.Column(
            "claimed_by_user_id", sa.BigInteger(),
            sa.ForeignKey("v1_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "claimed_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','VERIFIED','FAILED','REVERSED')",
            name="ck_v1_rent_payments_status",
        ),
        sa.CheckConstraint(
            "claimed_amount > 0",
            name="ck_v1_rent_payments_claimed_positive",
        ),
        sa.CheckConstraint(
            "verified_amount IS NULL OR verified_amount > 0",
            name="ck_v1_rent_payments_verified_positive",
        ),
        sa.UniqueConstraint(
            "org_id", "idempotency_key",
            name="uq_v1_rent_payments_org_idempotency_key",
        ),
    )
    op.create_index(
        "ix_v1_rent_payments_org_id", "v1_rent_payments", ["org_id"],
    )
    op.create_index(
        "ix_v1_rent_payments_due_schedule_id",
        "v1_rent_payments", ["due_schedule_id"],
    )

    op.create_table(
        "v1_rent_evidences",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True,
        ),
        sa.Column(
            "org_id", sa.BigInteger(),
            sa.ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "rent_payment_id", sa.BigInteger(),
            sa.ForeignKey("v1_rent_payments.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("reference", sa.String(length=500), nullable=False),
        sa.Column(
            "uploaded_by_user_id", sa.BigInteger(),
            sa.ForeignKey("v1_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "kind IN ('PHOTO','DOCUMENT','TEXT','TELEGRAM_FILE')",
            name="ck_v1_rent_evidences_kind",
        ),
    )
    op.create_index(
        "ix_v1_rent_evidences_org_id", "v1_rent_evidences", ["org_id"],
    )
    op.create_index(
        "ix_v1_rent_evidences_rent_payment_id",
        "v1_rent_evidences", ["rent_payment_id"],
    )

    op.create_table(
        "v1_rent_verifications",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True,
        ),
        sa.Column(
            "org_id", sa.BigInteger(),
            sa.ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "rent_payment_id", sa.BigInteger(),
            sa.ForeignKey("v1_rent_payments.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("verified_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column(
            "verifier_user_id", sa.BigInteger(),
            sa.ForeignKey("v1_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "decided_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "decision IN ('VERIFIED','REJECTED','REVERSED')",
            name="ck_v1_rent_verifications_decision",
        ),
    )
    op.create_index(
        "ix_v1_rent_verifications_org_id",
        "v1_rent_verifications", ["org_id"],
    )
    op.create_index(
        "ix_v1_rent_verifications_rent_payment_id",
        "v1_rent_verifications", ["rent_payment_id"],
    )

    op.create_table(
        "v1_rent_activities",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True,
        ),
        sa.Column(
            "org_id", sa.BigInteger(),
            sa.ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "due_schedule_id", sa.BigInteger(),
            sa.ForeignKey("v1_rent_due_schedules.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "rent_payment_id", sa.BigInteger(),
            sa.ForeignKey("v1_rent_payments.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("detail", sa.String(length=500), nullable=True),
        sa.Column(
            "actor_user_id", sa.BigInteger(),
            sa.ForeignKey("v1_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_v1_rent_activities_org_id", "v1_rent_activities", ["org_id"],
    )
    op.create_index(
        "ix_v1_rent_activities_due_schedule_id",
        "v1_rent_activities", ["due_schedule_id"],
    )

    # --- expense slice (Operation-is-Truth, Claim/Evidence/Verification separated) ---

    op.create_table(
        "v1_expense_claims",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True,
        ),
        sa.Column(
            "org_id", sa.BigInteger(),
            sa.ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("claimed_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("verified_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="OPEN"),
        sa.Column(
            "opened_by_user_id", sa.BigInteger(),
            sa.ForeignKey("v1_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "opened_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('OPEN','SUBMITTED','VERIFIED','FAILED','CANCELLED')",
            name="ck_v1_expense_claims_status",
        ),
        sa.CheckConstraint(
            "category IN ("
            "'UTILITIES','REPAIRS','SUPPLIES','TAX',"
            "'INSURANCE','SERVICE','OTHER'"
            ")",
            name="ck_v1_expense_claims_category",
        ),
        sa.CheckConstraint(
            "claimed_amount > 0",
            name="ck_v1_expense_claims_claimed_positive",
        ),
        sa.CheckConstraint(
            "verified_amount IS NULL OR verified_amount > 0",
            name="ck_v1_expense_claims_verified_positive",
        ),
        sa.UniqueConstraint(
            "org_id", "idempotency_key",
            name="uq_v1_expense_claims_org_idempotency_key",
        ),
    )
    op.create_index(
        "ix_v1_expense_claims_org_id", "v1_expense_claims", ["org_id"],
    )
    op.create_index(
        "ix_v1_expense_claims_org_status",
        "v1_expense_claims", ["org_id", "status"],
    )

    op.create_table(
        "v1_expense_receipts",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True,
        ),
        sa.Column(
            "org_id", sa.BigInteger(),
            sa.ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "claim_id", sa.BigInteger(),
            sa.ForeignKey("v1_expense_claims.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("reference", sa.String(length=500), nullable=False),
        sa.Column(
            "uploaded_by_user_id", sa.BigInteger(),
            sa.ForeignKey("v1_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "kind IN ('PHOTO','DOCUMENT','TEXT','TELEGRAM_FILE')",
            name="ck_v1_expense_receipts_kind",
        ),
    )
    op.create_index(
        "ix_v1_expense_receipts_org_id", "v1_expense_receipts", ["org_id"],
    )
    op.create_index(
        "ix_v1_expense_receipts_claim_id",
        "v1_expense_receipts", ["claim_id"],
    )

    op.create_table(
        "v1_expense_verifications",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True,
        ),
        sa.Column(
            "org_id", sa.BigInteger(),
            sa.ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "claim_id", sa.BigInteger(),
            sa.ForeignKey("v1_expense_claims.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("verified_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column(
            "verifier_user_id", sa.BigInteger(),
            sa.ForeignKey("v1_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "decided_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "decision IN ('VERIFIED','REJECTED','REVERSED')",
            name="ck_v1_expense_verifications_decision",
        ),
    )
    op.create_index(
        "ix_v1_expense_verifications_org_id",
        "v1_expense_verifications", ["org_id"],
    )
    op.create_index(
        "ix_v1_expense_verifications_claim_id",
        "v1_expense_verifications", ["claim_id"],
    )

    op.create_table(
        "v1_expense_activities",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True,
        ),
        sa.Column(
            "org_id", sa.BigInteger(),
            sa.ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "claim_id", sa.BigInteger(),
            sa.ForeignKey("v1_expense_claims.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "receipt_id", sa.BigInteger(),
            sa.ForeignKey("v1_expense_receipts.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("detail", sa.String(length=500), nullable=True),
        sa.Column(
            "actor_user_id", sa.BigInteger(),
            sa.ForeignKey("v1_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_v1_expense_activities_org_id",
        "v1_expense_activities", ["org_id"],
    )
    op.create_index(
        "ix_v1_expense_activities_claim_id",
        "v1_expense_activities", ["claim_id"],
    )


def downgrade() -> None:
    op.drop_table("v1_expense_activities")
    op.drop_table("v1_expense_verifications")
    op.drop_table("v1_expense_receipts")
    op.drop_table("v1_expense_claims")
    op.drop_table("v1_rent_activities")
    op.drop_table("v1_rent_verifications")
    op.drop_table("v1_rent_evidences")
    op.drop_table("v1_rent_payments")
    op.drop_table("v1_rent_due_schedules")
    op.drop_table("v1_tasks")
    op.drop_table("v1_operations")
    op.drop_table("v1_leases")
    op.drop_table("v1_tenants")
    op.drop_table("v1_units")
    op.drop_table("v1_properties")
    op.drop_table("v1_api_credentials")
    op.drop_table("v1_memberships")
    op.drop_table("v1_users")
    op.drop_table("v1_organizations")