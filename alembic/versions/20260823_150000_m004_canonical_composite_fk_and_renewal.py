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
from datetime import timedelta
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

    # 1d. renewal_metadata.renewed_lease_id validity — M2/M3 strict contract
    #     M2: no Python date + SQL string add TypeError
    #     M3: predecessor.status MUST == 'expired' (no auto-mutate status);
    #         renewed_lease_id must be parseable bigint; renewed_at timestamptz;
    #         successor not soft-deleted; same unit+tenant; seamless start_date;
    #         at most ONE predecessor per successor id.
    bad_leases = []
    predecessor_per_successor: dict[int, int] = {}
    lease_rows = conn.execute(
        sa.text(
            """
            SELECT
                l.id,
                l.status,
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
        # --- M3 #7: renewed_lease_id must be valid bigint parseable ---
        try:
            successor_id = int(str(lr.renewed_lease_id_str).strip())
        except (TypeError, ValueError):
            bad_leases.append(
                f"lease#{lr.id}: invalid renewed_lease_id={lr.renewed_lease_id_str!r} "
                f"(expected integer bigint). Migration will NOT auto-repair; "
                f"fix the JSONB value first or delete the renewal_metadata entry."
            )
            continue
        # --- M3 #6: renewed_at (if present) must parse as timestamptz ---
        if lr.renewed_at_str is not None:
            try:
                from datetime import datetime as _dt
                _dt.fromisoformat(str(lr.renewed_at_str).replace("Z", "+00:00"))
            except Exception as _ex:
                bad_leases.append(
                    f"lease#{lr.id}: renewal_metadata.renewed_at={lr.renewed_at_str!r} "
                    f"not parseable as timestamptz ({type(_ex).__name__}: {_ex})."
                )
        # --- M3 #1: predecessor status must equal 'expired' exactly; NO auto-change to expired ---
        if str(lr.status) != "expired":
            bad_leases.append(
                f"lease#{lr.id}: predecessor status={lr.status!r} but renewal_metadata "
                f"points at successor#{successor_id}. Canonical superseded link requires "
                f"predecessor.status == 'expired'. Migration refuses to auto-transition the "
                f"lease; run renew endpoint correctly (which sets predecessor=expired) first."
            )
        # --- M3 #2-5: successor exists/not soft-deleted; same unit; same tenant; seamless date ---
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
            bad_leases.append(
                f"lease#{lr.id}: successor lease #{successor_id} not found in DB. "
                f"renewal_metadata.renewed_lease_id is dangling."
            )
            continue
        if succ.deleted_at is not None:
            bad_leases.append(
                f"lease#{lr.id}: successor lease #{successor_id} is soft-deleted "
                f"(deleted_at={succ.deleted_at}). Remove the renewal_metadata entry "
                f"or restore the successor row BEFORE running this migration."
            )
            continue
        if succ.unit_id != lr.unit_id:
            bad_leases.append(
                f"lease#{lr.id}: successor#{successor_id}.unit_id={succ.unit_id} "
                f"!= predecessor.unit_id={lr.unit_id}. Renewal successor must share Unit."
            )
        if succ.tenant_id != lr.tenant_id:
            bad_leases.append(
                f"lease#{lr.id}: successor#{successor_id}.tenant_id={succ.tenant_id} "
                f"!= predecessor.tenant_id={lr.tenant_id}. Renewal successor must share Tenant."
            )
        # --- M2 safe date arithmetic: Python timedelta (NOT Python + SQL literal str) ---
        expected_start = lr.end_date + timedelta(days=1)
        if succ.start_date != expected_start:
            bad_leases.append(
                f"lease#{lr.id}: successor#{successor_id}.start_date={succ.start_date} "
                f"!= predecessor.end_date + 1 day = {expected_start}. "
                f"Seamless renewal contract requires exactly start == end+1."
            )
        # --- M3 #8: same successor must not be shared by another predecessor ---
        if successor_id in predecessor_per_successor:
            other_pred = predecessor_per_successor[successor_id]
            bad_leases.append(
                f"successor#{successor_id} is claimed by BOTH predecessor#{other_pred} "
                f"AND predecessor#{lr.id}. Each successor lease must have at most ONE "
                f"canonical predecessor (partial UNIQUE idx will later enforce this). "
                f"Remove the stale renewal_metadata on whichever predecessor is wrong."
            )
        else:
            predecessor_per_successor[successor_id] = lr.id

    if bad_leases:
        # --- M3 strict: transaction ROLLBACK automatically — whole upgrade() is one Alembic tx ---
        #     No half schema state: no constraints/columns are left behind because
        #     we abort BEFORE running any DDL below.
        bad_joined = "; ".join(bad_leases)
        raise Exception(
            "MIGRATION ABORTED (m4c000000001 upgrade) — existing renewal_metadata fails "
            f"canonical superseded truth validation. {len(bad_leases)} issue(s): {bad_joined}. "
            "The entire upgrade() has been rolled back; no DDL was applied. "
            "Fix the listed JSONB / lease status / successor rows first, then re-run upgrade."
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
