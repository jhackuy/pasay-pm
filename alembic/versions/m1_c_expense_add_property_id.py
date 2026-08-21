"""PASAY-MILESTONE-001 FIX1: Expense.property_id NOT NULL + fail-closed backfill (m1c).

Owner Blocker #4: 业务能力回退 — Expense 原本允许 unit_id nullable (building / property
level expenses). M1 通过 inner join Unit 强制 scoped，导致全部历史 unitless Expense
从 scoped list/get 消失，且新 create 拒绝“Expense must be associated with a unit”。

本迁移策略（严格遵循 Owner §3 + §4：不冗余 organization_id，只引入 property_id 作为
building-level canonical ownership 锚点）：

  1. ALTER TABLE expenses ADD COLUMN property_id BIGINT NULL REFERENCES properties(id).
  2. FAIL-CLOSED backfill 每一条 expenses：
       A. 若 unit_id NOT NULL → 直接解析 unit.property_id 填充（unit 仍存在则必定
          唯一；unit 已被删除 / orphan → 进入 ambiguous）。
       B. 若 unit_id NULL → 按 created_by → ACTIVE memberships 解析，若跨多
          Organization 或 0 候选 → FAIL CLOSED + 列出具体 expense_id。
  3. 回填完成后 ALTER COLUMN property_id SET NOT NULL + CREATE INDEX。

Downgrade safety:
  - sa.inspect 审计 property_id 类型为 BIGINT + FK(properties.id)；漂移则拒绝。
  - 先 DROP INDEX → relax NOT NULL → DROP COLUMN。

Revision ID: m1c000000001
Revises: m1b000000001
Create Date: 2026-08-21
"""
from __future__ import annotations

import logging

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine import Connection

logger = logging.getLogger("alembic.runtime.migration")

revision: str = "m1c000000001"
down_revision: str | None = "m1b000000001"
branch_labels = None
depends_on = None


class _AmbiguousExpenseBackfill(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# UPGRADE helpers
# ---------------------------------------------------------------------------


def _list_all_expense_ids(conn: Connection) -> list[int]:
    rows = conn.execute(
        sa.text("SELECT e.id FROM expenses e ORDER BY e.id")
    ).all()
    return [r[0] for r in rows]


def _resolve_unit_property(conn: Connection, expense_ids: list[int]) -> dict[int, int | None]:
    """For each expense, if unit_id is set AND unit still exists, resolve unit.property_id.
    Returns {expense_id: pid | None} where None means unit_id NULL or orphan unit.
    """
    result: dict[int, int | None] = {eid: None for eid in expense_ids}
    rows = conn.execute(
        sa.text(
            "SELECT e.id, u.property_id "
            "FROM expenses e "
            "JOIN units u ON u.id = e.unit_id "
            "WHERE e.id = ANY(:ids)"
        ),
        {"ids": expense_ids},
    ).all()
    for eid, pid in rows:
        result[eid] = pid
    return result


def _resolve_created_by_orgs(conn: Connection, expense_ids: list[int]) -> dict[int, list[int]]:
    """Fallback for expenses with no resolvable unit → created_by user's ACTIVE memberships.
    Returns {expense_id: [sorted candidate org_ids]}.
    """
    result: dict[int, list[int]] = {eid: [] for eid in expense_ids}
    for eid in expense_ids:
        rows = conn.execute(
            sa.text(
                "SELECT DISTINCT m.organization_id "
                "FROM expenses e "
                "JOIN memberships m ON m.user_id = e.created_by "
                "WHERE e.id = :eid AND m.state = 'ACTIVE' AND m.removed_at IS NULL "
                "ORDER BY m.organization_id"
            ),
            {"eid": eid},
        ).all()
        result[eid] = sorted(r[0] for r in rows)
    return result


def _backfill_expenses(conn: Connection, backfill_map: dict[int, int]) -> None:
    if not backfill_map:
        return
    chunk_size = 500
    by_property: dict[int, list[int]] = {}
    for eid, pid in sorted(backfill_map.items()):
        by_property.setdefault(pid, []).append(eid)
    stmt = sa.text(
        "UPDATE expenses SET property_id = :pid WHERE id IN :ids"
    ).bindparams(sa.bindparam("ids", expanding=True))
    for pid, ids in sorted(by_property.items()):
        for i in range(0, len(ids), chunk_size):
            conn.execute(stmt, {"pid": pid, "ids": ids[i : i + chunk_size]})


def _organizations_lookup(conn: Connection) -> dict[int, str]:
    rows = conn.execute(sa.text("SELECT id, name FROM organizations ORDER BY id")).all()
    return {r[0]: r[1] for r in rows}


def _fail_closed(conn: Connection, bad: dict[int, list[int]]) -> None:
    names = _organizations_lookup(conn)
    lines = [
        "PASAY-MILESTONE-001 FIX1 FAIL CLOSED: Expense.property_id backfill is AMBIGUOUS "
        "for the following unitless/orphan-unit legacy expenses. Owner must decide manually."
    ]
    for eid, orgs in sorted(bad.items()):
        cand = ", ".join(f"org_id={o} ({names.get(o, 'UNKNOWN')})" for o in orgs) or (
            "NO candidates (expense has unit_id NULL and created_by has 0 ACTIVE memberships)."
        )
        lines.append(f"  - expense_id={eid} candidates=[{cand}]")
    lines.append("All candidate Organizations:")
    for oid, oname in sorted(names.items()):
        lines.append(f"  - org_id={oid} name={oname!r}")
    raise _AmbiguousExpenseBackfill("\n".join(lines))


# ---------------------------------------------------------------------------
# UPGRADE
# ---------------------------------------------------------------------------


def upgrade() -> None:
    conn = op.get_bind()
    # (1) Add nullable column + FK
    op.add_column(
        "expenses",
        sa.Column(
            "property_id",
            sa.BigInteger(),
            sa.ForeignKey("properties.id"),
            nullable=True,
        ),
    )
    # (2) Backfill
    eids = _list_all_expense_ids(conn)
    if eids:
        unit_pids = _resolve_unit_property(conn, eids)
        needs_created_by_fallback: list[int] = [
            eid for eid, pid in unit_pids.items() if pid is None
        ]
        created_by_orgs = (
            _resolve_created_by_orgs(conn, needs_created_by_fallback)
            if needs_created_by_fallback else {}
        )
        backfill: dict[int, int] = {}
        ambiguous: dict[int, list[int]] = {}
        for eid in eids:
            pid = unit_pids.get(eid)
            if pid is not None:
                backfill[eid] = pid
                continue
            orgs = created_by_orgs.get(eid, [])
            if len(orgs) == 1:
                backfill[eid] = orgs[0]
            else:
                ambiguous[eid] = orgs
        if ambiguous:
            _fail_closed(conn, ambiguous)
        _backfill_expenses(conn, backfill)
    # (3) Enforce NOT NULL + Index
    op.alter_column(
        "expenses",
        "property_id",
        existing_type=sa.BigInteger(),
        existing_nullable=True,
        nullable=False,
    )
    op.create_index("ix_expenses_property_id", "expenses", ["property_id"])


# ---------------------------------------------------------------------------
# DOWNGRADE
# ---------------------------------------------------------------------------


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    cols = {c["name"]: c for c in insp.get_columns("expenses")}
    col = cols.get("property_id")
    if col is None:
        raise RuntimeError(
            "PASAY-MILESTONE-001 FIX1 downgrade FAIL CLOSED: "
            "expenses.property_id not present (schema drift)."
        )
    tname = type(col["type"]).__name__
    compiled = ""
    try:
        compiled = str(col["type"].compile(dialect=conn.dialect))
    except Exception:  # noqa: BLE001
        logger.warning("cannot compile expenses.property_id type", exc_info=True)
    if "BIGINT" not in tname.upper() and "BigInteger" not in tname and "BIGINT" not in compiled.upper():
        raise RuntimeError(
            f"PASAY-MILESTONE-001 FIX1 downgrade FAIL CLOSED: "
            f"expenses.property_id type drifted to {tname!r} (compiled={compiled!r})."
        )
    fks = insp.get_foreign_keys("expenses")
    matches = any(
        set(f.get("constrained_columns") or []) == {"property_id"}
        and f.get("referred_table") == "properties"
        and set(f.get("referred_columns") or []) == {"id"}
        for f in fks
    )
    any_match = False
    if not matches:
        for f in fks:
            if "property_id" in (f.get("constrained_columns") or []):
                if f.get("referred_table") == "properties":
                    any_match = True
                    break
    if not matches and not any_match:
        raise RuntimeError(
            "PASAY-MILESTONE-001 FIX1 downgrade FAIL CLOSED: "
            "FK expenses.property_id -> properties(id) missing."
        )
    # Drop index FIRST (IF EXISTS via raw SQL, avoid cascade surprises).
    try:
        conn.execute(sa.text("DROP INDEX IF EXISTS ix_expenses_property_id"))
    except Exception:  # noqa: BLE001
        logger.warning("ix_expenses_property_id drop failed", exc_info=True)
    # Relax NOT NULL -> NULL
    op.alter_column(
        "expenses",
        "property_id",
        existing_type=sa.BigInteger(),
        existing_nullable=False,
        nullable=True,
    )
    # Drop column
    op.drop_column("expenses", "property_id")
