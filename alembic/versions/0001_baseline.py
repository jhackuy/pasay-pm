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
        "v1_secretary_invites",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "org_id", sa.BigInteger(),
            sa.ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "invited_by_user_id", sa.BigInteger(),
            sa.ForeignKey("v1_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("invite_token", sa.String(length=64), nullable=False),
        sa.Column("invitee_username", sa.String(length=64), nullable=True),
        sa.Column("invitee_telegram_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "role", sa.String(length=16), nullable=False, server_default="SECRETARY",
        ),
        sa.Column(
            "state", sa.String(length=16), nullable=False, server_default="PENDING",
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "accepted_by_user_id", sa.BigInteger(),
            sa.ForeignKey("v1_users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "invite_token", name="uq_v1_secretary_invites_token",
        ),
        sa.CheckConstraint(
            "state IN ('PENDING','ACCEPTED','CANCELLED','EXPIRED')",
            name="ck_v1_secretary_invites_state",
        ),
    )
    op.create_index(
        "ix_v1_secretary_invites_org_id",
        "v1_secretary_invites", ["org_id"],
    )
    op.create_index(
        "ix_v1_secretary_invites_state",
        "v1_secretary_invites", ["state"],
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
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
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
        "v1_unit_lifecycle_events",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "unit_id", sa.BigInteger(),
            sa.ForeignKey("v1_units.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "org_id", sa.BigInteger(),
            sa.ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("from_state", sa.String(length=32), nullable=True),
        sa.Column("to_state", sa.String(length=32), nullable=True),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column(
            "actor_user_id", sa.BigInteger(),
            sa.ForeignKey("v1_users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.CheckConstraint(
            "kind IN ('STATUS_CHANGE','RENT_CHANGE','ARCHIVED',"
            "'MAINTENANCE_START','MAINTENANCE_END')",
            name="ck_v1_unit_lifecycle_events_kind",
        ),
    )
    op.create_index(
        "ix_v1_unit_lifecycle_events_unit_id",
        "v1_unit_lifecycle_events", ["unit_id"],
    )
    op.create_index(
        "ix_v1_unit_lifecycle_events_org_id",
        "v1_unit_lifecycle_events", ["org_id"],
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
            "contact_status", sa.String(length=16), nullable=False,
            server_default="PENDING",
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
        sa.CheckConstraint(
            "contact_status IN ('PENDING','REPLIED','WRONG_NUMBER','DISCONNECTED','NO_ANSWER')",
            name="ck_v1_leases_contact_status",
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
            "status IN ("
            "'OPEN','SUBMITTED','VERIFIED','SETTLED','FAILED','CANCELLED'"
            ")",
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
            "reversed_by_verification_id", sa.BigInteger(),
            sa.ForeignKey(
                "v1_expense_verifications.id", ondelete="RESTRICT",
            ),
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
    op.create_index(
        "ix_v1_expense_verifications_reversed_by",
        "v1_expense_verifications", ["reversed_by_verification_id"],
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

    # ---- v1_repair_reports -------------------------------------------------
    op.create_table(
        "v1_repair_reports",
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
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column(
            "reported_by_user_id", sa.BigInteger(),
            sa.ForeignKey("v1_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "reported_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("technician_name", sa.String(length=200), nullable=True),
        sa.Column("technician_source", sa.String(length=16), nullable=True),
        sa.Column(
            "technician_eta_at", sa.DateTime(timezone=True), nullable=True,
        ),
        sa.Column("quoted_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("linked_expense_payment_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "completed_at", sa.DateTime(timezone=True), nullable=True,
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
            "state IN ("
            "'REPORTED','CONFIRMED','AWAITING_TECHNICIAN','QUOTE_REQUESTED',"
            "'QUOTE_RECEIVED','QUOTE_APPROVED','IN_PROGRESS',"
            "'COMPLETION_CLAIMED','COMPLETED','CANCELLED'"
            ")",
            name="ck_v1_repair_reports_state",
        ),
        sa.CheckConstraint(
            "category IN ("
            "'PLUMBING','ELECTRICAL','APPLIANCE','STRUCTURAL',"
            "'PEST','HVAC','OTHER'"
            ")",
            name="ck_v1_repair_reports_category",
        ),
        sa.CheckConstraint(
            "severity IN ('LOW','MEDIUM','HIGH','URGENT')",
            name="ck_v1_repair_reports_severity",
        ),
        sa.CheckConstraint(
            "linked_expense_payment_id IS NULL "
            "OR linked_expense_payment_id > 0",
            name="ck_v1_repair_reports_linked_expense_positive",
        ),
        sa.UniqueConstraint(
            "org_id", "idempotency_key",
            name="uq_v1_repair_reports_org_idempotency_key",
        ),
    )
    op.create_index(
        "ix_v1_repair_reports_org_id", "v1_repair_reports", ["org_id"],
    )
    op.create_index(
        "ix_v1_repair_reports_org_state",
        "v1_repair_reports", ["org_id", "state"],
    )
    op.create_index(
        "ix_v1_repair_reports_unit_id", "v1_repair_reports", ["unit_id"],
    )

    # ---- v1_repair_quotes --------------------------------------------------
    op.create_table(
        "v1_repair_quotes",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True,
        ),
        sa.Column(
            "org_id", sa.BigInteger(),
            sa.ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "report_id", sa.BigInteger(),
            sa.ForeignKey("v1_repair_reports.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("technician_name", sa.String(length=200), nullable=False),
        sa.Column(
            "submitted_by_user_id", sa.BigInteger(),
            sa.ForeignKey("v1_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "decided_by_user_id", sa.BigInteger(),
            sa.ForeignKey("v1_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "decided_at", sa.DateTime(timezone=True), nullable=True,
        ),
        sa.Column("reason", sa.String(length=500), nullable=True),
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
            "decision IN ('SUBMITTED','APPROVED','REJECTED')",
            name="ck_v1_repair_quotes_decision",
        ),
        sa.CheckConstraint(
            "amount > 0", name="ck_v1_repair_quotes_amount_positive",
        ),
        sa.UniqueConstraint(
            "org_id", "idempotency_key",
            name="uq_v1_repair_quotes_org_idempotency_key",
        ),
    )
    op.create_index(
        "ix_v1_repair_quotes_org_id", "v1_repair_quotes", ["org_id"],
    )
    op.create_index(
        "ix_v1_repair_quotes_report_id", "v1_repair_quotes", ["report_id"],
    )

    # ---- v1_repair_works ---------------------------------------------------
    op.create_table(
        "v1_repair_works",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True,
        ),
        sa.Column(
            "org_id", sa.BigInteger(),
            sa.ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "report_id", sa.BigInteger(),
            sa.ForeignKey("v1_repair_reports.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("note", sa.String(length=2000), nullable=False),
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
        sa.CheckConstraint(
            "state IN ('STARTED','BLOCKED','PROGRESS','DONE_ON_SITE')",
            name="ck_v1_repair_works_state",
        ),
    )
    op.create_index(
        "ix_v1_repair_works_org_id", "v1_repair_works", ["org_id"],
    )
    op.create_index(
        "ix_v1_repair_works_report_id", "v1_repair_works", ["report_id"],
    )

    # ---- v1_repair_completion_claims --------------------------------------
    op.create_table(
        "v1_repair_completion_claims",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True,
        ),
        sa.Column(
            "org_id", sa.BigInteger(),
            sa.ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "report_id", sa.BigInteger(),
            sa.ForeignKey("v1_repair_reports.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("summary", sa.String(length=2000), nullable=False),
        sa.Column(
            "claimed_by_user_id", sa.BigInteger(),
            sa.ForeignKey("v1_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "claimed_at", sa.DateTime(timezone=True), nullable=False,
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
        "ix_v1_repair_completion_claims_org_id",
        "v1_repair_completion_claims", ["org_id"],
    )
    op.create_index(
        "ix_v1_repair_completion_claims_report_id",
        "v1_repair_completion_claims", ["report_id"],
    )

    # ---- v1_repair_verifications ------------------------------------------
    op.create_table(
        "v1_repair_verifications",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True,
        ),
        sa.Column(
            "org_id", sa.BigInteger(),
            sa.ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "report_id", sa.BigInteger(),
            sa.ForeignKey("v1_repair_reports.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("decision", sa.String(length=16), nullable=False),
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
            "reversed_by_verification_id", sa.BigInteger(),
            sa.ForeignKey("v1_repair_verifications.id", ondelete="RESTRICT"),
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
            "decision IN ('VERIFIED','REJECTED','REVERSED')",
            name="ck_v1_repair_verifications_decision",
        ),
    )
    op.create_index(
        "ix_v1_repair_verifications_org_id",
        "v1_repair_verifications", ["org_id"],
    )
    op.create_index(
        "ix_v1_repair_verifications_report_id",
        "v1_repair_verifications", ["report_id"],
    )
    op.create_index(
        "ix_v1_repair_verifications_reversed_by",
        "v1_repair_verifications", ["reversed_by_verification_id"],
    )

    # ---- v1_repair_activities ---------------------------------------------
    op.create_table(
        "v1_repair_activities",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True,
        ),
        sa.Column(
            "org_id", sa.BigInteger(),
            sa.ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "report_id", sa.BigInteger(),
            sa.ForeignKey("v1_repair_reports.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "quote_id", sa.BigInteger(),
            sa.ForeignKey("v1_repair_quotes.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "work_id", sa.BigInteger(),
            sa.ForeignKey("v1_repair_works.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "claim_id", sa.BigInteger(),
            sa.ForeignKey("v1_repair_completion_claims.id", ondelete="RESTRICT"),
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
        "ix_v1_repair_activities_org_id", "v1_repair_activities", ["org_id"],
    )
    op.create_index(
        "ix_v1_repair_activities_report_id",
        "v1_repair_activities", ["report_id"],
    )

    # ---- Lease Renewal ----
    op.create_table(
        "v1_lease_renewals",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "org_id", sa.BigInteger(),
            sa.ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "source_lease_id", sa.BigInteger(),
            sa.ForeignKey("v1_leases.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "new_lease_id", sa.BigInteger(),
            sa.ForeignKey("v1_leases.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "state", sa.String(length=16), nullable=False, server_default="PROPOSED",
        ),
        sa.Column("proposed_start_date", sa.Date(), nullable=False),
        sa.Column("proposed_end_date", sa.Date(), nullable=False),
        sa.Column(
            "proposed_monthly_rent", sa.Numeric(14, 2), nullable=False,
        ),
        sa.Column(
            "proposed_deposit", sa.Numeric(14, 2), nullable=False,
            server_default="0",
        ),
        sa.Column(
            "proposed_by_user_id", sa.BigInteger(),
            sa.ForeignKey("v1_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "proposed_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "decided_by_user_id", sa.BigInteger(),
            sa.ForeignKey("v1_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "decided_at", sa.DateTime(timezone=True), nullable=True,
        ),
        sa.Column(
            "decision_reason", sa.String(length=500), nullable=True,
        ),
        sa.Column(
            "executed_at", sa.DateTime(timezone=True), nullable=True,
        ),
        sa.Column(
            "idempotency_key", sa.String(length=128), nullable=False,
        ),
        sa.Column(
            "payload_hash", sa.String(length=64), nullable=False,
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
            "state IN ('PROPOSED','APPROVED','REJECTED','EXECUTED','CANCELLED')",
            name="ck_v1_lease_renewals_state",
        ),
        sa.CheckConstraint(
            "proposed_end_date > proposed_start_date",
            name="ck_v1_lease_renewals_dates",
        ),
        sa.CheckConstraint(
            "proposed_monthly_rent > 0",
            name="ck_v1_lease_renewals_rent_positive",
        ),
        sa.CheckConstraint(
            "proposed_deposit >= 0",
            name="ck_v1_lease_renewals_deposit_nonneg",
        ),
        sa.UniqueConstraint(
            "org_id", "idempotency_key",
            name="uq_v1_lease_renewals_org_idempotency_key",
        ),
    )
    op.create_index(
        "ix_v1_lease_renewals_org_id", "v1_lease_renewals", ["org_id"],
    )
    op.create_index(
        "ix_v1_lease_renewals_org_state",
        "v1_lease_renewals", ["org_id", "state"],
    )
    op.create_index(
        "ix_v1_lease_renewals_source_lease_id",
        "v1_lease_renewals", ["source_lease_id"],
    )
    op.create_index(
        "ix_v1_lease_renewals_new_lease_id",
        "v1_lease_renewals", ["new_lease_id"],
    )

    op.create_table(
        "v1_renewal_activities",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "org_id", sa.BigInteger(),
            sa.ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "renewal_id", sa.BigInteger(),
            sa.ForeignKey("v1_lease_renewals.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "kind", sa.String(length=40), nullable=False,
        ),
        sa.Column(
            "actor_user_id", sa.BigInteger(),
            sa.ForeignKey("v1_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "detail", sa.String(length=500), nullable=True,
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
        "ix_v1_renewal_activities_org_id", "v1_renewal_activities", ["org_id"],
    )
    op.create_index(
        "ix_v1_renewal_activities_renewal_id",
        "v1_renewal_activities", ["renewal_id"],
    )

    # ---- Move-out / Settlement ----
    op.create_table(
        "v1_move_outs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
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
        sa.Column(
            "state", sa.String(length=16), nullable=False,
            server_default="REQUESTED",
        ),
        sa.Column(
            "requested_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "requested_by_user_id", sa.BigInteger(),
            sa.ForeignKey("v1_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("planned_move_out_date", sa.Date(), nullable=True),
        sa.Column(
            "inspected_at", sa.DateTime(timezone=True), nullable=True,
        ),
        sa.Column(
            "inspected_by_user_id", sa.BigInteger(),
            sa.ForeignKey("v1_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("inspection_notes", sa.String(length=4000), nullable=True),
        sa.Column(
            "settled_at", sa.DateTime(timezone=True), nullable=True,
        ),
        # NOTE: settlement_id references v1_deposit_settlements.id which
        # is created later in this baseline. We declare it as a plain
        # BigInteger here and add the FK as an ALTER TABLE after both
        # tables exist; the application layer still enforces referential
        # integrity via org-scope and the closure-gate flow.
        sa.Column(
            "settlement_id", sa.BigInteger(), nullable=True,
        ),
        sa.Column(
            "cancelled_at", sa.DateTime(timezone=True), nullable=True,
        ),
        sa.Column("cancel_reason", sa.String(length=500), nullable=True),
        sa.Column(
            "idempotency_key", sa.String(length=128), nullable=False,
        ),
        sa.Column(
            "payload_hash", sa.String(length=64), nullable=False,
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
            "state IN ('REQUESTED','INSPECTED','SETTLED','CANCELLED')",
            name="ck_v1_move_outs_state",
        ),
        sa.UniqueConstraint(
            "org_id", "idempotency_key",
            name="uq_v1_move_outs_org_idempotency_key",
        ),
    )
    op.create_index(
        "ix_v1_move_outs_org_id", "v1_move_outs", ["org_id"],
    )
    op.create_index(
        "ix_v1_move_outs_org_state", "v1_move_outs", ["org_id", "state"],
    )
    op.create_index(
        "ix_v1_move_outs_lease_id", "v1_move_outs", ["lease_id"],
    )
    op.create_index(
        "ix_v1_move_outs_settlement_id", "v1_move_outs", ["settlement_id"],
    )

    op.create_table(
        "v1_deposit_settlements",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "org_id", sa.BigInteger(),
            sa.ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "move_out_id", sa.BigInteger(),
            sa.ForeignKey("v1_move_outs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "disposition", sa.String(length=20), nullable=False,
        ),
        sa.Column(
            "deposit_held", sa.Numeric(14, 2), nullable=False,
        ),
        sa.Column(
            "deductions_total", sa.Numeric(14, 2), nullable=False,
            server_default="0",
        ),
        sa.Column(
            "refund_amount", sa.Numeric(14, 2), nullable=False,
            server_default="0",
        ),
        sa.Column(
            "additional_owed", sa.Numeric(14, 2), nullable=False,
            server_default="0",
        ),
        sa.Column("notes", sa.String(length=2000), nullable=True),
        sa.Column(
            "settled_by_user_id", sa.BigInteger(),
            sa.ForeignKey("v1_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "settled_at", sa.DateTime(timezone=True), nullable=False,
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
        sa.CheckConstraint(
            "disposition IN ("
            "'FULL_REFUND','PARTIAL_REFUND','NO_REFUND','ADDITIONAL_OWED'"
            ")",
            name="ck_v1_deposit_settlements_disposition",
        ),
        sa.CheckConstraint(
            "deposit_held >= 0",
            name="ck_v1_deposit_settlements_deposit_held_nonneg",
        ),
        sa.CheckConstraint(
            "deductions_total >= 0",
            name="ck_v1_deposit_settlements_deductions_nonneg",
        ),
        sa.CheckConstraint(
            "refund_amount >= 0",
            name="ck_v1_deposit_settlements_refund_nonneg",
        ),
        sa.CheckConstraint(
            "additional_owed >= 0",
            name="ck_v1_deposit_settlements_additional_owed_nonneg",
        ),
        sa.CheckConstraint(
            "(disposition = 'FULL_REFUND' AND refund_amount = deposit_held "
            "AND additional_owed = 0) OR disposition <> 'FULL_REFUND'",
            name="ck_v1_deposit_settlements_full_refund_amounts",
        ),
        sa.CheckConstraint(
            "(disposition = 'NO_REFUND' AND refund_amount = 0 "
            "AND additional_owed = 0) OR disposition <> 'NO_REFUND'",
            name="ck_v1_deposit_settlements_no_refund_amounts",
        ),
    )
    op.create_index(
        "ix_v1_deposit_settlements_org_id",
        "v1_deposit_settlements", ["org_id"],
    )
    op.create_index(
        "ix_v1_deposit_settlements_move_out_id",
        "v1_deposit_settlements", ["move_out_id"],
    )

    op.create_table(
        "v1_move_out_inspections",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "org_id", sa.BigInteger(),
            sa.ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "move_out_id", sa.BigInteger(),
            sa.ForeignKey("v1_move_outs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "inspected_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "inspected_by_user_id", sa.BigInteger(),
            sa.ForeignKey("v1_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "summary", sa.String(length=4000), nullable=False,
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
        "ix_v1_move_out_inspections_org_id",
        "v1_move_out_inspections", ["org_id"],
    )
    op.create_index(
        "ix_v1_move_out_inspections_move_out_id",
        "v1_move_out_inspections", ["move_out_id"],
    )

    op.create_table(
        "v1_move_out_damages",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "org_id", sa.BigInteger(),
            sa.ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "move_out_id", sa.BigInteger(),
            sa.ForeignKey("v1_move_outs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "kind", sa.String(length=32), nullable=False,
        ),
        sa.Column(
            "description", sa.String(length=2000), nullable=False,
        ),
        sa.Column(
            "amount", sa.Numeric(14, 2), nullable=False,
        ),
        sa.Column(
            "accepted_amount", sa.Numeric(14, 2), nullable=False,
            server_default="0",
        ),
        sa.Column(
            "recorded_by_user_id", sa.BigInteger(),
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
            "kind IN ('CLEANING','REPAIR','REPLACEMENT','UTILITIES','OTHER')",
            name="ck_v1_move_out_damages_kind",
        ),
        sa.CheckConstraint(
            "amount >= 0",
            name="ck_v1_move_out_damages_amount_nonneg",
        ),
        sa.CheckConstraint(
            "accepted_amount >= 0",
            name="ck_v1_move_out_damages_accepted_nonneg",
        ),
        sa.CheckConstraint(
            "accepted_amount <= amount",
            name="ck_v1_move_out_damages_accepted_le_amount",
        ),
    )
    op.create_index(
        "ix_v1_move_out_damages_org_id", "v1_move_out_damages", ["org_id"],
    )
    op.create_index(
        "ix_v1_move_out_damages_move_out_id",
        "v1_move_out_damages", ["move_out_id"],
    )

    op.create_table(
        "v1_move_out_activities",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "org_id", sa.BigInteger(),
            sa.ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "move_out_id", sa.BigInteger(),
            sa.ForeignKey("v1_move_outs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "kind", sa.String(length=32), nullable=False,
        ),
        sa.Column(
            "actor_user_id", sa.BigInteger(),
            sa.ForeignKey("v1_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "detail", sa.String(length=500), nullable=True,
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
        "ix_v1_move_out_activities_org_id",
        "v1_move_out_activities", ["org_id"],
    )
    op.create_index(
        "ix_v1_move_out_activities_move_out_id",
        "v1_move_out_activities", ["move_out_id"],
    )

    # Now that both tables exist, add the circular FK between
    # v1_move_outs.settlement_id and v1_deposit_settlements.id.
    op.create_foreign_key(
        "fk_v1_move_outs_settlement_id",
        "v1_move_outs", "v1_deposit_settlements",
        ["settlement_id"], ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_table("v1_move_out_activities")
    op.drop_table("v1_move_out_damages")
    op.drop_table("v1_move_out_inspections")
    # Drop the FK from v1_move_outs.settlement_id before dropping
    # v1_deposit_settlements.
    op.drop_constraint(
        "fk_v1_move_outs_settlement_id", "v1_move_outs", type_="foreignkey",
    )
    op.drop_table("v1_deposit_settlements")
    op.drop_table("v1_move_outs")
    op.drop_table("v1_renewal_activities")
    op.drop_table("v1_lease_renewals")
    op.drop_table("v1_repair_activities")
    op.drop_table("v1_repair_verifications")
    op.drop_table("v1_repair_completion_claims")
    op.drop_table("v1_repair_works")
    op.drop_table("v1_repair_quotes")
    op.drop_table("v1_repair_reports")
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
    op.drop_table("v1_unit_lifecycle_events")
    op.drop_table("v1_units")
    op.drop_table("v1_properties")
    op.drop_table("v1_secretary_invites")
    op.drop_table("v1_api_credentials")
    op.drop_table("v1_memberships")
    op.drop_table("v1_users")
    op.drop_table("v1_organizations")