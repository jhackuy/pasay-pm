"""PASAY-M004 G3/G4 Alembic round-trip + financial data preservation driver.

Scope (Owner PASAY-TASK-012 三 + 四):
  * Disposable PG: pasay_pm_m4d_roundtrip
  * alembic upgrade head -> seed successor same-party sample + financial rows
  * 4 direct-SQL invariant probes (cross unit / cross tenant / nonexistent /
    valid same-party) each in an isolated SAVEPOINT transaction
  * alembic downgrade m4c000000001 -> snapshot
  * alembic upgrade head -> snapshot
  * 3 JSON snapshots: g4_financial_before_seed.json / after_downgrade / after_reupgrade
  * Financial invariants (Income/Expense/DS rows/counts/sums identical)
  * idempotency_key sets identical
  * move-out columns (notes/evidence/deleted_at) still exist
  * no orphan successor link after roundtrip
  * Probes: explicit per-probe savepoint + rollback to prevent tx pollution
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from decimal import Decimal
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import create_engine, text, func as sqla_func
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.exc import IntegrityError, InternalError, ProgrammingError


ADMIN_DB_URL = "postgresql+psycopg2://postgres:postgres@localhost:5432/postgres"
DISPOSABLE_NAME = "pasay_pm_m4d_roundtrip"
DISPOSABLE_URL = f"postgresql+psycopg2://postgres:postgres@localhost:5432/{DISPOSABLE_NAME}"
M4D_DOWN_REV = "m4c000000001"

G4_BEFORE = "g4_financial_before_seed.json"
G4_AFTER_DG = "g4_after_downgrade.json"
G4_AFTER_RU = "g4_after_reupgrade.json"


def run(cmd: list[str], *, env: dict | None = None) -> subprocess.CompletedProcess:
    merged = {**os.environ, **(env or {})}
    return subprocess.run(
        cmd, capture_output=True, text=True, env=merged, cwd=os.getcwd(),
        encoding="utf-8", errors="replace",
    )


def pg_run_admin(sql: str) -> None:
    """Run an admin-level statement via the postgres superuser DB (AUTOCOMMIT)."""
    eng = create_engine(ADMIN_DB_URL, isolation_level="AUTOCOMMIT")
    with eng.connect() as conn:
        conn.execute(text(sql))
    eng.dispose()


def drop_disposable_safe() -> None:
    pg_run_admin(
        f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{DISPOSABLE_NAME}';"
    )
    pg_run_admin(f"DROP DATABASE IF EXISTS {DISPOSABLE_NAME};")


def create_disposable() -> None:
    pg_run_admin(f"CREATE DATABASE {DISPOSABLE_NAME};")


def alembic_run(*args: str) -> None:
    """Run alembic with DATABASE_URL overridden to disposable DB."""
    env = {"DATABASE_URL": DISPOSABLE_URL}
    result = run(
        [".venv\\Scripts\\python.exe", "-m", "alembic", *args],
        env=env,
    )
    marker = "  \n"  # ensure print flush
    print(f"$ alembic {' '.join(args)} exit={result.returncode}")
    if result.stdout:
        print(result.stdout, end=marker)
    if result.stderr:
        print("STDERR:", result.stderr, end=marker)
    if result.returncode != 0:
        raise SystemExit(
            f"ALEMBIC FAILED: alembic {' '.join(args)}\n"
            f"stdout:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )


def make_session() -> Session:
    eng = create_engine(DISPOSABLE_URL, pool_pre_ping=True)
    return sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)()


def seed_all(db: Session) -> dict:
    """Seed DB: org, prop, 2 units, 2 tenants, pred+succ leases (same party),
    financial Income/Expense/DepositSettlement with known idempotency_keys.

    Returns summary dict used for G4 JSON snapshot.
    """
    from app.models.membership import Organization
    from app.models.property import Property, Unit
    from app.models.tenant import Tenant
    from app.models.lease import Lease, LeaseStatus
    from app.models.move_out import MoveOutInspection
    from app.models.evidence import Evidence, EvidenceCategory
    from app.models.financial import Income, Expense
    from app.models.deposit_settlement import DepositSettlement, DepositSettlementStatus

    now = datetime.now(timezone.utc)

    org = Organization(name="g3org")
    db.add(org); db.flush()
    prop = Property(organization_id=org.id, name="g3prop", address="a", city="c", total_units=2)
    db.add(prop); db.flush()
    u1 = Unit(property_id=prop.id, unit_number="G3-U1", monthly_rent=Decimal("1000"))
    u2 = Unit(property_id=prop.id, unit_number="G3-U2", monthly_rent=Decimal("2000"))
    t1 = Tenant(organization_id=org.id, full_name="G3-T1")
    t2 = Tenant(organization_id=org.id, full_name="G3-T2")
    db.add_all([u1, u2, t1, t2]); db.flush()

    start = date(2025,1,1)
    mid = date(2025,12,31)
    end2 = mid + timedelta(days=365)
    # pred must start with superseded_at=NULL AND superseded_by_lease_id=NULL
    # per ck_leases_superseded_pair. Only AFTER succ id is known do we atomically
    # mark pred as expired + set superseded_at + superseded_by_lease_id.
    pred = Lease(
        unit_id=u1.id, tenant_id=t1.id, start_date=start, end_date=mid,
        monthly_rent=Decimal("1000"), deposit=Decimal("24000"),
        deposit_received=Decimal("24000"), notes="pred-g3-notes",
        status=LeaseStatus.active,
    )
    succ = Lease(
        unit_id=u1.id, tenant_id=t1.id, start_date=mid+timedelta(days=1), end_date=end2,
        monthly_rent=Decimal("1000"), deposit=Decimal("24000"),
        status=LeaseStatus.active,
    )
    db.add_all([pred, succ]); db.flush()
    # Atomic transition: status=expired + superseded_at + superseded_by_lease_id
    # ALL together to satisfy ck_leases_superseded_pair
    pred.status = LeaseStatus.expired
    pred.superseded_at = now
    pred.superseded_by_lease_id = succ.id

    insp = MoveOutInspection(
        lease_id=pred.id, unit_id=u1.id, tenant_id=t1.id,
        scheduled_at=now, status="SCHEDULED",
        notes="pre-g3-moveout-internal-note",
    )
    db.add(insp); db.flush()
    # Evidence table (SoftDeleteMixin -> deleted_at column exists)
    ev = Evidence(
        storage_provider="local", unit_id=u1.id,
        external_file_id="g3-photo-1",
        category=EvidenceCategory.move_out_photo,
        property_id=prop.id,
    )
    db.add(ev); db.flush()

    ik_income = "g3-income-rent-2025-12"
    ik_expense = "g3-expense-repairs-2025-12"
    income = Income(
        lease_id=pred.id,
        amount=Decimal("1000"),
        received_date=date(2025, 12, 31),
        status="confirmed",
        idempotency_key=ik_income,
        confirmed_at=now,
        description="rent dec 2025",
    )
    expense = Expense(
        property_id=prop.id,
        unit_id=u1.id,
        expense_date=date(2025, 12, 15),
        category="REPAIRS",
        amount=Decimal("500"),
        payee="Local Repair Co.",
        status="paid",
        idempotency_key=ik_expense,
        description="tap repair",
    )
    ds = DepositSettlement(
        move_out_inspection_id=insp.id, lease_id=pred.id,
        deposit_received=Decimal("24000"), total_deductions=Decimal("5000"),
        refund_amount=Decimal("19000"), status=DepositSettlementStatus.CONFIRMED,
        confirmed_at=now,
    )
    db.add_all([income, expense, ds]); db.flush()
    db.commit()

    return {
        "pred_lease_id": pred.id,
        "succ_lease_id": succ.id,
        "unit_ids": {"u1": u1.id, "u2": u2.id},
        "tenant_ids": {"t1": t1.id, "t2": t2.id},
        "insp_id": insp.id,
        "ev_id": ev.id,
        "ds_id": ds.id,
        "ids": [ik_income, ik_expense],
    }


def _financial_snapshot(db: Session, seed: dict) -> dict:
    from app.models.financial import Income, Expense
    from app.models.deposit_settlement import DepositSettlement

    def snap(model, amount_col, has_ik: bool):
        rows = db.query(model).all()
        total = (
            db.query(sqla_func.coalesce(sqla_func.sum(amount_col), Decimal("0")))
            .scalar()
        )
        iks = []
        if has_ik:
            iks = sorted([r.idempotency_key for r in rows if r.idempotency_key])
        return {
            "count": len(rows),
            "sum": str(Decimal(total)),
            "idempotency_keys": iks,
        }

    from sqlalchemy import inspect as sqa_inspect
    insp = sqa_inspect(db.get_bind())
    evidence_cols = [c["name"] for c in insp.get_columns("evidence")]
    insp_cols = [c["name"] for c in insp.get_columns("move_out_inspections")]
    lease_cols = [c["name"] for c in insp.get_columns("leases")]
    ds_cols = [c["name"] for c in insp.get_columns("deposit_settlements")]
    ev_has_deleted_at = "deleted_at" in evidence_cols
    insp_has_notes = "notes" in insp_cols
    lease_has_notes = "notes" in lease_cols
    ds_has_confirmed_at = "confirmed_at" in ds_cols
    from app.models.lease import Lease
    orphan = (
        db.query(Lease.id)
        .filter(Lease.superseded_by_lease_id.is_not(None))
        .filter(~Lease.superseded_by_lease_id.in_(
            db.query(Lease.id).scalar_subquery()
        ))
        .all()
    )
    pred = db.get(Lease, seed["pred_lease_id"])
    succ_link_ok = pred is not None and pred.superseded_by_lease_id == seed["succ_lease_id"]
    return {
        "Income": snap(Income, Income.amount, has_ik=True),
        "Expense": snap(Expense, Expense.amount, has_ik=True),
        "DepositSettlement": snap(DepositSettlement, DepositSettlement.deposit_received, has_ik=False),
        "cols": {
            "evidence.has_deleted_at": ev_has_deleted_at,
            "move_out_inspections.has_notes": insp_has_notes,
            "leases.has_notes": lease_has_notes,
            "deposit_settlements.has_confirmed_at": ds_has_confirmed_at,
        },
        "successor": {
            "pred_id": seed["pred_lease_id"],
            "expected_succ_id": seed["succ_lease_id"],
            "actual_succ_id": pred.superseded_by_lease_id if pred else None,
            "link_roundtrip_ok": succ_link_ok,
            "orphan_count": len(orphan),
        },
    }


def snapshot_constraints(db: Session) -> dict:
    """Check canonical constraint names uq_leases_id_unit_tenant and
    fk_leases_superseded_same_party exist in the current DB schema.
    Also check the single-col FK (fk_leases_superseded_by)."""
    rs = db.execute(text("""
        SELECT conname, contype, pg_get_constraintdef(c.oid) AS def
        FROM pg_constraint c
        JOIN pg_namespace n ON n.oid = c.connamespace
        WHERE conrelid = 'leases'::regclass
        ORDER BY conname;
    """))
    cons = [{"name": r[0], "type": r[1], "def": r[2]} for r in rs.all()]
    names = {c["name"] for c in cons}
    want = {
        "uq_leases_id_unit_tenant",
        "fk_leases_superseded_same_party",
        "fk_leases_superseded_by",
        "ck_leases_superseded_pair",
    }
    return {
        "constraints": cons,
        "all_canonical_present": want.issubset(names),
        "missing": sorted(want - names),
    }


def probe_sameparty_successor_direct_sql(db: Session, seed: dict) -> None:
    """Direct SQL UPDATE attempt for SAME unit+tenant: must COMMIT cleanly."""
    try:
        db.execute(text("SAVEPOINT probe_same;"))
        db.execute(text("""
            UPDATE leases
            SET superseded_by_lease_id = :sid,
                status = 'expired',
                superseded_at = now()
            WHERE id = :pid
        """), {"sid": seed["succ_lease_id"], "pid": seed["pred_lease_id"]})
        db.execute(text("RELEASE SAVEPOINT probe_same;"))
    except Exception as exc:
        db.execute(text("ROLLBACK TO SAVEPOINT probe_same;"))
        raise AssertionError(f"SAME-party successor direct SQL UPDATE was REJECTED by DB. Link should be allowed. Exc={exc!r}")
    db.expire_all()


def probe_cross_unit_same_tenant_rejected(db: Session, seed: dict) -> None:
    """Cross unit same tenant: DB MUST REJECT via composite FK."""
    from app.models.membership import Organization
    from app.models.property import Unit, Property
    from app.models.tenant import Tenant
    from app.models.lease import Lease
    start = date(2026,1,1)
    mid = date(2026,12,31)
    org = db.query(Organization).filter(Organization.name == "g3org").one()
    prop = db.query(Property).filter(Property.name == "g3prop").one()
    t1 = db.query(Tenant).filter(Tenant.organization_id == org.id, Tenant.full_name == "G3-T1").one()
    u2 = db.query(Unit).filter(Unit.property_id == prop.id, Unit.unit_number == "G3-U2").one()
    alt_succ = Lease(
        unit_id=u2.id, tenant_id=t1.id, start_date=start, end_date=mid,
        monthly_rent=Decimal("1000"), deposit=Decimal("24000"),
        status="active",
    )
    db.add(alt_succ); db.flush()
    alt_succ_id = alt_succ.id
    raised = False
    try:
        db.execute(text("SAVEPOINT probe_cu;"))
        db.execute(text("""
            UPDATE leases SET superseded_by_lease_id = :sid,
                status='expired', superseded_at=now()
            WHERE id = :pid
        """), {"sid": alt_succ_id, "pid": seed["pred_lease_id"]})
        db.execute(text("RELEASE SAVEPOINT probe_cu;"))
    except (IntegrityError, InternalError):
        raised = True
        db.execute(text("ROLLBACK TO SAVEPOINT probe_cu;"))
    db.rollback()  # also discard the alt_succ row
    if not raised:
        raise AssertionError("cross-unit same-tenant successor was NOT rejected by DB. Composite FK invariant broken.")


def probe_same_unit_cross_tenant_rejected(db: Session, seed: dict) -> None:
    from app.models.membership import Organization
    from app.models.property import Unit, Property
    from app.models.tenant import Tenant
    from app.models.lease import Lease
    start = date(2026,1,1)
    mid = date(2026,12,31)
    org = db.query(Organization).filter(Organization.name == "g3org").one()
    prop = db.query(Property).filter(Property.name == "g3prop").one()
    t2 = db.query(Tenant).filter(Tenant.organization_id == org.id, Tenant.full_name == "G3-T2").one()
    u1 = db.query(Unit).filter(Unit.property_id == prop.id, Unit.unit_number == "G3-U1").one()
    alt_succ = Lease(
        unit_id=u1.id, tenant_id=t2.id, start_date=start, end_date=mid,
        monthly_rent=Decimal("1000"), deposit=Decimal("24000"),
        status="active",
    )
    db.add(alt_succ); db.flush()
    alt_succ_id = alt_succ.id
    raised = False
    try:
        db.execute(text("SAVEPOINT probe_ct;"))
        db.execute(text("""
            UPDATE leases SET superseded_by_lease_id = :sid,
                status='expired', superseded_at=now()
            WHERE id = :pid
        """), {"sid": alt_succ_id, "pid": seed["pred_lease_id"]})
        db.execute(text("RELEASE SAVEPOINT probe_ct;"))
    except (IntegrityError, InternalError):
        raised = True
        db.execute(text("ROLLBACK TO SAVEPOINT probe_ct;"))
    db.rollback()
    if not raised:
        raise AssertionError("same-unit cross-tenant successor was NOT rejected by DB. Composite FK invariant broken.")


def probe_nonexistent_succ_rejected(db: Session, seed: dict) -> None:
    raised = False
    try:
        db.execute(text("SAVEPOINT probe_nx;"))
        db.execute(text("""
            UPDATE leases SET superseded_by_lease_id = :sid,
                status='expired', superseded_at=now()
            WHERE id = :pid
        """), {"sid": 9_999_999, "pid": seed["pred_lease_id"]})
        db.execute(text("RELEASE SAVEPOINT probe_nx;"))
    except (IntegrityError, InternalError):
        raised = True
        db.execute(text("ROLLBACK TO SAVEPOINT probe_nx;"))
    # outer session remains clean; NO db_session.rollback() — only expire.
    db.expire_all()
    if not raised:
        raise AssertionError("nonexistent successor_id was NOT rejected by DB. FK invariant broken.")


def write_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    print(f"Wrote {path}")


def assert_identical_financial(before: dict, after: dict, label: str) -> None:
    for table in ("Income", "Expense", "DepositSettlement"):
        b = before[table]; a = after[table]
        if b["count"] != a["count"]:
            raise SystemExit(f"{label} {table} count MISMATCH: before={b['count']} after={a['count']}")
        if Decimal(b["sum"]) != Decimal(a["sum"]):
            raise SystemExit(f"{label} {table} sum MISMATCH: before={b['sum']} after={a['sum']}")
        if b["idempotency_keys"] != a["idempotency_keys"]:
            raise SystemExit(
                f"{label} {table} idempotency_keys MISMATCH:\n"
                f"  before={b['idempotency_keys']}\n  after ={a['idempotency_keys']}"
            )
    print(f"[OK] {label} financial rows/counts/sums/idempotency_keys identical.")


def main() -> int:
    print(f"[G3] Disposable DB: {DISPOSABLE_NAME}")
    drop_disposable_safe()
    create_disposable()
    try:
        print("\n=== Step 1: alembic upgrade head ===")
        alembic_run("upgrade", "head")

        db = make_session()
        seed: dict | None = None
        snap_before: dict | None = None
        snap_after_dg: dict | None = None
        try:
            print("\n=== Step 2: seed successor + financial ===")
            seed = seed_all(db)
            print(f"seeded pred={seed['pred_lease_id']} succ={seed['succ_lease_id']} IK={seed['ids']}")

            print("\n=== Step 3: 4 Direct-SQL invariant probes ===")
            probe_sameparty_successor_direct_sql(db, seed)
            print("  probe SAME party direct SQL: [OK] (committed clean)")
            probe_cross_unit_same_tenant_rejected(db, seed)
            print("  probe CROSS unit same tenant: [OK] (DB REJECTED)")
            probe_same_unit_cross_tenant_rejected(db, seed)
            print("  probe SAME unit cross tenant: [OK] (DB REJECTED)")
            probe_nonexistent_succ_rejected(db, seed)
            print("  probe NONEXISTENT succ id: [OK] (DB REJECTED)")

            print("\n=== Step 4: Constraints canonical names (after upgrade head) ===")
            c_before = snapshot_constraints(db)
            print(f"  constraint names present: {c_before['all_canonical_present']} missing={c_before['missing']}")
            if not c_before["all_canonical_present"]:
                raise SystemExit(f"MISSING canonical constraints after upgrade head: {c_before['missing']}")

            snap_before = _financial_snapshot(db, seed)
            write_json(G4_BEFORE, snap_before)
            db.close()  # close session — use FRESH session after any alembic DDL
            db = None
        except Exception:
            if db is not None:
                db.close()
            raise

        print(f"\n=== Step 5: alembic downgrade {M4D_DOWN_REV} ===")
        alembic_run("downgrade", M4D_DOWN_REV)
        # Re-open session from fresh engine after downgrade DDL
        db = make_session()
        try:
            assert seed is not None and snap_before is not None
            snap_after_dg = _financial_snapshot(db, seed)
            write_json(G4_AFTER_DG, snap_after_dg)
            assert_identical_financial(snap_before, snap_after_dg, label="before_seed vs after_downgrade")
            # cols still exist post-downgrade?
            col_ok = all([
                snap_after_dg["cols"]["evidence.has_deleted_at"],
                snap_after_dg["cols"]["move_out_inspections.has_notes"],
                snap_after_dg["cols"]["leases.has_notes"],
                snap_after_dg["cols"]["deposit_settlements.has_confirmed_at"],
            ])
            if not col_ok:
                raise SystemExit(f"Post-downgrade column check FAILED cols={snap_after_dg['cols']}")
            print("  cols evidence.deleted_at / move_out_inspections.notes / leases.notes / deposit_settlements.confirmed_at: [OK]")
            db.close()
            db = None
        except Exception:
            if db is not None:
                db.close()
            raise

        print("\n=== Step 6: alembic upgrade head (re-upgrade) ===")
        alembic_run("upgrade", "head")
        db = make_session()
        try:
            snap_after_ru = _financial_snapshot(db, seed)
            write_json(G4_AFTER_RU, snap_after_ru)
            assert_identical_financial(snap_before, snap_after_ru, label="before_seed vs after_reupgrade")
            assert_identical_financial(snap_after_dg, snap_after_ru, label="after_downgrade vs after_reupgrade")

            c_after = snapshot_constraints(db)
            print(f"  after re-upgrade constraints present: {c_after['all_canonical_present']} missing={c_after['missing']}")
            if not c_after["all_canonical_present"]:
                raise SystemExit(f"MISSING canonical constraints after re-upgrade: {c_after['missing']}")

            if not snap_after_ru["successor"]["link_roundtrip_ok"]:
                raise SystemExit(
                    "Successor link broken after roundtrip: "
                    f"{snap_after_ru['successor']}"
                )
            if snap_after_ru["successor"]["orphan_count"] != 0:
                raise SystemExit(f"Orphan successors after roundtrip count={snap_after_ru['successor']['orphan_count']}")
            print("  [OK] successor link same-party pred->succ intact, 0 orphans")
        finally:
            if db is not None:
                db.close()

        print("\n[G3/G4 ALL CHECKS PASSED]")
        return 0
    finally:
        drop_disposable_safe()
        print(f"[CLEANUP] Disposable DB {DISPOSABLE_NAME} dropped.")


if __name__ == "__main__":
    sys.exit(main())
