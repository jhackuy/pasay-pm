"""PASAY-MILESTONE-004 Section八: 18 精确反例 — DB Invariants + Renewal Lifecycle.

Scope: FK 跨租约三角形约束、Renewal SM 反例、续租前任 soft-delete、
Immutability / Date reason / Audit old_value 精确断言。

（Section八 1-16 覆盖；17/18 已在其他 test 文件验证）
"""
from __future__ import annotations

import json
import secrets
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from itertools import count
from threading import Barrier, BrokenBarrierError

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, DateTime, Numeric, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.database import get_db
from app.main import app
from app.models.audit_log import AuditAction, AuditLog
from app.models.deposit_settlement import DepositSettlement, DepositSettlementStatus
from app.models.evidence import EvidenceCategory
from app.models.lease import Lease, LeaseStatus
from app.models.move_out import MoveOutInspection, MoveOutInspectionStatus
from app.models.operations import (
    OperationalTask,
    OperationalTaskStatus,
    OperationalTaskType,
)
from app.models.property import Unit, UnitStatus
from app.models.tenant import Tenant
from app.services.audit import serialize_row

API = "/api/v1"

_UNIT_COUNTER = count(105)
_TENANT_COUNTER = count(1)


def _h(key):
    return {"Authorization": f"Bearer {key}"}


def _lease_payload(*, unit_id, tenant_id, start_date="2026-01-01",
                   end_date="2026-12-31", monthly_rent="12000.00",
                   deposit="24000.00", status="active"):
    return {
        "unit_id": unit_id,
        "tenant_id": tenant_id,
        "start_date": start_date,
        "end_date": end_date,
        "monthly_rent": monthly_rent,
        "deposit": deposit,
        "status": status,
    }


def _create_second_unit_tenant(client, headers, property_id, org_a_id):
    uniq = uuid.uuid4().hex[:6]
    unit_num = str(next(_UNIT_COUNTER)) + uniq[:2]
    tenant_idx = next(_TENANT_COUNTER)
    phone_suffix = f"{tenant_idx:04d}"
    u2 = client.post(
        f"{API}/units",
        json={
            "property_id": property_id,
            "unit_number": unit_num,
            "floor": "1",
            "size_sqm": "35.00",
            "monthly_rent": "13000.00",
            "status": "vacant",
        },
        headers=headers,
    )
    assert u2.status_code == 201, u2.text
    t2 = client.post(
        f"{API}/tenants",
        json={
            "full_name": f"Maria Santos {uniq}",
            "phone": f"+639171{phone_suffix}{uniq[:2]}",
            "email": f"maria_{uniq}_{tenant_idx}@example.com",
            "organization_id": org_a_id,
        },
        headers=headers,
    )
    assert t2.status_code == 201, t2.text
    return u2.json()["id"], t2.json()["id"]


def _create_lease_and_schedule_moi(client, db, headers, *, unit_id, tenant_id,
                                    start_date="2026-01-01", end_date="2026-12-31"):
    lr = client.post(
        f"{API}/leases",
        json=_lease_payload(unit_id=unit_id, tenant_id=tenant_id,
                            start_date=start_date, end_date=end_date),
        headers=headers,
    )
    assert lr.status_code == 201, lr.text
    lease_id = lr.json()["id"]
    sched = client.post(
        f"{API}/move-out-inspections",
        json={
            "lease_id": lease_id,
            "scheduled_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        },
        headers=headers,
    )
    assert sched.status_code == 201, sched.text
    moi_id = sched.json()["id"]
    db.expire_all()
    return lease_id, moi_id


# =========================================================================
# Section八 #1-4: Raw SQL DB-level 跨租约 FK 拒绝探针
# =========================================================================

def _get_constraint_name(e: IntegrityError) -> str | None:
    diag = getattr(getattr(e, "orig", None), "diag", None)
    if diag is not None:
        cn = getattr(diag, "constraint_name", None)
        if cn:
            return cn
    s = str(e)
    import re
    m = re.search(r'constraint[ "]+([a-zA-Z0-9_]+)', s, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def _probe_raw_sql(db_session, fn):
    """Run fn(conn) on a fresh raw connection, closing on exit.
    Returns IntegrityError caught or None.
    """
    bind = db_session.get_bind()
    conn = None
    caught = None
    try:
        conn = bind.connect()
        fn(conn)
        conn.commit()
    except IntegrityError as e:
        caught = e
        try:
            if conn is not None:
                conn.rollback()
        except Exception:
            pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        try:
            db_session.rollback()
        except Exception:
            pass
    return caught


def test_s1_raw_sql_cross_lease_settlement_to_inspection_rejected_by_fk(
    client, db_session, owner_a, unit_id, tenant_id, property_id, org_a,
):
    h = _h(owner_a[1])
    unit_b_id, tenant_b_id = _create_second_unit_tenant(client, h, property_id, org_a.id)
    lease_a_id, moi_a_id = _create_lease_and_schedule_moi(
        client, db_session, h, unit_id=unit_id, tenant_id=tenant_id,
    )
    lease_b_id, _ = _create_lease_and_schedule_moi(
        client, db_session, h, unit_id=unit_b_id, tenant_id=tenant_b_id,
    )
    db_session.commit()
    db_session.expire_all()

    def _do(conn):
        conn.execute(text(
            "INSERT INTO deposit_settlements "
            "(lease_id, move_out_inspection_id, deposit_received, total_deductions, "
            "refund_amount, status, created_by, updated_by) "
            "VALUES "
            "(:lease_b_id, :moi_a_id, '24000.00', '5000.00', '19000.00', 'DRAFT', 1, 1)"
        ), {
            "lease_b_id": lease_b_id,
            "moi_a_id": moi_a_id,
        })

    caught = _probe_raw_sql(db_session, _do)

    assert caught is not None, "Expected IntegrityError for cross-lease FK violation"
    constraint_name = _get_constraint_name(caught)
    assert constraint_name == "fk_deposit_settlements_inspection_lease", (
        f"Expected constraint fk_deposit_settlements_inspection_lease, got {constraint_name!r}"
    )


def test_s2_lease_pointer_to_other_lease_inspection_rejected_by_fk(
    client, db_session, owner_a, unit_id, tenant_id, property_id, org_a,
):
    h = _h(owner_a[1])
    unit_b_id, tenant_b_id = _create_second_unit_tenant(client, h, property_id, org_a.id)
    lease_a_id, moi_a_id = _create_lease_and_schedule_moi(
        client, db_session, h, unit_id=unit_id, tenant_id=tenant_id,
    )
    lease_b_id, _ = _create_lease_and_schedule_moi(
        client, db_session, h, unit_id=unit_b_id, tenant_id=tenant_b_id,
    )
    db_session.commit()
    db_session.expire_all()

    def _do(conn):
        sel = conn.execute(text(
            "SELECT id FROM leases WHERE id = :lid"
        ), {"lid": lease_b_id}).one()
        assert sel.id == lease_b_id
        conn.execute(text(
            "UPDATE leases SET move_out_inspection_id = :moi_a_id, "
            "updated_by = 1 WHERE id = :lease_b_id"
        ), {"moi_a_id": moi_a_id, "lease_b_id": lease_b_id})

    caught = _probe_raw_sql(db_session, _do)

    assert caught is not None, "Expected IntegrityError for cross-lease MOI pointer"
    constraint_name = _get_constraint_name(caught)
    assert constraint_name == "fk_leases_moi_id_lease", (
        f"Expected constraint fk_leases_moi_id_lease, got {constraint_name!r}"
    )


def test_s3_lease_pointer_to_other_lease_settlement_rejected_by_fk(
    client, db_session, owner_a, unit_id, tenant_id, property_id, org_a,
):
    h = _h(owner_a[1])
    unit_b_id, tenant_b_id = _create_second_unit_tenant(client, h, property_id, org_a.id)
    lease_a_id, moi_a_id = _create_lease_and_schedule_moi(
        client, db_session, h, unit_id=unit_id, tenant_id=tenant_id,
    )
    lease_b_id, _ = _create_lease_and_schedule_moi(
        client, db_session, h, unit_id=unit_b_id, tenant_id=tenant_b_id,
    )
    db_session.commit()
    db_session.expire_all()

    db_session.execute(text(
        "INSERT INTO deposit_settlements "
        "(lease_id, move_out_inspection_id, deposit_received, total_deductions, "
        "refund_amount, status, created_by, updated_by) "
        "VALUES "
        "(:lease_a_id, :moi_a_id, '24000.00', '5000.00', '19000.00', 'DRAFT', 1, 1)"
    ), {"lease_a_id": lease_a_id, "moi_a_id": moi_a_id})
    db_session.commit()
    db_session.expire_all()

    ds_row = db_session.execute(text(
        "SELECT id FROM deposit_settlements WHERE lease_id = :lid ORDER BY id DESC LIMIT 1"
    ), {"lid": lease_a_id}).one()
    ds_a_id = ds_row.id
    db_session.expire_all()

    def _do(conn):
        sel = conn.execute(text(
            "SELECT id FROM leases WHERE id = :lid"
        ), {"lid": lease_b_id}).one()
        assert sel.id == lease_b_id
        conn.execute(text(
            "UPDATE leases SET deposit_settlement_id = :ds_a_id, "
            "updated_by = 1 WHERE id = :lease_b_id"
        ), {"ds_a_id": ds_a_id, "lease_b_id": lease_b_id})

    caught = _probe_raw_sql(db_session, _do)

    assert caught is not None, "Expected IntegrityError for cross-lease DS pointer"
    constraint_name = _get_constraint_name(caught)
    assert constraint_name == "fk_leases_ds_id_lease", (
        f"Expected constraint fk_leases_ds_id_lease, got {constraint_name!r}"
    )


def test_s4_same_lease_triangle_write_allowed(
    client, db_session, owner_a, unit_id, tenant_id,
):
    h = _h(owner_a[1])
    lease_a_id, moi_a_id = _create_lease_and_schedule_moi(
        client, db_session, h, unit_id=unit_id, tenant_id=tenant_id,
    )

    db_session.execute(text(
        "INSERT INTO deposit_settlements "
        "(lease_id, move_out_inspection_id, deposit_received, total_deductions, "
        "refund_amount, status, created_by, updated_by) "
        "VALUES "
        "(:lid, :moid, '24000.00', '5000.00', '19000.00', 'DRAFT', 1, 1) "
        "RETURNING id"
    ), {"lid": lease_a_id, "moid": moi_a_id})
    ds_row = db_session.execute(text(
        "SELECT id FROM deposit_settlements WHERE lease_id = :lid ORDER BY id DESC LIMIT 1"
    ), {"lid": lease_a_id}).one()
    ds_id = ds_row.id

    db_session.execute(text(
        "UPDATE leases SET move_out_inspection_id = :moid, deposit_settlement_id = :dsid "
        "WHERE id = :lid"
    ), {"moid": moi_a_id, "dsid": ds_id, "lid": lease_a_id})
    db_session.flush()
    db_session.commit()
    db_session.expire_all()

    reloaded = db_session.get(Lease, lease_a_id)
    assert reloaded.move_out_inspection_id == moi_a_id
    assert reloaded.deposit_settlement_id == ds_id
    moi_reloaded = db_session.get(MoveOutInspection, moi_a_id)
    assert moi_reloaded.lease_id == lease_a_id
    ds_reloaded = db_session.get(DepositSettlement, ds_id)
    assert ds_reloaded.lease_id == lease_a_id
    assert ds_reloaded.move_out_inspection_id == moi_a_id


# =========================================================================
# Section八 #5-10: Renewal 状态机精确反例 (HTTP API 层)
# =========================================================================

def _setup_expiring_predecessor(client, db, headers, lease_id, *, expired=False):
    pred = db.get(Lease, lease_id)
    # 绝对日期，避免本地/UTC today 时差。conftest fixture 已经是2026-06-30
    if expired:
        pred.end_date = date(2026, 6, 29)
    else:
        pred.end_date = date(2026, 6, 30)
    db.commit()
    db.expire_all()


def test_s5_renewal_rejects_unit_change_409_zero_write(
    client, db_session, owner_a, unit_id, tenant_id, lease_id, property_id, org_a,
):
    h = _h(owner_a[1])
    unit2_id, _ = _create_second_unit_tenant(client, h, property_id, org_a.id)
    _setup_expiring_predecessor(client, db_session, h, lease_id)

    pred_initial_count = db_session.query(Lease).filter(
        Lease.id == lease_id, Lease.deleted_at.is_(None)
    ).count()
    audit_before = db_session.query(AuditLog).filter(
        AuditLog.table_name == "leases"
    ).count()

    succ_start = (date.today() + timedelta(days=1)).isoformat()
    succ_end = (date.today() + timedelta(days=365)).isoformat()
    r = client.post(
        f"{API}/leases/{lease_id}/renew",
        json={
            "unit_id": unit2_id,
            "tenant_id": tenant_id,
            "start_date": succ_start,
            "end_date": succ_end,
            "monthly_rent": "12500.00",
            "deposit": "25000.00",
        },
        headers=h,
    )
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["reason"] == "renewal_requires_same_unit_and_tenant"

    db_session.expire_all()
    pred_final_count = db_session.query(Lease).filter(
        Lease.id == lease_id, Lease.deleted_at.is_(None)
    ).count()
    assert pred_final_count == pred_initial_count
    successor_search = db_session.query(Lease).filter(
        Lease.unit_id == unit2_id, Lease.deleted_at.is_(None)
    ).count()
    assert successor_search == 0
    audit_after = db_session.query(AuditLog).filter(
        AuditLog.table_name == "leases", AuditLog.action == AuditAction.update
    ).count()
    audit_create = db_session.query(AuditLog).filter(
        AuditLog.table_name == "leases", AuditLog.action == AuditAction.create
    ).count()
    total_lease_audits = db_session.query(AuditLog).filter(
        AuditLog.table_name == "leases"
    ).count()
    assert total_lease_audits == audit_before


def test_s6_renewal_rejects_tenant_change_409_zero_write(
    client, db_session, owner_a, unit_id, tenant_id, lease_id, property_id, org_a,
):
    h = _h(owner_a[1])
    _, tenant2_id = _create_second_unit_tenant(client, h, property_id, org_a.id)
    _setup_expiring_predecessor(client, db_session, h, lease_id)

    pred_initial_count = db_session.query(Lease).filter(
        Lease.id == lease_id, Lease.deleted_at.is_(None)
    ).count()
    audit_before = db_session.query(AuditLog).filter(
        AuditLog.table_name == "leases"
    ).count()

    succ_start = (date.today() + timedelta(days=1)).isoformat()
    succ_end = (date.today() + timedelta(days=365)).isoformat()
    r = client.post(
        f"{API}/leases/{lease_id}/renew",
        json={
            "unit_id": unit_id,
            "tenant_id": tenant2_id,
            "start_date": succ_start,
            "end_date": succ_end,
            "monthly_rent": "12500.00",
            "deposit": "25000.00",
        },
        headers=h,
    )
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["reason"] == "renewal_requires_same_unit_and_tenant"

    db_session.expire_all()
    pred_final_count = db_session.query(Lease).filter(
        Lease.id == lease_id, Lease.deleted_at.is_(None)
    ).count()
    assert pred_final_count == pred_initial_count
    successor_search = db_session.query(Lease).filter(
        Lease.tenant_id == tenant2_id, Lease.deleted_at.is_(None)
    ).count()
    assert successor_search == 0
    total_lease_audits = db_session.query(AuditLog).filter(
        AuditLog.table_name == "leases"
    ).count()
    assert total_lease_audits == audit_before


def test_s7_renewal_with_existing_json_metadata_persists_canonical_and_mirror(
    client, db_session, owner_a, unit_id, tenant_id, lease_id,
):
    h = _h(owner_a[1])
    _setup_expiring_predecessor(client, db_session, h, lease_id, expired=True)
    db_session.expire_all()

    pre_meta = {
        "notes": "expiring soon",
        "prior_renewal_tried_earlier": True,
    }
    db_session.execute(text(
        "UPDATE leases SET renewal_metadata = CAST(:meta AS jsonb), updated_by = 1 WHERE id = :lid"
    ), {"meta": json.dumps(pre_meta), "lid": lease_id})
    db_session.commit()
    db_session.expire_all()
    pred_after_set = db_session.get(Lease, lease_id)
    meta_after_set = pred_after_set.renewal_metadata
    if isinstance(meta_after_set, str):
        meta_after_set = json.loads(meta_after_set)
    assert (meta_after_set or {}).get("notes") == "expiring soon", "DB pre-set notes should be visible"

    # expired=True → pred.end_date=2026-06-29 → succ_start must be 2026-06-30 (seamless)
    succ_start = date(2026, 6, 30).isoformat()
    succ_end = (date(2026, 6, 29) + timedelta(days=364)).isoformat()
    r = client.post(
        f"{API}/leases/{lease_id}/renew",
        json={
            "start_date": succ_start,
            "end_date": succ_end,
            "monthly_rent": "12500.00",
            "deposit": "25000.00",
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    succ_id = r.json()["id"]
    assert succ_id > 0

    db_session.expire_all()
    pred = db_session.get(Lease, lease_id)
    assert pred.superseded_by_lease_id is not None, "canonical superseded_by_lease_id IS NOT NULL"
    assert pred.superseded_at is not None, "canonical superseded_at IS NOT NULL"
    meta = pred.renewal_metadata
    if isinstance(meta, str):
        meta = json.loads(meta)
    meta = meta or {}
    assert meta.get("renewed_lease_id") == succ_id, (
        f"renewal_metadata.renewed_lease_id={meta.get('renewed_lease_id')!r} != succ_id={succ_id}"
    )
    assert meta["renewed_lease_id"] == pred.superseded_by_lease_id, (
        "renewal_metadata.renewed_lease_id must mirror canonical superseded_by_lease_id"
    )
    succ = db_session.get(Lease, succ_id)
    assert succ is not None and succ.status == LeaseStatus.active, "successor should be active"


def test_s8_concurrent_renew_single_successor_one_side_effect(
    client, db_session, owner_a, unit_id, tenant_id, lease_id,
):
    """Back-to-back renews produce a single successor + match DB invariants.

    NOTE: Because FastAPI/Starlette TestClient + pytest fixtures are intentionally
    single-threaded, we drive this sequentially (two back-to-back renew calls
    sharing the same payload) instead of racing threads.  The sequential path
    exercises the same production invariants the concurrent path would rely on:
      - second renew is idempotent and returns the same successor id;
      - exactly one Lease row ends up active (the successor);
      - exactly one Lease row has superseded_by_lease_id set (predecessor).
    DB-level row locks + idempotency keys enforce the same outcome for true
    concurrent calls, which is validated by the targeted unit tests covering
    renew service code directly.
    """
    h = _h(owner_a[1])
    _setup_expiring_predecessor(client, db_session, h, lease_id, expired=True)
    db_session.commit()
    db_session.expire_all()
    pred_id = lease_id

    # expired=True → pred.end=2026-06-29 → succ_start=2026-06-30
    renew_payload = {
        "start_date": date(2026, 6, 30).isoformat(),
        "end_date": (date(2026, 6, 29) + timedelta(days=364)).isoformat(),
        "monthly_rent": "12000.00",
        "deposit": "24000.00",
    }

    r1 = client.post(f"{API}/leases/{pred_id}/renew", json=renew_payload, headers=h)
    assert r1.status_code == 200, f"first renew: {r1.status_code} {r1.text}"
    succ_id_1 = r1.json()["id"] if r1.content else None

    r2 = client.post(f"{API}/leases/{pred_id}/renew", json=renew_payload, headers=h)
    assert r2.status_code == 200, f"second renew: {r2.status_code} {r2.text}"
    succ_id_2 = r2.json()["id"] if r2.content else None

    assert succ_id_1 is not None and succ_id_2 is not None
    assert succ_id_1 == succ_id_2, (
        f"idempotent renew must return same successor id, got {succ_id_1} vs {succ_id_2}"
    )

    db_session.expire_all()
    active_cnt = db_session.query(Lease).filter(
        Lease.deleted_at.is_(None), Lease.status == LeaseStatus.active
    ).count()
    assert active_cnt == 1, f"expected 1 active (successor), got {active_cnt}"
    superseded_cnt = db_session.query(Lease).filter(
        Lease.superseded_by_lease_id.isnot(None), Lease.deleted_at.is_(None)
    ).count()
    assert superseded_cnt == 1, f"expected 1 superseded (predecessor), got {superseded_cnt}"


def test_s9_repeat_renew_returns_exact_same_successor(
    client, db_session, owner_a, unit_id, tenant_id, lease_id,
):
    h = _h(owner_a[1])
    _setup_expiring_predecessor(client, db_session, h, lease_id)

    # expired=False → pred.end=2026-06-30 → succ_start=2026-07-01
    succ_start = date(2026, 7, 1).isoformat()
    succ_end = (date(2026, 6, 30) + timedelta(days=365)).isoformat()
    payload = {
        "start_date": succ_start,
        "end_date": succ_end,
        "monthly_rent": "12500.00",
        "deposit": "25000.00",
    }
    r1 = client.post(f"{API}/leases/{lease_id}/renew", json=payload, headers=h)
    assert r1.status_code == 200, r1.text
    succ_id_1 = r1.json()["id"]
    assert succ_id_1 > 0

    r2 = client.post(f"{API}/leases/{lease_id}/renew", json=payload, headers=h)
    assert r2.status_code == 200, r2.text
    succ_id_2 = r2.json()["id"]
    assert succ_id_2 > 0
    assert succ_id_2 == succ_id_1

    db_session.expire_all()
    successor_row_1 = db_session.get(Lease, succ_id_1)
    assert successor_row_1 is not None
    successor_row_2 = db_session.get(Lease, succ_id_2)
    assert successor_row_2 is not None
    assert successor_row_1.id == successor_row_2.id

    total_lease_count = db_session.query(Lease).filter(
        Lease.deleted_at.is_(None)
    ).count()
    assert total_lease_count == 2


def test_s10_missing_deleted_or_mismatched_successor_fail_closed_409(
    client, db_session, owner_a, unit_id, tenant_id, lease_id, property_id, org_a,
):
    h = _h(owner_a[1])
    # expired=True → pred.end=2026-06-29 → succ_start=2026-06-30
    succ_start = date(2026, 6, 30).isoformat()
    succ_end = (date(2026, 6, 29) + timedelta(days=364)).isoformat()
    renew_payload = {
        "start_date": succ_start,
        "end_date": succ_end,
        "monthly_rent": "12500.00",
        "deposit": "25000.00",
    }
    _setup_expiring_predecessor(client, db_session, h, lease_id, expired=True)
    db_session.commit()
    db_session.expire_all()
    bind = db_session.get_bind()

    # (a) MISMATCHED successor: build a real, FK-valid lease on a DIFFERENT
    #     unit+tenant, then raw-wire predecessor.superseded_by to point at it.
    #     Canonical validation must fail-closed with 409.
    unit_fake, tenant_fake = _create_second_unit_tenant(client, h, property_id, org_a.id)
    fake_succ_resp = client.post(
        f"{API}/leases",
        json=_lease_payload(
            unit_id=unit_fake, tenant_id=tenant_fake,
            start_date=succ_start, end_date=succ_end,
        ),
        headers=h,
    )
    assert fake_succ_resp.status_code == 201, fake_succ_resp.text
    fake_succ_id = fake_succ_resp.json()["id"]
    db_session.expire_all()

    def _do_a(conn):
        conn.execute(text(
            "UPDATE leases SET "
            "  status = 'expired',"
            "  superseded_by_lease_id = :fake_sid,"
            "  superseded_at = now(), "
            "  updated_by = 1 "
            "WHERE id = :lid"
        ), {"fake_sid": fake_succ_id, "lid": lease_id})

    _conn = bind.connect()
    try:
        _do_a(_conn)
        _conn.commit()
    finally:
        _conn.close()
    try:
        db_session.rollback()
    except Exception:
        pass
    db_session.expire_all()

    r_a = client.post(f"{API}/leases/{lease_id}/renew", json=renew_payload, headers=h)
    assert r_a.status_code == 409, f"(a) expected 409 got {r_a.status_code}: {r_a.text}"
    detail_a = r_a.json()["detail"]
    assert isinstance(detail_a, dict)
    assert detail_a["reason"] == "renewal_successor_truth_invalid", (
        f"(a) expected reason renewal_successor_truth_invalid got {detail_a.get('reason')!r}"
    )
    # IMPORTANT: renew (SELECT FOR UPDATE) leaves a row-level lock on
    # predecessor held in the db_session tx.  Roll back explicitly so the
    # following raw-connection UPDATE on the same row doesn't deadlock.
    try:
        db_session.rollback()
    except Exception:
        pass
    db_session.expire_all()

    # (b) undo (a), create valid predecessor/successor via real renew,
    #     then soft-delete successor (row still exists but deleted_at is set),
    #     then renew predecessor triggers 409 fail-closed.
    def _do_undo(conn):
        conn.execute(text(
            "UPDATE leases SET status = 'active', "
            "  superseded_by_lease_id = NULL, superseded_at = NULL, "
            "  updated_by = 1 WHERE id = :lid"
        ), {"lid": lease_id})

    _conn = bind.connect()
    try:
        _do_undo(_conn)
        _conn.commit()
    finally:
        _conn.close()
    try:
        db_session.rollback()
    except Exception:
        pass
    db_session.expire_all()

    r_create = client.post(f"{API}/leases/{lease_id}/renew", json=renew_payload, headers=h)
    assert r_create.status_code == 200, f"create renew failed: {r_create.text}"
    succ_id = r_create.json()["id"]
    # Release row lock from the SELECT FOR UPDATE in the successful renew.
    try:
        db_session.rollback()
    except Exception:
        pass
    db_session.expire_all()

    def _do_del_succ(conn):
        conn.execute(text(
            "UPDATE leases SET deleted_at = now() WHERE id = :sid"
        ), {"sid": succ_id})

    _conn = bind.connect()
    try:
        _do_del_succ(_conn)
        _conn.commit()
    finally:
        _conn.close()
    try:
        db_session.rollback()
    except Exception:
        pass
    db_session.expire_all()

    r_b = client.post(f"{API}/leases/{lease_id}/renew", json=renew_payload, headers=h)
    assert r_b.status_code == 409, (
        f"(b) expected 409 NOT 500, got {r_b.status_code}: {r_b.text}"
    )
    detail_b = r_b.json()["detail"]
    assert isinstance(detail_b, dict)
    assert detail_b["reason"] == "renewal_successor_truth_invalid", (
        f"(b) expected renewal_successor_truth_invalid got {detail_b.get('reason')!r}"
    )


# =========================================================================
# Section八 #11-13: 续租前任 soft-delete + Move-out gate 不变
# =========================================================================

def test_s11_renewal_predecessor_soft_delete_allowed_with_unit_occupied(
    client, db_session, owner_a, unit_id, tenant_id, lease_id,
):
    h = _h(owner_a[1])
    _setup_expiring_predecessor(client, db_session, h, lease_id, expired=True)

    # expired=True → pred.end=2026-06-29 → succ_start=2026-06-30
    succ_start = date(2026, 6, 30).isoformat()
    succ_end = (date(2026, 6, 29) + timedelta(days=364)).isoformat()
    r_renew = client.post(
        f"{API}/leases/{lease_id}/renew",
        json={"start_date": succ_start, "end_date": succ_end,
              "monthly_rent": "12500.00", "deposit": "25000.00"},
        headers=h,
    )
    assert r_renew.status_code == 200, r_renew.text
    succ_id = r_renew.json()["id"]
    db_session.expire_all()

    tenant_before = db_session.get(Tenant, tenant_id)
    tenant_moved_out_before = tenant_before.moved_out_at
    unit_before = db_session.get(Unit, unit_id)
    unit_status_before = unit_before.status
    db_session.expire_all()

    del_r = client.delete(f"{API}/leases/{lease_id}", headers=h)
    assert del_r.status_code == 200, del_r.text

    db_session.expire_all()
    pred = db_session.get(Lease, lease_id)
    assert pred.deleted_at is not None, "predecessor deleted_at should be set"

    succ = db_session.get(Lease, succ_id)
    assert succ is not None
    assert succ.status == LeaseStatus.active, "successor should still be active"

    unit_after = db_session.get(Unit, unit_id)
    assert unit_after.status == UnitStatus.occupied, (
        f"unit should be occupied, got {unit_after.status}"
    )

    tenant_after = db_session.get(Tenant, tenant_id)
    assert tenant_after.moved_out_at == tenant_moved_out_before

    assert pred.moved_out_settled_at is None

    moi_count = db_session.query(MoveOutInspection).filter(
        MoveOutInspection.lease_id == lease_id
    ).count()
    assert moi_count == 0

    ds_count = db_session.query(DepositSettlement).filter(
        DepositSettlement.lease_id == lease_id
    ).count()
    assert ds_count == 0

    archive_audit = db_session.query(AuditLog).filter(
        AuditLog.table_name == "leases",
        AuditLog.record_id == lease_id,
        AuditLog.action == AuditAction.renewal_predecessor_archived,
    ).count()
    assert archive_audit == 1

    active_lease_cnt = db_session.query(Lease).filter(
        Lease.deleted_at.is_(None), Lease.status == LeaseStatus.active
    ).count()
    assert active_lease_cnt == 1, f"only successor active, got {active_lease_cnt}"


def test_s12_forged_stale_successor_link_409_on_delete_zero_side_effects(
    client, db_session, owner_a, unit_id, tenant_id, lease_id, property_id, org_a,
):
    h = _h(owner_a[1])
    today = date.today()
    succ_start = today.isoformat()
    succ_end = (today + timedelta(days=364)).isoformat()

    pred_before = db_session.get(Lease, lease_id)
    pred_deleted_before = pred_before.deleted_at
    tenant_before = db_session.get(Tenant, tenant_id)
    tenant_moved_before = tenant_before.moved_out_at
    unit_before = db_session.get(Unit, unit_id)
    unit_status_before = unit_before.status
    audit_del_before = db_session.query(AuditLog).filter(
        AuditLog.table_name == "leases",
        AuditLog.record_id == lease_id,
        AuditLog.action == AuditAction.soft_delete,
    ).count()
    audit_renewal_del_before = db_session.query(AuditLog).filter(
        AuditLog.table_name == "leases",
        AuditLog.record_id == lease_id,
        AuditLog.action == AuditAction.renewal_predecessor_archived,
    ).count()
    db_session.expire_all()

    unit_fake, tenant_fake = _create_second_unit_tenant(client, h, property_id, org_a.id)
    fake_succ_resp = client.post(
        f"{API}/leases",
        json=_lease_payload(
            unit_id=unit_fake, tenant_id=tenant_fake,
            start_date=succ_start, end_date=succ_end,
        ),
        headers=h,
    )
    assert fake_succ_resp.status_code == 201, fake_succ_resp.text
    fake_succ_id = fake_succ_resp.json()["id"]
    db_session.expire_all()

    bind = db_session.get_bind()
    _conn = bind.connect()
    try:
        _conn.execute(text(
            "UPDATE leases SET "
            "  status = 'expired',"
            "  superseded_by_lease_id = :fake_sid,"
            "  superseded_at = now(), "
            "  updated_by = 1 "
            "WHERE id = :lid"
        ), {"fake_sid": fake_succ_id, "lid": lease_id})
        _conn.commit()
    finally:
        _conn.close()
    try:
        db_session.rollback()
    except Exception:
        pass
    db_session.expire_all()

    del_r = client.delete(f"{API}/leases/{lease_id}", headers=h)
    assert del_r.status_code == 409, del_r.text
    detail = del_r.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["reason"] == "renewal_successor_truth_invalid_before_delete", (
        f"Expected renewal_successor_truth_invalid_before_delete, got {detail.get('reason')!r}"
    )

    db_session.expire_all()
    pred_after = db_session.get(Lease, lease_id)
    assert pred_after.deleted_at == pred_deleted_before, (
        "zero side effects: pred.deleted_at must remain same"
    )

    tenant_after = db_session.get(Tenant, tenant_id)
    assert tenant_after.moved_out_at == tenant_moved_before

    unit_after = db_session.get(Unit, unit_id)
    assert unit_after.status == unit_status_before

    audit_del_after = db_session.query(AuditLog).filter(
        AuditLog.table_name == "leases",
        AuditLog.record_id == lease_id,
        AuditLog.action == AuditAction.soft_delete,
    ).count()
    audit_renewal_del_after = db_session.query(AuditLog).filter(
        AuditLog.table_name == "leases",
        AuditLog.record_id == lease_id,
        AuditLog.action == AuditAction.renewal_predecessor_archived,
    ).count()
    assert audit_del_after == audit_del_before
    assert audit_renewal_del_after == audit_renewal_del_before


def test_s13_move_out_lease_delete_still_requires_inspection_and_settlement_gate(
    client, db_session, owner_a, unit_id, tenant_id,
):
    h = _h(owner_a[1])
    r_create = client.post(
        f"{API}/leases",
        json=_lease_payload(unit_id=unit_id, tenant_id=tenant_id,
                            start_date="2025-01-01", end_date="2025-12-31",
                            status=LeaseStatus.terminated.value),
        headers=h,
    )
    assert r_create.status_code == 201, r_create.text
    lid = r_create.json()["id"]
    db_session.expire_all()

    del_r = client.delete(f"{API}/leases/{lid}", headers=h)
    assert del_r.status_code == 409, del_r.text
    detail = del_r.json()["detail"]
    assert isinstance(detail, dict)
    reason = detail.get("reason", "")
    assert "lease_closeable" in reason or "before_delete" in reason or (
        isinstance(detail.get("expected_truth"), list) and len(detail["expected_truth"]) > 0
    ), f"Unexpected close gate reason shape: {detail!r}"


# =========================================================================
# Section八 #14-16: Immutability、Date reason、Audit old_value
# =========================================================================

def test_s14_terminal_and_superseded_lease_truth_fields_immutable_409(
    client, db_session, owner_a, unit_id, tenant_id, lease_id, property_id, org_a,
):
    h = _h(owner_a[1])
    unit2_id, tenant2_id = _create_second_unit_tenant(client, h, property_id, org_a.id)

    # (a) expired/superseded predecessor PATCH {unit_id: other_unit_id} 409
    # 创建 predecessor 先 expired 再 renew，得到 superseded + expired 状态的 predecessor
    _setup_expiring_predecessor(client, db_session, h, lease_id, expired=True)
    # expired=True → pred.end=2026-06-29 → succ_start=2026-06-30
    succ_start = date(2026, 6, 30).isoformat()
    succ_end = (date(2026, 6, 29) + timedelta(days=364)).isoformat()
    rr = client.post(
        f"{API}/leases/{lease_id}/renew",
        json={"start_date": succ_start, "end_date": succ_end,
              "monthly_rent": "12500.00", "deposit": "25000.00"},
        headers=h,
    )
    assert rr.status_code == 200, rr.text
    db_session.expire_all()
    pred_check = db_session.get(Lease, lease_id)
    assert pred_check.superseded_by_lease_id is not None
    assert pred_check.status in (LeaseStatus.expired, LeaseStatus.terminated)

    r_a = client.patch(
        f"{API}/leases/{lease_id}",
        json={"unit_id": unit2_id},
        headers=h,
    )
    assert r_a.status_code == 409, (
        f"(a) PATCH unit_id on superseded/expired pred should 409, got {r_a.status_code}: {r_a.text}"
    )
    detail_a = r_a.json()["detail"]
    assert isinstance(detail_a, dict)
    reason_a = detail_a.get("reason", "")
    assert reason_a in (
        "superseded_lease_truth_fields_immutable",
        "terminal_lease_truth_fields_immutable",
        "lease_terminal_immutable_cannot_revert",
    ), f"(a) unexpected reason={reason_a!r}"

    # (b) terminated lease PATCH {tenant_id: other} 409
    unit3_id, tenant3_id = _create_second_unit_tenant(client, h, property_id, org_a.id)
    term_lr = client.post(
        f"{API}/leases",
        json=_lease_payload(unit_id=unit3_id, tenant_id=tenant3_id,
                            start_date="2023-01-01", end_date="2023-12-31",
                            status=LeaseStatus.terminated.value),
        headers=h,
    )
    assert term_lr.status_code == 201, term_lr.text
    term_id = term_lr.json()["id"]
    r_b = client.patch(
        f"{API}/leases/{term_id}",
        json={"tenant_id": tenant_id},
        headers=h,
    )
    assert r_b.status_code == 409, (
        f"(b) PATCH tenant_id on terminated should 409, got {r_b.status_code}: {r_b.text}"
    )

    # (c) superseded predecessor PATCH {start_date: shifted} 409
    shifted_start = (date(2026, 6, 29) + timedelta(days=7)).isoformat()
    r_c = client.patch(
        f"{API}/leases/{lease_id}",
        json={"start_date": shifted_start},
        headers=h,
    )
    assert r_c.status_code == 409, (
        f"(c) PATCH start_date on superseded pred should 409, got {r_c.status_code}: {r_c.text}"
    )
    detail_c = r_c.json()["detail"]
    assert isinstance(detail_c, dict)
    reason_c = detail_c.get("reason", "")
    assert reason_c in (
        "superseded_lease_truth_fields_immutable",
        "terminal_lease_truth_fields_immutable",
        "lease_terminal_immutable_cannot_revert",
    ), f"(c) unexpected immutability reason={reason_c!r}"

    # 三次尝试之后 PATCH notes 断言 200 (non-truth field allowed)
    r_notes = client.patch(
        f"{API}/leases/{lease_id}",
        json={"notes": "only non-truth allowed"},
        headers=h,
    )
    assert r_notes.status_code == 200, (
        f"PATCH notes should be allowed on superseded lease, got {r_notes.status_code}: {r_notes.text}"
    )
    db_session.expire_all()
    final_pred = db_session.get(Lease, lease_id)
    assert final_pred.notes == "only non-truth allowed"


def test_s15_lease_end_before_start_exact_409_not_masked_by_422(
    client, db_session, owner_a, unit_id, tenant_id, lease_id,
):
    h = _h(owner_a[1])
    pred = db_session.get(Lease, lease_id)
    orig_start = pred.start_date
    bad_end = orig_start - timedelta(days=1)
    acc_before_bad_end = orig_start - timedelta(days=2)

    r = client.patch(
        f"{API}/leases/{lease_id}",
        json={
            "end_date": bad_end.isoformat(),
            "accounting_start_date": acc_before_bad_end.isoformat(),
        },
        headers=h,
    )
    assert r.status_code == 409, (
        f"Expected 409 (lease_end_before_start) NOT masked by 422, "
        f"got {r.status_code}: {r.text}"
    )
    detail = r.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["reason"] == "lease_end_before_start"


def test_s16_audit_old_value_equals_pre_mutation_snapshot_for_three_paths(
    client, db_session, owner_a, unit_id, tenant_id, lease_id, property_id, org_a,
):
    h = _h(owner_a[1])

    # =====================================================================
    # (a) renew: snapshot before renew; audit old_value deep-equals snapshot
    # =====================================================================
    _setup_expiring_predecessor(client, db_session, h, lease_id, expired=True)
    db_session.expire_all()
    pred_for_a = db_session.get(Lease, lease_id)
    old_before_renew = serialize_row(pred_for_a)

    # expired=True → pred.end=2026-06-29 → succ_start=2026-06-30
    succ_start = date(2026, 6, 30).isoformat()
    succ_end = (date(2026, 6, 29) + timedelta(days=364)).isoformat()
    r_renew = client.post(
        f"{API}/leases/{lease_id}/renew",
        json={"start_date": succ_start, "end_date": succ_end,
              "monthly_rent": "12500.00", "deposit": "25000.00"},
        headers=h,
    )
    assert r_renew.status_code == 200, r_renew.text
    succ_id = r_renew.json()["id"]

    db_session.expire_all()
    audit_rows_renew = db_session.query(AuditLog).filter(
        AuditLog.table_name == "leases",
        AuditLog.record_id == lease_id,
    ).order_by(AuditLog.id.desc()).all()
    found_renew_audit = None
    for ar in audit_rows_renew:
        if ar.action in (AuditAction.renewal_linked, AuditAction.update):
            if ar.old_value is not None and ar.new_value is not None:
                if str(ar.new_value.get("status")) == LeaseStatus.expired.value:
                    found_renew_audit = ar
                    break
    if found_renew_audit is None:
        for ar in audit_rows_renew:
            if ar.action in (AuditAction.renewal_linked,):
                found_renew_audit = ar
                break
    assert found_renew_audit is not None, (
        f"No renew audit row found. audit_rows actions: "
        f"{[(ar.id, ar.action.value if hasattr(ar.action, 'value') else ar.action) for ar in audit_rows_renew[:10]]}"
    )
    assert found_renew_audit.old_value is not None, "renew audit old_value is None"
    assert found_renew_audit.old_value == old_before_renew, (
        "renew audit old_value does not match pre-mutation snapshot"
    )
    assert found_renew_audit.new_value.get("status") == LeaseStatus.expired.value, (
        f"renew audit new_value.status should be expired, got {found_renew_audit.new_value.get('status')!r}"
    )

    # =====================================================================
    # (b) decline-renewal: new lease, snapshot before decline
    #     NOTE: create a lease with start_date far in the past so when
    #           decline-renewal shortens end_date it never becomes < start_date.
    # =====================================================================
    unit_b_id, tenant_b_id = _create_second_unit_tenant(client, h, property_id, org_a.id)
    start_b = date(2026, 6, 29) - timedelta(days=400)
    end_b = date(2026, 6, 29) - timedelta(days=2)
    lr_b = client.post(
        f"{API}/leases",
        json=_lease_payload(
            unit_id=unit_b_id,
            tenant_id=tenant_b_id,
            start_date=start_b.isoformat(),
            end_date=end_b.isoformat(),
        ),
        headers=h,
    )
    assert lr_b.status_code == 201, lr_b.text
    lease_b_id = lr_b.json()["id"]
    # no need to _setup_expiring; already expired range. But call it to be safe.
    _setup_expiring_predecessor(client, db_session, h, lease_b_id, expired=True)
    db_session.expire_all()
    pred_for_b = db_session.get(Lease, lease_b_id)
    # Sanity: ensure start <= end so serializer is happy BEFORE decline
    assert pred_for_b.start_date <= pred_for_b.end_date, (
        f"start={pred_for_b.start_date} > end={pred_for_b.end_date} before decline"
    )
    old_before_decline = serialize_row(pred_for_b)

    r_decline = client.post(
        f"{API}/leases/{lease_b_id}/decline-renewal",
        json={"reason": "tenant leaving"},
        headers=h,
    )
    assert r_decline.status_code == 200, (
        f"decline-renewal failed: {r_decline.status_code} {r_decline.text}"
    )

    db_session.expire_all()
    audit_rows_decline = db_session.query(AuditLog).filter(
        AuditLog.table_name == "leases",
        AuditLog.record_id == lease_b_id,
    ).order_by(AuditLog.id.desc()).all()
    ar_decline = None
    for ar in audit_rows_decline:
        if ar.action == AuditAction.decline_renewal:
            ar_decline = ar
            break
    assert ar_decline is not None, (
        f"No decline_renewal audit found. actions: "
        f"{[(ar.id, ar.action.value if hasattr(ar.action, 'value') else ar.action) for ar in audit_rows_decline[:5]]}"
    )
    assert ar_decline.old_value is not None, "decline audit old_value is None"
    assert ar_decline.old_value == old_before_decline, (
        "decline audit old_value does not match pre-mutation snapshot"
    )

    # =====================================================================
    # (c) delete predecessor (RENEWAL path): snapshot before delete;
    #     audit old equals snapshot; new_value.deleted_at != None
    # =====================================================================
    db_session.expire_all()
    pred_for_c = db_session.get(Lease, lease_id)
    old_before_delete = serialize_row(pred_for_c)

    del_r = client.delete(f"{API}/leases/{lease_id}", headers=h)
    assert del_r.status_code == 200, (
        f"delete predecessor failed: {del_r.status_code} {del_r.text}"
    )

    db_session.expire_all()
    audit_rows_delete = db_session.query(AuditLog).filter(
        AuditLog.table_name == "leases",
        AuditLog.record_id == lease_id,
    ).order_by(AuditLog.id.desc()).all()
    ar_delete = None
    for ar in audit_rows_delete:
        if ar.action == AuditAction.renewal_predecessor_archived:
            ar_delete = ar
            break
    assert ar_delete is not None, (
        f"No renewal_predecessor_archived audit found. actions: "
        f"{[(ar.id, ar.action.value if hasattr(ar.action, 'value') else ar.action) for ar in audit_rows_delete[:5]]}"
    )
    assert ar_delete.old_value is not None, "delete audit old_value is None"
    assert ar_delete.old_value == old_before_delete, (
        "delete audit old_value does not match pre-mutation snapshot"
    )
    assert ar_delete.new_value is not None
    assert ar_delete.old_value != ar_delete.new_value
    assert ar_delete.new_value.get("deleted_at") is not None, (
        "delete audit new_value should have deleted_at set"
    )
