"""PASAY-MILESTONE-001: Property.organization_id backfill + NOT NULL harden (P1).

Revision ID: m1a000000001
Revises: b5c6d7e8f9a0
Create Date: 2026-08-21

Owner contract:
  1. Legacy NULL Property.organization_id rows are auto-backfilled ONLY when the
     ownership is UNIQUE AND UNAMBIGUOUS.
  2. If multiple Organizations exist and ownership cannot be uniquely determined,
     FAIL CLOSED with specific Property IDs and candidate Organizations.
  3. NEVER take the first organization (oldest/newest/smallest id) guesswork IS forbidden.

Backfill rules (deterministic, ordered by priority):
  A. If organizations table has exactly 1 row -> backfill all NULL rows to that id.
  B. If 0 rows -> FAIL CLOSED (no org to backfill to).
  C. If >1 rows: for every NULL property_id we try Property.created_by ->
     Membership ACTIVE -> organization_id. If that resolves to exactly 1 org id
     across all created_by memberships of the property, backfill it. Otherwise list
     the property_id together with all candidate orgs and FAIL CLOSED.

Downgrade safety (Alembic downgrade MUST preserve column semantics):
  - Before dropping NOT NULL we verify organization_id is still BIGINT + no type drift
    via `sa.inspect` — otherwise refuse downgrade (FAIL CLOSED).
  - The column remains BIGINT REFERENCES organizations(id) (nullable after downgrade).
"""
from __future__ import annotations

from typing import Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine import Connection

revision: str = "m1a000000001"
down_revision: str | None = "b5c6d7e8f9a0"
branch_labels = None
depends_on = None


class _AmbiguousBackfill(RuntimeError):
    """Raised when legacy data cannot be uniquely backfilled (Owner rule #2)."""


# ---------------------------------------------------------------------------
# UPGRADE helpers
# ---------------------------------------------------------------------------


def _gather_null_property_ids_without_org(conn: Connection) -> list[int]:
    rows = conn.execute(
        sa.text(
            "SELECT p.id FROM properties p "
            "WHERE p.deleted_at IS NULL AND p.organization_id IS NULL "
            "ORDER BY p.id"
        )
    ).all()
    return [r[0] for r in rows]


def _count_organizations(conn: Connection) -> int:
    return conn.execute(sa.text("SELECT COUNT(*) FROM organizations")).scalar() or 0


def _only_org_id(conn: Connection) -> int:
    row = conn.execute(sa.text("SELECT id FROM organizations ORDER BY id LIMIT 1")).one()
    return row[0]


def _backfill_with_org_id(conn: Connection, org_id: int, target_ids: list[int]) -> None:
    if not target_ids:
        return
    chunk_size = 500
    for i in range(0, len(target_ids), chunk_size):
        chunk = target_ids[i : i + chunk_size]
        conn.execute(
            sa.text(
                "UPDATE properties SET organization_id = :org WHERE id = ANY(:ids)"
            ),
            {"org": org_id, "ids": chunk},
        )


def _resolve_created_by_org(conn: Connection, property_ids: list[int]) -> dict[int, list[int]]:
    """For each NULL property, collect DISTINCT org_ids reachable via
    property.created_by -> membership ACTIVE. Returns
    {property_id: [sorted candidate org_ids]}.
    """
    result: dict[int, list[int]] = {}
    for pid in property_ids:
        rows = conn.execute(
            sa.text(
                "SELECT DISTINCT m.organization_id "
                "FROM properties p "
                "JOIN memberships m ON m.user_id = p.created_by "
                "WHERE p.id = :pid AND m.state = 'ACTIVE' "
                "AND m.removed_at IS NULL "
                "ORDER BY m.organization_id"
            ),
            {"pid": pid},
        ).all()
        result[pid] = sorted(r[0] for r in rows)
    return result


def _organizations_lookup(conn: Connection) -> dict[int, str]:
    rows = conn.execute(sa.text("SELECT id, name FROM organizations ORDER BY id")).all()
    return {r[0]: r[1] for r in rows}


def _fail_closed_ambiguous(conn: Connection, ambiguous: dict[int, list[int]]) -> None:
    """Abort the upgrade deterministically with a deterministic error report for Owner review."""
    org_names = _organizations_lookup(conn)
    lines = [
        "PASAY-MILESTONE-001 FAIL CLOSED: Property.org_id backfill is AMBIGUOUS for the",
        "following legacy properties. Owner must decide their org manually.",
    ]
    for pid, candidates in sorted(ambiguous.items()):
        cand_repr = ", ".join(
            f"org_id={c} ({org_names.get(c, 'UNKNOWN')})" for c in candidates
        ) or "NO candidates (0 ACTIVE membership for created_by)"
        lines.append(f"  - property_id={pid} candidates=[{cand_repr}]")
    lines.append("Candidates Organizations (known):")
    for oid, oname in sorted(org_names.items()):
        lines.append(f"  - org_id={oid} name={oname!r}")
    raise _AmbiguousBackfill("\n".join(lines))


def upgrade() -> None:
    conn = op.get_bind()
    null_ids = _gather_null_property_ids_without_org(conn)
    if not null_ids:
        # Nothing to backfill — directly tighten NOT NULL.
        pass
    else:
        org_count = _count_organizations(conn)
        if org_count == 0:
            # No orgs exist at all: this is either a fresh empty test DB with
            # zero legacy data (Base.metadata.create_all path). In that case the
            # properties rows wouldn't exist either → this case is actually empty set,
            # should not reach here because null_ids=[]. If it does reach here fail
            # deterministically:
            _fail_closed_ambiguous(conn, {pid: [] for pid in null_ids})
        if org_count == 1:
            # Case A: exactly one organization → auto-backfill deterministically.
            _backfill_with_org_id(conn, _only_org_id(conn), null_ids)
        else:
            # Case C: >1 org → created_by membership resolution, per-property.
            resolved = _resolve_created_by_org(conn, null_ids)
            ambiguous: dict[int, list[int]] = {}
            can_backfill: dict[int, int] = {}
            for pid, orgs in resolved.items():
                if len(orgs) == 1:
                    can_backfill[pid] = orgs[0]
                else:
                    ambiguous[pid] = orgs
            if ambiguous:
                _fail_closed_ambiguous(conn, ambiguous)
            for org_id in sorted(set(can_backfill.values())):
                ids_for_org = sorted(p for p, o in can_backfill.items() if o == org_id)
                _backfill_with_org_id(conn, org_id, ids_for_org)
    # -------------------- tighten NOT NULL constraint --------------------
    op.alter_column(
        "properties",
        "organization_id",
        existing_type=sa.BigInteger(),
        existing_nullable=True,
        nullable=False,
    )


# ---------------------------------------------------------------------------
# DOWNGRADE (safe with sa.inspect audit gate)
# ---------------------------------------------------------------------------


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    cols = {c["name"]: c for c in insp.get_columns("properties")}
    col = cols.get("organization_id")
    if col is None:
        raise RuntimeError(
            "PASAY-MILESTONE-001 downgrade FAIL CLOSED: "
            "column properties.organization_id not present "
            "(schema drift; refusing downgrade)."
        )
    col_type = col.get("type")
    # Must be BIGINT-compatible; NOT a different scalar type.
    type_name = type(col_type).__name__
    if "BIGINT" not in type_name.upper() and "BigInteger" not in type_name:
        # Best-effort textual check too
        try:
            compiled = str(col_type.compile(dialect=conn.dialect))
        except Exception:  # noqa: BLE001
            compiled = ""
        if "BIGINT" not in compiled.upper():
            raise RuntimeError(
                f"PASAY-MILESTONE-001 downgrade FAIL CLOSED: "
                f"properties.organization_id type drifted to {type_name!r} "
                f"(compiled={compiled!r}); expected BIGINT. "
                f"Refusing downgrade to prevent numeric semantic loss."
            )
    # Foreign key sanity: the column must still reference organizations(id).
    fks = insp.get_foreign_keys("properties")
    fk_match = any(
        set(fk.get("constrained_columns") or []) == {"organization_id"}
        and (fk.get("referred_table") == "organizations")
        and set(fk.get("referred_columns") or []) == {"id"}
        for fk in fks
    )
    if not fk_match:
        raise RuntimeError(
            "PASAY-MILESTONE-001 downgrade FAIL CLOSED: "
            "FK properties.organization_id -> organizations(id) "
            "drifted or missing; refusing downgrade."
        )
    # Semantics audit passed; relax NOT NULL.
    op.alter_column(
        "properties",
        "organization_id",
        existing_type=sa.BigInteger(),
        existing_nullable=False,
        nullable=True,
    )
