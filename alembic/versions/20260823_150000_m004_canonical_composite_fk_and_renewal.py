"""PASAY-M004 — Canonical composite FK constraints + lease renewal superseded columns.

Revision ID: m4c000000001
Revises: m4b000000001
Create Date: 2026-08-23

UPGRADE SCOPE:
1. Data integrity pre-checks (abort on any violation):
   a. DepositSettlement.lease_id must match MoveOutInspection.lease_id
   b. Lease.move_out_inspection_id must point to MOI of same lease
   c. Lease.deposit_settlement_id must point to DS of same lease
   d. Existing renewal_metadata validity (successor existence + unit/tenant match + date continuity)
2. move_out_inspections: add UNIQUE(id, lease_id) -> uq_move_out_inspections_id_lease_id
3. deposit_settlements: add UNIQUE(id, lease_id) -> uq_deposit_settlements_id_lease_id
4. deposit_settlements: composite FK (move_out_inspection_id, lease_id) -> MOI(id, lease_id)
5. leases: composite FK pointers
   a. (move_out_inspection_id, id) -> MOI(id, lease_id)
   b. (deposit_settlement_id, id) -> DS(id, lease_id)
6. leases: add superseded_by_lease_id BIGINT nullable + superseded_at TIMESTAMPTZ nullable
7. leases: superseded constraints (FK + 2 CK + partial UQ)
8. Backfill: migrate renewal_metadata->renewed_lease_id / renewed_at into canonical columns

DOWNGRADE SAFETY (逆序还原):
1. DROP superseded_by group: partial UQ -> 2 CK -> FK, then drop columns
2. DROP leases two composite pointer FKs
3. DROP deposit_settlements composite FK
4. DROP uq_deposit_settlements_id_lease_id
5. DROP uq_move_out_inspections_id_lease_id

ROLLBACK:
    alembic downgrade m4b000000001
"""
from typing import Union

from alembic import op
import sqlalchemy as sa


revision: str = "m4c000000001"
down_revision: Union[str, None] = "m4b000000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ------------------------------------------------------------------
    # 1. Data integrity pre-checks
    # ------------------------------------------------------------------

    # 1a. Settlement.Inspection cross-lease mismatch
    bad_rows = conn.execute(
        sa.text(
            """
            SELECT ds.id, ds.lease_id, ds.move_out_inspection_id, moi.lease_id AS insp_lease_id
            FROM deposit_settlements ds
            JOIN move_out_inspections moi ON moi.id = ds.move_out_inspection_id
            WHERE ds.lease_id <> moi.lease_id
            """
        )
    ).fetchall()
    if bad_rows:
        raise Exception(
            "DepositSettlement lease_id mismatch with MoveOutInspection: "
            f"rows={bad_rows}"
        )

    # 1b. Lease.MOI pointer cross-lease
    bad_rows = conn.execute(
        sa.text(
            """
            SELECT l.id AS lease_id, l.move_out_inspection_id, moi.lease_id AS insp_lease_id
            FROM leases l JOIN move_out_inspections moi ON moi.id = l.move_out_inspection_id
            WHERE moi.lease_id <> l.id
            """
        )
    ).fetchall()
    if bad_rows:
        raise Exception(
            "Lease move_out_inspection_id points across lease boundary: "
            f"rows={bad_rows}"
        )

    # 1c. Lease.DS pointer cross-lease
    bad_rows = conn.execute(
        sa.text(
            """
            SELECT l.id AS lease_id, l.deposit_settlement_id, ds.lease_id AS ds_lease_id
            FROM leases l JOIN deposit_settlements ds ON ds.id = l.deposit_settlement_id
            WHERE ds.lease_id <> l.id
            """
        )
    ).fetchall()
    if bad_rows:
        raise Exception(
            "Lease deposit_settlement_id points across lease boundary: "
            f"rows={bad_rows}"
        )

    # 1d. renewal_metadata.renewed_lease_id validity
    bad_leases = []
    lease_rows = conn.execute(
        sa.text(
            """
            SELECT
                l.id,
                l.unit_id,
                l.tenant_id,
                l.end_date,
                l.renewal_metadata->>'renewed_lease_id' AS renewed_lease_id_str,
                l.renewal_metadata->>'renewed_at' AS renewed_at_str
            FROM leases l
            WHERE l.renewal_metadata->>'renewed_lease_id' IS NOT NULL
            """
        )
    ).fetchall()
    for lr in lease_rows:
        try:
            successor_id = int(lr.renewed_lease_id_str)
        except (TypeError, ValueError):
            bad_leases.append(f"#{lr.id}: invalid renewed_lease_id={lr.renewed_lease_id_str!r}")
            continue
        succ = conn.execute(
            sa.text(
                """
                SELECT id, unit_id, tenant_id, start_date, deleted_at
                FROM leases WHERE id = :sid
                """
            ),
            {"sid": successor_id},
        ).fetchone()
        if succ is None:
            bad_leases.append(f"#{lr.id}: successor lease #{successor_id} not found")
            continue
        if succ.deleted_at is not None:
            bad_leases.append(f"#{lr.id}: successor lease #{successor_id} is soft-deleted")
            continue
        if succ.unit_id != lr.unit_id:
            bad_leases.append(
                f"#{lr.id}: successor unit_id={succ.unit_id} != predecessor unit_id={lr.unit_id}"
            )
        if succ.tenant_id != lr.tenant_id:
            bad_leases.append(
                f"#{lr.id}: successor tenant_id={succ.tenant_id} != predecessor tenant_id={lr.tenant_id}"
            )
        expected_start = lr.end_date + sa.text("interval '1 day'").compile().string
        if succ.start_date != (lr.end_date + __import__("datetime").timedelta(days=1)):
            bad_leases.append(
                f"#{lr.id}: successor start_date={succ.start_date} != predecessor end_date+1="
                f"{lr.end_date + __import__('datetime').timedelta(days=1)}"
            )
    if bad_leases:
        raise Exception(
            "Existing renewal_metadata invalid for leases: "
            + "; ".join(bad_leases)
        )

    # ------------------------------------------------------------------
    # 2. move_out_inspections UNIQUE(id, lease_id)
    # ------------------------------------------------------------------
    op.create_unique_constraint(
        "uq_move_out_inspections_id_lease_id",
        "move_out_inspections",
        ["id", "lease_id"],
    )

    # ------------------------------------------------------------------
    # 3. deposit_settlements UNIQUE(id, lease_id)
    # ------------------------------------------------------------------
    op.create_unique_constraint(
        "uq_deposit_settlements_id_lease_id",
        "deposit_settlements",
        ["id", "lease_id"],
    )

    # ------------------------------------------------------------------
    # 4. deposit_settlements composite FK -> MOI(id, lease_id)
    # ------------------------------------------------------------------
    op.create_foreign_key(
        "fk_deposit_settlements_inspection_lease",
        "deposit_settlements",
        "move_out_inspections",
        ["move_out_inspection_id", "lease_id"],
        ["id", "lease_id"],
    )

    # ------------------------------------------------------------------
    # 5. leases composite FK pointers
    # ------------------------------------------------------------------
    # 5a. (move_out_inspection_id, id) -> MOI(id, lease_id)
    op.create_foreign_key(
        "fk_leases_moi_id_lease",
        "leases",
        "move_out_inspections",
        ["move_out_inspection_id", "id"],
        ["id", "lease_id"],
    )
    # 5b. (deposit_settlement_id, id) -> DS(id, lease_id)
    op.create_foreign_key(
        "fk_leases_ds_id_lease",
        "leases",
        "deposit_settlements",
        ["deposit_settlement_id", "id"],
        ["id", "lease_id"],
    )

    # ------------------------------------------------------------------
    # 6. leases: superseded_by_lease_id + superseded_at columns
    # ------------------------------------------------------------------
    op.add_column(
        "leases",
        sa.Column("superseded_by_lease_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "leases",
        sa.Column(
            "superseded_at", sa.DateTime(timezone=True), nullable=True
        ),
    )

    # ------------------------------------------------------------------
    # 7. leases: superseded constraints
    # ------------------------------------------------------------------
    # 7a. FK
    op.create_foreign_key(
        "fk_leases_superseded_by",
        "leases",
        "leases",
        ["superseded_by_lease_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    # 7b. CK pair (both NULL or both NOT NULL + status=expired)
    op.create_check_constraint(
        "ck_leases_superseded_pair",
        "leases",
        (
            "(superseded_by_lease_id IS NULL AND superseded_at IS NULL) OR "
            "(superseded_by_lease_id IS NOT NULL AND superseded_at IS NOT NULL AND status = 'expired')"
        ),
    )
    # 7c. CK not self
    op.create_check_constraint(
        "ck_leases_superseded_not_self",
        "leases",
        "superseded_by_lease_id IS NULL OR superseded_by_lease_id != id",
    )
    # 7d. Partial UQ: at most one predecessor per successor
    op.create_index(
        "uq_leases_superseded_by_one_predecessor",
        "leases",
        ["superseded_by_lease_id"],
        unique=True,
        postgresql_where=sa.text("superseded_by_lease_id IS NOT NULL"),
    )

    # ------------------------------------------------------------------
    # 8. Backfill: renewal_metadata -> canonical columns
    # ------------------------------------------------------------------
    conn.execute(
        sa.text(
            """
            UPDATE leases
            SET superseded_by_lease_id = CAST(renewal_metadata->>'renewed_lease_id' AS bigint),
                superseded_at = CASE
                    WHEN renewal_metadata->>'renewed_at' IS NOT NULL
                        THEN CAST(renewal_metadata->>'renewed_at' AS timestamptz)
                    ELSE now()
                END
            WHERE renewal_metadata->>'renewed_lease_id' IS NOT NULL
              AND superseded_by_lease_id IS NULL
            """
        )
    )


def downgrade() -> None:
    # ------------------------------------------------------------------
    # 1 (reverse). DROP superseded_by group: index -> fk -> ck -> columns
    # ------------------------------------------------------------------
    op.drop_index(
        "uq_leases_superseded_by_one_predecessor", table_name="leases"
    )
    op.drop_constraint(
        "ck_leases_superseded_not_self", "leases", type_="check"
    )
    op.drop_constraint(
        "ck_leases_superseded_pair", "leases", type_="check"
    )
    op.drop_constraint(
        "fk_leases_superseded_by", "leases", type_="foreignkey"
    )
    op.drop_column("leases", "superseded_at")
    op.drop_column("leases", "superseded_by_lease_id")

    # ------------------------------------------------------------------
    # 2 (reverse). DROP leases two composite pointer FKs
    # ------------------------------------------------------------------
    op.drop_constraint(
        "fk_leases_ds_id_lease", "leases", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_leases_moi_id_lease", "leases", type_="foreignkey"
    )

    # ------------------------------------------------------------------
    # 3 (reverse). DROP deposit_settlements composite FK
    # ------------------------------------------------------------------
    op.drop_constraint(
        "fk_deposit_settlements_inspection_lease",
        "deposit_settlements",
        type_="foreignkey",
    )

    # ------------------------------------------------------------------
    # 4 (reverse). DROP uq_deposit_settlements_id_lease_id
    # ------------------------------------------------------------------
    op.drop_constraint(
        "uq_deposit_settlements_id_lease_id",
        "deposit_settlements",
        type_="unique",
    )

    # ------------------------------------------------------------------
    # 5 (reverse). DROP uq_move_out_inspections_id_lease_id
    # ------------------------------------------------------------------
    op.drop_constraint(
        "uq_move_out_inspections_id_lease_id",
        "move_out_inspections",
        type_="unique",
    )
