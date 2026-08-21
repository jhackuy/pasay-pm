"""PASAY-MILESTONE-001: Tenant.organization_id NOT NULL + backfill (P2).

Revision ID: m1b000000001
Revises: m1a000000001
Create Date: 2026-08-21

Owner contract:
  - Tenant.organization_id NOT NULL.
  - Legacy Tenant inferred via Lease -> Unit -> Property -> Organization chain.
  - UNIQUE inference or FAIL CLOSED. Guessing forbidden.

Downgrade safety:
  - sa.inspect must confirm column: BIGINT REFERENCES organizations(id),
    type drift/missing FK -> refuse downgrade.
  - Index removal verified by sa.inspect existence too.
"""
from __future__ import annotations

from typing import Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine import Connection

revision: str = "m1b000000001"
down_revision: str | None = "m1a000000001"
branch_labels = None
depends_on = None


class _AmbiguousTenantBackfill(RuntimeError):
    pass


def _list_all_tenant_ids(conn: Connection) -> list[int]:
    rows = conn.execute(
        sa.text(
            "SELECT t.id FROM tenants t WHERE t.deleted_at IS NULL ORDER BY t.id"
        )
    ).all()
    return [r[0] for r in rows]


def _infer_tenant_orgs(conn: Connection, tenant_ids: list[int]) -> dict[int, list[int]]:
    """For every tenant_id, collect all DISTINCT org_ids via
    tenant -> Lease -> Unit -> Property.org. If tenant has 0 leases -> empty.
    """
    out: dict[int, list[int]] = {tid: [] for tid in tenant_ids}
    rows = conn.execute(
        sa.text(
            "SELECT DISTINCT t.id AS tenant_id, pr.organization_id "
            "FROM tenants t "
            "JOIN leases l ON l.tenant_id = t.id AND l.deleted_at IS NULL "
            "JOIN units u ON u.id = l.unit_id AND u.deleted_at IS NULL "
            "JOIN properties pr ON pr.id = u.property_id AND pr.deleted_at IS NULL "
            "WHERE t.deleted_at IS NULL AND pr.organization_id IS NOT NULL "
            "ORDER BY t.id, pr.organization_id"
        )
    ).all()
    for tid, oid in rows:
        if tid in out and oid not in out[tid]:
            out[tid].append(oid)
    return out


def _backfill_tenant_orgs(conn: Connection, backfill_map: dict[int, int]) -> None:
    if not backfill_map:
        return
    chunk_size = 500
    ids = sorted(backfill_map.keys())
    for i in range(0, len(ids), chunk_size):
        chunk = ids[i : i + chunk_size]
        stmt = sa.text(
            "UPDATE tenants SET organization_id = :oid WHERE id = :tid"
        )
        for tid in chunk:
            conn.execute(stmt, {"tid": tid, "oid": backfill_map[tid]})


def _organizations_lookup(conn: Connection) -> dict[int, str]:
    rows = conn.execute(sa.text("SELECT id, name FROM organizations ORDER BY id")).all()
    return {r[0]: r[1] for r in rows}


def _fail_closed(conn: Connection, bad: dict[int, list[int]]) -> None:
    names = _organizations_lookup(conn)
    lines = [
        "PASAY-MILESTONE-001 FAIL CLOSED: Tenant.organization_id backfill is "
        "AMBIGUOUS for the following legacy tenants. Owner must decide manually."
    ]
    for tid, orgs in sorted(bad.items()):
        cand = ", ".join(f"org_id={o} ({names.get(o,'UNKNOWN')})" for o in orgs) or (
            "NO candidates (tenant has 0 leases or all leases/org NULL)."
        )
        lines.append(f"  - tenant_id={tid} candidates=[{cand}]")
    lines.append("All candidate Organizations:")
    for oid, oname in sorted(names.items()):
        lines.append(f"  - org_id={oid} name={oname!r}")
    raise _AmbiguousTenantBackfill("\n".join(lines))


def upgrade() -> None:
    conn = op.get_bind()
    # (1) Add column BIGINT NULL + FK
    op.add_column(
        "tenants",
        sa.Column(
            "organization_id",
            sa.BigInteger(),
            sa.ForeignKey("organizations.id"),
            nullable=True,
            index=True,
        ),
    )
    # (2) Backfill via Lease->Unit->Property
    tids = _list_all_tenant_ids(conn)
    if tids:
        inferred = _infer_tenant_orgs(conn, tids)
        ambiguous: dict[int, list[int]] = {}
        backfill: dict[int, int] = {}
        for tid, orgs in inferred.items():
            if len(orgs) == 1:
                backfill[tid] = orgs[0]
            else:
                ambiguous[tid] = orgs
        if ambiguous:
            _fail_closed(conn, ambiguous)
        _backfill_tenant_orgs(conn, backfill)
    # (3) Enforce NOT NULL
    op.alter_column(
        "tenants",
        "organization_id",
        existing_type=sa.BigInteger(),
        existing_nullable=True,
        nullable=False,
    )


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    cols = {c["name"]: c for c in insp.get_columns("tenants")}
    col = cols.get("organization_id")
    if col is None:
        raise RuntimeError(
            "PASAY-MILESTONE-001 downgrade FAIL CLOSED: "
            "tenants.organization_id already dropped (schema drift)."
        )
    tname = type(col["type"]).__name__
    compiled = ""
    try:
        compiled = str(col["type"].compile(dialect=conn.dialect))
    except Exception:  # noqa: BLE001
        pass
    if "BIGINT" not in tname.upper() and "BigInteger" not in tname and "BIGINT" not in compiled.upper():
        raise RuntimeError(
            f"PASAY-MILESTONE-001 downgrade FAIL CLOSED: "
            f"tenants.organization_id type drifted to {tname!r} (compiled={compiled!r})."
        )
    fks = insp.get_foreign_keys("tenants")
    matches = any(
        set(f.get("constrained_columns") or []) == {"organization_id"}
        and f.get("referred_table") == "organizations"
        and set(f.get("referred_columns") or []) == {"id"}
        for f in fks
    )
    if not matches:
        # In some DB introspections the FK name may have auto-named; also
        # accept if ANY FK carries organization_id -> organizations.id
        any_match = False
        for f in fks:
            if "organization_id" in (f.get("constrained_columns") or []):
                if f.get("referred_table") == "organizations":
                    any_match = True
                    break
        if not any_match:
            raise RuntimeError(
                "PASAY-MILESTONE-001 downgrade FAIL CLOSED: "
                "FK tenants.organization_id -> organizations(id) missing."
            )
    # Drop index FIRST (before dropping column, column drop cascades index).
    # Use IF EXISTS via raw SQL to stay fail-safe even if index was renamed.
    conn = op.get_bind()
    try:
        conn.execute(
            sa.text("DROP INDEX IF EXISTS ix_tenants_organization_id")
        )
    except Exception:  # noqa: BLE001 — any index-drop issue is non-fatal in downgrade
        pass
    # Relax NOT NULL -> NULL
    op.alter_column(
        "tenants",
        "organization_id",
        existing_type=sa.BigInteger(),
        existing_nullable=False,
        nullable=True,
    )
    # Drop column
    op.drop_column("tenants", "organization_id")
