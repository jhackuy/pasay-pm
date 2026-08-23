"""Section五 M004 Alembic real-data round-trip script.

Creates a disposable pasay_pm_test_rt DB, runs alembic upgrade head, seeds
production-style M004 rows with real FK chains via ORM, downgrades to
m2a000000001 (after first deleting rows that depend on M004-only enum
values/columns so legacy CHECK constraints don't violate), re-upgrades head,
and runs final verification probes.

Exit 0 on all 3 probes (after-seed / after-downgrade / after-re-upgrade)
pass. Exit 1 on any probe or subprocess failure.

Run directly:
    .venv\\Scripts\\python.exe _f4_pg_roundtrip_seed.py
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
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://pasay_pm:pasay_pm@localhost:5432/pasay_pm")

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker, Session

from app.config import settings
from app.models import Base  # noqa: F401  (register all tables on Base.metadata)
from app.models.audit_log import AuditAction, AuditLog
from app.models.deposit_settlement import DepositSettlement, DepositSettlementStatus
from app.models.evidence import Evidence, EvidenceCategory
from app.models.financial import Expense, ExpenseStatus, Income, IncomeStatus
from app.models.lease import Lease, LeaseStatus
from app.models.membership import (
    Membership,
    MembershipState,
    Organization,
    OrganizationRole,
)
from app.models.move_out import MoveOutInspection, MoveOutInspectionStatus
from app.models.operations import (
    OperationalTask,
    OperationalTaskPriority,
    OperationalTaskStatus,
    OperationalTaskType,
    Recurrence,
    RecurringRule,
)
from app.models.property import Property, Unit, UnitStatus
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.audit import record_audit, serialize_row

RT_DB_NAME = os.getenv("PASAY_RT_TEST_DB", "pasay_pm_test_rt")
M2A_REV = "m2a000000001"
PROBE_FAILED = 0
SEED_SNAPSHOT: dict[str, object] = {}


def _rt_url():
    base = make_url(settings.database_url)
    return base.set(database=RT_DB_NAME)


def _alembic_cmd(verb: str, rev: str) -> list[str]:
    py_exe = sys.executable
    rt_str = str(_rt_url())
    env = os.environ.copy()
    env["DATABASE_URL"] = rt_str
    return [
        py_exe, "-m", "alembic",
        "-c", os.path.join(SCRIPT_DIR, "alembic.ini"),
        "-x", f"db_url={rt_str}",
        verb, rev,
    ], env


def _run_alembic(verb: str, rev: str) -> int:
    cmd, env = _alembic_cmd(verb, rev)
    print(f"[alembic] $ {' '.join(cmd)}", flush=True)
    proc = subprocess.run(cmd, env=env, cwd=SCRIPT_DIR)
    return proc.returncode


def ensure_rt_database() -> None:
    base = make_url(settings.database_url)
    admin_url = base.set(database="postgres")
    eng = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with eng.connect() as conn:
            row = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"),
                {"n": RT_DB_NAME},
            ).scalar()
            if row:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{RT_DB_NAME}"'))
            conn.execute(text(f'CREATE DATABASE "{RT_DB_NAME}"'))
    finally:
        eng.dispose()


def seed_m004_rows(db: Session) -> tuple[dict, int]:
    org = Organization(name="RT-Org", display_name="RT Display Name")
    db.add(org)
    db.flush()
    owner = User(username="rt_owner", role=UserRole.admin,
                 api_key_hash="rt_owner_hash_placeholder", is_active=True)
    secy = User(username="rt_secy", role=UserRole.manager,
                api_key_hash="rt_secy_hash_placeholder", is_active=True)
    db.add_all([owner, secy])
    db.flush()
    for u in (owner, secy):
        db.add(Membership(
            organization_id=org.id,
            user_id=u.id,
            role=OrganizationRole.OWNER if u is owner else OrganizationRole.SECRETARY,
            state=MembershipState.ACTIVE,
        ))
    db.flush()
    owner_uid = owner.id

    prop = Property(
        organization_id=org.id,
        name="RT Sunset Tower",
        address="1 Roxas Blvd",
        city="Pasay",
        total_units=4,
        is_active=True,
        operational_notes="RT operational notes value round-trip",
    )
    prop.created_by = owner_uid
    prop.updated_by = owner_uid
    db.add(prop)
    db.flush()

    unit_active = Unit(
        property_id=prop.id,
        unit_number="101",
        floor="1",
        size_sqm=Decimal("32.50"),
        monthly_rent=Decimal("12000.00"),
        status=UnitStatus.occupied,
        is_active=True,
    )
    unit_active.created_by = owner_uid
    unit_active.updated_by = owner_uid
    unit_deleted = Unit(
        property_id=prop.id,
        unit_number="102",
        floor="1",
        size_sqm=Decimal("35.00"),
        monthly_rent=Decimal("15000.00"),
        status=UnitStatus.vacant,
        is_active=False,
        deleted_at=datetime.now(timezone.utc),
    )
    unit_deleted.created_by = owner_uid
    unit_deleted.updated_by = owner_uid
    db.add_all([unit_active, unit_deleted])
    db.flush()

    tenant = Tenant(
        organization_id=org.id,
        full_name="Juan RT Dela Cruz",
        phone="+639170000001",
        email="rt.juan@example.com",
        is_active=True,
    )
    tenant.created_by = owner_uid
    tenant.updated_by = owner_uid
    db.add(tenant)
    db.flush()

    today = date.today()
    start_d = today - timedelta(days=365)
    end_d = today - timedelta(days=1)
    lease = Lease(
        unit_id=unit_active.id,
        tenant_id=tenant.id,
        start_date=start_d,
        end_date=end_d,
        accounting_start_date=start_d,
        monthly_rent=Decimal("12000.00"),
        deposit=Decimal("50000.00"),
        deposit_received=Decimal("50000.00"),
        status=LeaseStatus.active,
        renewal_metadata={"not_renewed": True},
    )
    lease.created_by = owner_uid
    lease.updated_by = owner_uid
    db.add(lease)
    db.flush()

    pred_start = today - timedelta(days=730)
    pred_end = start_d - timedelta(days=1)
    lease_pred = Lease(
        unit_id=unit_active.id,
        tenant_id=tenant.id,
        start_date=pred_start,
        end_date=pred_end,
        accounting_start_date=pred_start,
        monthly_rent=Decimal("11000.00"),
        deposit=Decimal("45000.00"),
        deposit_received=Decimal("45000.00"),
        status=LeaseStatus.expired,
        superseded_by_lease_id=lease.id,
        superseded_at=datetime.combine(start_d, datetime.min.time(), tzinfo=timezone.utc),
        renewal_metadata={"renewed_to_lease_id": lease.id, "renewed_at": start_d.isoformat()},
    )
    lease_pred.created_by = owner_uid
    lease_pred.updated_by = owner_uid
    db.add(lease_pred)
    db.flush()

    insp = MoveOutInspection(
        lease_id=lease.id,
        unit_id=unit_active.id,
        tenant_id=tenant.id,
        scheduled_at=datetime.now(timezone.utc),
        inspected_at=datetime.now(timezone.utc),
        confirmed_at=datetime.now(timezone.utc),
        status=MoveOutInspectionStatus.CONFIRMED,
        findings=[
            {"item": "wall", "severity": "high", "cost": "3500.00",
             "description": None},
        ],
        evidence_ids=[1, 2],
        notes="Notes",
        confirmed_by=owner_uid,
    )
    insp.created_by = owner_uid
    insp.updated_by = owner_uid
    db.add(insp)
    db.flush()

    e1 = Evidence(
        storage_provider="local",
        external_file_id="rt-moveout-photo-1",
        category=EvidenceCategory.move_out_photo,
        unit_id=unit_active.id,
        uploaded_by=owner_uid,
        property_id=prop.id,
    )
    e1.created_by = owner_uid
    e1.updated_by = owner_uid
    e2 = Evidence(
        storage_provider="local",
        external_file_id="rt-moveout-photo-2",
        category=EvidenceCategory.move_out_photo,
        unit_id=unit_active.id,
        uploaded_by=owner_uid,
        property_id=prop.id,
        deleted_at=datetime.now(timezone.utc),
    )
    e2.created_by = owner_uid
    e2.updated_by = owner_uid
    db.add_all([e1, e2])
    db.flush()

    insp.evidence_ids = [e1.id, e2.id]
    db.flush()

    settle = DepositSettlement(
        lease_id=lease.id,
        move_out_inspection_id=insp.id,
        deposit_received=Decimal("50000.00"),
        total_deductions=Decimal("3500.00"),
        refund_amount=Decimal("46500.00"),
        status=DepositSettlementStatus.DRAFT,
        deductions=[
            {"description": "wall repair", "amount": "3500.00", "income_id": None},
        ],
    )
    settle.created_by = owner_uid
    settle.updated_by = owner_uid
    db.add(settle)
    db.flush()

    ikey = "deposit_settlement:SEED:deduction:0"
    income_row = Income(
        lease_id=lease.id,
        amount=Decimal("3500.00"),
        received_date=today,
        idempotency_key=ikey,
        status=IncomeStatus.confirmed,
        description="[SEED DepositSettlement] wall repair deduction",
        confirmed_by=owner_uid,
        confirmed_at=datetime.now(timezone.utc),
    )
    income_row.created_by = owner_uid
    income_row.updated_by = owner_uid
    db.add(income_row)
    db.flush()
    deductions = [dict(d) for d in (settle.deductions or [])]
    if deductions:
        deductions[0]["income_id"] = income_row.id
        settle.deductions = deductions
        db.flush()

    ekey = "deposit_settlement:SEED:refund"
    exp = Expense(
        expense_date=today,
        due_date=None,
        category="deposit_refund",
        amount=Decimal("46500.00"),
        payee="TENANT_REFUND",
        description="[SEED DepositSettlement] 押金退款 - Lease #%d" % lease.id,
        property_id=prop.id,
        unit_id=unit_active.id,
        status=ExpenseStatus.pending,
        payer_user_id=None,
        idempotency_key=ekey,
    )
    exp.created_by = owner_uid
    exp.updated_by = owner_uid
    db.add(exp)
    db.flush()

    legacy_income = Income(
        lease_id=lease.id,
        amount=Decimal("12000.00"),
        received_date=today - timedelta(days=30),
        idempotency_key=None,
        status=IncomeStatus.confirmed,
        description="[SEED LEGACY M003] monthly rent received control",
        confirmed_by=owner_uid,
        confirmed_at=datetime.now(timezone.utc),
    )
    legacy_income.created_by = owner_uid
    legacy_income.updated_by = owner_uid
    legacy_expense = Expense(
        expense_date=today - timedelta(days=45),
        due_date=None,
        category="maintenance_repair",
        amount=Decimal("2500.00"),
        payee="Local AC Repair Shop",
        description="[SEED LEGACY M003] quarterly AC maintenance control row",
        property_id=prop.id,
        unit_id=unit_active.id,
        status=ExpenseStatus.paid,
        payer_user_id=None,
        idempotency_key=None,
    )
    legacy_expense.created_by = owner_uid
    legacy_expense.updated_by = owner_uid
    db.add_all([legacy_income, legacy_expense])
    db.flush()

    task_insp = OperationalTask(
        task_type=OperationalTaskType.MOVE_OUT_INSPECTION,
        title="RT move-out inspection",
        description="RT dedupe",
        property_id=prop.id,
        tenant_id=tenant.id,
        lease_id=lease.id,
        source_type="move_out_inspection",
        source_id=insp.id,
        priority=OperationalTaskPriority.high,
        status=OperationalTaskStatus.PENDING,
        due_at=datetime.now(timezone.utc) + timedelta(days=1),
        dedupe_key=f"lease:{lease.id}:MOVE_OUT_INSPECTION",
        details={"seed": True},
    )
    task_insp.created_by = owner_uid
    task_insp.updated_by = owner_uid
    task_settle = OperationalTask(
        task_type=OperationalTaskType.DEPOSIT_SETTLEMENT,
        title="RT deposit settlement",
        property_id=prop.id,
        tenant_id=tenant.id,
        lease_id=lease.id,
        source_type="deposit_settlement",
        source_id=settle.id,
        priority=OperationalTaskPriority.high,
        status=OperationalTaskStatus.PENDING,
        due_at=datetime.now(timezone.utc) + timedelta(days=2),
        dedupe_key=f"deposit_settlement:{settle.id}:DEPOSIT_SETTLEMENT",
        details={"seed": True},
    )
    task_settle.created_by = owner_uid
    task_settle.updated_by = owner_uid
    task_rent_due_control = OperationalTask(
        task_type=OperationalTaskType.RENT_DUE,
        title="RT monthly rent due (M003 control)",
        property_id=prop.id,
        tenant_id=tenant.id,
        lease_id=lease.id,
        source_type="rent_due",
        source_id=None,
        priority=OperationalTaskPriority.medium,
        status=OperationalTaskStatus.PENDING,
        due_at=datetime.now(timezone.utc) + timedelta(days=3),
        dedupe_key=f"lease:{lease.id}:RENT_DUE:control",
        details={"seed": True, "m003_control": True},
    )
    task_rent_due_control.created_by = owner_uid
    task_rent_due_control.updated_by = owner_uid
    db.add_all([task_insp, task_settle, task_rent_due_control])
    db.flush()

    rr1 = RecurringRule(
        rule_type=OperationalTaskType.MOVE_OUT_INSPECTION,
        title="RT move-out inspection rule (M004)",
        property_id=prop.id,
        recurrence=Recurrence.monthly,
        next_run_at=datetime.now(timezone.utc) + timedelta(days=5),
        enabled=True,
    )
    rr1.created_by = owner_uid
    rr1.updated_by = owner_uid
    rr2 = RecurringRule(
        rule_type=OperationalTaskType.DEPOSIT_SETTLEMENT,
        title="RT deposit settlement rule (M004)",
        property_id=prop.id,
        recurrence=Recurrence.quarterly,
        next_run_at=datetime.now(timezone.utc) + timedelta(days=90),
        enabled=True,
    )
    rr2.created_by = owner_uid
    rr2.updated_by = owner_uid
    rr3 = RecurringRule(
        rule_type=OperationalTaskType.RENT_DUE,
        title="RT monthly rent rule (M003 control)",
        property_id=prop.id,
        recurrence=Recurrence.monthly,
        next_run_at=datetime.now(timezone.utc) + timedelta(days=7),
        enabled=True,
    )
    rr3.created_by = owner_uid
    rr3.updated_by = owner_uid
    rr4 = RecurringRule(
        rule_type=OperationalTaskType.AC_MAINTENANCE,
        title="RT quarterly AC rule (M003 control)",
        property_id=prop.id,
        recurrence=Recurrence.quarterly,
        next_run_at=datetime.now(timezone.utc) + timedelta(days=120),
        enabled=True,
    )
    rr4.created_by = owner_uid
    rr4.updated_by = owner_uid
    db.add_all([rr1, rr2, rr3, rr4])
    db.flush()

    for obj, tname in [
        (org, "organizations"),
        (owner, "users"),
        (secy, "users"),
        (prop, "properties"),
        (unit_active, "units"),
        (unit_deleted, "units"),
        (tenant, "tenants"),
        (lease_pred, "leases"),
        (lease, "leases"),
        (insp, "move_out_inspections"),
        (e1, "evidence"),
        (e2, "evidence"),
        (settle, "deposit_settlements"),
        (income_row, "incomes"),
        (legacy_income, "incomes"),
        (exp, "expenses"),
        (legacy_expense, "expenses"),
        (task_insp, "operational_tasks"),
        (task_settle, "operational_tasks"),
        (task_rent_due_control, "operational_tasks"),
        (rr1, "recurring_rules"),
        (rr2, "recurring_rules"),
        (rr3, "recurring_rules"),
        (rr4, "recurring_rules"),
    ]:
        record_audit(
            db,
            table_name=tname,
            record_id=obj.id,
            action=AuditAction.create,
            actor_id=owner_uid,
            new_value=serialize_row(obj),
        )

    db.commit()

    counts = {
        "insp": db.query(MoveOutInspection).count(),
        "settle": db.query(DepositSettlement).count(),
        "income": db.query(Income).filter(Income.idempotency_key == ikey).count(),
        "expense": db.query(Expense).filter(Expense.idempotency_key == ekey).count(),
        "op_tasks": db.query(OperationalTask).filter(
            OperationalTask.task_type.in_([
                OperationalTaskType.MOVE_OUT_INSPECTION,
                OperationalTaskType.DEPOSIT_SETTLEMENT,
            ])
        ).count(),
        "leases": db.query(Lease).count(),
        "legacy_op_tasks": db.query(OperationalTask).filter(
            OperationalTask.task_type.notin_([
                OperationalTaskType.MOVE_OUT_INSPECTION,
                OperationalTaskType.DEPOSIT_SETTLEMENT,
            ])
        ).count(),
        "legacy_rr": db.query(RecurringRule).filter(
            RecurringRule.rule_type.notin_([
                OperationalTaskType.MOVE_OUT_INSPECTION,
                OperationalTaskType.DEPOSIT_SETTLEMENT,
            ])
        ).count(),
        "m004_rr": db.query(RecurringRule).filter(
            RecurringRule.rule_type.in_([
                OperationalTaskType.MOVE_OUT_INSPECTION,
                OperationalTaskType.DEPOSIT_SETTLEMENT,
            ])
        ).count(),
    }
    return counts, owner_uid


def probe_after_seed(db: Session) -> bool:
    global PROBE_FAILED, SEED_SNAPSHOT
    ok = True
    cnt_insp = db.query(MoveOutInspection).count()
    cnt_settle = db.query(DepositSettlement).count()
    # --- M1: require M004-specific OperationalTask / RecurringRule counts >0 ---
    # BEFORE downgrade we must have real rows present, so later 9a downgrade
    # migration is forced to clean them (not test-side pre-cleanup).
    cnt_m004_op = db.query(OperationalTask).filter(
        OperationalTask.task_type.in_([
            OperationalTaskType.MOVE_OUT_INSPECTION,
            OperationalTaskType.DEPOSIT_SETTLEMENT,
        ])
    ).count()
    if cnt_m004_op < 2:
        print(f"[PROBE1 FAIL] M1 expected >=2 M004 op tasks before downgrade, got {cnt_m004_op}", flush=True)
        ok = False
    else:
        print(f"[PROBE1 M1-OK] M004 op tasks present before downgrade: {cnt_m004_op}", flush=True)
    cnt_m004_rr = db.query(RecurringRule).filter(
        RecurringRule.rule_type.in_([
            OperationalTaskType.MOVE_OUT_INSPECTION,
            OperationalTaskType.DEPOSIT_SETTLEMENT,
        ])
    ).count()
    if cnt_m004_rr < 2:
        print(f"[PROBE1 FAIL] M1 expected >=2 M004 RecurringRule rows before downgrade, got {cnt_m004_rr}", flush=True)
        ok = False
    else:
        print(f"[PROBE1 M1-OK] M004 RecurringRule present before downgrade: {cnt_m004_rr}", flush=True)
    # --- M4: snapshot all Income/Expense rows by stable PK tuple (id,amount,description,lease_id,status) ---
    #     Used later in probe_after_downgrade to prove NO rows deleted,
    #     only idempotency_key column dropped.
    # NOTE: Income has (id,lease_id,amount,status,desc) NO property_id/unit_id.
    #       Expense has (id,property_id,unit_id,amount,status,desc) NO lease_id.
    #       Both snapshotted as 7-tuple (id, lease_id_or_None, property_id_or_None,
    #       unit_id_or_None, amount_str, status, desc_or_empty) via raw SQL.
    incomes_raw = db.execute(text("""
        SELECT id, lease_id, NULL::bigint AS property_id, NULL::bigint AS unit_id,
               amount::text, status::text, COALESCE(description, '') AS description,
               idempotency_key
        FROM incomes ORDER BY id
    """)).fetchall()
    expenses_raw = db.execute(text("""
        SELECT id, NULL::bigint AS lease_id, property_id, unit_id,
               amount::text, status::text, COALESCE(description, '') AS description,
               idempotency_key
        FROM expenses ORDER BY id
    """)).fetchall()
    m004_incomes = [r for r in incomes_raw if (r[7] or "").startswith("deposit_settlement:")]
    m004_expenses = [r for r in expenses_raw if (r[7] or "").startswith("deposit_settlement:")]
    legacy_incomes = [r for r in incomes_raw if not (r[7] or "").startswith("deposit_settlement:")]
    legacy_expenses = [r for r in expenses_raw if not (r[7] or "").startswith("deposit_settlement:")]
    if len(m004_incomes) < 1 or len(m004_expenses) < 1:
        print(f"[PROBE1 FAIL] M4 expected >=1 m004 income/expense rows, got income={len(m004_incomes)} expense={len(m004_expenses)}", flush=True)
        ok = False
    if len(legacy_incomes) < 1 or len(legacy_expenses) < 1:
        print(f"[PROBE1 FAIL] M4 control legacy financial rows missing: income={len(legacy_incomes)} expense={len(legacy_expenses)}", flush=True)
        ok = False
    SEED_SNAPSHOT["incomes"] = [(r[0], r[1], r[2], r[3], r[4], r[5], r[6]) for r in incomes_raw]
    SEED_SNAPSHOT["expenses"] = [(r[0], r[1], r[2], r[3], r[4], r[5], r[6]) for r in expenses_raw]
    SEED_SNAPSHOT["cnt_m004_op"] = cnt_m004_op
    SEED_SNAPSHOT["cnt_m004_rr"] = cnt_m004_rr
    print(f"[PROBE1 M4-OK] financial rows snapshotted: income={len(incomes_raw)} expense={len(expenses_raw)} m004_inc={len(m004_incomes)} m004_exp={len(m004_expenses)} legacy_inc={len(legacy_incomes)} legacy_exp={len(legacy_expenses)}", flush=True)
    cnt_income = db.query(Income).count()
    cnt_expense = db.query(Expense).count()
    cnt_op = db.query(OperationalTask).filter(
        OperationalTask.task_type.in_([
            OperationalTaskType.MOVE_OUT_INSPECTION,
            OperationalTaskType.DEPOSIT_SETTLEMENT,
        ])
    ).count()
    cnt_legacy_op = db.query(OperationalTask).filter(
        OperationalTask.task_type.notin_([
            OperationalTaskType.MOVE_OUT_INSPECTION,
            OperationalTaskType.DEPOSIT_SETTLEMENT,
        ])
    ).count()
    cnt_legacy_rr = db.query(RecurringRule).filter(
        RecurringRule.rule_type.notin_([
            OperationalTaskType.MOVE_OUT_INSPECTION,
            OperationalTaskType.DEPOSIT_SETTLEMENT,
        ])
    ).count()
    cnt_leases = db.query(Lease).count()
    if cnt_insp != 1:
        print(f"[PROBE1 FAIL] expected 1 insp, got {cnt_insp}", flush=True)
        ok = False
    if cnt_settle != 1:
        print(f"[PROBE1 FAIL] expected 1 settle, got {cnt_settle}", flush=True)
        ok = False
    if cnt_income < 1:
        print(f"[PROBE1 FAIL] expected >=1 income, got {cnt_income}", flush=True)
        ok = False
    if cnt_expense < 1:
        print(f"[PROBE1 FAIL] expected >=1 expense, got {cnt_expense}", flush=True)
        ok = False
    if cnt_op != 2:
        print(f"[PROBE1 FAIL] expected 2 MOVE_OUT/DEPOSIT op_tasks, got {cnt_op}", flush=True)
        ok = False
    if cnt_legacy_op < 1:
        print(f"[PROBE1 FAIL] expected >=1 legacy op_tasks (RENT_DUE control), got {cnt_legacy_op}", flush=True)
        ok = False
    if cnt_legacy_rr < 2:
        print(f"[PROBE1 FAIL] expected >=2 legacy recurring_rules (RENT_DUE/AC_MAINTENANCE controls), got {cnt_legacy_rr}", flush=True)
        ok = False
    if cnt_leases < 2:
        print(f"[PROBE1 FAIL] expected >=2 leases (pred+succ renewal pair), got {cnt_leases}", flush=True)
        ok = False
    for s in db.query(DepositSettlement).all():
        if s.move_out_inspection_id is None or s.lease_id is None:
            print("[PROBE1 FAIL] settlement FK invalid", flush=True)
            ok = False
    for i in db.query(MoveOutInspection).all():
        if i.lease_id is None:
            print("[PROBE1 FAIL] inspection lease_id missing", flush=True)
            ok = False
    superseded_pair = db.query(Lease).filter(Lease.superseded_by_lease_id.isnot(None)).first()
    if superseded_pair is None:
        print("[PROBE1 FAIL] no renewal predecessor with superseded_by_lease_id found", flush=True)
        ok = False
    else:
        succ = db.query(Lease).get(superseded_pair.superseded_by_lease_id)
        if succ is None:
            print("[PROBE1 FAIL] superseded_by_lease_id points to nonexistent successor", flush=True)
            ok = False
        else:
            expected_start = superseded_pair.end_date + timedelta(days=1)
            if succ.start_date != expected_start:
                print(f"[PROBE1 FAIL] successor.start_date {succ.start_date} != pred.end+1 {expected_start}", flush=True)
                ok = False
            if succ.unit_id != superseded_pair.unit_id:
                print("[PROBE1 FAIL] successor.unit_id mismatch with predecessor", flush=True)
                ok = False
            if succ.tenant_id != superseded_pair.tenant_id:
                print("[PROBE1 FAIL] successor.tenant_id mismatch with predecessor", flush=True)
                ok = False

    expected_constraints = [
        ("uq_move_out_inspections_id_lease_id", "UNIQUE"),
        ("uq_deposit_settlements_id_lease_id", "UNIQUE"),
        ("fk_deposit_settlements_inspection_lease", "FOREIGN KEY"),
        ("fk_leases_moi_id_lease", "FOREIGN KEY"),
        ("fk_leases_ds_id_lease", "FOREIGN KEY"),
        ("ck_leases_superseded_pair", "CHECK"),
        ("uq_leases_superseded_by_one_predecessor", "UNIQUE"),
    ]
    for cname, ctype in expected_constraints:
        if ctype == "FOREIGN KEY":
            row = db.execute(text("""
                SELECT 1 FROM information_schema.table_constraints
                WHERE constraint_name = :cname AND constraint_type = 'FOREIGN KEY'
                LIMIT 1
            """), {"cname": cname}).scalar()
        elif ctype == "UNIQUE":
            row = db.execute(text("""
                SELECT 1 FROM information_schema.table_constraints
                WHERE constraint_name = :cname AND constraint_type = 'UNIQUE'
                LIMIT 1
            """), {"cname": cname}).scalar()
            if row != 1:
                row = db.execute(text("""
                    SELECT 1 FROM pg_indexes
                    WHERE indexname = :cname AND indexdef LIKE '%UNIQUE%'
                    LIMIT 1
                """), {"cname": cname}).scalar()
        elif ctype == "CHECK":
            row = db.execute(text("""
                SELECT 1 FROM information_schema.table_constraints
                WHERE constraint_name = :cname AND constraint_type = 'CHECK'
                LIMIT 1
            """), {"cname": cname}).scalar()
        else:
            row = None
        if row != 1:
            print(f"[PROBE1 FAIL] {ctype} constraint '{cname}' NOT found in information_schema / pg_indexes", flush=True)
            ok = False

    if ok:
        print("[PROBE1 PASS] after-seed counts + FK + renewal pair + 7 constraints OK", flush=True)
    else:
        PROBE_FAILED += 1
    return ok


def probe_after_downgrade(db: Session) -> bool:
    global PROBE_FAILED, SEED_SNAPSHOT
    ok = True
    try:
        col_units_deleted = db.execute(text("""
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'units' AND column_name = 'deleted_at'
            LIMIT 1
        """)).scalar()
        if col_units_deleted is None:
            print(f"[PROBE2 FAIL] units.deleted_at column missing (info_schema)", flush=True)
            ok = False
    except Exception as e:
        db.rollback()
        print(f"[PROBE2 FAIL] probe units.deleted_at info_schema query error: {e}", flush=True)
        ok = False
    try:
        col_prop_notes = db.execute(text("""
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'properties' AND column_name = 'operational_notes'
            LIMIT 1
        """)).scalar()
        if col_prop_notes is None:
            print(f"[PROBE2 FAIL] properties.operational_notes missing (info_schema)", flush=True)
            ok = False
    except Exception as e:
        db.rollback()
        print(f"[PROBE2 FAIL] probe properties.operational_notes info_schema query error: {e}", flush=True)
        ok = False
    # --- M1: M004-only OperationalTask/RecurringRule rows MUST be 0 now ---
    #     (seeded earlier as >0; downgrade is handled by 9a migration itself,
    #      NOT by any test-side delete_m004_rows_before_downgrade which we deleted.)
    m004_op_after = db.execute(text("""
        SELECT COUNT(*) FROM operational_tasks
        WHERE task_type IN ('MOVE_OUT_INSPECTION', 'DEPOSIT_SETTLEMENT')
    """)).scalar() or 0
    expected_m004_op = int(SEED_SNAPSHOT.get("cnt_m004_op") or 0)
    if expected_m004_op < 2:
        print(f"[PROBE2 FAIL] M1 seed did not record expected M004 op cnt: {expected_m004_op}", flush=True)
        ok = False
    if m004_op_after != 0:
        print(f"[PROBE2 FAIL] M1 9a downgrade did not clean M004 op tasks! before={expected_m004_op} after={m004_op_after} (migration must handle this, not test script)", flush=True)
        ok = False
    else:
        print(f"[PROBE2 M1-OK] M004 op tasks cleaned BY MIGRATION ALONE: before={expected_m004_op} after=0", flush=True)
    m004_rr_after = db.execute(text("""
        SELECT COUNT(*) FROM recurring_rules
        WHERE rule_type IN ('MOVE_OUT_INSPECTION', 'DEPOSIT_SETTLEMENT')
    """)).scalar() or 0
    expected_m004_rr = int(SEED_SNAPSHOT.get("cnt_m004_rr") or 0)
    if m004_rr_after != 0:
        print(f"[PROBE2 FAIL] M1 9a downgrade did not clean M004 RecurringRule! before={expected_m004_rr} after={m004_rr_after}", flush=True)
        ok = False
    else:
        print(f"[PROBE2 M1-OK] M004 RecurringRule cleaned BY MIGRATION: before={expected_m004_rr} after=0", flush=True)
    legacy_op_count = db.execute(text("""
        SELECT COUNT(*) FROM operational_tasks
        WHERE task_type NOT IN ('MOVE_OUT_INSPECTION', 'DEPOSIT_SETTLEMENT')
    """)).scalar()
    if legacy_op_count is None or legacy_op_count < 1:
        print(f"[PROBE2 FAIL] 9a downgrade deleted M003 legacy op_tasks! count={legacy_op_count} (expected >0 RENT_DUE controls preserved)", flush=True)
        ok = False
    else:
        print(f"[PROBE2 INFO] legacy op_tasks preserved after 9a downgrade: count={legacy_op_count}", flush=True)
    legacy_rr_count = db.execute(text("""
        SELECT COUNT(*) FROM recurring_rules
        WHERE rule_type NOT IN ('MOVE_OUT_INSPECTION', 'DEPOSIT_SETTLEMENT')
    """)).scalar()
    if legacy_rr_count is None or legacy_rr_count < 1:
        print(f"[PROBE2 FAIL] 9a downgrade deleted M003 legacy recurring_rules! count={legacy_rr_count} (expected >0 RENT_DUE/AC_MAINTENANCE controls preserved)", flush=True)
        ok = False
    else:
        print(f"[PROBE2 INFO] legacy recurring_rules preserved after 9a downgrade: count={legacy_rr_count}", flush=True)
    # --- M4: financial rows preserved exactly (PK + lease+property+unit+amount+status+description match snapshot) ---
    # After f4b downgrade: expenses.idempotency_key column has been DROPPED;
    # income.idempotency_key still exists. All rows must remain intact.
    # Use raw SQL with same NULL-placeholders as probe_after_seed for 7-tuple alignment.
    incomes_after_raw = db.execute(text("""
        SELECT id, lease_id, NULL::bigint AS property_id, NULL::bigint AS unit_id,
               amount::text, status::text, COALESCE(description, '') AS description
        FROM incomes ORDER BY id
    """)).fetchall()
    inc_tuples_after = [(r[0], r[1], r[2], r[3], r[4], r[5], r[6]) for r in incomes_after_raw]
    inc_tuples_before = list(SEED_SNAPSHOT.get("incomes") or [])
    if inc_tuples_after != inc_tuples_before:
        print(f"[PROBE2 FAIL] M4 income rows changed after downgrade! before cnt={len(inc_tuples_before)} after cnt={len(inc_tuples_after)}; diff expected=empty.", flush=True)
        if len(inc_tuples_before) == len(inc_tuples_after):
            for i, (a, b) in enumerate(zip(inc_tuples_before, inc_tuples_after)):
                if a != b:
                    print(f"    M4 diff at income#{i}: before={a} after={b}", flush=True)
        ok = False
    else:
        print(f"[PROBE2 M4-OK] ALL {len(inc_tuples_after)} income rows preserved after downgrade: no delete, no duplicate, no amount change.", flush=True)
    expenses_after_raw = db.execute(text("""
        SELECT id, NULL::bigint AS lease_id, property_id, unit_id,
               amount::text, status::text, COALESCE(description, '') AS description
        FROM expenses ORDER BY id
    """)).fetchall()
    exp_tuples_after = [(r[0], r[1], r[2], r[3], r[4], r[5], r[6]) for r in expenses_after_raw]
    exp_tuples_before = list(SEED_SNAPSHOT.get("expenses") or [])
    if exp_tuples_after != exp_tuples_before:
        print(f"[PROBE2 FAIL] M4 expense rows changed after downgrade! before cnt={len(exp_tuples_before)} after cnt={len(exp_tuples_after)}.", flush=True)
        if len(exp_tuples_before) == len(exp_tuples_after):
            for i, (a, b) in enumerate(zip(exp_tuples_before, exp_tuples_after)):
                if a != b:
                    print(f"    M4 diff at expense#{i}: before={a} after={b}", flush=True)
        ok = False
    else:
        print(f"[PROBE2 M4-OK] ALL {len(exp_tuples_after)} expense rows preserved after downgrade: idempotency_key column dropped ONLY, no business rows deleted.", flush=True)
    if ok:
        print("[PROBE2 PASS] after-downgrade: m2a cols ok + M1 migration-cleanup confirmed + M4 financial rows 100% preserved + legacy M003 rows retained", flush=True)
    else:
        PROBE_FAILED += 1
    return ok


def probe_after_reupgrade(db: Session) -> bool:
    global PROBE_FAILED
    ok = True
    insp_count = db.query(MoveOutInspection).count()
    settle_count = db.query(DepositSettlement).count()
    if insp_count != 0:
        print(f"[PROBE3 FAIL] expected 0 insp after empty re-upgrade, got {insp_count}", flush=True)
        ok = False
    if settle_count != 0:
        print(f"[PROBE3 FAIL] expected 0 settle after empty re-upgrade, got {settle_count}", flush=True)
        ok = False
    try:
        db.execute(text("SELECT findings FROM move_out_inspections LIMIT 1"))
        db.execute(text("SELECT deductions FROM deposit_settlements LIMIT 1"))
        db.execute(text("SELECT renewal_metadata FROM leases LIMIT 1"))
    except Exception as e:
        print(f"[PROBE3 FAIL] M004 JSONB columns missing after re-upgrade: {e}", flush=True)
        ok = False
    legacy_op_control = db.query(OperationalTask).filter(
        OperationalTask.task_type == OperationalTaskType.RENT_DUE
    ).count()
    if legacy_op_control < 1:
        print(f"[PROBE3 FAIL] M003 control OperationalTask RENT_DUE missing after re-upgrade! count={legacy_op_control}", flush=True)
        ok = False
    else:
        print(f"[PROBE3 INFO] M003 control OperationalTask RENT_DUE rows after re-upgrade: {legacy_op_control}", flush=True)
    legacy_rr_control = db.query(RecurringRule).filter(
        RecurringRule.rule_type.in_([OperationalTaskType.RENT_DUE, OperationalTaskType.AC_MAINTENANCE])
    ).count()
    if legacy_rr_control < 2:
        print(f"[PROBE3 FAIL] M003 control RecurringRules RENT_DUE/AC_MAINTENANCE missing after re-upgrade! count={legacy_rr_control}", flush=True)
        ok = False
    else:
        print(f"[PROBE3 INFO] M003 control RecurringRule rows after re-upgrade: {legacy_rr_control}", flush=True)

    expected_constraints = [
        ("uq_move_out_inspections_id_lease_id", "UNIQUE"),
        ("uq_deposit_settlements_id_lease_id", "UNIQUE"),
        ("fk_deposit_settlements_inspection_lease", "FOREIGN KEY"),
        ("fk_leases_moi_id_lease", "FOREIGN KEY"),
        ("fk_leases_ds_id_lease", "FOREIGN KEY"),
        ("ck_leases_superseded_pair", "CHECK"),
        ("uq_leases_superseded_by_one_predecessor", "UNIQUE"),
    ]
    for cname, ctype in expected_constraints:
        if ctype == "FOREIGN KEY":
            row = db.execute(text("""
                SELECT 1 FROM information_schema.table_constraints
                WHERE constraint_name = :cname AND constraint_type = 'FOREIGN KEY'
                LIMIT 1
            """), {"cname": cname}).scalar()
        elif ctype == "UNIQUE":
            row = db.execute(text("""
                SELECT 1 FROM information_schema.table_constraints
                WHERE constraint_name = :cname AND constraint_type = 'UNIQUE'
                LIMIT 1
            """), {"cname": cname}).scalar()
            if row != 1:
                row = db.execute(text("""
                    SELECT 1 FROM pg_indexes
                    WHERE indexname = :cname AND indexdef LIKE '%UNIQUE%'
                    LIMIT 1
                """), {"cname": cname}).scalar()
        elif ctype == "CHECK":
            row = db.execute(text("""
                SELECT 1 FROM information_schema.table_constraints
                WHERE constraint_name = :cname AND constraint_type = 'CHECK'
                LIMIT 1
            """), {"cname": cname}).scalar()
        else:
            row = None
        if row != 1:
            print(f"[PROBE3 FAIL] {ctype} constraint '{cname}' NOT found after re-upgrade (pg_indexes+table_constraints)", flush=True)
            ok = False

    if ok:
        print("[PROBE3 PASS] after re-upgrade schema reinstated + control rows preserved + 7 constraints OK", flush=True)
    else:
        PROBE_FAILED += 1
    return ok


def _g4_query_fiscal(db: Session) -> dict:
    inc_has_ik = db.execute(text("""
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='public' AND table_name='incomes' AND column_name='idempotency_key'
        LIMIT 1
    """)).scalar() == 1
    exp_has_ik = db.execute(text("""
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='public' AND table_name='expenses' AND column_name='idempotency_key'
        LIMIT 1
    """)).scalar() == 1
    inc_sql = (
        "SELECT id, lease_id, amount::text, status, description, received_date, idempotency_key "
        if inc_has_ik else
        "SELECT id, lease_id, amount::text, status, description, received_date, NULL::text "
    ) + "FROM incomes ORDER BY id"
    exp_sql = (
        "SELECT id, property_id, unit_id, amount::text, status, description, idempotency_key "
        if exp_has_ik else
        "SELECT id, property_id, unit_id, amount::text, status, description, NULL::text "
    ) + "FROM expenses ORDER BY id"
    incomes_raw = db.execute(text(inc_sql)).fetchall()
    expenses_raw = db.execute(text(exp_sql)).fetchall()
    return {
        "income_has_idempotency_key": inc_has_ik,
        "expense_has_idempotency_key": exp_has_ik,
        "incomes": [list(r) for r in incomes_raw],
        "expenses": [list(r) for r in expenses_raw],
    }


def _save_g4_snapshot_json(db: Session, stage_name: str) -> str:
    payload = {"stage": stage_name, "ts_utc": datetime.now(timezone.utc).isoformat()}
    payload.update(_g4_query_fiscal(db))
    out_path = os.path.join(SCRIPT_DIR, f"g4_{stage_name}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    print(f"[G4-SNAP] saved {out_path} income={len(payload['incomes'])} expense={len(payload['expenses'])}", flush=True)
    return out_path


def main() -> int:
    print("[RT] ensure_rt_database()", flush=True)
    ensure_rt_database()

    print("[RT] upgrade head (first time)", flush=True)
    rc = _run_alembic("upgrade", "head")
    if rc != 0:
        print(f"[RT] FAIL: alembic upgrade head -> exit {rc}", flush=True)
        return 1

    eng = create_engine(_rt_url())
    S = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    try:
        with S() as db:
            print("[RT] seed_m004_rows()", flush=True)
            seed_m004_rows(db)

        with S() as db:
            probe_after_seed(db)
            _save_g4_snapshot_json(db, "financial_before_seed")

        print(f"[RT] downgrade to {M2A_REV}", flush=True)
        rc = _run_alembic("downgrade", M2A_REV)
        if rc != 0:
            print(f"[RT] FAIL: alembic downgrade -> exit {rc}", flush=True)
            return 1

        with S() as db:
            probe_after_downgrade(db)
            _save_g4_snapshot_json(db, "after_downgrade")

        print("[RT] re-upgrade head", flush=True)
        rc = _run_alembic("upgrade", "head")
        if rc != 0:
            print(f"[RT] FAIL: alembic re-upgrade head -> exit {rc}", flush=True)
            return 1

        with S() as db:
            probe_after_reupgrade(db)
            _save_g4_snapshot_json(db, "after_reupgrade")

    finally:
        eng.dispose()

    if PROBE_FAILED == 0:
        print("[RT] SUCCESS all 3 probes OK exit=0", flush=True)
        return 0
    print(f"[RT] FAIL probes failed={PROBE_FAILED} exit=1", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
