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

We pick (b), but the COUNT alone is **not** race-safe. Under
PostgreSQL's default ``READ COMMITTED`` isolation, two concurrent
INSERTs/UPDATEs targeting the same ``property_id`` both observe the
same pre-write count and both commit past the cap (empirically
verified — five rounds of two concurrent INSERTs at 14 existing
units produced five rounds of 16 final rows). The trigger must
therefore serialize writers per target property before counting.

Serialization is implemented by ``SELECT ... FOR UPDATE`` on the
parent ``v1_properties`` row identified by ``NEW.property_id``
**before** the COUNT runs. The parent row is the canonical lock
target for several reasons:

  * It is a real schema object (BIGINT primary key on
    ``v1_properties.id``), so a row lock is unambiguous and
    participates in PostgreSQL's deadlock detection.
  * The ``v1_units.property_id`` foreign key is ``RESTRICT``, so the
    parent is guaranteed to exist for any INSERT or
    UPDATE OF property_id that reaches this trigger. If it does not
    (e.g. an operator inserted a unit with a stale property id), we
    raise a clear ``foreign_key_violation`` instead of silently
    passing the cap check.
  * ``FOR UPDATE`` is held for the rest of the transaction, so
    concurrent writers block until the first transaction commits or
    rolls back, after which the second sees the new committed
    count.

Both INSERT and UPDATE OF ``property_id`` go through the same
trigger and use the same lock discipline. The deadlock corner case
(unit A moves from property P1 to P2 while unit B moves from P2 to
P1) is handled by PostgreSQL's deadlock detector: the loser
receives ``40P01 deadlock_detected``, which the application maps
to a 409.

Mechanism:

* BEFORE INSERT OR UPDATE OF ``property_id`` ON ``v1_units``:
  1. ``SELECT 1 FROM v1_properties WHERE id = NEW.property_id
     FOR UPDATE`` — serialize writers per target property; fail
     closed if the parent is missing.
  2. ``SELECT COUNT(*) FROM v1_units WHERE property_id =
     NEW.property_id AND id <> NEW.id`` — count rows that will
     exist after the statement.
  3. If the resulting count would exceed the cap, raise
     ``check_violation units_per_property_cap_exceeded`` which
     the application maps to HTTP 409.

* The cap is exposed as a server-side constant
  ``UNITS_PER_PROPERTY_CAP`` (default 15). If the cap is
  relaxed, the migration must be edited and the trigger
  recreated (the audit explicitly calls this out). The trigger
  body uses the literal so PostgreSQL can inline it.

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

Updated: 2026-09-02 — added row-level ``FOR UPDATE`` lock on the
parent ``v1_properties`` to make the cap race-safe under
``READ COMMITTED``. See PR #116 review (CHANGES_REQUESTED).
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
    #
    #    Serialization step: lock the parent ``v1_properties`` row
    #    identified by ``NEW.property_id`` BEFORE counting. Without
    #    this lock, two concurrent INSERTs/UPDATEs targeting the
    #    same property both observe the same pre-write COUNT under
    #    PostgreSQL's default READ COMMITTED and both commit past
    #    the cap (empirically verified: 5/5 races ended at 16 units
    #    in PR #116 review). The ``FOR UPDATE`` blocks the second
    #    transaction until the first commits, after which the
    #    second sees the updated count.
    #
    #    ``v1_units.property_id`` is ``RESTRICT`` FK to
    #    ``v1_properties.id``, so the parent must exist for any
    #    INSERT/UPDATE that reaches this trigger. If it does not,
    #    we raise ``foreign_key_violation`` rather than silently
    #    passing — fail closed.
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
                -- Serialize writers per target property. The parent
                -- v1_properties row is the canonical lock target for
                -- both INSERT and UPDATE OF property_id (same
                -- discipline). FOR UPDATE participates in
                -- PostgreSQL deadlock detection, so the pathological
                -- "unit A P1->P2 / unit B P2->P1 swap" case is
                -- handled by the engine (40P01 -> 409).
                PERFORM 1
                FROM v1_properties
                WHERE id = NEW.property_id
                FOR UPDATE;

                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'units_per_property_cap_parent_missing: '
                        'property_id=% has no v1_properties row',
                        NEW.property_id
                        USING ERRCODE = 'foreign_key_violation';
                END IF;

                -- Count rows that will exist for NEW.property_id after
                -- this statement. The row being updated (NEW.id) is
                -- currently at OLD.property_id (UPDATE OF property_id
                -- is the only UPDATE case), so we exclude NEW.id to
                -- avoid double-counting it.
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
