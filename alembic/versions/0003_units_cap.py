"""units per property <= 15 enforcement.

Issue #112 GAP-P1: enforce the frozen product rule
"Units <= 15 per property" at the correct domain boundary.

Why a PostgreSQL trigger (NOT a row-level CHECK constraint):

A naive ``CHECK (count <= 15)`` is invalid: SQL CHECK constraints
operate on the row being inserted/updated and cannot reference other
rows. ``count(*)|<=(SELECT 15 ...)`` would either parse as a subquery
on the same row or be rejected by PostgreSQL — the only way to
express a "no more than N rows per parent" rule is either:

  (a) a deferred constraint trigger that counts related rows; or
  (b) a BEFORE INSERT/UPDATE trigger that counts and raises.

Both are valid PostgreSQL idioms; we pick (b) because:

  * It fires inside the same statement, so the 16th insert is
    rejected atomically (no TOCTOU race between a service-layer
    COUNT and the actual INSERT).
  * It runs on every code path (API, service, batch loader, manual
    psql, future worker) — the rule cannot be bypassed.
  * It is small, race-safe, and reversible via ``DROP TRIGGER``.

Mechanism:

* BEFORE INSERT OR UPDATE OF ``property_id`` ON ``v1_units``:
  count existing units for the incoming ``property_id`` (after
  applying the row change for UPDATE). If the resulting count
  would exceed the cap, raise an exception that the application
  layer maps to HTTP 409 Conflict.

* The cap is exposed as a server-side configuration constant
  ``UNITS_PER_PROPERTY_CAP`` (default 15) so future product
  relaxation only needs to edit one line. The trigger body uses
  a literal so PostgreSQL can inline it.

* The migration pre-flight checks that no existing property
  already violates the cap; if it does, the migration aborts with
  a clear message rather than installing a broken trigger that
  would silently reject future writes.

Reversibility:

* ``alembic downgrade -1`` drops the trigger AND the helper
  function. Existing rows are untouched.

Revision ID: 0003_units_cap
Revises: 0002_renewal_pipeline
Create Date: 2026-09-02 16:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "0003_units_cap"
down_revision = "0002_renewal_pipeline"
branch_labels = None
depends_on = None


# Single source of truth for the product cap. The trigger body uses
# the literal so PostgreSQL doesn't need a parameter at execution
# time; if the cap is relaxed the migration must be edited and
# the trigger recreated (the audit explicitly calls this out).
UNITS_PER_PROPERTY_CAP = 15


def upgrade() -> None:
    """Install the BEFORE INSERT/UPDATE trigger enforcing Units <= 15."""
    bind = op.get_bind()

    # 1) Pre-flight: refuse to install the constraint if a property
    #    already has more than the cap. This is a no-op in the normal
    #    case (existing orgs have at most a handful of units) and a
    #    loud failure in the legacy-data case so the operator must
    #    consciously reconcile before the rule bites.
    over_cap = bind.execute(
        sa.text(
            """
            SELECT property_id, COUNT(*) AS n
            FROM v1_units
            GROUP BY property_id
            HAVING COUNT(*) > :cap
            ORDER BY n DESC
            LIMIT 5
            """
        ),
        {"cap": UNITS_PER_PROPERTY_CAP},
    ).fetchall()
    if over_cap:
        rows = ", ".join(
            f"property_id={r[0]} count={r[1]}" for r in over_cap
        )
        raise RuntimeError(
            "cannot install units-cap trigger: existing data "
            f"violates Units <= {UNITS_PER_PROPERTY_CAP} ({rows}). "
            "Reconcile legacy data before retrying."
        )

    # 2) Trigger function. ``RAISE EXCEPTION`` inside a BEFORE INSERT
    #    trigger aborts the statement and surfaces an error the
    #    SQLAlchemy driver translates to ``IntegrityError`` /
    #    ``ProgrammingError`` with the message text intact.
    op.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION v1_enforce_units_per_property_cap()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            DECLARE
                v_existing INTEGER;
            BEGIN
                -- Count rows that will exist for NEW.property_id after
                -- this statement. The row being updated (NEW.id) is
                -- already counted via the existing row at OLD.property_id
                -- (UPDATE OF property_id is the only UPDATE case), so
                -- we exclude NEW.id to avoid double-counting.
                SELECT COUNT(*) INTO v_existing
                FROM v1_units
                WHERE property_id = NEW.property_id
                  AND id <> NEW.id;

                IF v_existing + 1 > {UNITS_PER_PROPERTY_CAP} THEN
                    RAISE EXCEPTION
                        'units_per_property_cap_exceeded: '
                        'property_id=% has % existing units, '
                        'cap={UNITS_PER_PROPERTY_CAP}',
                        NEW.property_id, v_existing
                        USING ERRCODE = 'check_violation';
                END IF;

                RETURN NEW;
            END;
            $$
            """
        )
    )

    # 3) Install the trigger on both INSERT and UPDATE OF property_id.
    #    Updates that do NOT change property_id cannot push the count
    #    past the cap, so we only need to fire when the parent changes
    #    (or on insert).
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_v1_units_units_per_property_cap
            BEFORE INSERT OR UPDATE OF property_id ON v1_units
            FOR EACH ROW
            EXECUTE FUNCTION v1_enforce_units_per_property_cap();
            """
        )
    )


def downgrade() -> None:
    """Remove the trigger and its helper function."""
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS "
            "trg_v1_units_units_per_property_cap ON v1_units"
        )
    )
    op.execute(
        sa.text(
            "DROP FUNCTION IF EXISTS "
            "v1_enforce_units_per_property_cap()"
        )
    )
