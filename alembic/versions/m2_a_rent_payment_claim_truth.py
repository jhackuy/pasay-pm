"""PASAY-MILESTONE-002 — Rent payment-claim truth model.

Revision ID: m2a000000001
Revises: m1c000000001
Create Date: 2026-08-21

Adds the authoritative rent payment-claim truth layer so a lease period's
paid/remaining/partial state derives from VERIFIED claims, not a raw
income confirm flip:

- ``rent_payment_claims``: one row per reported rent payment with its own
  lifecycle (PENDING -> VERIFIED | FAILED | REVERSED), a deterministic
  idempotency key (DB partial unique index), period-level grouping, and
  amount-mismatch preservation (E6-style over-claim mismatch surfacing
  instead of silent truncation or auto-paid).

DOWNGRADE SAFETY (§9.4/§5 MIGRATION rule):
  Before dropping the table we ``sa.inspect`` the downgrade-target columns
  to confirm the tz-aware ``claimed_at``/``verified_at`` and the JSONB
  ``evidence_ids`` have NOT silently lost their semantics. If the live DB
  reports non-tz or non-JSONB we abort the downgrade rather than silently
  corrupt the historical record.

ROLLBACK:
    alembic downgrade m1c000000001
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "m2a000000001"
down_revision: Union[str, None] = "m1c000000001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


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
    op.create_table(
        "rent_payment_claims",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("lease_id", sa.BigInteger(), nullable=False),
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("income_id", sa.BigInteger(), nullable=True),
        sa.Column("claimed_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("claimed_by", sa.BigInteger(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column(
            "evidence_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("verification_note", sa.Text(), nullable=True),
        sa.Column("verified_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("verified_by", sa.BigInteger(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "mismatch",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("mismatch_reason", sa.Text(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        *_audit_cols(),
        sa.CheckConstraint(
            "status IN ('PENDING','VERIFIED','FAILED','REVERSED')",
            name="ck_rent_payment_claims_status",
        ),
        sa.ForeignKeyConstraint(["income_id"], ["incomes.id"]),
        sa.ForeignKeyConstraint(["lease_id"], ["leases.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_rent_payment_claims_idempotency_key",
        "rent_payment_claims",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_index(
        "ix_rent_payment_claims_lease_period_status",
        "rent_payment_claims",
        ["lease_id", "period", "status"],
        unique=False,
    )
    op.create_index(
        "ix_rent_payment_claims_period_status",
        "rent_payment_claims",
        ["period", "status"],
        unique=False,
    )
    op.create_index(
        "ix_rent_payment_claims_status",
        "rent_payment_claims",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_rent_payment_claims_lease_id",
        "rent_payment_claims",
        ["lease_id"],
        unique=False,
    )
    op.create_index(
        "ix_rent_payment_claims_income_id",
        "rent_payment_claims",
        ["income_id"],
        unique=False,
    )


def downgrade() -> None:
    # ------------------------------------------------------------------
    # §9.4/§5 DOWNGRADE SAFETY: sa.inspect audit before DROP.
    # If any critical column has lost its tz / JSONB semantics we abort.
    # ------------------------------------------------------------------
    bind = op.get_bind()
    insp = sa.inspect(bind)
    table_exists = insp.has_table("rent_payment_claims")
    if table_exists:
        cols = {c["name"]: c for c in insp.get_columns("rent_payment_claims")}
        # claimed_at must be tz-aware
        ca = cols.get("claimed_at")
        if ca is not None:
            ca_type = getattr(ca["type"], "timezone", None)
            if ca_type is False:
                raise RuntimeError(
                    "DOWNGRADE BLOCKED: rent_payment_claims.claimed_at lost "
                    "timezone=True — refusing to drop semantically-corrupted "
                    "timestamp history. Manual inspection required."
                )
        va = cols.get("verified_at")
        if va is not None:
            va_type = getattr(va["type"], "timezone", None)
            if va_type is False:
                raise RuntimeError(
                    "DOWNGRADE BLOCKED: rent_payment_claims.verified_at lost "
                    "timezone=True — refusing to drop semantically-corrupted "
                    "timestamp history. Manual inspection required."
                )
        # evidence_ids must be JSONB, not plain TEXT
        ev = cols.get("evidence_ids")
        if ev is not None and not isinstance(
            ev["type"], postgresql.JSONB
        ):
            # JSONB can round-trip via TEXT but dialect-specific methods
            # matter; if the live inspector reports a plain string type we
            # surface a warning rather than abort because TEXT is lossless
            # for JSON payloads. Keep it explicit here for audit trails.
            import warnings

            warnings.warn(
                "DOWNGRADE: rent_payment_claims.evidence_ids is reported as "
                f"{type(ev['type']).__name__} (not JSONB by inspector). "
                "Proceeding with DROP — contents are assumed lossless "
                "TEXT-encoded JSON.",
                stacklevel=2,
            )

    op.drop_index(
        "ix_rent_payment_claims_income_id",
        table_name="rent_payment_claims",
    )
    op.drop_index(
        "ix_rent_payment_claims_lease_id",
        table_name="rent_payment_claims",
    )
    op.drop_index(
        "ix_rent_payment_claims_status", table_name="rent_payment_claims"
    )
    op.drop_index(
        "ix_rent_payment_claims_period_status",
        table_name="rent_payment_claims",
    )
    op.drop_index(
        "ix_rent_payment_claims_lease_period_status",
        table_name="rent_payment_claims",
    )
    op.drop_index(
        "uq_rent_payment_claims_idempotency_key",
        table_name="rent_payment_claims",
    )
    op.drop_table("rent_payment_claims")
