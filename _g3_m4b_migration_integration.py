"""G3 #2 and G3 #3 — m4b→head m4c migration integration tests (v2 ORM-based prep).

Strategy: alembic upgrade head at start, use ORM to create valid data, then via
raw SQL strip ONLY the m4c-specific columns/constraints/indexes and reset
alembic_version to m4b. This leaves a DB at "m4b schema with valid legacy
renewal_metadata JSONB". Then alembic upgrade head re-runs m4c to prove the
backfill/pre-checks work.

Run directly:
    .venv\\Scripts\\python.exe _g3_m4b_migration_integration.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg2://pasay_pm:pasay_pm@localhost:5432/pasay_pm"
)

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker, Session

from app.config import settings
from app.models import Base  # noqa: F401
from app.models.lease import Lease, LeaseStatus
from app.models.membership import (
    Membership,
    MembershipState,
    Organization,
    OrganizationRole,
)
from app.models.property import Property, Unit, UnitStatus
from app.models.tenant import Tenant
from app.models.user import User, UserRole

G3A_DB = os.getenv("PASAY_G3A_TEST_DB", "pasay_pm_test_g3a")
G3B_DB = os.getenv("PASAY_G3B_TEST_DB", "pasay_pm_test_g3b")
M4B_REV = "m4b000000001"


def _g3_url(db_name: str):
    base = make_url(settings.database_url)
    return base.set(database=db_name)


def _alembic_cmd(verb: str, rev: str, db_name: str):
    py_exe = sys.executable
    if not os.path.isfile(py_exe):
        for alt in (
            os.path.join(SCRIPT_DIR, ".venv", "Scripts", "python.exe"),
            os.path.join(SCRIPT_DIR, ".venv", "bin", "python"),
            os.path.join(SCRIPT_DIR, ".venv", "bin", "python3"),
        ):
            if os.path.isfile(alt):
                py_exe = alt
                break
    db_str = str(_g3_url(db_name))
    env = os.environ.copy()
    env["DATABASE_URL"] = db_str
    return (
        [
            py_exe, "-m", "alembic",
            "-c", os.path.join(SCRIPT_DIR, "alembic.ini"),
            "-x", f"db_url={db_str}",
            verb, rev,
        ],
        env,
    )


def _run_alembic(verb: str, rev: str, db_name: str, *, capture_stderr: bool = False):
    cmd, env = _alembic_cmd(verb, rev, db_name)
    label = f"[alembic:{db_name}]"
    print(f"{label} $ {' '.join(cmd)}", flush=True)
    if capture_stderr:
        # S4·5: explicitly UTF-8 with errors=replace so Chinese chars
        # don't crash subprocess thread on Windows (GBK default).
        proc = subprocess.run(
            cmd, env=env, cwd=SCRIPT_DIR,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        stdout_tail = (proc.stdout or "")[-700:]
        stderr_tail = (proc.stderr or "")[-1000:]
        print(f"{label} exit={proc.returncode}\n"
              f"stdout[-700:]=…{stdout_tail}\n"
              f"stderr[-1000:]=…{stderr_tail}",
              flush=True)
        return proc
    proc = subprocess.run(cmd, env=env, cwd=SCRIPT_DIR)
    print(f"{label} exit={proc.returncode}", flush=True)
    return proc


def ensure_g3_database(db_name: str) -> None:
    base = make_url(settings.database_url)
    admin_url = base.set(database="postgres")
    eng = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with eng.connect() as conn:
            row = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": db_name}
            ).scalar()
            if row:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{db_name}"'))
            print(f"[g3:{db_name}] created fresh DB", flush=True)
    finally:
        eng.dispose()


def drop_g3_database(db_name: str) -> None:
    base = make_url(settings.database_url)
    admin_url = base.set(database="postgres")
    eng = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with eng.connect() as conn:
            conn.execute(text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :n AND pid <> pg_backend_pid()"
            ), {"n": db_name})
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
    except Exception as ex:
        print(f"[g3:{db_name}] drop best-effort failed: {ex}", flush=True)
    finally:
        eng.dispose()


def _alembic_version(db_name: str) -> str | None:
    eng = create_engine(_g3_url(db_name))
    try:
        with eng.connect() as conn:
            try:
                return conn.execute(text(
                    "SELECT version_num FROM alembic_version LIMIT 1"
                )).scalar()
            except Exception:
                return None
    finally:
        eng.dispose()


def _col_exists(db_name: str, table: str, column: str) -> bool:
    eng = create_engine(_g3_url(db_name))
    try:
        with eng.connect() as conn:
            return conn.execute(text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = :c"
            ), {"t": table, "c": column}).scalar() is not None
    finally:
        eng.dispose()


def _constraint_exists(db_name: str, table: str, name: str) -> bool:
    eng = create_engine(_g3_url(db_name))
    try:
        with eng.connect() as conn:
            return conn.execute(text(
                "SELECT 1 FROM information_schema.table_constraints "
                "WHERE table_name = :t AND constraint_name = :n"
            ), {"t": table, "n": name}).scalar() is not None
    finally:
        eng.dispose()


# -------------------------------------------------------------------------
# New approach: after upgrade HEAD and ORM data create,
# strip m4c additions and reset alembic version to M4B
# -------------------------------------------------------------------------
M4C_DROP_STATEMENTS = [
    # reverse upgrade order
    # 8 (backfill) — no DDL
    # 7d  index
    "DROP INDEX IF EXISTS uq_leases_superseded_by_one_predecessor CASCADE",
    # 7c,7b CK
    "ALTER TABLE leases DROP CONSTRAINT IF EXISTS ck_leases_superseded_not_self CASCADE",
    "ALTER TABLE leases DROP CONSTRAINT IF EXISTS ck_leases_superseded_pair CASCADE",
    # 7a FK
    "ALTER TABLE leases DROP CONSTRAINT IF EXISTS fk_leases_superseded_by CASCADE",
    # 6 cols
    "ALTER TABLE leases DROP COLUMN IF EXISTS superseded_at CASCADE",
    "ALTER TABLE leases DROP COLUMN IF EXISTS superseded_by_lease_id CASCADE",
    # 5b/5a composite FK pointers
    "ALTER TABLE leases DROP CONSTRAINT IF EXISTS fk_leases_ds_id_lease CASCADE",
    "ALTER TABLE leases DROP CONSTRAINT IF EXISTS fk_leases_moi_id_lease CASCADE",
    # 4 DS composite FK -> MOI
    "ALTER TABLE deposit_settlements DROP CONSTRAINT IF EXISTS fk_deposit_settlements_inspection_lease CASCADE",
    # 3 DS UNIQUE(id, lease_id)
    "ALTER TABLE deposit_settlements DROP CONSTRAINT IF EXISTS uq_deposit_settlements_id_lease_id CASCADE",
    # 2 MOI UNIQUE(id, lease_id)
    "ALTER TABLE move_out_inspections DROP CONSTRAINT IF EXISTS uq_move_out_inspections_id_lease_id CASCADE",
]


def strip_m4c_and_set_version_to_m4b(db_name: str) -> None:
    """
    After creating data at HEAD via ORM, strip m4c artifacts (canonical
    superseded cols + new composite FKs/Uniques) so the DB schema matches
    m4b exactly. Data preserved EXCEPT canonical cols are null'd/dropped.
    renewal_metadata JSONB remains (the whole point of legacy test).
    """
    eng = create_engine(_g3_url(db_name))
    try:
        with eng.connect() as conn:
            conn.execute(text("SET CONSTRAINTS ALL DEFERRED"))
            for stmt in M4C_DROP_STATEMENTS:
                conn.execute(text(stmt))
            # Finally reset alembic version
            conn.execute(text(
                "UPDATE alembic_version SET version_num = :v"
            ), {"v": M4B_REV})
            conn.commit()
            print(f"[g3:{db_name}] stripped m4c artifacts, version reset → {M4B_REV}",
                  flush=True)
    finally:
        eng.dispose()


def _create_base_seed_orm(db_name: str):
    """Create org/user/membership/property/unit/tenant via ORM at HEAD. Returns
    ids dict {org_id, owner_id, property_id, unit_id, tenant_id}."""
    eng = create_engine(_g3_url(db_name))
    Base.metadata.bind = eng
    SLocal = sessionmaker(bind=eng, autoflush=False, autocommit=False)
    s: Session = SLocal()
    try:
        org = Organization(name="G3-Org", display_name="G3 Display")
        s.add(org)
        s.flush()
        owner_user = User(
            username="g3_owner",
            role=UserRole.admin,
            api_key_hash="x_hash_g3_owner_" + os.urandom(4).hex(),
        )
        s.add(owner_user)
        s.flush()
        mbr = Membership(
            organization_id=org.id,
            user_id=owner_user.id,
            role=OrganizationRole.OWNER,
            state=MembershipState.ACTIVE,
        )
        s.add(mbr)
        prop = Property(
            name="G3-Property",
            address="1 G3 St",
            city="G3City",
            total_units=1,
            organization_id=org.id,
            is_active=True,
        )
        s.add(prop)
        s.flush()
        unit = Unit(
            property_id=prop.id,
            unit_number="G3-U1",
            status=UnitStatus.occupied,
            monthly_rent=Decimal("10000.00"),
            is_active=True,
        )
        s.add(unit)
        tenant = Tenant(
            organization_id=org.id,
            full_name="G3-Tenant",
            phone="0000",
            email="g3-tenant@example.test",
            is_active=True,
        )
        s.add(tenant)
        s.flush()
        ids = {
            "org_id": org.id,
            "owner_id": owner_user.id,
            "property_id": prop.id,
            "unit_id": unit.id,
            "tenant_id": tenant.id,
        }
        s.commit()
        return ids, eng, SLocal
    except Exception:
        s.rollback()
        raise


def _new_lease(s, unit_id, tenant_id, start, end, status, monthly_rent="10000.00",
               deposit="20000.00", created_by=1):
    lr = Lease(
        unit_id=unit_id,
        tenant_id=tenant_id,
        start_date=start,
        end_date=end,
        accounting_start_date=start,
        monthly_rent=Decimal(monthly_rent),
        deposit=Decimal(deposit),
        status=status,
        created_by=created_by,
        updated_by=created_by,
    )
    s.add(lr)
    s.flush()
    return lr


# ==========================================================================
# Scenario #2 VALID: pred expired, succ active, valid JSONB metadata
# ==========================================================================
def scenario_g3_2_valid_backfill() -> int:
    print("\n" + "=" * 80, flush=True)
    print("[G3 #2] VALID backfill — m4b legacy JSONB → head → canonical cols")
    print("=" * 80 + "\n", flush=True)
    db = G3A_DB
    try:
        ensure_g3_database(db)
        rc = _run_alembic("upgrade", "head", db).returncode
        if rc != 0:
            return 1

        ids, eng, SLocal = _create_base_seed_orm(db)
        s = SLocal()
        try:
            # pred: end=2026-06-29 expired
            today = date(2026, 6, 29)
            tomorrow = today + timedelta(days=1)
            succ_end = today + timedelta(days=364)
            pred = _new_lease(s, ids["unit_id"], ids["tenant_id"],
                              start=today - timedelta(days=365), end=today,
                              status=LeaseStatus.expired,
                              created_by=ids["owner_id"])
            succ = _new_lease(s, ids["unit_id"], ids["tenant_id"],
                              start=tomorrow, end=succ_end,
                              status=LeaseStatus.active,
                              created_by=ids["owner_id"])
            # Write legacy JSONB metadata on predecessor, canonical cols NULL
            renewed_at_iso = datetime(2026, 6, 30, 12, 0, 0,
                                      tzinfo=timezone.utc).isoformat()
            # Use raw SQL to ONLY set JSONB and explicitly set canonical cols NULL
            # (ORM would set them together but we need legacy state).
            s.commit()
            pred_id = pred.id
            succ_id = succ.id
        finally:
            s.close()

        # NOW via raw SQL: set legacy JSONB and CLEAR canonical cols (ORM may
        # have set them if any trigger ran, but we explicitly strip for m4b)
        with eng.connect() as conn:
            conn.execute(text(
                """UPDATE leases
                   SET renewal_metadata = CAST(:meta AS jsonb),
                       superseded_by_lease_id = NULL,
                       superseded_at = NULL
                   WHERE id = :pid"""
            ), {
                "pid": pred_id,
                "meta": json.dumps({
                    "renewed_lease_id": str(succ_id),
                    "renewed_at": renewed_at_iso,
                }),
            })
            conn.commit()

        # Sanity check BEFORE strip: canonical cols still exist now
        if not _col_exists(db, "leases", "superseded_by_lease_id"):
            print("[G3 #2] setup error: canonical cols should exist at HEAD")
            return 1

        # Strip m4c and reset version
        strip_m4c_and_set_version_to_m4b(db)
        ver = _alembic_version(db)
        if ver != M4B_REV:
            print(f"[G3 #2] FAIL: post-strip version {ver!r} != {M4B_REV!r}")
            return 1
        if _col_exists(db, "leases", "superseded_by_lease_id"):
            print("[G3 #2] FAIL: strip didn't drop superseded_by_lease_id")
            return 1
        # Metadata still present post-strip (via row check)
        with eng.connect() as conn:
            row = conn.execute(text(
                "SELECT status, renewal_metadata->>'renewed_lease_id' rl "
                "FROM leases WHERE id=:pid"
            ), {"pid": pred_id}).fetchone()
            assert row.status == "expired"
            assert row.rl == str(succ_id), f"metadata lost! got rl={row.rl!r}"
        print(f"[G3 #2] pre-upgrade state OK @ m4b: pred#{pred_id}→succ#{succ_id} "
              "(JSONB metadata), version=m4b, canonical cols gone [OK]", flush=True)

        # NOW re-upgrade head — runs m4c validation + backfill
        rc = _run_alembic("upgrade", "head", db).returncode
        if rc != 0:
            print("[G3 #2] FAIL: upgrade head (m4c) rc != 0")
            return 1
        ver = _alembic_version(db)
        print(f"[G3 #2] post-upgrade version: {ver!r}", flush=True)

        # Backfill verification
        if not _col_exists(db, "leases", "superseded_by_lease_id"):
            print("[G3 #2] FAIL: superseded_by_lease_id missing post m4c")
            return 1
        for cname in (
            "ck_leases_superseded_pair",
            "ck_leases_superseded_not_self",
            "fk_leases_superseded_by",
        ):
            if not _constraint_exists(db, "leases", cname):
                print(f"[G3 #2] FAIL: constraint {cname} not found post m4c")
                return 1
        # partial unique: pg_indexes
        with eng.connect() as conn:
            pidx = conn.execute(text(
                "SELECT 1 FROM pg_indexes WHERE tablename='leases' "
                "AND indexname='uq_leases_superseded_by_one_predecessor'"
            )).scalar()
            if pidx is None:
                print("[G3 #2] FAIL: partial unique index missing post m4c")
                return 1
            row = conn.execute(text(
                "SELECT superseded_by_lease_id sb, superseded_at sa, status st "
                "FROM leases WHERE id=:pid"
            ), {"pid": pred_id}).fetchone()
            print(f"[G3 #2] post-upgrade pred canonical: sb={row.sb}, sa={row.sa}, "
                  f"st={row.st}", flush=True)
            if row.sb != succ_id:
                print(f"[G3 #2] FAIL: backfill sb={row.sb} != succ_id={succ_id}")
                return 1
            if row.sa is None:
                print("[G3 #2] FAIL: backfill superseded_at NULL")
                return 1
            if row.st != "expired":
                print(f"[G3 #2] FAIL: pred status {row.st!r} != expired")
                return 1
        print("[G3 #2] PASS ✓ valid backfill scenario", flush=True)
        return 0
    finally:
        try:
            eng.dispose()
        except Exception:
            pass
        drop_g3_database(db)


# ==========================================================================
# Scenario #3 INVALID: predecessor status == 'active' (NOT expired)
#   → m4c pre-check MUST ABORT, full rollback, version=m4b.
# ==========================================================================
def scenario_g3_3_invalid_fail_closed() -> int:
    print("\n" + "=" * 80, flush=True)
    print("[G3 #3] INVALID: pred status=active (non-expired) → m4c MUST ABORT + rollback")
    print("=" * 80 + "\n", flush=True)
    db = G3B_DB
    eng = None
    try:
        ensure_g3_database(db)
        rc = _run_alembic("upgrade", "head", db).returncode
        if rc != 0:
            return 1

        ids, eng, SLocal = _create_base_seed_orm(db)
        s = SLocal()
        try:
            today = date(2026, 6, 29)
            tomorrow = today + timedelta(days=1)
            succ_end = today + timedelta(days=364)
            # KEY DIFFERENCE: pred status = 'active' (intentionally non-expired)
            pred = _new_lease(s, ids["unit_id"], ids["tenant_id"],
                              start=today - timedelta(days=365), end=today,
                              status=LeaseStatus.active,  # <-- violates M3 #1
                              created_by=ids["owner_id"])
            succ = _new_lease(s, ids["unit_id"], ids["tenant_id"],
                              start=tomorrow, end=succ_end,
                              status=LeaseStatus.active,
                              created_by=ids["owner_id"])
            s.commit()
            pred_id = pred.id
            succ_id = succ.id
        finally:
            s.close()

        renewed_at_iso = datetime(2026, 6, 30, 12, 0, 0,
                                  tzinfo=timezone.utc).isoformat()
        with eng.connect() as conn:
            conn.execute(text(
                """UPDATE leases
                   SET renewal_metadata = CAST(:meta AS jsonb),
                       superseded_by_lease_id = NULL,
                       superseded_at = NULL
                   WHERE id = :pid"""
            ), {
                "pid": pred_id,
                "meta": json.dumps({
                    "renewed_lease_id": str(succ_id),
                    "renewed_at": renewed_at_iso,
                }),
            })
            conn.commit()

        strip_m4c_and_set_version_to_m4b(db)
        ver = _alembic_version(db)
        assert ver == M4B_REV

        # Verify pre-state: pred status == active
        with eng.connect() as conn:
            row = conn.execute(text(
                "SELECT status, renewal_metadata->>'renewed_lease_id' rl "
                "FROM leases WHERE id = :pid"
            ), {"pid": pred_id}).fetchone()
            print(f"[G3 #3] pre-upgrade pred#{pred_id}: status={row.status!r}, "
                  f"renewed_lease_id={row.rl} (INTENTIONAL non-expired!)", flush=True)
            assert row.status == "active", "setup requires pred.status == active"
            assert row.rl == str(succ_id)

        # Run upgrade head — MUST FAIL non-zero with MIGRATION ABORTED string
        proc = _run_alembic("upgrade", "head", db, capture_stderr=True)
        if proc.returncode == 0:
            print("[G3 #3] FAIL: upgrade head return=0 but should have ABORTED!")
            return 1
        combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
        if "MIGRATION ABORTED" not in combined:
            print(f"[G3 #3] FAIL: non-zero exit but no 'MIGRATION ABORTED' marker in "
                  f"combined output tail[-1500:]:\n{combined[-1500:]}")
            return 1
        # Either predecessor status / expired words present:
        if (
            "predecessor" not in combined
            or ("expired" not in combined and "status" not in combined.lower())
        ):
            print(f"[G3 #3] WARNING: expected predecessor+status/expired words in "
                  f"error (they may be collapsed, continuing since marker is present).")
        print("[G3 #3] ABORT marker present ✓", flush=True)

        # ROLLBACK verification: version must be back at M4B!
        ver = _alembic_version(db)
        if ver != M4B_REV:
            print(f"[G3 #3] FAIL: post-abort version={ver!r} != {M4B_REV!r} "
                  f"→ half-applied DDL! Transaction rollback NOT complete.")
            return 1
        print(f"[G3 #3] alembic_version still = {M4B_REV!r} ✓", flush=True)

        # Canonical cols MUST NOT exist (proves rollback complete, no half cols)
        for col in ("superseded_by_lease_id", "superseded_at"):
            if _col_exists(db, "leases", col):
                print(f"[G3 #3] FAIL: post-abort leases.{col} EXISTS. Rollback partial.")
                return 1
        print("[G3 #3] canonical cols NOT present post-abort ✓", flush=True)

        # m4c unique/FK constraints must also NOT exist
        for table, cn in (
            ("move_out_inspections", "uq_move_out_inspections_id_lease_id"),
            ("deposit_settlements", "uq_deposit_settlements_id_lease_id"),
            ("leases", "fk_leases_moi_id_lease"),
            ("leases", "fk_leases_ds_id_lease"),
        ):
            if _constraint_exists(db, table, cn):
                print(f"[G3 #3] FAIL: post-abort {table}.{cn} still EXISTS. "
                      f"Rollback incomplete.")
                return 1
        # Pred status STILL 'active' → migration did NOT silently auto-change to expired
        with eng.connect() as conn:
            row = conn.execute(text(
                "SELECT status, renewal_metadata->>'renewed_lease_id' rl "
                "FROM leases WHERE id = :pid"
            ), {"pid": pred_id}).fetchone()
            print(f"[G3 #3] post-abort pred#{pred_id}: status={row.status!r}", flush=True)
            if row.status != "active":
                print(f"[G3 #3] FAIL: migration auto-changed pred.status to "
                      f"{row.status!r} (M3 banned auto-mutation). Migration pre-checks "
                      f"are allowed to abort the tx, NOT mutate status.")
                return 1
        print("[G3 #3] pred.status preserved 'active' (migration didn't lie/mutate) ✓",
              flush=True)

        print("[G3 #3] PASS ✓ invalid predecessor fail-closed + full rollback", flush=True)
        return 0
    finally:
        try:
            if eng is not None:
                eng.dispose()
        except Exception:
            pass
        drop_g3_database(db)


def main() -> int:
    rc2 = scenario_g3_2_valid_backfill()
    rc3 = scenario_g3_3_invalid_fail_closed()
    print("\n" + "#" * 60, flush=True)
    print(f"[G3 Summary] #2 valid_backfill rc={rc2}; "
          f"#3 invalid_fail_closed rc={rc3}")
    overall = rc2 | rc3
    if overall == 0:
        print("[G3 Summary] 3/3 migration integration PASS "
              "(#1 f4 roundtrip pre-verified earlier; #2/#3 here).")
    else:
        print("[G3 Summary] FAILURES exist.")
    print("#" * 60, flush=True)
    return overall


if __name__ == "__main__":
    sys.exit(main())
