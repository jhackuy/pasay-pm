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


def downgrade() -> None:
    op.drop_table("v1_leases")
    op.drop_table("v1_tenants")
    op.drop_table("v1_units")
    op.drop_table("v1_properties")
    op.drop_table("v1_api_credentials")
    op.drop_table("v1_memberships")
    op.drop_table("v1_users")
    op.drop_table("v1_organizations")