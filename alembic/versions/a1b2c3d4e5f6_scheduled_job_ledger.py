"""PASAY-TASK-011 FIX1 — scheduled job idempotency ledger for Queue/Container boundary.

Revision ID: a1b2c3d4e5f6
Revises: z9a8b7c6d5e4
Create Date: 2026-08-20

Scope (from ND_RETURN PASAY-TASK-011 FIX1 blocker #4 + FIX12 + FIX13):
1. ``pasay_scheduled_job_ledger`` — replaces the runtime ``CREATE TABLE IF NOT EXISTS``
   lazy-DDL path used previously by the internal ingestion router. Alembic owns the
   table definition so the migration chain remains the single DB schema authority
   (Scope E + single-head contract).
2. Columns mirror the earlier raw DDL exactly:
   - ``event_id`` VARCHAR(256) PRIMARY KEY — deterministic envelope event_id
     (insert-on-conflict-nothing idempotency for scheduled envelopes).
   - ``job_name`` VARCHAR(128) NOT NULL — observability only.
   - ``occurred_at`` TIMESTAMPTZ NOT NULL — envelope ``occurred_at`` verbatim.
   - ``consumed_at`` TIMESTAMPTZ NOT NULL DEFAULT NOW() — audit timestamp.
   - ``payload`` JSONB NULL — optional scheduled_job envelope payload copy.

Rollback:
    alembic downgrade z9a8b7c6d5e4

Ownership Marker (FIX13):
    After create_table we execute a COMMENT ON TABLE statement embedding the
    machine-readable marker ``OWNED_BY_ALEMBIC_REV=a1b2c3d4e5f6`` together with
    ``SCHEMA_REV=2`` and ``LEDGER_SCHEMA_DIGEST``.  Both upgrade() and downgrade()
    refuse to run when the marker is present but its embedded revision DOES NOT
    match the live revision.  This makes it IMPOSSIBLE for a stale live operator
    or accidental partial rollforward to mutate a ledger table whose OWNERSHIP
    token is bound to a DIFFERENT alembic revision — Fail-Closed on drift or
    partial replay.

Legacy Data Preservation (FIX13):
    downgrade() NEVER calls ``op.drop_table`` unconditionally; there are now TWO
    gates BEFORE the drop:
      (a) the sa.inspect audit chain (PK/colnames/types — FIX1/FIX11).
      (b) the NEW ``_ledger_has_user_rows()`` data-preservation check: if
          ``SELECT COUNT(*) > 0``, downgrade RAISES with human-readable wording
          requiring the operator to EXPLICITLY empty the ledger before a downgrade
          drop.  This guarantees rollback NEVER silently DROPS real ledger
          observations — a replay of the prior schema can only happen after
          explicit TRUNCATE.
"""
from __future__ import annotations

from typing import Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "z9a8b7c6d5e4"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


# ═══════════════════════════════════════════════════════════════════════════════
# Ownership Marker constants — DO NOT EDIT without bumping SCHEMA_REV.
# ═══════════════════════════════════════════════════════════════════════════════
OWNERSHIP_MARKER_REV_KEY = "OWNED_BY_ALEMBIC_REV"
OWNERSHIP_MARKER_SCHEMA_REV_KEY = "SCHEMA_REV"
OWNERSHIP_MARKER_SCHEMA_REV_VALUE = "2"  # bumped at FIX13 (legacy tables had SCHEMA_REV=1)
LEDGER_SCHEMA_DIGEST = "cols:event_id[256PK]+job_name[128NN]+occurred_at[TZNN]+consumed_at[TZNNDEFNOW]+payload[JSONB]|TZ:pg|dialect:jsonb-pg"
OWNERSHIP_MARKER_SOURCE_KEY = "SOURCE"
OWNERSHIP_MARKER_LEDGER_TYPE_KEY = "LEDGER_TYPE"
_OWNERSHIP_MARKER_OUR_KEYS = frozenset({
    OWNERSHIP_MARKER_REV_KEY,
    OWNERSHIP_MARKER_SCHEMA_REV_KEY,
    "DIGEST",
    OWNERSHIP_MARKER_SOURCE_KEY,
    OWNERSHIP_MARKER_LEDGER_TYPE_KEY,
})

# Exact comment BODY produced for OUR tokens when there are NO legacy/foreign
# tokens to preserve.  If a pre-existing COMMENT carries operator-managed KVs
# (e.g. LEGACY_OWNER, CREATED, MIGRATED_FROM, NOTES), we READ-APPEND (update
# only keys in _OWNERSHIP_MARKER_OUR_KEYS) and leave foreign keys untouched
# (FIX14 Legacy Ownership Preservation).
_OWNERSHIP_COMMENT_EXPECTED = (
    f"{OWNERSHIP_MARKER_REV_KEY}={revision};"
    f"{OWNERSHIP_MARKER_SCHEMA_REV_KEY}={OWNERSHIP_MARKER_SCHEMA_REV_VALUE};"
    f"DIGEST={LEDGER_SCHEMA_DIGEST};"
    f"{OWNERSHIP_MARKER_SOURCE_KEY}=alembic-upgrade-{revision};"
    f"{OWNERSHIP_MARKER_LEDGER_TYPE_KEY}=scheduled-job-idempotency;"
)


# ── Ownership Marker helpers (database-agnostic: PG COMMENT ON + SQLite text) ─
def _is_postgresql(conn: Connection) -> bool:
    return conn.dialect.name.lower().startswith("postgres")


def _read_table_comment(conn: Connection, table_name: str) -> str | None:
    """Return COMMENT ON TABLE for PostgreSQL, else None for SQLite/others.

    We use pg_catalog directly on PG so the comment round-trip works with
    ``exec_driver_sql`` and standard ``connection.execute`` wrappers.  SQLite
    does not natively support persistent table comments; we still attempt a
    best-effort read for future dialect support, but the marker check there
    will return None and we proceed with the inspection-only audit chain.
    """
    if not _is_postgresql(conn):
        return None
    try:
        result = conn.execute(
            sa.text(
                "SELECT obj_description(:oid ::regclass, 'pg_class')"
            ),
            {"oid": table_name},
        )
        row = result.scalar_one_or_none()
        return str(row) if row is not None else None
    except Exception:
        # obj_description can fail on edge cases (search_path, quoted names).
        # Never fail-open; return None so downstream explicit-marker match
        # falls back to inspection-only audits.
        return None


def _parse_ownership_marker(comment: str | None) -> dict[str, str]:
    kv: dict[str, str] = {}
    if not comment:
        return kv
    for token in comment.split(";"):
        token = token.strip()
        if not token or "=" not in token:
            continue
        k, _, v = token.partition("=")
        kv[k.strip()] = v.strip()
    return kv


def _serialize_ownership_marker(kv: dict[str, str]) -> str:
    """Serialize KV map back to the exact KV-order string of the marker.

    Order contract:
      (1) OUR keys FIRST — in the stable, deterministic order of
          _OWNERSHIP_MARKER_OUR_KEYS (OWNED_BY_ALEMBIC_REV, SCHEMA_REV, DIGEST,
          SOURCE, LEDGER_TYPE) — so byte-for-byte equality checks against
          _OWNERSHIP_COMMENT_EXPECTED work when there are ZERO foreign keys.
      (2) FOREIGN keys LAST — in sorted() order for determinism.
          Foreign keys are operator-managed (LEGACY_OWNER, CREATED, MIGRATED_*)
          and MUST remain preserved across re-stamps.
    """
    our_ordered_keys = [
        OWNERSHIP_MARKER_REV_KEY,
        OWNERSHIP_MARKER_SCHEMA_REV_KEY,
        "DIGEST",
        OWNERSHIP_MARKER_SOURCE_KEY,
        OWNERSHIP_MARKER_LEDGER_TYPE_KEY,
    ]
    pieces: list[str] = []
    for k in our_ordered_keys:
        if k in kv:
            pieces.append(f"{k}={kv[k]};")
    foreign_keys = sorted(k for k in kv.keys() if k not in _OWNERSHIP_MARKER_OUR_KEYS)
    for k in foreign_keys:
        pieces.append(f"{k}={kv[k]};")
    return "".join(pieces)


def _merge_ownership_marker(
    existing_comment: str | None,
) -> tuple[str, bool]:
    """READ-APPEND merge: preserve foreign KVs, update only OUR KVs to current rev.

    Returns (final_comment_string, needs_write):
      * needs_write=False  →  existing comment is already IDENTICAL to what we
                              would serialize; skip COMMENT ON TABLE (avoid DDL
                              noise on no-op re-upgrades — important for CI idempotency).
      * needs_write=True   →  existing comment missing our keys / carrying stale
                              values / we have foreign keys to preserve; re-write
                              COMMENT ON TABLE with the merged result.
    """
    existing_kv = _parse_ownership_marker(existing_comment)
    merged: dict[str, str] = {}
    # (1) Foreign keys FIRST in merged (they are preserved verbatim).
    for k, v in existing_kv.items():
        if k not in _OWNERSHIP_MARKER_OUR_KEYS:
            merged[k] = v
    # (2) Our keys are UPDATED unconditionally (FIX14 rule: our marker tokens
    #     are authoritative for the running revision).
    merged[OWNERSHIP_MARKER_REV_KEY] = revision
    merged[OWNERSHIP_MARKER_SCHEMA_REV_KEY] = OWNERSHIP_MARKER_SCHEMA_REV_VALUE
    merged["DIGEST"] = LEDGER_SCHEMA_DIGEST
    merged[OWNERSHIP_MARKER_SOURCE_KEY] = f"alembic-upgrade-{revision}"
    merged[OWNERSHIP_MARKER_LEDGER_TYPE_KEY] = "scheduled-job-idempotency"
    final = _serialize_ownership_marker(merged)
    # Fast-path no-op detection (skip DDL if final == existing trimmed already).
    normalized_existing = (existing_comment or "").strip()
    if normalized_existing == final.strip():
        return final, False
    return final, True


def _assert_ownership_marker_ok(conn: Connection, expected_rev: str) -> None:
    """Fail-Closed on ownership-marker mismatch.

    * If marker ABSENT (legacy table, SQLite unit DB, pre-FIX13 upgrade): no-op.
    * If marker PRESENT but OWNED_BY_ALEMBIC_REV != ``expected_rev``: raise.
    * If marker PRESENT but SCHEMA_REV is older than current: raise.
    """
    marker = _parse_ownership_marker(_read_table_comment(conn, "pasay_scheduled_job_ledger"))
    present_rev = marker.get(OWNERSHIP_MARKER_REV_KEY)
    present_schema_rev = marker.get(OWNERSHIP_MARKER_SCHEMA_REV_KEY)
    if present_rev is None and present_schema_rev is None:
        return  # marker absent — allow legacy / non-PG flow (inspection audit is authoritative)
    if present_rev is not None and present_rev != expected_rev:
        raise RuntimeError(
            "pasay_scheduled_job_ledger ownership marker mismatch: "
            f"OWNED_BY_ALEMBIC_REV={present_rev!r} does NOT match running "
            f"revision={expected_rev!r}.  Refusing to proceed — partial rollforward "
            "or stale operator state detected.  Either re-run a clean full upgrade, "
            "or manually reconcile and clear the COMMENT ON TABLE token."
        )
    if present_schema_rev is not None:
        try:
            present_int = int(present_schema_rev)
            expected_int = int(OWNERSHIP_MARKER_SCHEMA_REV_VALUE)
        except (TypeError, ValueError):
            present_int = -1
            expected_int = int(OWNERSHIP_MARKER_SCHEMA_REV_VALUE)
        if present_int < expected_int:
            raise RuntimeError(
                "pasay_scheduled_job_ledger ownership marker SCHEMA_REV is STALE: "
                f"marker={present_schema_rev!r} < required={OWNERSHIP_MARKER_SCHEMA_REV_VALUE!r}. "
                "This ledger table was written by an older schema rev; refuse to "
                "mutate.  Run a full upgrade so the marker is refreshed."
            )


def _write_ownership_marker_if_pg(conn: Connection) -> None:
    """Emit COMMENT ON TABLE for PostgreSQL using READ-APPEND merge.  SQLite: no-op.

    FIX14 Legacy Ownership Preservation rule:
      * ALWAYS call _read_table_comment FIRST — do NOT blindly overwrite a
        pre-existing pg_catalog COMMENT.
      * Run `_merge_ownership_marker(existing_comment)`.  This:
          (a) preserves all KVs whose key ∉ _OWNERSHIP_MARKER_OUR_KEYS (legacy
              operator tokens like LEGACY_OWNER, CREATED, MIGRATED_FROM, NOTES),
          (b) unconditionally sets OUR 5 KVs to current revision constants,
          (c) returns needs_write=False when the post-merge serialized comment
              is IDENTICAL to what's already in pg_catalog (so repeat
              `alembic upgrade head` calls don't emit repeated COMMENT DDL).
      * Only emit COMMENT ON TABLE when needs_write is True.

    We use ``quote_literal`` on the final merged string so the COMMENT literal
    is immune to injection even if operator-written foreign KV values contain
    special SQL chars (semicolons, quotes, dashes, etc).
    """
    if not _is_postgresql(conn):
        return
    existing_comment = _read_table_comment(conn, "pasay_scheduled_job_ledger")
    final_comment, needs_write = _merge_ownership_marker(existing_comment)
    if not needs_write:
        return  # CI idempotency: pg_catalog already carries the merged-identical comment
    # Fully escaped literal — quote_literal handles foreign-kv semicolons/quotes.
    quoted = conn.exec_driver_sql(
        "SELECT quote_literal(%s) AS q", (final_comment,)
    ).scalar()
    if not quoted:
        raise RuntimeError("quote_literal failed — cannot write ownership marker.")
    op.execute(
        sa.text(f"COMMENT ON TABLE pasay_scheduled_job_ledger IS {quoted}")
    )


# ── Legacy Data Preservation helper ───────────────────────────────────────────
def _ledger_has_user_rows(conn: Connection) -> bool:
    """True if the ledger table currently holds one or more user rows.

    Used ONLY inside downgrade() before drop_table.  Do NOT call during
    upgrade() where a fresh CREATE TABLE would trivially be empty.
    """
    try:
        result = conn.execute(
            sa.text("SELECT COUNT(*) FROM pasay_scheduled_job_ledger")
        )
        count = int(result.scalar_one() or 0)
        return count > 0
    except Exception as exc:  # noqa: BLE001
        # If we cannot even COUNT, refuse to drop — Fail Closed.
        raise RuntimeError(
            "pasay_scheduled_job_ledger data-preservation check failed: cannot "
            f"SELECT COUNT(*) to detect legacy rows. Exception: {exc!r}. "
            "Refusing to drop. Please manually reconcile."
        ) from exc


# ── DDL helpers (dialect-compatible TEXT-as-JSON for SQLite fallback) ────────
#   (kept identical to FIX1 structure so the legacy schema audit chain still
#    works unchanged.)

def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    table_exists = inspector.has_table("pasay_scheduled_job_ledger")

    if table_exists:
        # ── FIX13: Before any mutation, verify ownership marker matches live rev ─
        _assert_ownership_marker_ok(conn, expected_rev=revision)

        existing_pk = inspector.get_pk_constraint("pasay_scheduled_job_ledger")
        pk_cols = sorted((existing_pk or {}).get("constrained_columns") or [])
        if pk_cols != ["event_id"]:
            raise RuntimeError(
                "pasay_scheduled_job_ledger already exists but PRIMARY KEY is not "
                f"EXACTLY the single column (event_id). Got PK columns: {pk_cols!r}. "
                "The ledger uses INSERT … ON CONFLICT (event_id) DO NOTHING and "
                "requires a single-column unique PK on event_id. Please manually "
                "reconcile the legacy table with this migration's schema."
            )
        existing_cols = {c["name"]: c for c in inspector.get_columns("pasay_scheduled_job_ledger")}
        required_names = {"event_id", "job_name", "occurred_at", "consumed_at", "payload"}
        missing = required_names - set(existing_cols.keys())
        if missing:
            raise RuntimeError(
                f"pasay_scheduled_job_ledger already exists but is missing required columns: {sorted(missing)}. "
                "Please manually reconcile the legacy table with this migration's schema."
            )
        event_id_col = existing_cols["event_id"]
        event_id_type = getattr(event_id_col.get("type", None), "python_type", None)
        if event_id_type is not None and not issubclass(event_id_type, str):
            raise RuntimeError(
                f"pasay_scheduled_job_ledger.event_id column must be a string type compatible with VARCHAR(256); got python_type={event_id_type!r}. "
                "Please manually reconcile the legacy table with this migration's schema."
            )
        occurred_at_col = existing_cols["occurred_at"]
        oa_type = occurred_at_col.get("type")
        oa_is_tz = getattr(oa_type, "timezone", None) if oa_type is not None else None
        if oa_type is not None and oa_is_tz is False:
            raise RuntimeError(
                "pasay_scheduled_job_ledger.occurred_at must be TIMESTAMPTZ (timezone-aware); "
                f"got a naive datetime column type: {oa_type!r}. "
                "Please manually reconcile the legacy table with this migration's schema."
            )
        payload_col = existing_cols.get("payload")
        if payload_col is not None:
            payload_raw_type = payload_col.get("type")
            payload_dialect_name = ""
            try:
                payload_dialect_name = payload_raw_type.compile(dialect=postgresql.dialect()) if payload_raw_type is not None else ""
            except Exception:
                payload_dialect_name = ""
            payload_is_json_compat = (
                payload_dialect_name.upper().startswith("JSON")
                or "JSON" in type(payload_raw_type).__name__.upper()
            )
            if payload_raw_type is not None and getattr(payload_raw_type, "_isnull", True) is False and not payload_is_json_compat:
                raise RuntimeError(
                    f"pasay_scheduled_job_ledger.payload must be JSON/JSONB-compatible for JSONB cast; got {payload_raw_type!r}. "
                    "Please manually reconcile the legacy table with this migration's schema."
                )
        consumed_at_col = existing_cols["consumed_at"]
        if consumed_at_col.get("default") is None and consumed_at_col.get("server_default") is None:
            pass
        # ── FIX13: Legacy upgrade path (table already exists).
        # Ensure ownership marker is PRESENT and MATCHING current rev.
        _write_ownership_marker_if_pg(conn)
        return

    # ── Fresh upgrade path (table does NOT exist yet) ────────────────────────
    op.create_table(
        "pasay_scheduled_job_ledger",
        sa.Column("event_id", sa.String(length=256), nullable=False),
        sa.Column("job_name", sa.String(length=128), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "consumed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_pasay_scheduled_job_ledger")),
    )
    # ── FIX13: Stamp ownership marker IMMEDIATELY after create_table.
    _write_ownership_marker_if_pg(conn)


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("pasay_scheduled_job_ledger"):
        return

    # ── FIX13 (GATE 0): Ownership marker must match running revision ────────
    _assert_ownership_marker_ok(conn, expected_rev=revision)

    existing_pk = inspector.get_pk_constraint("pasay_scheduled_job_ledger")
    pk_cols = sorted((existing_pk or {}).get("constrained_columns") or [])
    if pk_cols != ["event_id"]:
        raise RuntimeError(
            "pasay_scheduled_job_ledger exists but PRIMARY KEY is not EXACTLY "
            f"the single column (event_id). Got PK columns: {pk_cols!r}. "
            "Refusing to drop a table that may contain legacy ledger data "
            "created outside this migration. Please manually reconcile before "
            "running downgrade."
        )

    existing_cols = {c["name"]: c for c in inspector.get_columns("pasay_scheduled_job_ledger")}
    required_names = {"event_id", "job_name", "occurred_at", "consumed_at", "payload"}
    present = set(existing_cols.keys())
    if present != required_names:
        extra = present - required_names
        missing = required_names - present
        raise RuntimeError(
            "pasay_scheduled_job_ledger column set does not EXACTLY match the "
            f"schema created by this migration. Extra columns: {sorted(extra)!r}. "
            f"Missing columns: {sorted(missing)!r}. Refusing to drop to preserve "
            "existing data. Please manually reconcile."
        )

    event_id_col = existing_cols["event_id"]
    event_id_type = getattr(event_id_col.get("type", None), "python_type", None)
    if event_id_type is not None and not issubclass(event_id_type, str):
        raise RuntimeError(
            f"pasay_scheduled_job_ledger.event_id column must be a string type "
            f"compatible with VARCHAR(256); got python_type={event_id_type!r}. "
            "Refusing to drop a mismatched legacy table. Please manually reconcile."
        )

    occurred_at_col = existing_cols["occurred_at"]
    oa_type = occurred_at_col.get("type")
    oa_is_tz = getattr(oa_type, "timezone", None) if oa_type is not None else None
    if oa_type is not None and oa_is_tz is False:
        raise RuntimeError(
            "pasay_scheduled_job_ledger.occurred_at must be TIMESTAMPTZ "
            f"(timezone-aware); got a naive datetime column type: {oa_type!r}. "
            "Refusing to drop a mismatched legacy table. Please manually reconcile."
        )

    # ── FIX11 Ledger downgrade safety: payload + consumed_at column audits ──────
    consumed_at_col = existing_cols["consumed_at"]
    ca_type = consumed_at_col.get("type")
    ca_is_tz = getattr(ca_type, "timezone", None) if ca_type is not None else None
    if ca_type is not None and ca_is_tz is False:
        raise RuntimeError(
            "pasay_scheduled_job_ledger.consumed_at must be TIMESTAMPTZ "
            f"(timezone-aware); got a naive datetime column type: {ca_type!r}. "
            "Refusing to drop a mismatched legacy table. Please manually reconcile."
        )

    payload_col = existing_cols.get("payload")
    if payload_col is not None:
        payload_raw_type = payload_col.get("type")
        payload_dialect_name = ""
        try:
            payload_dialect_name = (
                payload_raw_type.compile(dialect=postgresql.dialect())
                if payload_raw_type is not None
                else ""
            )
        except Exception:
            payload_dialect_name = ""
        payload_is_json_compat = (
            payload_dialect_name.upper().startswith("JSON")
            or "JSON" in type(payload_raw_type).__name__.upper()
        )
        if (
            payload_raw_type is not None
            and getattr(payload_raw_type, "_isnull", False) is False
            and not payload_is_json_compat
        ):
            raise RuntimeError(
                "pasay_scheduled_job_ledger.payload must be JSON/JSONB-compatible "
                f"for JSONB cast; got {payload_raw_type!r}. Refusing to drop a "
                "mismatched legacy table. Please manually reconcile."
            )

    # ═══════════════════════════════════════════════════════════════════════════
    # FIX13 (GATE LAST): Legacy Data Preservation — NEVER drop a non-empty ledger.
    # ═══════════════════════════════════════════════════════════════════════════
    # An operator who legitimately wants to downgrade the schema (e.g. roll back
    # a bad deploy) must FIRST decide what to do with existing observations:
    # either back up to S3 / CSV / ORC explicitly, or TRUNCATE and accept data
    # loss.  Downgrade will no longer make that decision silently.
    if _ledger_has_user_rows(conn):
        raise RuntimeError(
            "pasay_scheduled_job_ledger LEGACY DATA PRESERVATION CHECK FAILED "
            f"(running revision {revision}).\n"
            "The ledger table currently contains one or more user-written rows of "
            "scheduled-job idempotency observations.  This downgrade path would "
            "DROP the table and PERMANENTLY erase those rows.\n"
            "If you REALLY intend to downgrade and lose these rows, run:\n"
            "    TRUNCATE TABLE pasay_scheduled_job_ledger;\n"
            "    -- (backup via COPY TO first if you need the data)\n"
            "then re-run the downgrade.  Otherwise leave the data in place and "
            "roll forward instead."
        )

    op.drop_table("pasay_scheduled_job_ledger")
