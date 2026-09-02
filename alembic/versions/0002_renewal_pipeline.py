"""renewal_pipeline

Issue #112 / PR #112 ``feat(renewal): 7-stage lifecycle pipeline``.

Bounded extension to the V1 ``v1_lease_renewals`` schema to support the
frozen Issue #112 §"Lease Renewal" lifecycle::

    DETECT_EXPIRY → CONTACT_TENANT → TENANT_RESPONSE
        → OWNER_DECISION → EXECUTE → VERIFY → CLOSED

The migration is additive and non-destructive:

1. ``v1_lease_renewals.state`` CHECK constraint is extended to allow
   the six new state values. Pre-existing rows are unaffected (all
   legacy rows have state in {PROPOSED, APPROVED, REJECTED, EXECUTED,
   CANCELLED}, all of which remain legal).

2. Six nullable columns are added to ``v1_lease_renewals`` for the new
   pipeline (scan-window metadata, tenant response, owner decision,
   verification, closure). All default NULL; legacy rows are
   unaffected.

3. A unique idempotency index is added on
   ``(org_id, source_lease_id, scan_window_days)`` so the new
   ``detect_upcoming`` entry point never duplicates a renewal for the
   same lease within the same window. PostgreSQL treats NULL
   ``scan_window_days`` values as distinct, so legacy rows (NULL)
   remain unrestricted.

Migration safety:

- ``alembic upgrade head`` succeeds (verified locally with
  PostgreSQL 16).
- ``alembic downgrade -1`` succeeds (reverses every step).
- Alembic single head is preserved (no chain branches).
- No DROP / ALTER on a column with semantic meaning; all changes are
  additive.

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
    # ---- 1. extend the renewal state CHECK constraint -------------------
    op.drop_constraint(
        "ck_v1_lease_renewals_state",
        "v1_lease_renewals",
        type_="check",
    )
    op.create_check_constraint(
        "ck_v1_lease_renewals_state",
        "v1_lease_renewals",
        (
            "state IN ("
            "'PROPOSED','APPROVED','REJECTED','EXECUTED','CANCELLED',"
            "'DETECT_EXPIRY','CONTACT_TENANT','TENANT_RESPONSE',"
            "'OWNER_DECISION','VERIFY','CLOSED'"
            ")"
        ),
    )

    # ---- 2. add the new pipeline columns (all nullable, default NULL) --
    op.add_column(
        "v1_lease_renewals",
        sa.Column(
            "scan_window_days",
            sa.BigInteger(),
            nullable=True,
        ),
    )
    op.add_column(
        "v1_lease_renewals",
        sa.Column(
            "scan_key",
            sa.String(length=160),
            nullable=True,
        ),
    )
    op.add_column(
        "v1_lease_renewals",
        sa.Column(
            "tenant_response",
            sa.String(length=16),
            nullable=True,
        ),
    )
    op.add_column(
        "v1_lease_renewals",
        sa.Column(
            "tenant_response_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "v1_lease_renewals",
        sa.Column(
            "owner_decision",
            sa.String(length=16),
            nullable=True,
        ),
    )
    op.add_column(
        "v1_lease_renewals",
        sa.Column(
            "owner_decision_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "v1_lease_renewals",
        sa.Column(
            "verified_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "v1_lease_renewals",
        sa.Column(
            "verified_by_user_id",
            sa.BigInteger(),
            sa.ForeignKey("v1_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "v1_lease_renewals",
        sa.Column(
            "closed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "v1_lease_renewals",
        sa.Column(
            "closed_by_user_id",
            sa.BigInteger(),
            sa.ForeignKey("v1_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # ---- 3. new check + idempotency surface ----------------------------
    op.create_check_constraint(
        "ck_v1_lease_renewals_scan_window_positive",
        "v1_lease_renewals",
        "scan_window_days IS NULL OR scan_window_days > 0",
    )
    op.create_index(
        "uq_v1_lease_renewals_org_source_scan",
        "v1_lease_renewals",
        ["org_id", "source_lease_id", "scan_window_days"],
        unique=True,
    )


def downgrade() -> None:
    # Reverse in strict opposite order so we never leave the table in a
    # state that violates the new constraint.
    op.drop_index(
        "uq_v1_lease_renewals_org_source_scan",
        table_name="v1_lease_renewals",
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
    op.drop_column("v1_lease_renewals", "scan_key")
    op.drop_column("v1_lease_renewals", "scan_window_days")

    # Narrow the state CHECK back to the legacy 5-state vocabulary.
    op.drop_constraint(
        "ck_v1_lease_renewals_state",
        "v1_lease_renewals",
        type_="check",
    )
    op.create_check_constraint(
        "ck_v1_lease_renewals_state",
        "v1_lease_renewals",
        (
            "state IN ("
            "'PROPOSED','APPROVED','REJECTED','EXECUTED','CANCELLED'"
            ")"
        ),
    )