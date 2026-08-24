"""PASAY-M004 — Enforce successor shares exact (unit_id, tenant_id) with predecessor.

Revision ID: m4d000000001
Revises: m4c000000001
Create Date: 2026-08-24

UPGRADE SCOPE (fail-closed):
1. Pre-check dirty data: any existing superseded_by_lease_id that DOES NOT match
   successor.unit_id == predecessor.unit_id AND successor.tenant_id == predecessor.tenant_id
   -> Migration ABORTED with exact offending rows; full tx rollback; no half DDL.
2. Create UNIQUE(id, unit_id, tenant_id) on leases -> uq_leases_id_unit_tenant
   (covers both ends of the composite self-ref FK — id already PK, so the compound
   set is automatically unique; explicit unique ensures PostgreSQL accepts a
   multi-col FK referencing it.)
3. Add composite FK: (superseded_by_lease_id, unit_id, tenant_id) REFERENCES
   leases(id, unit_id, tenant_id) — database-level same-party invariant.

DOWNGRADE SAFETY (逆序):
1. DROP composite FK fk_leases_superseded_same_party
2. DROP UNIQUE uq_leases_id_unit_tenant
3. No data columns dropped, no financial rows touched, successor links preserved
   via original single-col fk_leases_superseded_by (still enforced).

Direct-SQL PROBES (added tests in test_m004_db_invariants):
  a) Same unit + same tenant successor   -> DB OK (COMMIT)
  b) Cross unit   same tenant successor   -> DB ERROR  (rollback)
  c) Same unit  cross tenant  successor   -> DB ERROR  (rollback)
  d) Non-existent successor id             -> DB ERROR  (rollback)
Each probe runs inside an explicit SAVEPOINT; on error the savepoint is
rolled back so the migration test script itself is not contaminated.
"""
from typing import Union

from alembic import op
import sqlalchemy as sa


revision: str = "m4d000000001"
down_revision: Union[str, None] = "m4c000000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ------------------------------------------------------------------
    # 1. Dirty-data pre-check (DDL前 fail-closed)
    # ------------------------------------------------------------------
    bad = conn.execute(
        sa.text(
            """
            SELECT
                pred.id     AS predecessor_id,
                pred.unit_id  AS pred_unit,
                pred.tenant_id AS pred_tenant,
                succ.id     AS successor_id,
                succ.unit_id  AS succ_unit,
                succ.tenant_id AS succ_tenant
            FROM leases pred
            JOIN leases succ ON succ.id = pred.superseded_by_lease_id
            WHERE pred.superseded_by_lease_id IS NOT NULL
              AND (succ.unit_id <> pred.unit_id OR succ.tenant_id <> pred.tenant_id)
            """
        )
    ).fetchall()
    if bad:
        raise Exception(
            "MIGRATION ABORTED (m4d000000001 upgrade) — existing "
            "superseded_by_lease_id references a DIFFERENT unit/tenant "
            f"than its predecessor. Offending rows: {bad!r}. "
            "Repair canonical links BEFORE running this migration; "
            "the whole upgrade has been rolled back with no DDL applied."
        )

    # ------------------------------------------------------------------
    # 2. UNIQUE(id, unit_id, tenant_id)  — required compound parent key
    # ------------------------------------------------------------------
    op.create_unique_constraint(
        "uq_leases_id_unit_tenant",
        "leases",
        ["id", "unit_id", "tenant_id"],
    )

    # ------------------------------------------------------------------
    # 3. Composite self-referential FK
    # ------------------------------------------------------------------
    op.create_foreign_key(
        "fk_leases_superseded_same_party",
        "leases",
        "leases",
        ["superseded_by_lease_id", "unit_id", "tenant_id"],
        ["id", "unit_id", "tenant_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_leases_superseded_same_party", "leases", type_="foreignkey"
    )
    op.drop_constraint(
        "uq_leases_id_unit_tenant", "leases", type_="unique"
    )
