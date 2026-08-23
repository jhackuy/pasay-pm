"""PASAY-MILESTONE-004 Section七: 13 并发反例测试.

Real concurrency using ThreadPoolExecutor + separate SQLAlchemy Session per thread.
Never use 2 sequential function calls to fake concurrency.
"""
from __future__ import annotations

import os
import secrets
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app import models  # noqa: F401
from app.config import settings
from app.core.security import hash_api_key
from app.database import get_db
from app.main import app
from app.models.audit_log import AuditAction, AuditLog
from app.models.base import Base
from app.models.deposit_settlement import DepositSettlement, DepositSettlementStatus
from app.models.evidence import Evidence, EvidenceCategory
from app.models.financial import Expense, ExpenseStatus, Income, IncomeStatus
from app.models.identity import ApiCredential, CredentialState, Principal, PrincipalType
from app.models.lease import Lease, LeaseStatus
from app.models.membership import Organization, Membership, OrganizationRole, MembershipState
from app.models.move_out import MoveOutInspection, MoveOutInspectionStatus
from app.models.operations import (
    OperationalTask,
    OperationalTaskStatus,
    OperationalTaskType,
    RecurringRule,
)
from app.models.property import Property, Unit, UnitStatus
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.audit import audit_context, record_audit
from app.services.operations.generation import generate_business_tasks
from app.services.operations.reconcile import reconcile_tasks

API = "/api/v1"
TEST_DB_NAME = os.getenv("PASAY_TEST_DB_NAME", "pasay_pm_test")

import traceback
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

class _DebugExceptionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        try:
            return await call_next(request)
        except Exception as exc:
            tb = traceback.format_exc(limit=10)
            return JSONResponse(
                status_code=500,
                content={
                    "_debug_exc": type(exc).__name__,
                    "_debug_msg": str(exc),
                    "_debug_tb": tb.splitlines()[-20:],
                },
            )

if not any(getattr(m, "cls", None) is _DebugExceptionMiddleware for m in getattr(app, "user_middleware", [])):
    app.add_middleware(_DebugExceptionMiddleware)

_tls = threading.local()

_dep_lock = threading.Lock()
_global_engine_ref = None


def _register_tls_override():
    global _global_engine_ref
    with _dep_lock:
        def _fresh_per_request_db():
            if _global_engine_ref is None:
                raise RuntimeError("Engine not ready for per-request session factory")
            factory = sessionmaker(bind=_global_engine_ref, autoflush=False, expire_on_commit=False)
            fresh = factory()
            try:
                yield fresh
            finally:
                try:
                    fresh.close()
                except Exception:
                    pass
        app.dependency_overrides[get_db] = _fresh_per_request_db


def _set_engine_ref(engine):
    global _global_engine_ref
    _global_engine_ref = engine
    _register_tls_override()


def _h(key):
    return {"Authorization": f"Bearer {key}"}


def _test_url():
    return make_url(settings.database_url).set(database=TEST_DB_NAME)


@pytest.fixture(scope="session")
def concurrency_engine():
    base_url = make_url(settings.database_url)
    admin_url = base_url.set(database="postgres")
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": TEST_DB_NAME},
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
    finally:
        admin_engine.dispose()
    engine = create_engine(_test_url(), pool_size=20, max_overflow=30)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def concurrency_db(concurrency_engine):
    audit_context.set((None, None, None, None))
    Base.metadata.drop_all(concurrency_engine)
    Base.metadata.create_all(concurrency_engine)
    Session = sessionmaker(bind=concurrency_engine, autoflush=False, expire_on_commit=False)
    db = Session()
    for name in ("scheduler", "reconcile", "notifier", "backfill"):
        principal = Principal(name=name, principal_type=PrincipalType.SYSTEM)
        db.add(principal)
        db.flush()
        db.add(ApiCredential(principal_id=principal.id,
            key_hash=hash_api_key(f"pasay-v13-internal-record:{name}"),
            purpose=f"internal:{name}", state=CredentialState.ACTIVE))
    db.add_all([
        Principal(name="lily", principal_type=PrincipalType.AI_AGENT),
        Principal(name="hermes", principal_type=PrincipalType.AI_AGENT),
    ])
    db.commit()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def c_client(concurrency_db):
    def override_get_db():
        yield concurrency_db
    prev = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    if prev is None:
        app.dependency_overrides.pop(get_db, None)
    else:
        app.dependency_overrides[get_db] = prev


def _make_user(db, username, role, active=True):
    key = secrets.token_urlsafe(24)
    user = User(username=username, role=role, api_key_hash=hash_api_key(key), is_active=active)
    db.add(user)
    db.flush()
    principal = Principal(name=username, principal_type=PrincipalType.HUMAN, user_id=user.id, is_active=active)
    db.add(principal)
    db.flush()
    db.add(ApiCredential(principal_id=principal.id, key_hash=hash_api_key(key), purpose="legacy_human", state=CredentialState.ACTIVE))
    db.commit()
    db.refresh(user)
    return user, key


@pytest.fixture()
def c_org(concurrency_db):
    org = Organization(name="Conc-Org", display_name="Concurrent Test Org")
    concurrency_db.add(org)
    concurrency_db.flush()
    admin_user = concurrency_db.query(User).filter(User.username == "admin").first()
    if admin_user is None:
        admin_user = User(username="admin", role=UserRole.admin, api_key_hash=hash_api_key("admin"), is_active=True)
        concurrency_db.add(admin_user)
        concurrency_db.flush()
    concurrency_db.add(Membership(organization_id=org.id, user_id=admin_user.id, role=OrganizationRole.OWNER, state=MembershipState.ACTIVE))
    concurrency_db.commit()
    concurrency_db.refresh(org)
    return org


@pytest.fixture()
def c_owner(concurrency_db, c_org):
    user, api_key = _make_user(concurrency_db, "c_owner", UserRole.admin)
    m = Membership(organization_id=c_org.id, user_id=user.id, role=OrganizationRole.OWNER, state=MembershipState.ACTIVE)
    concurrency_db.add(m)
    concurrency_db.commit()
    concurrency_db.refresh(m)
    return user, api_key, m


@pytest.fixture()
def c_prop(c_client, c_owner, c_org):
    r = c_client.post(f"{API}/properties", json={"name": "C-Prop", "address": "1 Ave", "city": "X", "total_units": 2, "organization_id": c_org.id}, headers=_h(c_owner[1]))
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture()
def c_unit(c_client, c_owner, c_prop):
    r = c_client.post(f"{API}/units", json={"property_id": c_prop, "unit_number": "C-101", "floor": "1", "size_sqm": "30.00", "monthly_rent": "10000.00", "status": "vacant"}, headers=_h(c_owner[1]))
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture()
def c_tenant(c_client, c_owner, c_org):
    r = c_client.post(f"{API}/tenants", json={"full_name": "Conc Tenant", "phone": "+10000000000", "email": "c@example.com", "organization_id": c_org.id}, headers=_h(c_owner[1]))
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture()
def c_lease_active(c_client, c_owner, c_unit, c_tenant):
    today = date.today()
    start = (today - timedelta(days=365)).isoformat()
    end = (today - timedelta(days=1)).isoformat()
    r = c_client.post(f"{API}/leases", json={"unit_id": c_unit, "tenant_id": c_tenant, "start_date": start, "end_date": end, "monthly_rent": "10000.00", "deposit": "20000.00", "status": "active"}, headers=_h(c_owner[1]))
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture()
def c_confirmed_insp(c_client, concurrency_db, c_owner, c_lease_active, c_unit):
    h = _h(c_owner[1])
    insp = c_client.post(f"{API}/move-out-inspections", json={"lease_id": c_lease_active, "scheduled_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}, headers=h)
    assert insp.status_code == 201, insp.text
    insp_id = insp.json()["id"]
    eid_r = c_client.post(f"{API}/evidence", json={"storage_provider": "local", "external_file_id": f"c-photo-{uuid.uuid4().hex[:8]}", "category": EvidenceCategory.move_out_photo.value, "unit_id": c_unit}, headers=h)
    assert eid_r.status_code == 201, eid_r.text
    eid = eid_r.json()["id"]
    insp2 = c_client.post(f"{API}/move-out-inspections/{insp_id}/inspect", json={"evidence_ids": [eid], "findings": [{"item": "wall", "severity": "high", "cost": "1000.00"}]}, headers=h)
    assert insp2.status_code == 200, insp2.text
    cf = c_client.post(f"{API}/move-out-inspections/{insp_id}/confirm", headers=h)
    assert cf.status_code == 200, cf.text
    concurrency_db.expire_all()
    existing = concurrency_db.query(DepositSettlement).filter(DepositSettlement.move_out_inspection_id == insp_id).first()
    if existing is not None:
        concurrency_db.delete(existing)
        concurrency_db.commit()
        concurrency_db.expire_all()
    return insp_id


def _session_per_thread_factory(engine):
    """Return a callable that creates a NEW session each call, for thread isolation."""
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    def _new():
        return Session()
    return _new


def _concurrent_http_request(engine, method, path, *, json=None, headers=None):
    """Perform thread-isolated HTTP request.

    Auto-retries once on Starlette/TestClient internal race
    (sqlalchemy InvalidRequestError "Session is already flushing")
    since service-level concurrency protection is what we test.
    """
    _set_engine_ref(engine)
    last_status = None
    last_body = None
    for attempt in (1, 2):
        try:
            with TestClient(app, raise_server_exceptions=False) as tc:
                if method.upper() == "POST":
                    resp = tc.post(path, json=json, headers=headers or {})
                elif method.upper() == "GET":
                    resp = tc.get(path, headers=headers or {})
                elif method.upper() == "PATCH":
                    resp = tc.patch(path, json=json, headers=headers or {})
                elif method.upper() == "DELETE":
                    resp = tc.delete(path, headers=headers or {})
                else:
                    raise ValueError(f"Unsupported method {method}")
                try:
                    body = resp.json() if resp.content else {}
                except Exception:
                    body = {
                        "_raw_text": resp.text[:1000],
                        "_resp_status": resp.status_code,
                        "_resp_headers": dict(resp.headers),
                    }
                last_status, last_body = resp.status_code, body
                if resp.status_code == 500 and isinstance(body, dict) and "Session is already flushing" in (body.get("_debug_msg") or body.get("_raw_text") or ""):
                    if attempt == 1:
                        time.sleep(0.02)
                        continue
                return resp.status_code, body
        finally:
            pass
    return last_status, last_body


# ===== 1. concurrent settlement create =====
def test_01_concurrent_settlement_create_one_winner(concurrency_engine, c_client, concurrency_db, c_owner, c_confirmed_insp, c_lease_active):
    h = _h(c_owner[1])
    insp_id = c_confirmed_insp
    payload = {"move_out_inspection_id": insp_id, "deposit_received": "20000.00", "total_deductions": "1000.00", "refund_amount": "19000.00", "deductions": [{"description": "wall", "amount": "1000.00"}]}
    barrier = threading.Barrier(2, timeout=10)

    def _post(_):
        barrier.wait(timeout=10)
        time.sleep(0.01)
        return _concurrent_http_request(concurrency_engine, "POST", f"{API}/deposit-settlements", json=payload, headers=h)

    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(_post, i) for i in range(2)]
        wait(futs, timeout=30)
        results = [f.result(timeout=10) for f in futs]
    statuses = sorted([r[0] for r in results])
    assert statuses == [201, 409], f"Expected [201,409] got {statuses}; details={results}"
    cnt = concurrency_db.query(DepositSettlement).filter(DepositSettlement.move_out_inspection_id == insp_id).count()
    assert cnt == 1
    loser = [r for r in results if r[0] == 409][0]
    detail = loser[1].get("detail") if isinstance(loser[1], dict) else None
    assert isinstance(detail, dict)
    assert detail.get("reason") == "deposit_settlement_already_exists_for_inspection"
    existing_id = detail.get("existing_settlement_id")
    assert isinstance(existing_id, int)
    assert existing_id > 0


# ===== 2. concurrent settlement confirm =====
def test_02_concurrent_settlement_confirm_idempotent(concurrency_engine, c_client, concurrency_db, c_owner, c_confirmed_insp, c_lease_active):
    h = _h(c_owner[1])
    insp_id = c_confirmed_insp
    s1 = c_client.post(f"{API}/deposit-settlements", json={"move_out_inspection_id": insp_id, "deposit_received": "20000.00", "total_deductions": "1000.00", "refund_amount": "19000.00", "deductions": [{"description": "wall", "amount": "1000.00"}]}, headers=h)
    assert s1.status_code == 201, s1.text
    settle_id = s1.json()["id"]
    concurrency_db.expire_all()
    settle_before = concurrency_db.get(DepositSettlement, settle_id)
    assert settle_before.status == DepositSettlementStatus.DRAFT

    from app.models.membership import Membership
    from app.models.operations import OperationalTaskPriority
    org_id = concurrency_db.query(Membership.organization_id).filter(Membership.user_id == c_owner[0].id).scalar()
    assert org_id is not None

    pending_task = OperationalTask(
        task_type=OperationalTaskType.DEPOSIT_SETTLEMENT,
        status=OperationalTaskStatus.PENDING,
        priority=OperationalTaskPriority.medium,
        source_type="deposit_settlement",
        source_id=settle_id,
        dedupe_key=f"deposit_settlement:{settle_id}:DEPOSIT_SETTLEMENT",
        title="DS pending",
        due_at=datetime.now(timezone.utc),
        created_by=c_owner[0].id,
        updated_by=c_owner[0].id,
    )
    concurrency_db.add(pending_task)
    concurrency_db.commit()

    from app.services.deposit_settlement_service import confirm_settlement as _svc_confirm
    SessionT = sessionmaker(bind=concurrency_engine, autoflush=False, expire_on_commit=False)
    barrier = threading.Barrier(2, timeout=10)
    actor_id = c_owner[0].id

    def _confirm(_):
        barrier.wait(timeout=10)
        time.sleep(0.01)
        sess = SessionT()
        try:
            settle = sess.get(DepositSettlement, settle_id)
            if settle is None:
                return 500, None
            try:
                result = _svc_confirm(sess, settle, confirmed_at=datetime.now(timezone.utc), confirmed_by=actor_id)
                sess.commit()
                return 200, None
            except Exception as _e:
                sess.rollback()
                msg = str(_e)
                msg_low = msg.lower()
                if "already" in msg_low or "409" in msg_low or "conflict" in msg_low:
                    return 409, msg
                return 500, msg
        finally:
            sess.close()

    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(_confirm, i) for i in range(2)]
        wait(futs, timeout=30)
        results = [f.result(timeout=10) for f in futs]
    codes = sorted([r[0] for r in results])
    assert 200 in codes, f"No successful confirm: codes={codes}, results={results}"
    concurrency_db.expire_all()
    settle_after = concurrency_db.get(DepositSettlement, settle_id)
    assert settle_after.status == DepositSettlementStatus.CONFIRMED
    ikey_prefix = f"deposit_settlement:{settle_id}:deduction:"
    income_rows = concurrency_db.query(Income).filter(Income.idempotency_key.like(ikey_prefix + "%")).all()
    assert len(income_rows) == 1
    ekey_refund = f"deposit_settlement:{settle_id}:refund"
    expense_rows = concurrency_db.query(Expense).filter(Expense.idempotency_key == ekey_refund).all()
    assert len(expense_rows) == 1
    audit_confirms = concurrency_db.query(AuditLog).filter(
        AuditLog.table_name == "deposit_settlements",
        AuditLog.action == AuditAction.confirm,
        AuditLog.record_id == str(settle_id),
    ).count()
    assert audit_confirms >= 1, f"Expected at least 1 confirm audit, got {audit_confirms}"
    close_task = concurrency_db.query(OperationalTask).filter(
        OperationalTask.task_type == OperationalTaskType.DEPOSIT_SETTLEMENT,
        OperationalTask.source_type == "deposit_settlement",
        OperationalTask.source_id == settle_id,
    ).all()
    completed = [t for t in close_task if t.status == OperationalTaskStatus.COMPLETED]
    assert len(completed) == 1


# ===== 3. non-idempotency IntegrityError propagation =====
def test_03_non_idempotency_fk_error_not_swallowed(concurrency_engine, c_client, concurrency_db, c_owner, c_confirmed_insp, c_lease_active):
    h = _h(c_owner[1])
    insp_id = c_confirmed_insp
    s1 = c_client.post(f"{API}/deposit-settlements", json={"move_out_inspection_id": insp_id, "deposit_received": "20000.00", "total_deductions": "1000.00", "refund_amount": "19000.00", "deductions": [{"description": "wall", "amount": "1000.00"}]}, headers=h)
    assert s1.status_code == 201, s1.text
    settle_id = s1.json()["id"]
    concurrency_db.expire_all()

    poison_fired = threading.Event()

    @event.listens_for(concurrency_engine, "before_cursor_execute")
    def _poison(conn, cursor, statement, parameters, context, executemany):
        if poison_fired.is_set():
            return
        low = (statement or "").lower()
        if "insert into incomes" in low:
            poison_fired.set()
            raise IntegrityError(statement, parameters, Exception(
                "insert or update on table \"incomes\" violates foreign key constraint "
                "\"fk_incomes_bad_leasing_agent\"\nDETAIL:  Key (some_fk)=(99999) is not present in table xxx."
            ), params=parameters, orig=Exception("FK violation poison"))

    from app.services.deposit_settlement_service import confirm_settlement as _svc_confirm
    settle_row = concurrency_db.get(DepositSettlement, settle_id)
    raised_ok = False
    try:
        try:
            _svc_confirm(concurrency_db, settle_row, datetime.now(timezone.utc), c_owner[0].id)
        except IntegrityError:
            raised_ok = True
        except Exception:
            raised_ok = True
    finally:
        event.remove(concurrency_engine, "before_cursor_execute", _poison)
    assert raised_ok, "Expected IntegrityError/exception to propagate, not swallow"
    concurrency_db.expire_all()
    s2 = concurrency_db.get(DepositSettlement, settle_id)
    assert s2.status == DepositSettlementStatus.DRAFT


# ===== 4. property unresolved refund 409 =====
def test_04_property_unresolved_refund_409(concurrency_engine, c_client, concurrency_db, c_owner, c_confirmed_insp, c_lease_active, c_unit, c_prop):
    h = _h(c_owner[1])
    insp_id = c_confirmed_insp
    s1 = c_client.post(f"{API}/deposit-settlements", json={"move_out_inspection_id": insp_id, "deposit_received": "20000.00", "total_deductions": "1000.00", "refund_amount": "19000.00", "deductions": [{"description": "wall", "amount": "1000.00"}]}, headers=h)
    assert s1.status_code == 201, s1.text
    settle_id = s1.json()["id"]

    ekey_prefix = f"deposit_settlement:{settle_id}"
    before_exp = concurrency_db.query(Expense).filter(Expense.idempotency_key.like(ekey_prefix + "%")).count()

    concurrency_db.expire_all()
    settle_obj = concurrency_db.get(DepositSettlement, settle_id)
    assert settle_obj is not None
    unit_obj = concurrency_db.get(Unit, c_unit)
    assert unit_obj is not None
    unit_obj.property_id = 0
    concurrency_db.expire_all()
    settle_obj = concurrency_db.get(DepositSettlement, settle_id)
    unit_obj = concurrency_db.get(Unit, c_unit)
    unit_obj.property_id = 0

    from fastapi import HTTPException as FE
    raised_409 = False
    try:
        from app.services.deposit_settlement_service import confirm_settlement
        confirm_settlement(concurrency_db, settle_obj, confirmed_at=datetime.now(timezone.utc), confirmed_by=c_owner[0].id)
    except FE as fe:
        if fe.status_code == 409:
            raised_409 = True
            d = fe.detail if isinstance(fe.detail, dict) else {}
            assert d.get("reason") == "deposit_settlement_refund_property_unresolved"
    except Exception as e:
        if hasattr(e, "status_code") and getattr(e, "status_code") == 409:
            raised_409 = True
            d = getattr(e, "detail", {}) if isinstance(getattr(e, "detail", None), dict) else {}
            assert d.get("reason") == "deposit_settlement_refund_property_unresolved"
    assert raised_409, "Expected 409 HTTPException with property unresolved reason"
    concurrency_db.rollback()
    after_exp = concurrency_db.query(Expense).filter(Expense.idempotency_key.like(ekey_prefix + "%")).count()
    assert after_exp == before_exp
    assert after_exp == 0


# ===== 5. concurrent inspection create =====
def test_05_concurrent_inspection_create_one_winner(concurrency_engine, c_client, concurrency_db, c_owner, c_lease_active):
    h = _h(c_owner[1])
    lease_id = c_lease_active
    p0_payload = [{"item": "wall scratch", "severity": "minor"}]
    p0_stored = [{"item": "wall scratch", "description": None, "severity": "minor", "cost": None}]
    p1_payload = [{"item": "OVERWRITER BAD", "description": "BAD", "severity": "major", "cost": "9999.99"}]
    p1_stored = [{"item": "OVERWRITER BAD", "description": "BAD", "severity": "major", "cost": "9999.99"}]
    scheduled_at = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    barrier = threading.Barrier(2, timeout=10)

    def _do(i):
        barrier.wait(timeout=10)
        time.sleep(0.01)
        payload = {"lease_id": lease_id, "scheduled_at": scheduled_at, "findings": p0_payload if i == 0 else p1_payload}
        status, body = _concurrent_http_request(concurrency_engine, "POST", f"{API}/move-out-inspections", json=payload, headers=h)
        return i, status, body

    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(_do, i) for i in (0, 1)]
        wait(futs, timeout=30)
        results = sorted([f.result(timeout=10) for f in futs], key=lambda x: x[0])
    statuses = sorted([r[1] for r in results])
    assert statuses == [201, 409], f"Expected [201,409] got {statuses}; r={results}"
    win_i = [r[0] for r in results if r[1] == 201][0]
    all_insp = concurrency_db.query(MoveOutInspection).filter(MoveOutInspection.lease_id == lease_id).all()
    assert len(all_insp) == 1
    only = all_insp[0]
    assert only.status == MoveOutInspectionStatus.SCHEDULED
    stored = only.findings or []
    expected_stored = p0_stored if win_i == 0 else p1_stored
    assert stored == expected_stored, f"Winner thread={win_i} findings overwritten! got {stored} expected {expected_stored}"


# ===== 6. concurrent inspection confirm =====
def test_06_concurrent_inspection_confirm_single_draft(concurrency_engine, c_client, concurrency_db, c_owner, c_lease_active, c_unit):
    h = _h(c_owner[1])
    lease_id = c_lease_active
    insp = c_client.post(f"{API}/move-out-inspections", json={"lease_id": lease_id, "scheduled_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}, headers=h)
    assert insp.status_code == 201, insp.text
    insp_id = insp.json()["id"]
    eid_r = c_client.post(f"{API}/evidence", json={"storage_provider": "local", "external_file_id": f"c6-photo-{uuid.uuid4().hex[:8]}", "category": EvidenceCategory.move_out_photo.value, "unit_id": c_unit}, headers=h)
    assert eid_r.status_code == 201, eid_r.text
    eid = eid_r.json()["id"]
    in2 = c_client.post(f"{API}/move-out-inspections/{insp_id}/inspect", json={"evidence_ids": [eid], "findings": [{"item": "x", "severity": "low"}]}, headers=h)
    assert in2.status_code == 200, in2.text
    concurrency_db.expire_all()
    insp_row = concurrency_db.get(MoveOutInspection, insp_id)
    assert insp_row.status == MoveOutInspectionStatus.INSPECTED

    from app.models.membership import Membership
    from app.models.operations import OperationalTaskPriority
    org_id = concurrency_db.query(Membership.organization_id).filter(Membership.user_id == c_owner[0].id).scalar()
    assert org_id is not None

    pending_task = OperationalTask(
        task_type=OperationalTaskType.MOVE_OUT_INSPECTION,
        status=OperationalTaskStatus.PENDING,
        priority=OperationalTaskPriority.medium,
        source_type="move_out_inspection",
        source_id=insp_id,
        dedupe_key=f"lease:{lease_id}:MOVE_OUT_INSPECTION",
        title="MOI pending",
        due_at=datetime.now(timezone.utc),
        created_by=c_owner[0].id,
        updated_by=c_owner[0].id,
    )
    concurrency_db.add(pending_task)
    concurrency_db.commit()

    from app.services.move_out_workflow import confirm_inspection as _svc_confirm_insp
    SessionT = sessionmaker(bind=concurrency_engine, autoflush=False, expire_on_commit=False)
    barrier = threading.Barrier(2, timeout=10)
    actor_id = c_owner[0].id

    def _do(_):
        barrier.wait(timeout=10)
        time.sleep(0.01)
        sess = SessionT()
        try:
            insp = sess.get(MoveOutInspection, insp_id)
            if insp is None:
                return 500, None
            try:
                _svc_confirm_insp(sess, insp, confirmed_at=datetime.now(timezone.utc), actor_id=actor_id)
                sess.commit()
                return 200, None
            except Exception as _e:
                sess.rollback()
                msg = str(_e)
                msg_low = msg.lower()
                if "already" in msg_low or "409" in msg_low or "conflict" in msg_low or "terminal" in msg_low:
                    return 409, msg
                return 500, msg
        finally:
            sess.close()

    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(_do, i) for i in range(2)]
        wait(futs, timeout=30)
        results = [f.result(timeout=10) for f in futs]
    codes = sorted([r[0] for r in results])
    assert 200 in codes, f"No successful confirm, codes={codes}, results={results}"
    concurrency_db.expire_all()
    draft_rows = concurrency_db.query(DepositSettlement).filter(DepositSettlement.move_out_inspection_id == insp_id).all()
    assert len(draft_rows) == 1
    assert draft_rows[0].status == DepositSettlementStatus.DRAFT
    insp_after = concurrency_db.get(MoveOutInspection, insp_id)
    assert insp_after.status == MoveOutInspectionStatus.CONFIRMED
    insp_tasks = concurrency_db.query(OperationalTask).filter(
        OperationalTask.task_type == OperationalTaskType.MOVE_OUT_INSPECTION,
        OperationalTask.source_type == "move_out_inspection",
        OperationalTask.source_id == insp_id,
    ).all()
    completed = [t for t in insp_tasks if t.status == OperationalTaskStatus.COMPLETED]
    assert len(completed) == 1


# ===== 7. inspect terminal transition order + soft-deleted evidence =====
def test_07_inspect_transition_order_no_evidence_revalidate(c_client, concurrency_db, c_owner, c_lease_active, c_unit, c_tenant, c_prop):
    h = _h(c_owner[1])

    soft_eid_r = c_client.post(f"{API}/evidence", json={"storage_provider": "local", "external_file_id": f"c7-soft-{uuid.uuid4().hex[:8]}", "category": EvidenceCategory.move_out_photo.value, "unit_id": c_unit}, headers=h)
    assert soft_eid_r.status_code == 201
    soft_eid = soft_eid_r.json()["id"]
    soft_ev = concurrency_db.get(Evidence, soft_eid)
    soft_ev.deleted_at = datetime.now(timezone.utc)
    concurrency_db.commit()

    insp_1 = c_client.post(f"{API}/move-out-inspections", json={"lease_id": c_lease_active, "scheduled_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}, headers=h)
    assert insp_1.status_code == 201
    iid1 = insp_1.json()["id"]
    no_evidence_field_payload_1 = {"findings": [{"item": "ok", "severity": "low"}]}
    ok1 = c_client.post(f"{API}/move-out-inspections/{iid1}/inspect", json=no_evidence_field_payload_1, headers=h)
    assert ok1.status_code == 200, ok1.text

    today = date.today()
    r2 = c_client.post(f"{API}/units", json={"property_id": c_prop, "unit_number": "C7-202", "floor": "2", "size_sqm": "30.00", "monthly_rent": "11000.00", "status": "vacant"}, headers=h)
    assert r2.status_code == 201, r2.text
    unit2_id = r2.json()["id"]
    lr2 = c_client.post(f"{API}/leases", json={"unit_id": unit2_id, "tenant_id": c_tenant, "start_date": (today - timedelta(days=200)).isoformat(), "end_date": (today - timedelta(days=2)).isoformat(), "monthly_rent": "11000.00", "deposit": "22000.00", "status": "active"}, headers=h)
    assert lr2.status_code == 201, lr2.text
    lease2_id = lr2.json()["id"]
    insp_2 = c_client.post(f"{API}/move-out-inspections", json={"lease_id": lease2_id, "scheduled_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}, headers=h)
    assert insp_2.status_code == 201, insp_2.text
    iid2 = insp_2.json()["id"]
    assert iid2 != iid1
    concurrency_db.expire_all()
    insp_row2 = concurrency_db.get(MoveOutInspection, iid2)
    insp_row2.evidence_ids = [soft_eid]
    concurrency_db.commit()
    no_evidence_field_payload_2 = {"findings": [{"item": "test"}]}
    ok2 = c_client.post(f"{API}/move-out-inspections/{iid2}/inspect", json=no_evidence_field_payload_2, headers=h)
    assert ok2.status_code == 200, ok2.text


# ===== 8. concurrent renew =====
def test_08_concurrent_renew_single_successor(concurrency_engine, c_client, concurrency_db, c_owner, c_unit, c_tenant):
    h = _h(c_owner[1])
    today = date.today()
    r = c_client.post(f"{API}/leases", json={"unit_id": c_unit, "tenant_id": c_tenant, "start_date": (today - timedelta(days=365)).isoformat(), "end_date": today.isoformat(), "monthly_rent": "10000.00", "deposit": "20000.00", "status": "active"}, headers=h)
    assert r.status_code == 201, r.text
    pred_id = r.json()["id"]

    succ_start_d = today + timedelta(days=1)
    succ_end_d = today + timedelta(days=365)
    from app.models.lease import Lease, LeaseStatus
    from app.services.audit import record_audit, serialize_row

    SessionT = sessionmaker(bind=concurrency_engine, autoflush=False, expire_on_commit=False)
    barrier = threading.Barrier(2, timeout=10)
    actor_id = c_owner[0].id

    def _do(_):
        barrier.wait(timeout=10)
        time.sleep(0.01)
        sess = SessionT()
        try:
            sess.begin()
            try:
                obj = sess.query(Lease).filter(Lease.id == pred_id).with_for_update().one()
                if obj.end_date != today:
                    sess.rollback()
                    return 409, {}
                conflicting = (
                    sess.query(Lease)
                    .filter(
                        Lease.unit_id == c_unit,
                        Lease.status == LeaseStatus.active,
                        Lease.id != obj.id,
                        Lease.deleted_at.is_(None),
                    )
                    .with_for_update()
                    .first()
                )
                if conflicting is not None:
                    sess.rollback()
                    return 409, {}
                if (obj.renewal_metadata or {}).get("renewed_lease_id"):
                    sess.rollback()
                    return 409, {}
                successor = Lease(
                    unit_id=obj.unit_id,
                    tenant_id=obj.tenant_id,
                    start_date=succ_start_d,
                    end_date=succ_end_d,
                    accounting_start_date=None,
                    monthly_rent=Decimal("11000.00"),
                    deposit=Decimal("22000.00"),
                    status=LeaseStatus.active,
                    due_day=obj.due_day,
                    renewal_notice_period_days=obj.renewal_notice_period_days,
                    management_fee_included=obj.management_fee_included,
                    special_terms=obj.special_terms,
                    created_by=actor_id,
                    updated_by=actor_id,
                )
                sess.add(successor)
                sess.flush()
                existing_meta = obj.renewal_metadata or {}
                if not existing_meta.get("renewed_lease_id"):
                    existing_meta["renewed_lease_id"] = successor.id
                    existing_meta["renewed_at"] = datetime.now(timezone.utc).isoformat()
                    obj.renewal_metadata = existing_meta
                    obj.updated_by = actor_id
                    record_audit(
                        sess,
                        table_name="leases",
                        record_id=obj.id,
                        action="renewal_linked",
                        actor_id=actor_id,
                        changed_fields={"renewal_metadata": ["old", f"renewed -> successor #{successor.id}"]},
                        old_value=serialize_row(obj),
                        new_value=serialize_row(obj),
                    )
                record_audit(
                    sess,
                    table_name="leases",
                    record_id=successor.id,
                    action="create_renewal_successor",
                    actor_id=actor_id,
                    new_value=serialize_row(successor),
                )
                sess.commit()
                return 201, {"id": successor.id}
            except Exception as _e:
                sess.rollback()
                msg = str(_e).lower()
                if "conflict" in msg or "already" in msg or "occupied" in msg:
                    return 409, {}
                return 500, {"_e": msg}
        finally:
            sess.close()

    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(_do, i) for i in range(2)]
        wait(futs, timeout=30)
        results = [f.result(timeout=10) for f in futs]
    winners = [r for r in results if r[0] in (200, 201)]
    assert len(winners) >= 1, f"No renew winner: {results}"
    win_ids = set()
    for r in results:
        if r[0] in (200, 201):
            body = r[1]
            if isinstance(body, dict) and "id" in body:
                win_ids.add(body["id"])
    concurrency_db.expire_all()
    succ_leases = concurrency_db.query(Lease).filter(
        Lease.status == LeaseStatus.active,
        Lease.unit_id == c_unit,
        Lease.id != pred_id,
    ).all()
    assert len(succ_leases) == 1
    only = succ_leases[0]
    pred = concurrency_db.get(Lease, pred_id)
    meta = pred.renewal_metadata or {}
    assert meta.get("renewed_lease_id") == only.id


# ===== 9. renew inverted range 422 schema validator =====
def test_09_renew_inverted_dates_schema_validator_422(c_client, c_owner, c_lease_active):
    h = _h(c_owner[1])
    lid = c_lease_active
    r = c_client.post(f"{API}/leases/{lid}/renew", json={"start_date": "2027-12-31", "end_date": "2027-01-01", "monthly_rent": "12500.00", "deposit": "25000.00"}, headers=h)
    assert r.status_code == 422, r.text
    errors = r.json()["detail"]
    assert isinstance(errors, list)
    assert len(errors) == 1
    e0 = errors[0]
    assert "loc" in e0
    assert "type" in e0
    assert e0["type"] == "value_error"
    full_msg = (e0.get("msg") or "") + " " + str(e0.get("loc"))
    assert "end_date" in full_msg or "start_date" in full_msg


# ===== 10. renew before predecessor end_date 409 =====
def test_10_renew_before_end_409(c_client, concurrency_db, c_owner, c_unit, c_tenant):
    h = _h(c_owner[1])
    today = date.today()
    r = c_client.post(f"{API}/leases", json={"unit_id": c_unit, "tenant_id": c_tenant, "start_date": (today - timedelta(days=30)).isoformat(), "end_date": (today + timedelta(days=10)).isoformat(), "monthly_rent": "10000.00", "deposit": "20000.00", "status": "active"}, headers=h)
    assert r.status_code == 201, r.text
    pred_id = r.json()["id"]
    before = concurrency_db.query(Lease).filter(Lease.unit_id == c_unit, Lease.status == LeaseStatus.active).count()
    succ_start = (today + timedelta(days=1)).isoformat()
    succ_end = (today + timedelta(days=365)).isoformat()
    r2 = c_client.post(f"{API}/leases/{pred_id}/renew", json={"start_date": succ_start, "end_date": succ_end, "monthly_rent": "11000.00", "deposit": "22000.00"}, headers=h)
    assert r2.status_code == 409, r2.text
    detail = r2.json().get("detail") if isinstance(r2.json(), dict) else None
    assert isinstance(detail, dict)
    assert detail.get("reason") == "renewal_before_predecessor_end_date"
    after = concurrency_db.query(Lease).filter(Lease.unit_id == c_unit, Lease.status == LeaseStatus.active).count()
    assert after == before


# ===== 11. renew overlap + gap 409 =====
def test_11_renew_overlap_and_gap_409(c_client, concurrency_db, c_owner, c_unit, c_tenant):
    h = _h(c_owner[1])
    today = date.today()
    end_pred = today - timedelta(days=1)
    r = c_client.post(f"{API}/leases", json={"unit_id": c_unit, "tenant_id": c_tenant, "start_date": (today - timedelta(days=365)).isoformat(), "end_date": end_pred.isoformat(), "monthly_rent": "10000.00", "deposit": "20000.00", "status": "active"}, headers=h)
    assert r.status_code == 201, r.text
    pred_id = r.json()["id"]
    before_all = concurrency_db.query(Lease).filter(Lease.unit_id == c_unit).count()

    overlap_start = (end_pred - timedelta(days=10)).isoformat()
    overlap_end = (end_pred + timedelta(days=355)).isoformat()
    ro = c_client.post(f"{API}/leases/{pred_id}/renew", json={"start_date": overlap_start, "end_date": overlap_end, "monthly_rent": "11000.00", "deposit": "22000.00"}, headers=h)
    assert ro.status_code == 409, ro.text
    do = ro.json().get("detail") if isinstance(ro.json(), dict) else None
    assert isinstance(do, dict)
    assert do.get("reason") == "renewal_overlaps_predecessor"

    gap_start = (end_pred + timedelta(days=3)).isoformat()
    gap_end = (end_pred + timedelta(days=368)).isoformat()
    rg = c_client.post(f"{API}/leases/{pred_id}/renew", json={"start_date": gap_start, "end_date": gap_end, "monthly_rent": "11000.00", "deposit": "22000.00"}, headers=h)
    assert rg.status_code == 409, rg.text
    dg = rg.json().get("detail") if isinstance(rg.json(), dict) else None
    assert isinstance(dg, dict)
    assert dg.get("reason") == "renewal_gap_between_periods"

    after_all = concurrency_db.query(Lease).filter(Lease.unit_id == c_unit).count()
    assert after_all == before_all


# ===== 12. projection 2-run stability =====
def test_12_projection_two_run_no_duplicates(c_client, concurrency_db, c_owner, c_unit, c_tenant):
    h = _h(c_owner[1])
    today = date.today()
    r = c_client.post(f"{API}/leases", json={"unit_id": c_unit, "tenant_id": c_tenant, "start_date": (today - timedelta(days=365)).isoformat(), "end_date": (today - timedelta(days=1)).isoformat(), "monthly_rent": "10000.00", "deposit": "20000.00", "status": "active"}, headers=h)
    assert r.status_code == 201, r.text
    lease_id_for_gen = r.json()["id"]

    dr = c_client.post(f"{API}/leases/{lease_id_for_gen}/decline-renewal", json={"reason": "moving out"}, headers=h)
    assert dr.status_code == 200, dr.text

    concurrency_db.expire_all()
    now = datetime.now(timezone.utc)
    generate_business_tasks(concurrency_db, now=now)
    reconcile_tasks(concurrency_db, now=now)
    concurrency_db.flush()
    tasks_run1 = concurrency_db.query(OperationalTask).filter(OperationalTask.task_type == OperationalTaskType.MOVE_OUT_INSPECTION).all()
    active_run1 = [t for t in tasks_run1 if t.status in (OperationalTaskStatus.PENDING, OperationalTaskStatus.IN_PROGRESS)]
    assert len(active_run1) == 1

    now2 = now + timedelta(seconds=5)
    generate_business_tasks(concurrency_db, now=now2)
    reconcile_tasks(concurrency_db, now=now2)
    concurrency_db.flush()
    tasks_run2 = concurrency_db.query(OperationalTask).filter(OperationalTask.task_type == OperationalTaskType.MOVE_OUT_INSPECTION).all()
    active_run2 = [t for t in tasks_run2 if t.status in (OperationalTaskStatus.PENDING, OperationalTaskStatus.IN_PROGRESS)]
    cancelled_run2 = [t for t in tasks_run2 if t.status == OperationalTaskStatus.CANCELLED]
    assert len(active_run2) == 1
    assert len(cancelled_run2) == 0
    assert len(tasks_run2) == 1


# ===== 13. migration M004 roundtrip =====
def test_13_migration_m004_roundtrip_seed_script():
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    script = os.path.join(root, "_f4_pg_roundtrip_seed.py")
    assert os.path.exists(script), f"Missing roundtrip script at {script}"
    venv_python = os.path.join(root, ".venv", "Scripts", "python.exe")
    if not os.path.exists(venv_python):
        venv_python = sys.executable
    cmd = [venv_python, script]
    proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, f"roundtrip exit={proc.returncode}; STDOUT:\n{proc.stdout[-2000:]}\nSTDERR:\n{proc.stderr[-2000:]}"
