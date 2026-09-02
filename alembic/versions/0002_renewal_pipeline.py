"""renewal pipeline 7-stage lifecycle.

Issue #112 GAP-R1: extend the V1 renewal table to carry the frozen
7-stage lifecycle

    DETECT_EXPIRY → CONTACT_TENANT → TENANT_RESPONSE →
    OWNER_DECISION → EXECUTE → VERIFY → CLOSED

The migration is strictly additive:

- Extend the existing ``ck_v1_lease_renewals_state`` CHECK constraint
  to include the 6 new state literals (DETECT_EXPIRY, CONTACT_TENANT,
  TENANT_RESPONSE, OWNER_DECISION, VERIFY, CLOSED).
- Loosen the existing ``uq_v1_lease_renewals_org_idempotency_key`` to
  ``NULLS DISTINCT`` semantics: ``idempotency_key`` becomes nullable
  because 7-stage ``detect_upcoming``-created rows are deduplicated
  by ``scan_key`` instead. The unique constraint itself is preserved
  (it still bites when ``idempotency_key`` is non-NULL).
- Add nullable columns that the 7-stage pipeline writes:
  scan_window_days, scan_key, contact_method, contacted_at,
  tenant_response, tenant_response_at, owner_decision,
  owner_decision_at, verified_at, verified_by_user_id, closed_at,
  closed_by_user_id.
- Add the additive ``ck_v1_lease_renewals_scan_window_positive``
  CHECK.
- Add a new UNIQUE index ``uq_v1_lease_renewals_scan_key`` on
  ``(org_id, source_lease_id, scan_window_days)`` — PostgreSQL
  default ``NULLS DISTINCT`` semantics ensure propose-created rows
  with NULL ``scan_window_days`` are unaffected (they were never
  affected — they remain so).
- Add ``ix_v1_lease_renewals_org_window`` to speed up the scan-by-
  window query path.

The legacy rows survive untouched: every new column is NULL, every
existing row passes the extended CHECK, the original idempotency_key
UNIQUE still bites. ``alembic downgrade -1`` cleanly reverses the
operation. No data is dropped; the new unique index drops with the
table.

Revision ID: 0002_renewal_pipeline
Revises: 0001_baseline
Create Date: 2026-09-02 06:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0002_renewal_pipeline"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Apply the additive 7-stage pipeline migration."""
    # 1) Drop the old state CHECK and replace with the 11-state list.
    op.drop_constraint(
        "ck_v1_lease_renewals_state", "v1_lease_renewals",
        type_="check",
    )
    op.create_check_constraint(
        "ck_v1_lease_renewals_state",
        "v1_lease_renewals",
        "state IN ("
        "'PROPOSED','APPROVED','REJECTED','EXECUTED',"
        "'DETECT_EXPIRY','CONTACT_TENANT','TENANT_RESPONSE',"
        "'OWNER_DECISION','EXECUTE','VERIFY','CLOSED',"
        "'CANCELLED'"
        ")",
    )

    # 2) Loosen idempotency_key to nullable. The UNIQUE constraint
    #    already allows multiple NULLs in PostgreSQL (NULLs distinct
    #    by default), so legacy rows are unaffected. We do not need to
    #    drop the unique constraint itself.
    op.alter_column(
        "v1_lease_renewals", "idempotency_key",
        existing_type=sa.String(length=128),
        nullable=True,
    )
    op.alter_column(
        "v1_lease_renewals", "payload_hash",
        existing_type=sa.String(length=64),
        nullable=False,
        server_default="",
    )

    # 3) Add the new nullable columns used by the 7-stage pipeline.
    op.add_column(
        "v1_lease_renewals",
        sa.Column("scan_window_days", sa.Integer(), nullable=True),
    )
    op.add_column(
        "v1_lease_renewals",
        sa.Column("scan_key", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "v1_lease_renewals",
        sa.Column("contact_method", sa.String(length=40), nullable=True),
    )
    op.add_column(
        "v1_lease_renewals",
        sa.Column(
            "contacted_at", sa.DateTime(timezone=True), nullable=True,
        ),
    )
    op.add_column(
        "v1_lease_renewals",
        sa.Column("tenant_response", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "v1_lease_renewals",
        sa.Column(
            "tenant_response_at", sa.DateTime(timezone=True), nullable=True,
        ),
    )
    op.add_column(
        "v1_lease_renewals",
        sa.Column("owner_decision", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "v1_lease_renewals",
        sa.Column(
            "owner_decision_at", sa.DateTime(timezone=True), nullable=True,
        ),
    )
    op.add_column(
        "v1_lease_renewals",
        sa.Column(
            "verified_at", sa.DateTime(timezone=True), nullable=True,
        ),
    )
    op.add_column(
        "v1_lease_renewals",
        sa.Column(
            "verified_by_user_id", sa.BigInteger(),
            sa.ForeignKey("v1_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "v1_lease_renewals",
        sa.Column(
            "closed_at", sa.DateTime(timezone=True), nullable=True,
        ),
    )
    op.add_column(
        "v1_lease_renewals",
        sa.Column(
            "closed_by_user_id", sa.BigInteger(),
            sa.ForeignKey("v1_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # 4) New CHECK constraint guaranteeing scan_window_days > 0 when
    #    provided (NULL passes).
    op.create_check_constraint(
        "ck_v1_lease_renewals_scan_window_positive",
        "v1_lease_renewals",
        "scan_window_days IS NULL OR scan_window_days > 0",
    )

    # 5) Scan idempotency UNIQUE. PostgreSQL treats NULLs as distinct
    #    in UNIQUE indexes by default, so propose-created rows with
    #    NULL scan_window_days are unaffected.
    op.create_unique_constraint(
        "uq_v1_lease_renewals_scan_key",
        "v1_lease_renewals",
        ["org_id", "source_lease_id", "scan_window_days"],
    )

    # 6) Composite index for the scan-by-window query path.
    op.create_index(
        "ix_v1_lease_renewals_org_window",
        "v1_lease_renewals",
        ["org_id", "scan_window_days"],
    )


def downgrade() -> None:
    """Reverse the additive 7-stage pipeline migration."""
    op.drop_index(
        "ix_v1_lease_renewals_org_window", table_name="v1_lease_renewals",
    )
    op.drop_constraint(
        "uq_v1_lease_renewals_scan_key", "v1_lease_renewals",
        type_="unique",
    )
    op.drop_constraint(
        "ck_v1_lease_renewals_scan_window_positive",
        "v1_lease_renewals",
        type_="check",
    )
    op.drop_column("v1_lease_renewals", "closed_by_user_id")
    op.drop_column("v1_lease_renewals", "closed_at")
    op.drop_column("v1_lease_renewals", "verified_by_user_id")
    op.drop_column("v1_lease_renewals", "verified_at")
    op.drop_column("v1_lease_renewals", "owner_decision_at")
    op.drop_column("v1_lease_renewals", "owner_decision")
    op.drop_column("v1_lease_renewals", "tenant_response_at")
    op.drop_column("v1_lease_renewals", "tenant_response")
    op.drop_column("v1_lease_renewals", "contacted_at")
    op.drop_column("v1_lease_renewals", "contact_method")
    op.drop_column("v1_lease_renewals", "scan_key")
    op.drop_column("v1_lease_renewals", "scan_window_days")

    op.alter_column(
        "v1_lease_renewals", "payload_hash",
        existing_type=sa.String(length=64),
        nullable=False,
        server_default=None,
    )
    op.alter_column(
        "v1_lease_renewals", "idempotency_key",
        existing_type=sa.String(length=128),
        nullable=False,
    )

    op.drop_constraint(
        "ck_v1_lease_renewals_state", "v1_lease_renewals",
        type_="check",
    )
    op.create_check_constraint(
        "ck_v1_lease_renewals_state",
        "v1_lease_renewals",
        "state IN ('PROPOSED','APPROVED','REJECTED','EXECUTED','CANCELLED')",
    )
