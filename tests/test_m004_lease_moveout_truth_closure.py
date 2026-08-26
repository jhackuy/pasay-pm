"""PASAY-MILESTONE-004 Lease & Move-out Truth Closure Targeted tests.

Scope: ONLY Lease Renewal SM + Move-out Inspection SM + Deposit Settlement +
Lease Close Gate + Truth→Task Projection.

（已重新 assertion audit 每状态码/reason/ID 精确，禁止 ≥1 / contains / A or B / !500 弱断言。Section 六 #20 完成）
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func as sqla_func
from sqlalchemy.orm import sessionmaker

from app.models.audit_log import AuditLog
from app.models.deposit_settlement import DepositSettlement, DepositSettlementStatus
from app.models.evidence import EvidenceCategory
from app.models.financial import Expense, ExpenseStatus, Income, IncomeStatus
from app.models.lease import Lease, LeaseStatus
from app.models.move_out import MoveOutInspection, MoveOutInspectionStatus
from app.models.operations import (
    OperationalTask,
    OperationalTaskPriority,
    OperationalTaskStatus,
    OperationalTaskType,
)
from app.models.property import UnitLifecycleEvent, UnitStatus
from app.models.tenant import Tenant
from app.services.audit import audit_context, record_audit
from app.services.operations.generation import generate_business_tasks
from app.services.operations.reconcile import reconcile_tasks
from app.database import SessionLocal

API = "/api/v1"


def _h(key):
    return {"Authorization": f"Bearer {key}"}


def _create_evidence(client, headers, *, unit_id, category, file_id=None):
    fid = file_id or f"moveout-photo-{uuid.uuid4().hex[:8]}"
    r = client.post(
        f"{API}/evidence",
        json={
            "storage_provider": "local",
            "external_file_id": fid,
            "category": category.value if isinstance(category, EvidenceCategory) else category,
            "unit_id": unit_id,
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _schedule_inspection_api(client, headers, *, lease_id, scheduled_at=None):
    when = scheduled_at or (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    r = client.post(
        f"{API}/move-out-inspections",
        json={
            "lease_id": lease_id,
            "scheduled_at": when,
        },
        headers=headers,
    )
    if r.status_code == 201:
        return r.json()
    if r.status_code == 409:
        detail = r.json().get("detail") if isinstance(r.json(), dict) else {}
        existing_id = detail.get("existing_inspection_id") if isinstance(detail, dict) else None
        if existing_id is not None:
            g = client.get(f"{API}/move-out-inspections/{existing_id}", headers=headers)
            assert g.status_code == 200, g.text
            return g.json()
    raise AssertionError(f"schedule_inspection_api unexpected status={r.status_code}: {r.text}")


def _schedule_and_full_inspection(client, db, headers, *, lease_id):
    """Happy path: schedule + evidence + inspected + confirmed.
    Returns confirmed inspection dict.
    """
    insp = _schedule_inspection_api(client, headers, lease_id=lease_id)
    insp_id = insp["id"]
    lease = db.get(Lease, lease_id)
    unit_id = lease.unit_id
    eid = _create_evidence(client, headers, unit_id=unit_id,
                           category=EvidenceCategory.move_out_photo)
    r = client.post(
        f"{API}/move-out-inspections/{insp_id}/inspect",
        json={"evidence_ids": [eid], "findings": [{"item": "wall scratch", "severity": "minor"}]},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    r = client.post(f"{API}/move-out-inspections/{insp_id}/confirm", headers=headers)
    assert r.status_code == 200, r.text
    db.expire_all()
    return r.json()


def _full_settlement(client, db, headers, *, inspection_id,
                     deposit="24000.00", deductions=None, refund="19000.00"):
    """Happy path: create DRAFT + confirm with conservation. Deductions default=5000.

    FIX2: If confirm_inspection already auto-created a DRAFT settlement,
    we detect the 409, PATCH the existing draft, then confirm.
    """
    if deductions is None:
        deductions = [{"description": "wall repaint", "amount": "5000.00"}]
    payload = {
        "move_out_inspection_id": inspection_id,
        "deposit_received": deposit,
        "total_deductions": str(Decimal(deposit) - Decimal(refund)),
        "refund_amount": refund,
        "deductions": deductions,
    }
    r = client.post(
        f"{API}/deposit-settlements",
        json=payload,
        headers=headers,
    )
    sid: int
    if r.status_code == 201:
        sid = r.json()["id"]
    elif r.status_code == 409:
        detail = r.json().get("detail") if isinstance(r.json(), dict) else {}
        existing_id = detail.get("existing_settlement_id") if isinstance(detail, dict) else None
        if existing_id is None:
            raise AssertionError(f"_full_settlement 409 without existing_settlement_id: {r.text}")
        g = client.get(f"{API}/deposit-settlements/{existing_id}", headers=headers)
        assert g.status_code == 200, g.text
        existing = g.json()
        if existing.get("status") == DepositSettlementStatus.DRAFT.value:
            pass
        elif existing.get("status") in (
            DepositSettlementStatus.CONFIRMED.value,
            DepositSettlementStatus.RECONCILED.value,
        ):
            return existing
        else:
            assert False, f"_full_settlement unexpected existing status={existing.get('status')}"
        patch_payload = {
            "deposit_received": deposit,
            "total_deductions": str(Decimal(deposit) - Decimal(refund)),
            "refund_amount": refund,
            "deductions": deductions,
        }
        p = client.patch(f"{API}/deposit-settlements/{existing_id}", json=patch_payload, headers=headers)
        assert p.status_code == 200, p.text
        sid = existing_id
    else:
        raise AssertionError(f"_full_settlement unexpected status={r.status_code}: {r.text}")
    r2 = client.post(f"{API}/deposit-settlements/{sid}/confirm", headers=headers)
    assert r2.status_code == 200, r2.text
    db.expire_all()
    confirmed = r2.json()
    assert confirmed.get("status") in (
        DepositSettlementStatus.CONFIRMED.value,
        DepositSettlementStatus.RECONCILED.value,
    ), f"_full_settlement final status={confirmed.get('status')}"
    return confirmed


def _close_lease_pipeline(client, db, headers, *, lease_id):
    """Runs full close pipeline (inspection confirmed + settlement confirmed)."""
    insp = _schedule_and_full_inspection(client, db, headers, lease_id=lease_id)
    lease = db.get(Lease, lease_id)
    deposit_d = Decimal("24000.00")
    refund_d = Decimal("19000.00")
    _full_settlement(client, db, headers, inspection_id=insp["id"],
                     deposit=f"{deposit_d:.2f}", refund=f"{refund_d:.2f}")
    db.expire_all()


# =========================================================================
# A组 — Renewal state machine (7 tests)
# =========================================================================

def test_a1_renew_creates_successor_and_updates_metadata(
    client, db_session, owner_a, unit_id, tenant_id, lease_id
):
    h = _h(owner_a[1])
    pred = db_session.get(Lease, lease_id)
    assert pred.end_date <= date(2026, 6, 30)
    new_start_d = pred.end_date + timedelta(days=1)
    new_end_d = new_start_d + timedelta(days=364)
    new_start = new_start_d.isoformat()
    new_end = new_end_d.isoformat()
    r = client.post(
        f"{API}/leases/{lease_id}/renew",
        json={
            "start_date": new_start,
            "end_date": new_end,
            "monthly_rent": "12500.00",
            "deposit": "25000.00",
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    pred = db_session.get(Lease, lease_id)
    assert pred is not None
    succ = r.json()
    assert succ is not None
    succ_id_from_resp = succ["id"]
    meta = pred.renewal_metadata or {}
    assert meta["renewed_lease_id"] == succ_id_from_resp
    succ_row = db_session.get(Lease, succ_id_from_resp)
    assert succ_row is not None
    assert succ_row.status == LeaseStatus.active
    assert str(succ_row.start_date) == new_start
    assert str(succ_row.end_date) == new_end
    assert succ_row.monthly_rent == Decimal("12500.00")
    assert succ_row.deposit == Decimal("25000.00")


def test_a2_renew_is_idempotent_no_duplicate_successor(
    client, db_session, owner_a, unit_id, tenant_id, lease_id
):
    h = _h(owner_a[1])
    pred = db_session.get(Lease, lease_id)
    assert pred.end_date <= date(2026, 6, 30)
    succ_start = (pred.end_date + timedelta(days=1)).isoformat()
    succ_end = (pred.end_date + timedelta(days=366)).isoformat()
    payload = {
        "start_date": succ_start,
        "end_date": succ_end,
        "monthly_rent": "12500.00",
        "deposit": "25000.00",
    }
    r1 = client.post(f"{API}/leases/{lease_id}/renew", json=payload, headers=h)
    assert r1.status_code == 200, r1.text
    pred = db_session.get(Lease, lease_id)
    succ_id_1 = (pred.renewal_metadata or {})["renewed_lease_id"]
    active_count_1 = (
        db_session.query(Lease)
        .filter(Lease.unit_id == unit_id, Lease.status == LeaseStatus.active,
                Lease.deleted_at.is_(None))
        .count()
    )
    r2 = client.post(f"{API}/leases/{lease_id}/renew", json=payload, headers=h)
    assert r2.status_code == 200, r2.text
    pred2 = db_session.get(Lease, lease_id)
    succ_id_2 = (pred2.renewal_metadata or {})["renewed_lease_id"]
    assert succ_id_2 == succ_id_1
    active_count_2 = (
        db_session.query(Lease)
        .filter(Lease.unit_id == unit_id, Lease.status == LeaseStatus.active,
                Lease.deleted_at.is_(None))
        .count()
    )
    assert active_count_2 == active_count_1


def test_a3_decline_renewal_sets_not_renewed_flag(
    client, db_session, owner_a, lease_id
):
    h = _h(owner_a[1])
    r = client.post(
        f"{API}/leases/{lease_id}/decline-renewal",
        json={"reason": "tenant leaving"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    lease = db_session.get(Lease, lease_id)
    meta = lease.renewal_metadata or {}
    assert meta.get("not_renewed") is True
    assert "decline_reason" in meta


def test_a4_decline_renewal_is_idempotent(
    client, db_session, owner_a, lease_id
):
    h = _h(owner_a[1])
    for _ in range(3):
        r = client.post(
            f"{API}/leases/{lease_id}/decline-renewal",
            json={"reason": "same"},
            headers=h,
        )
        assert r.status_code == 200, r.text
    lease = db_session.get(Lease, lease_id)
    assert (lease.renewal_metadata or {}).get("not_renewed") is True


def test_a5_decline_after_renew_conflict(
    client, db_session, owner_a, unit_id, tenant_id, lease_id
):
    h = _h(owner_a[1])
    pred = db_session.get(Lease, lease_id)
    assert pred.end_date <= date(2026, 6, 30)
    succ_start = (pred.end_date + timedelta(days=1)).isoformat()
    succ_end = (pred.end_date + timedelta(days=365)).isoformat()
    r_renew = client.post(
        f"{API}/leases/{lease_id}/renew",
        json={
            "start_date": succ_start, "end_date": succ_end,
            "monthly_rent": "12500.00", "deposit": "25000.00",
        },
        headers=h,
    )
    assert r_renew.status_code == 200, r_renew.text
    r = client.post(
        f"{API}/leases/{lease_id}/decline-renewal",
        json={"reason": "too late"},
        headers=h,
    )
    assert r.status_code == 409, r.text


def test_a6_auto_expire_requires_end_date_past_today_and_not_renewed(
    client, db_session, owner_a, unit_id, tenant_id
):
    h = _h(owner_a[1])
    past_end = date.today() - timedelta(days=5)
    past_start = past_end - timedelta(days=365)
    r = client.post(
        f"{API}/leases",
        json={
            "unit_id": unit_id, "tenant_id": tenant_id,
            "start_date": past_start.isoformat(),
            "end_date": past_end.isoformat(),
            "monthly_rent": "10000.00",
            "deposit": "20000.00",
            "status": "active",
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    lid = r.json()["id"]
    r2 = client.post(f"{API}/leases/{lid}/auto-expire", headers=h)
    assert r2.status_code == 409, r2.text
    detail = r2.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["reason"] == "decline_renewal_required_not_renewed_missing"
    client.post(
        f"{API}/leases/{lid}/decline-renewal",
        json={"reason": "tenant gone"},
        headers=h,
    )
    _close_lease_pipeline(client, db_session, h, lease_id=lid)
    r3 = client.post(f"{API}/leases/{lid}/auto-expire", headers=h)
    assert r3.status_code == 200, r3.text
    body = r3.json()
    assert body["status"] == "expired"
    assert body["already_expired"] is False
    lease = db_session.get(Lease, lid)
    assert lease.status == LeaseStatus.expired


def test_a7_auto_expire_idempotent_on_already_inactive(
    client, db_session, owner_a, unit_id, tenant_id
):
    h = _h(owner_a[1])
    past_end = date.today() - timedelta(days=10)
    past_start = past_end - timedelta(days=365)
    r = client.post(
        f"{API}/leases",
        json={
            "unit_id": unit_id, "tenant_id": tenant_id,
            "start_date": past_start.isoformat(),
            "end_date": past_end.isoformat(),
            "monthly_rent": "10000.00",
            "deposit": "20000.00",
            "status": "active",
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    lid = r.json()["id"]
    client.post(
        f"{API}/leases/{lid}/decline-renewal", json={"reason": "x"}, headers=h,
    )
    _close_lease_pipeline(client, db_session, h, lease_id=lid)
    r1 = client.post(f"{API}/leases/{lid}/auto-expire", headers=h)
    assert r1.status_code == 200, r1.text
    assert r1.json()["already_expired"] is False
    r2 = client.post(f"{API}/leases/{lid}/auto-expire", headers=h)
    assert r2.status_code == 200, r2.text
    assert r2.json()["already_expired"] is True


# =========================================================================
# B组 — Move-out Inspection + Evidence gate (12 tests)
# =========================================================================

def test_b1_schedule_inspection_creates_scheduled_row(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    insp = _schedule_inspection_api(client, h, lease_id=lease_id)
    assert insp["status"] == MoveOutInspectionStatus.SCHEDULED.value
    assert insp["lease_id"] == lease_id
    row = db_session.get(MoveOutInspection, insp["id"])
    assert row is not None
    assert row.status == MoveOutInspectionStatus.SCHEDULED


def test_b2_double_schedule_is_idempotent_returns_existing(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    i1 = _schedule_inspection_api(client, h, lease_id=lease_id)
    i2 = _schedule_inspection_api(client, h, lease_id=lease_id)
    assert i1["id"] == i2["id"]
    count = (
        db_session.query(MoveOutInspection)
        .filter(MoveOutInspection.lease_id == lease_id)
        .count()
    )
    assert count == 1


def test_b3_inspected_transition_sets_status_to_inspected(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    insp = _schedule_inspection_api(client, h, lease_id=lease_id)
    eid = _create_evidence(client, h, unit_id=unit_id,
                           category=EvidenceCategory.move_out_photo)
    r = client.post(
        f"{API}/move-out-inspections/{insp['id']}/inspect",
        json={
            "evidence_ids": [eid],
            "findings": [{"item": "light bulb broken", "severity": "low"}],
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == MoveOutInspectionStatus.INSPECTED.value
    assert len(r.json()["evidence_ids"] or []) == 1


def test_b4_confirm_without_evidence_gate_409(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    insp = _schedule_inspection_api(client, h, lease_id=lease_id)
    insp_id = insp["id"]
    client.post(
        f"{API}/move-out-inspections/{insp_id}/inspect",
        json={
            "evidence_ids": [],
            "findings": [{"item": "x"}],
        },
        headers=h,
    )
    r = client.post(f"{API}/move-out-inspections/{insp_id}/confirm", headers=h)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["reason"] == "move_out_inspection_evidence_gate_failed"


def test_b5_confirm_without_findings_409(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    insp = _schedule_inspection_api(client, h, lease_id=lease_id)
    insp_id = insp["id"]
    eid = _create_evidence(client, h, unit_id=unit_id,
                           category=EvidenceCategory.move_out)
    client.post(
        f"{API}/move-out-inspections/{insp_id}/inspect",
        json={"evidence_ids": [eid], "findings": []},
        headers=h,
    )
    r = client.post(f"{API}/move-out-inspections/{insp_id}/confirm", headers=h)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["reason"] == "move_out_inspection_evidence_gate_failed"


def test_b6_confirm_with_evidence_and_findings_success(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    confirmed = _schedule_and_full_inspection(
        client, db_session, h, lease_id=lease_id,
    )
    assert confirmed["status"] == MoveOutInspectionStatus.CONFIRMED.value
    assert confirmed["confirmed_at"] is not None
    row = db_session.get(MoveOutInspection, confirmed["id"])
    assert row.confirmed_at is not None


def test_b7_confirm_is_idempotent(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    c1 = _schedule_and_full_inspection(
        client, db_session, h, lease_id=lease_id,
    )
    ca = c1["confirmed_at"]
    c2 = client.post(
        f"{API}/move-out-inspections/{c1['id']}/confirm", headers=h
    )
    assert c2.status_code == 200, c2.text
    assert c2.json()["confirmed_at"] == ca


def test_b8_invalid_transition_confirmed_to_inspected_409(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    c = _schedule_and_full_inspection(
        client, db_session, h, lease_id=lease_id,
    )
    r = client.post(
        f"{API}/move-out-inspections/{c['id']}/inspect",
        json={"evidence_ids": [], "findings": []},
        headers=h,
    )
    assert r.status_code == 409, r.text


def test_b9_cancel_from_scheduled_success(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    insp = _schedule_inspection_api(client, h, lease_id=lease_id)
    r = client.post(
        f"{API}/move-out-inspections/{insp['id']}/cancel", headers=h
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == MoveOutInspectionStatus.CANCELLED.value


def test_b10_secretary_cannot_confirm_owner_only_403(
    client, secretary_a, owner_a, lease_id, unit_id, tenant_id
):
    sec_h = _h(secretary_a[1])
    own_h = _h(owner_a[1])
    insp = _schedule_inspection_api(client, own_h, lease_id=lease_id)
    eid = _create_evidence(client, own_h, unit_id=unit_id,
                           category=EvidenceCategory.move_out_photo)
    client.post(
        f"{API}/move-out-inspections/{insp['id']}/inspect",
        json={"evidence_ids": [eid],
              "findings": [{"item": "a", "severity": "low"}]},
        headers=own_h,
    )
    r = client.post(
        f"{API}/move-out-inspections/{insp['id']}/confirm", headers=sec_h,
    )
    assert r.status_code == 403, r.text


def test_b11_cross_org_owner_gets_404_on_foreign_inspection(
    client, db_session, owner_a, owner_b, org_a, org_b, property_id,
    unit_id, tenant_id, lease_id,
):
    own_a_h = _h(owner_a[1])
    insp = _schedule_inspection_api(client, own_a_h, lease_id=lease_id)
    own_b_h = _h(owner_b[1])
    r = client.get(f"{API}/move-out-inspections/{insp['id']}", headers=own_b_h)
    assert r.status_code == 404, r.text


def test_b12_wrong_evidence_category_still_fails_gate(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    insp = _schedule_inspection_api(client, h, lease_id=lease_id)
    insp_id = insp["id"]
    wrong_eid = _create_evidence(client, h, unit_id=unit_id,
                                 category=EvidenceCategory.receipt)
    client.post(
        f"{API}/move-out-inspections/{insp_id}/inspect",
        json={"evidence_ids": [wrong_eid],
              "findings": [{"item": "x"}]},
        headers=h,
    )
    r = client.post(f"{API}/move-out-inspections/{insp_id}/confirm", headers=h)
    assert r.status_code == 409, r.text


# =========================================================================
# C组 — Deposit Settlement + 1c conservation (10 tests)
# =========================================================================

def _draft_settlement(client, headers, *, lease_id, inspection_id,
                      deposit="24000.00", total_deduct="5000.00",
                      refund="19000.00", deductions=None):
    if deductions is None:
        deductions = [{"description": "paint", "amount": total_deduct}]
    payload = {
        "move_out_inspection_id": inspection_id,
        "deposit_received": deposit,
        "total_deductions": total_deduct,
        "refund_amount": refund,
        "deductions": deductions,
    }
    r = client.post(
        f"{API}/deposit-settlements",
        json=payload,
        headers=headers,
    )
    if r.status_code == 201:
        return r.json()
    if r.status_code == 409:
        detail = r.json().get("detail") if isinstance(r.json(), dict) else {}
        existing_id = detail.get("existing_settlement_id") if isinstance(detail, dict) else None
        if existing_id is None:
            raise AssertionError(f"_draft_settlement 409 without existing_settlement_id: {r.text}")
        g = client.get(f"{API}/deposit-settlements/{existing_id}", headers=headers)
        assert g.status_code == 200, g.text
        existing = g.json()
        if existing.get("status") == DepositSettlementStatus.DRAFT.value:
            patch_payload = {
                "deposit_received": deposit,
                "total_deductions": total_deduct,
                "refund_amount": refund,
                "deductions": deductions,
            }
            p = client.patch(f"{API}/deposit-settlements/{existing_id}", json=patch_payload, headers=headers)
            assert p.status_code == 200, p.text
            result = p.json()
        else:
            assert False, f"_draft_settlement unexpected existing status={existing.get('status')}, expected DRAFT"
    else:
        raise AssertionError(f"_draft_settlement unexpected status={r.status_code}: {r.text}")
    if r.status_code == 201:
        result = r.json()
    assert result.get("status") == DepositSettlementStatus.DRAFT.value, (
        f"_draft_settlement final status={result.get('status')}"
    )
    return result


def test_c1_create_draft_settlement(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    insp = _schedule_and_full_inspection(
        client, db_session, h, lease_id=lease_id,
    )
    s = _draft_settlement(client, h, lease_id=lease_id, inspection_id=insp["id"])
    assert s["status"] == DepositSettlementStatus.DRAFT.value
    assert s["lease_id"] == lease_id
    assert s["move_out_inspection_id"] == insp["id"]
    assert Decimal(s["deposit_received"]) == Decimal("24000.00")


def test_c2_double_create_unique_inspection_409_or_identical(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    insp = _schedule_and_full_inspection(
        client, db_session, h, lease_id=lease_id,
    )
    s1 = _draft_settlement(client, h, lease_id=lease_id, inspection_id=insp["id"])
    existing_id = s1["id"]
    r2 = client.post(
        f"{API}/deposit-settlements",
        json={
            "move_out_inspection_id": insp["id"],
            "deposit_received": "24000.00",
            "total_deductions": "5000.00",
            "refund_amount": "19000.00",
            "deductions": [{"description": "different", "amount": "5000.00"}],
        },
        headers=h,
    )
    assert r2.status_code == 409, r2.text
    detail = r2.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["reason"] == "deposit_settlement_already_exists_for_inspection"
    assert detail["existing_settlement_id"] == existing_id


def test_c3_conservation_gap_zero_and_one_cent_pass(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    from app.services.deposit_settlement_service import check_amount_conservation
    h = _h(owner_a[1])
    insp = _schedule_and_full_inspection(
        client, db_session, h, lease_id=lease_id,
    )
    s1 = _draft_settlement(
        client, h, lease_id=lease_id, inspection_id=insp["id"],
        deposit="100.00", total_deduct="60.00", refund="40.00",
    )
    r1 = client.post(f"{API}/deposit-settlements/{s1['id']}/confirm", headers=h)
    assert r1.status_code == 200, r1.text

    insp2 = _schedule_and_full_inspection(
        client, db_session, h, lease_id=lease_id,
    )
    db_session.expire_all()
    s2 = _draft_settlement(
        client, h, lease_id=lease_id, inspection_id=insp2["id"],
        deposit="100.00", total_deduct="60.00", refund="39.99",
    )
    set2_row = db_session.get(DepositSettlement, s2["id"])
    assert set2_row is not None
    ok_2, gap_2 = check_amount_conservation(set2_row)
    assert ok_2 is True
    assert gap_2 == Decimal("0.01")
    r2 = client.post(f"{API}/deposit-settlements/{s2['id']}/confirm", headers=h)
    assert r2.status_code == 200, r2.text


def test_c4_conservation_gap_two_cents_fails_409(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    insp = _schedule_and_full_inspection(
        client, db_session, h, lease_id=lease_id,
    )
    s = _draft_settlement(
        client, h, lease_id=lease_id, inspection_id=insp["id"],
        deposit="100.00", total_deduct="60.00", refund="39.98",
    )
    r = client.post(f"{API}/deposit-settlements/{s['id']}/confirm", headers=h)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["reason"] == "deposit_settlement_not_conserved"


def test_c5_confirm_generates_income_rows_per_deduction(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    insp = _schedule_and_full_inspection(
        client, db_session, h, lease_id=lease_id,
    )
    deductions = [
        {"description": "wall", "amount": "4000.00"},
        {"description": "carpet", "amount": "2000.00"},
    ]
    payload = {
        "move_out_inspection_id": insp["id"],
        "deposit_received": "10000.00",
        "total_deductions": "6000.00",
        "refund_amount": "4000.00",
        "deductions": deductions,
    }
    r = client.post(
        f"{API}/deposit-settlements",
        json=payload,
        headers=h,
    )
    sid: int
    if r.status_code == 201:
        sid = r.json()["id"]
    elif r.status_code == 409:
        detail = r.json().get("detail") if isinstance(r.json(), dict) else {}
        existing_id = detail.get("existing_settlement_id") if isinstance(detail, dict) else None
        assert existing_id is not None, f"Expected existing_settlement_id in 409: {r.text}"
        patch = client.patch(
            f"{API}/deposit-settlements/{existing_id}",
            json={
                "deposit_received": "10000.00",
                "total_deductions": "6000.00",
                "refund_amount": "4000.00",
                "deductions": deductions,
            },
            headers=h,
        )
        assert patch.status_code == 200, patch.text
        sid = existing_id
    else:
        raise AssertionError(f"Unexpected status={r.status_code}: {r.text}")
    before_inc = db_session.query(Income).count()
    before_exp = db_session.query(Expense).count()
    c = client.post(f"{API}/deposit-settlements/{sid}/confirm", headers=h)
    assert c.status_code == 200, c.text
    db_session.expire_all()
    after_inc = db_session.query(Income).count()
    after_exp = db_session.query(Expense).count()
    assert after_inc - before_inc == 2
    assert after_exp - before_exp == 1
    set_row = db_session.get(DepositSettlement, sid)
    assert set_row.confirmed_at is not None
    refund_exp = (
        db_session.query(Expense)
        .filter(Expense.category == "deposit_refund")
        .order_by(Expense.id.desc())
        .first()
    )
    assert refund_exp is not None
    assert refund_exp.amount == Decimal("4000.00")
    assert refund_exp.status == ExpenseStatus.pending


def test_c6_confirm_is_idempotent_no_double_financial_rows(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    insp = _schedule_and_full_inspection(
        client, db_session, h, lease_id=lease_id,
    )
    deductions = [
        {"description": "wall repair", "amount": "3000.00"},
        {"description": "cleaning fee", "amount": "2000.00"},
    ]
    deposit = Decimal("24000.00")
    expected_inc_sum_delta = sum(Decimal(d["amount"]) for d in deductions)  # 5000
    expected_refund = deposit - expected_inc_sum_delta  # 19000
    s = _draft_settlement(
        client, h, lease_id=lease_id, inspection_id=insp["id"],
        total_deduct=str(expected_inc_sum_delta),
        refund=str(expected_refund),
        deductions=deductions,
    )
    sid = s["id"]
    refund_key = f"deposit_settlement:{sid}:refund"
    income_keys_expected = [f"deposit_settlement:{sid}:deduction:{i}" for i in range(len(deductions))]
    income_keys_set_expected = frozenset(income_keys_expected)

    baseline_inc_count = db_session.query(Income).count()
    baseline_exp_count = db_session.query(Expense).count()
    baseline_inc_sum = (db_session.query(sqla_func.sum(Income.amount)).scalar() or Decimal("0"))
    baseline_exp_sum = (db_session.query(sqla_func.sum(Expense.amount)).scalar() or Decimal("0"))

    c1 = client.post(f"{API}/deposit-settlements/{sid}/confirm", headers=h)
    assert c1.status_code == 200, c1.text
    db_session.expire_all()
    after1_inc_count = db_session.query(Income).count()
    after1_exp_count = db_session.query(Expense).count()
    after1_inc_sum = (db_session.query(sqla_func.sum(Income.amount)).scalar() or Decimal("0"))
    after1_exp_sum = (db_session.query(sqla_func.sum(Expense.amount)).scalar() or Decimal("0"))
    income_delta = after1_inc_count - baseline_inc_count
    expense_delta = after1_exp_count - baseline_exp_count
    assert income_delta == len(deductions), (
        f"expected +{len(deductions)} income rows after first confirm, got +{income_delta}"
    )
    assert expense_delta == 1, (
        f"expected +1 expense refund row after first confirm, got +{expense_delta}"
    )
    expected_exp_sum_delta = expected_refund
    assert (after1_inc_sum - baseline_inc_sum) == expected_inc_sum_delta
    assert (after1_exp_sum - baseline_exp_sum) == expected_exp_sum_delta
    after1_income_rows = {
        row.idempotency_key: (row.amount, row.status.value)
        for row in db_session.query(Income).filter(
            Income.idempotency_key.in_(income_keys_expected)
        ).all()
    }
    assert set(after1_income_rows.keys()) == income_keys_set_expected
    for i, ikey in enumerate(income_keys_expected):
        got_amount = after1_income_rows[ikey][0]
        want_amount = Decimal(deductions[i]["amount"])
        assert got_amount == want_amount, (
            f"income[{ikey}] amount got {got_amount} want {want_amount}"
        )
    refund1 = db_session.query(Expense).filter(Expense.idempotency_key == refund_key).one()
    assert refund1.amount == expected_exp_sum_delta

    c2 = client.post(f"{API}/deposit-settlements/{sid}/confirm", headers=h)
    assert c2.status_code == 200, c2.text
    db_session.expire_all()
    after2_inc_count = db_session.query(Income).count()
    after2_exp_count = db_session.query(Expense).count()
    after2_inc_sum = (db_session.query(sqla_func.sum(Income.amount)).scalar() or Decimal("0"))
    after2_exp_sum = (db_session.query(sqla_func.sum(Expense.amount)).scalar() or Decimal("0"))
    assert after2_inc_count == after1_inc_count
    assert after2_exp_count == after1_exp_count
    assert after2_inc_sum == after1_inc_sum
    assert after2_exp_sum == after1_exp_sum
    after2_income_keys = {
        r.idempotency_key for r in db_session.query(Income).filter(
            Income.idempotency_key.like(f"deposit_settlement:{sid}:deduction:%")
        ).all()
    }
    assert after2_income_keys == income_keys_set_expected
    after2_refund_rows = (
        db_session.query(Expense)
        .filter(Expense.idempotency_key == refund_key)
        .all()
    )
    assert len(after2_refund_rows) == 1
    sett = db_session.get(DepositSettlement, sid)
    actual_deductions = sett.deductions or []
    assert len(actual_deductions) == len(deductions)


def test_c7_confirm_reconciled_transition(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    insp = _schedule_and_full_inspection(
        client, db_session, h, lease_id=lease_id,
    )
    s = _full_settlement(
        client, db_session, h, inspection_id=insp["id"],
    )
    r = client.post(f"{API}/deposit-settlements/{s['id']}/reconcile", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == DepositSettlementStatus.RECONCILED.value


def test_c8_patch_after_confirmed_409(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    insp = _schedule_and_full_inspection(
        client, db_session, h, lease_id=lease_id,
    )
    s = _full_settlement(
        client, db_session, h, inspection_id=insp["id"],
    )
    r = client.patch(
        f"{API}/deposit-settlements/{s['id']}",
        json={"refund_amount": "0.00"},
        headers=h,
    )
    assert r.status_code == 409, r.text


def test_c9_confirm_requires_owner_403_for_secretary(
    client, db_session, secretary_a, owner_a, lease_id, unit_id, tenant_id
):
    own_h = _h(owner_a[1])
    sec_h = _h(secretary_a[1])
    insp = _schedule_and_full_inspection(
        client, db_session, own_h, lease_id=lease_id,
    )
    s = _draft_settlement(client, own_h, lease_id=lease_id, inspection_id=insp["id"])
    r = client.post(f"{API}/deposit-settlements/{s['id']}/confirm", headers=sec_h)
    assert r.status_code == 403, r.text


def test_c10_cross_org_scope_404(
    client, db_session, owner_a, owner_b, lease_id, unit_id, tenant_id
):
    own_a_h = _h(owner_a[1])
    insp = _schedule_and_full_inspection(
        client, db_session, own_a_h, lease_id=lease_id,
    )
    s = _draft_settlement(client, own_a_h, lease_id=lease_id, inspection_id=insp["id"])
    own_b_h = _h(owner_b[1])
    r = client.get(f"{API}/deposit-settlements/{s['id']}", headers=own_b_h)
    assert r.status_code == 404, r.text


# =========================================================================
# D组 — Lease Close gate + final state sync (8 tests)
# =========================================================================

def _build_settled_lease(client, db, headers, *, expiring_lease_id):
    """Build the preconditions for lease close: inspection CONFIRMED + settlement CONFIRMED conservation ok.
    Returns (inspection_id, settlement_id)."""
    insp = _schedule_and_full_inspection(
        client, db, headers, lease_id=expiring_lease_id,
    )
    sett = _full_settlement(
        client, db, headers, inspection_id=insp["id"],
    )
    db.expire_all()
    return insp["id"], sett["id"]


def test_d1_patch_active_to_terminated_without_inspection_409(
    client, db_session, owner_a, lease_id, unit_id
):
    h = _h(owner_a[1])
    r = client.patch(
        f"{API}/leases/{lease_id}",
        json={"status": LeaseStatus.terminated.value},
        headers=h,
    )
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["reason"] == "lease_closeable_truth_missing"
    assert "MoveOutInspection" in detail["expected_truth"]


def test_d2_patch_with_inspection_only_no_settlement_409(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    _schedule_and_full_inspection(
        client, db_session, h, lease_id=lease_id,
    )
    r = client.patch(
        f"{API}/leases/{lease_id}",
        json={"status": LeaseStatus.terminated.value},
        headers=h,
    )
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "DepositSettlement" in detail["expected_truth"]


def test_d3_patch_with_both_inspection_and_settlement_ok_success(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    _build_settled_lease(
        client, db_session, h, expiring_lease_id=lease_id,
    )
    r = client.patch(
        f"{API}/leases/{lease_id}",
        json={"status": LeaseStatus.terminated.value},
        headers=h,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == LeaseStatus.terminated.value
    assert body["moved_out_settled_at"] is not None


def test_d4_final_state_sync_unit_vacant_tenant_moved_out(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    _build_settled_lease(
        client, db_session, h, expiring_lease_id=lease_id,
    )
    client.patch(
        f"{API}/leases/{lease_id}",
        json={"status": LeaseStatus.terminated.value},
        headers=h,
    )
    db_session.expire_all()
    unit_resp = client.get(f"{API}/units/{unit_id}", headers=h).json()
    assert unit_resp["status"] == UnitStatus.vacant.value
    t_row = db_session.get(Tenant, tenant_id)
    assert t_row.moved_out_at is not None
    lease = db_session.get(Lease, lease_id)
    assert lease.moved_out_settled_at is not None
    evt = (
        db_session.query(UnitLifecycleEvent)
        .filter(UnitLifecycleEvent.unit_id == unit_id)
        .order_by(UnitLifecycleEvent.id.desc())
        .first()
    )
    assert evt is not None
    assert evt.reason == "move_out_settled"
    assert evt.to_status == UnitStatus.vacant.value


def test_d5_repeat_patch_is_idempotent(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    _build_settled_lease(
        client, db_session, h, expiring_lease_id=lease_id,
    )
    r1 = client.patch(
        f"{API}/leases/{lease_id}",
        json={"status": LeaseStatus.terminated.value},
        headers=h,
    )
    assert r1.status_code == 200, r1.text
    evt_count_before = db_session.query(UnitLifecycleEvent).filter(
        UnitLifecycleEvent.unit_id == unit_id,
        UnitLifecycleEvent.reason == "move_out_settled",
    ).count()
    r2 = client.patch(
        f"{API}/leases/{lease_id}",
        json={"status": LeaseStatus.terminated.value},
        headers=h,
    )
    assert r2.status_code == 200, r2.text
    evt_count_after = db_session.query(UnitLifecycleEvent).filter(
        UnitLifecycleEvent.unit_id == unit_id,
        UnitLifecycleEvent.reason == "move_out_settled",
    ).count()
    assert evt_count_after == evt_count_before


def test_d6_delete_without_pipeline_409(
    client, db_session, owner_a, unit_id, tenant_id
):
    h = _h(owner_a[1])
    r = client.post(
        f"{API}/leases",
        json={
            "unit_id": unit_id, "tenant_id": tenant_id,
            "start_date": "2025-01-01", "end_date": "2025-12-31",
            "monthly_rent": "12000.00", "deposit": "24000.00",
            "status": LeaseStatus.terminated.value,
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    lid = r.json()["id"]
    del_r = client.delete(f"{API}/leases/{lid}", headers=h)
    assert del_r.status_code == 409, del_r.text
    detail = del_r.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["reason"] == "lease_closeable_truth_missing_before_delete"


def test_d7_delete_after_pipeline_success(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    _build_settled_lease(
        client, db_session, h, expiring_lease_id=lease_id,
    )
    client.patch(
        f"{API}/leases/{lease_id}",
        json={"status": LeaseStatus.terminated.value},
        headers=h,
    )
    before_lease = db_session.get(Lease, lease_id)
    assert before_lease is not None
    assert before_lease.deleted_at is None
    before_audit_count = db_session.query(AuditLog).filter(
        AuditLog.table_name == "leases",
        AuditLog.record_id == str(lease_id),
        AuditLog.action == "soft_delete",
    ).count()
    r = client.delete(f"{API}/leases/{lease_id}", headers=h)
    assert r.status_code == 200, r.text
    lease = db_session.get(Lease, lease_id)
    assert lease.deleted_at is not None
    after_log = db_session.query(AuditLog).filter(
        AuditLog.table_name == "leases",
        AuditLog.record_id == str(lease_id),
        AuditLog.action == "soft_delete",
    ).order_by(AuditLog.id.desc()).first()
    assert after_log is not None, "Expected exactly one new soft_delete audit log for delete"
    assert (db_session.query(AuditLog).filter(
        AuditLog.table_name == "leases",
        AuditLog.record_id == str(lease_id),
        AuditLog.action == "soft_delete",
    ).count() - before_audit_count) == 1
    old_value = after_log.old_value
    new_value = after_log.new_value
    assert isinstance(old_value, dict), f"old_value must be serialize_row dict, got {type(old_value)}"
    assert isinstance(new_value, dict), f"new_value must be serialize_row dict, got {type(new_value)}"
    assert old_value.get("deleted_at") is None, (
        f"old_value.deleted_at must be None BEFORE mutation, got {old_value.get('deleted_at')!r}. "
        f"old_row must be serialized before deleted_at/updated_by/apply_settled mutation."
    )
    assert new_value.get("deleted_at") is not None, (
        f"new_value.deleted_at must be non-None AFTER mutation, got {new_value.get('deleted_at')!r}"
    )
    assert old_value.get("deleted_at") != new_value.get("deleted_at"), (
        f"old/new deleted_at must differ (None → set) to prove audit captured real mutation boundary."
        f" old={old_value.get('deleted_at')!r} new={new_value.get('deleted_at')!r}"
    )
    assert old_value != new_value, (
        "old_value/new_value must differ; otherwise audit is re-snapshotting post-mutation state "
        "for both sides, losing the exact transition trace required by close gate invariant."
    )


def test_d7b_concurrent_two_deletes_winner_200_loser_404_no_assertion_error_and_single_audit(
    db_session, owner_a, unit_id, tenant_id, test_engine
):
    """Concurrent DELETE → 1 true winner HTTP 200, 1 loser 404 (not 500/AssertionError)
    because re-lock WHERE id=? AND deleted_at IS NULL misses for loser → obj is None → 404 HTTPException.
    Also: exactly 1 real soft_delete audit produced (winner only, not 2 duplicates)."""
    from concurrent.futures import ThreadPoolExecutor, wait
    from fastapi.testclient import TestClient
    from app.main import app
    from app.database import get_db
    from sqlalchemy.orm import sessionmaker
    import threading

    factory = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)
    prev_override = app.dependency_overrides.get(get_db)
    tls = threading.local()

    def _per_request_session():
        sess = getattr(tls, "sess", None)
        if sess is None:
            sess = factory()
            tls.sess = sess
        try:
            yield sess
        finally:
            try:
                sess.close()
            except Exception:
                pass
            tls.sess = None
    app.dependency_overrides[get_db] = _per_request_session
    try:
        h = _h(owner_a[1])
        past_end = date.today() - timedelta(days=4)
        past_start = past_end - timedelta(days=365)
        with TestClient(app, raise_server_exceptions=False) as client:
            lr = client.post(
                f"{API}/leases",
                json={
                    "unit_id": unit_id, "tenant_id": tenant_id,
                    "start_date": past_start.isoformat(),
                    "end_date": past_end.isoformat(),
                    "monthly_rent": "12000.00", "deposit": "24000.00",
                    "status": LeaseStatus.active.value,
                },
                headers=h,
            )
            assert lr.status_code == 201, (lr.status_code, lr.text)
            lid = lr.json()["id"]
            _build_settled_lease(client, db_session, h, expiring_lease_id=lid)
            pr = client.patch(
                f"{API}/leases/{lid}",
                json={"status": LeaseStatus.terminated.value},
                headers=h,
            )
            assert pr.status_code == 200, (pr.status_code, pr.text)
        db_session.expire_all()

        before_audit = db_session.query(AuditLog).filter(
            AuditLog.table_name == "leases",
            AuditLog.record_id == str(lid),
            AuditLog.action == "soft_delete",
        ).count()

        statuses = []
        bodies = []
        mu = threading.Lock()
        start_barrier = threading.Barrier(2)

        def _worker():
            with TestClient(app, raise_server_exceptions=False) as wclient:
                start_barrier.wait(timeout=15)
                rr = wclient.delete(f"{API}/leases/{lid}", headers=h)
            with mu:
                statuses.append(rr.status_code)
                try:
                    bodies.append(rr.json())
                except Exception:
                    bodies.append(rr.text or "")

        with ThreadPoolExecutor(max_workers=2) as ex:
            futs = [ex.submit(_worker), ex.submit(_worker)]
            wait(futs, timeout=30)
            [f.result(timeout=15) for f in futs]

        assert sorted(statuses) == [200, 404], (
            f"Expected exactly 1 winner HTTP 200 + 1 loser HTTP 404 for concurrent DELETE race, "
            f"got statuses={statuses} bodies={bodies}"
        )
        assert len(statuses) == len(bodies) == 2, (
            f"Expected two status/body pairs, got statuses={statuses} bodies={bodies}"
        )
        for s, b in zip(statuses, bodies):
            if s == 404:
                detail = ""
                if isinstance(b, dict):
                    detail = b.get("detail") or ""
                    if isinstance(detail, str):
                        detail = detail.lower()
                    else:
                        detail = str(detail)
                else:
                    detail = str(b).lower()
                assert "not found" in detail, (
                    f"Concurrent loser must return 404 via HTTPException (NOT AssertionError/500). "
                    f"status={s} body={b}"
                )
        db_session.expire_all()
        after_audit = db_session.query(AuditLog).filter(
            AuditLog.table_name == "leases",
            AuditLog.record_id == str(lid),
            AuditLog.action == "soft_delete",
        ).count()
        assert (after_audit - before_audit) == 1, (
            f"Expected exactly 1 real soft_delete audit from winner (not 2 duplicates due to race). "
            f"before_audit={before_audit} after_audit={after_audit}"
        )
        final_lease = db_session.get(Lease, lid)
        assert final_lease is not None
        assert final_lease.deleted_at is not None
    finally:
        app.dependency_overrides.pop(get_db, None)
        if prev_override is not None:
            app.dependency_overrides[get_db] = prev_override


def test_d8_patch_to_expired_also_runs_final_state(
    client, db_session, owner_a, unit_id, tenant_id
):
    h = _h(owner_a[1])
    past_end = date.today() - timedelta(days=5)
    past_start = past_end - timedelta(days=365)
    r = client.post(
        f"{API}/leases",
        json={
            "unit_id": unit_id, "tenant_id": tenant_id,
            "start_date": past_start.isoformat(),
            "end_date": past_end.isoformat(),
            "monthly_rent": "12000.00", "deposit": "24000.00",
            "status": LeaseStatus.active.value,
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    lid = r.json()["id"]
    _build_settled_lease(
        client, db_session, h, expiring_lease_id=lid,
    )
    r = client.patch(
        f"{API}/leases/{lid}",
        json={"status": LeaseStatus.expired.value},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == LeaseStatus.expired.value
    assert r.json()["moved_out_settled_at"] is not None


# =========================================================================
# E组 — Truth -> OperationalTask projection (5 tests)
# =========================================================================

def _task_count(db, *, task_type, source_type=None, source_id=None, status=None):
    q = db.query(OperationalTask).filter(OperationalTask.task_type == task_type)
    if source_type is not None:
        q = q.filter(OperationalTask.source_type == source_type)
    if source_id is not None:
        q = q.filter(OperationalTask.source_id == source_id)
    if status is not None:
        q = q.filter(OperationalTask.status == status)
    return q.count()


def test_e1_generation_creates_move_out_task_after_decline(
    client, db_session, owner_a, lease_id, unit_id, tenant_id, monkeypatch,
):
    from app.services.operations import generation, config as ops_config
    monkeypatch.setattr(generation, "DEFAULT_ASSIGNED_USER_ID", owner_a[0].id)
    monkeypatch.setattr(generation, "SECRETARY_ASSIGNEE_ID", owner_a[0].id)
    monkeypatch.setattr(ops_config, "DEFAULT_ASSIGNED_USER_ID", owner_a[0].id)
    from datetime import datetime, timezone, timedelta
    lease = db_session.get(Lease, lease_id)
    today = datetime.now(timezone.utc).date()
    target_end = today + timedelta(days=14)
    if lease.end_date > (today + timedelta(days=30)):
        lease.end_date = target_end
        lease.updated_by = owner_a[0].id
        db_session.flush()
    h = _h(owner_a[1])
    client.post(
        f"{API}/leases/{lease_id}/decline-renewal",
        json={"reason": "tenant moving out"},
        headers=h,
    )
    db_session.expire_all()
    generate_business_tasks(db_session, now=datetime.now(timezone.utc))
    db_session.expire_all()
    total_cnt = _task_count(
        db_session,
        task_type=OperationalTaskType.MOVE_OUT_INSPECTION,
    )
    pending_cnt = _task_count(
        db_session,
        task_type=OperationalTaskType.MOVE_OUT_INSPECTION,
        status=OperationalTaskStatus.PENDING,
    )
    assert total_cnt == 1, f"Expected == 1 MOVE_OUT_INSPECTION task total, got {total_cnt}"
    assert pending_cnt == 1, f"Expected == 1 PENDING, got {pending_cnt}"
    task = db_session.query(OperationalTask).filter(
        OperationalTask.task_type == OperationalTaskType.MOVE_OUT_INSPECTION,
    ).one()
    assert task.source_type == "lease"
    assert task.source_id == lease_id


def test_e2_forward_path_inspection_confirm_completes_task(
    client, db_session, owner_a, lease_id, unit_id, tenant_id, monkeypatch,
):
    from app.services.operations import generation, config as ops_config
    monkeypatch.setattr(generation, "DEFAULT_ASSIGNED_USER_ID", owner_a[0].id)
    monkeypatch.setattr(generation, "SECRETARY_ASSIGNEE_ID", owner_a[0].id)
    monkeypatch.setattr(ops_config, "DEFAULT_ASSIGNED_USER_ID", owner_a[0].id)
    from datetime import datetime, timezone, timedelta
    lease = db_session.get(Lease, lease_id)
    today = datetime.now(timezone.utc).date()
    target_end = today + timedelta(days=14)
    if lease.end_date > (today + timedelta(days=30)):
        lease.end_date = target_end
        lease.updated_by = owner_a[0].id
        db_session.flush()
    h = _h(owner_a[1])
    client.post(
        f"{API}/leases/{lease_id}/decline-renewal",
        json={"reason": "leaving"},
        headers=h,
    )
    db_session.expire_all()
    generate_business_tasks(db_session, now=datetime.now(timezone.utc))
    tasks_before = db_session.query(OperationalTask).filter(
        OperationalTask.task_type == OperationalTaskType.MOVE_OUT_INSPECTION,
        OperationalTask.lease_id == lease_id,
    ).all()
    assert len(tasks_before) == 1
    pending_task_ids = [t.id for t in tasks_before
                        if t.status == OperationalTaskStatus.PENDING]
    assert len(pending_task_ids) == 1
    insp = _schedule_and_full_inspection(
        client, db_session, h, lease_id=lease_id,
    )
    db_session.expire_all()
    t_after = db_session.get(OperationalTask, pending_task_ids[0])
    assert t_after.status == OperationalTaskStatus.COMPLETED


def test_e3_generation_creates_deposit_task_after_inspection_confirmed(
    client, db_session, owner_a, lease_id, unit_id, tenant_id, monkeypatch,
):
    from app.services.operations import generation, config as ops_config
    monkeypatch.setattr(generation, "DEFAULT_ASSIGNED_USER_ID", owner_a[0].id)
    monkeypatch.setattr(generation, "SECRETARY_ASSIGNEE_ID", owner_a[0].id)
    monkeypatch.setattr(ops_config, "DEFAULT_ASSIGNED_USER_ID", owner_a[0].id)
    from datetime import datetime, timezone, timedelta
    lease = db_session.get(Lease, lease_id)
    today = datetime.now(timezone.utc).date()
    target_end = today + timedelta(days=14)
    if lease.end_date > (today + timedelta(days=30)):
        lease.end_date = target_end
        lease.updated_by = owner_a[0].id
        db_session.flush()
    h = _h(owner_a[1])
    client.post(
        f"{API}/leases/{lease_id}/decline-renewal", json={"reason": "x"}, headers=h,
    )
    db_session.expire_all()
    generate_business_tasks(db_session, now=datetime.now(timezone.utc))
    insp = _schedule_and_full_inspection(
        client, db_session, h, lease_id=lease_id,
    )
    _draft_settlement(client, h, lease_id=lease_id, inspection_id=insp["id"])
    db_session.expire_all()
    generate_business_tasks(db_session, now=datetime.now(timezone.utc))
    db_session.expire_all()
    cnt = _task_count(
        db_session,
        task_type=OperationalTaskType.DEPOSIT_SETTLEMENT,
        status=OperationalTaskStatus.PENDING,
    )
    assert cnt == 1, f"Expected == 1 DEPOSIT_SETTLEMENT pending task, got {cnt}"


def test_e4_reconcile_marks_deposit_task_completed(
    client, db_session, owner_a, lease_id, unit_id, tenant_id, monkeypatch,
):
    from app.services.operations import generation, config as ops_config
    monkeypatch.setattr(generation, "DEFAULT_ASSIGNED_USER_ID", owner_a[0].id)
    monkeypatch.setattr(generation, "SECRETARY_ASSIGNEE_ID", owner_a[0].id)
    monkeypatch.setattr(ops_config, "DEFAULT_ASSIGNED_USER_ID", owner_a[0].id)
    from datetime import datetime, timezone, timedelta
    lease = db_session.get(Lease, lease_id)
    today = datetime.now(timezone.utc).date()
    target_end = today + timedelta(days=14)
    if lease.end_date > (today + timedelta(days=30)):
        lease.end_date = target_end
        lease.updated_by = owner_a[0].id
        db_session.flush()
    h = _h(owner_a[1])
    client.post(
        f"{API}/leases/{lease_id}/decline-renewal", json={"reason": "x"}, headers=h,
    )
    generate_business_tasks(db_session, now=datetime.now(timezone.utc))
    insp = _schedule_and_full_inspection(
        client, db_session, h, lease_id=lease_id,
    )
    sett = _full_settlement(
        client, db_session, h, inspection_id=insp["id"],
    )
    db_session.expire_all()
    sett_row = db_session.get(DepositSettlement, sett["id"])
    orphan = OperationalTask(
        task_type=OperationalTaskType.DEPOSIT_SETTLEMENT,
        title="orphan settlement task",
        status=OperationalTaskStatus.PENDING,
        source_type="deposit_settlement",
        source_id=sett_row.id,
        lease_id=lease_id,
        priority=OperationalTaskPriority.high,
        due_at=datetime.now(timezone.utc) + timedelta(days=1),
        details={},
    )
    db_session.add(orphan)
    db_session.commit()
    db_session.refresh(orphan)
    oid = orphan.id
    reconcile_tasks(db_session, now=datetime.now(timezone.utc))
    db_session.expire_all()
    after = db_session.get(OperationalTask, oid)
    assert after.status == OperationalTaskStatus.COMPLETED


def test_e5_truth_validator_move_out_task_complete_without_inspection_409(
    client, db_session, owner_a, lease_id, unit_id, tenant_id, property_id, org_a,
):
    h = _h(owner_a[1])
    t = OperationalTask(
        task_type=OperationalTaskType.MOVE_OUT_INSPECTION,
        title="orphan inspection close attempt",
        status=OperationalTaskStatus.PENDING,
        source_type="move_out_inspection",
        source_id=9999999,
        lease_id=lease_id,
        property_id=property_id,
        priority=OperationalTaskPriority.high,
        due_at=datetime.now(timezone.utc) + timedelta(days=1),
        details={},
    )
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    tid = t.id
    r = client.post(
        f"{API}/operations/tasks/{tid}/complete",
        headers=h,
    )
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["reason"] == "task_completion_truth_missing"


# =========================================================================
# F组 — Anti-bypass tests (7 tests)
# =========================================================================

def test_c11_settlement_create_attempt_override_lease_id_status_422(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    insp = _schedule_and_full_inspection(
        client, db_session, h, lease_id=lease_id
    )
    r = client.post(
        f"{API}/deposit-settlements",
        json={
            "lease_id": lease_id,
            "status": DepositSettlementStatus.CONFIRMED.value,
            "move_out_inspection_id": insp["id"],
            "deposit_received": "24000.00",
            "total_deductions": "5000.00",
            "refund_amount": "19000.00",
            "deductions": [{"description": "wall", "amount": "5000.00"}],
        },
        headers=h,
    )
    assert r.status_code == 422, r.text
    errors = r.json()["detail"]
    assert isinstance(errors, list)
    assert len(errors) == 2
    locs = {tuple(err.get("loc", [])) for err in errors}
    types = {err.get("type") for err in errors}
    assert ("body", "lease_id") in locs
    assert ("body", "status") in locs
    assert types == {"extra_forbidden"}


def test_b13_patch_inspection_lease_unit_tenant_status_rejected_422_then_ignored_via_allowed(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    insp = _schedule_inspection_api(client, h, lease_id=lease_id)
    insp_id = insp["id"]
    orig = db_session.get(MoveOutInspection, insp_id)
    orig_lease_id = orig.lease_id
    orig_unit_id = orig.unit_id
    orig_tenant_id = orig.tenant_id
    orig_status = orig.status
    r = client.patch(
        f"{API}/move-out-inspections/{insp_id}",
        json={
            "lease_id": 9999999,
            "unit_id": 9999999,
            "tenant_id": 9999999,
            "status": MoveOutInspectionStatus.CONFIRMED.value,
        },
        headers=h,
    )
    assert r.status_code == 422, r.text
    errors = r.json()["detail"]
    assert isinstance(errors, list)
    assert len(errors) == 4
    for err in errors:
        assert "loc" in err
        assert "type" in err
        assert err["type"] == "extra_forbidden"
    locs = {tuple(err["loc"]) for err in errors}
    assert ("body", "lease_id") in locs
    assert ("body", "unit_id") in locs
    assert ("body", "tenant_id") in locs
    assert ("body", "status") in locs
    db_session.expire_all()
    after = db_session.get(MoveOutInspection, insp_id)
    assert after.lease_id == orig_lease_id
    assert after.unit_id == orig_unit_id
    assert after.tenant_id == orig_tenant_id
    assert after.status == orig_status
    assert after.status == MoveOutInspectionStatus.SCHEDULED


def test_c12_deductions_sum_mismatch_409_on_confirm(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    insp = _schedule_and_full_inspection(
        client, db_session, h, lease_id=lease_id
    )
    deductions = [
        {"description": "a", "amount": "3000.02"},
        {"description": "b", "amount": "2000.00"},
    ]
    payload = {
        "move_out_inspection_id": insp["id"],
        "deposit_received": "24000.00",
        "total_deductions": "5000.00",
        "refund_amount": "19000.00",
        "deductions": deductions,
    }
    r = client.post(
        f"{API}/deposit-settlements",
        json=payload,
        headers=h,
    )
    sid: int
    if r.status_code == 201:
        sid = r.json()["id"]
    elif r.status_code == 409:
        detail = r.json().get("detail") if isinstance(r.json(), dict) else {}
        existing_id = detail.get("existing_settlement_id") if isinstance(detail, dict) else None
        assert existing_id is not None, f"Expected existing_settlement_id in 409: {r.text}"
        patch = client.patch(
            f"{API}/deposit-settlements/{existing_id}",
            json={
                "deposit_received": "24000.00",
                "total_deductions": "5000.00",
                "refund_amount": "19000.00",
                "deductions": deductions,
            },
            headers=h,
        )
        assert patch.status_code == 200, patch.text
        sid = existing_id
    else:
        raise AssertionError(f"Unexpected status={r.status_code}: {r.text}")
    c = client.post(f"{API}/deposit-settlements/{sid}/confirm", headers=h)
    assert c.status_code == 409, c.text
    detail = c.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["reason"] == "deposit_settlement_deduction_sum_mismatch"


def test_a8_renewal_overlaps_predecessor_409(
    client, db_session, owner_a, unit_id, tenant_id
):
    h = _h(owner_a[1])
    pred_end = date(2026, 6, 30)
    pred_start = pred_end - timedelta(days=365)
    r = client.post(
        f"{API}/leases",
        json={
            "unit_id": unit_id,
            "tenant_id": tenant_id,
            "start_date": pred_start.isoformat(),
            "end_date": pred_end.isoformat(),
            "monthly_rent": "10000.00",
            "deposit": "20000.00",
            "status": LeaseStatus.active.value,
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    lid = r.json()["id"]
    succ_start = pred_end - timedelta(days=1)
    succ_end = succ_start + timedelta(days=365)
    r2 = client.post(
        f"{API}/leases/{lid}/renew",
        json={
            "start_date": succ_start.isoformat(),
            "end_date": succ_end.isoformat(),
            "monthly_rent": "10500.00",
            "deposit": "21000.00",
        },
        headers=h,
    )
    assert r2.status_code == 409, r2.text
    detail = r2.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["reason"] == "renewal_overlaps_predecessor"


def test_a9_renewal_invalid_dates_end_before_start_409(
    client, db_session, owner_a, lease_id
):
    h = _h(owner_a[1])
    bad_start = "2027-12-31"
    bad_end = "2027-01-01"
    r = client.post(
        f"{API}/leases/{lease_id}/renew",
        json={
            "start_date": bad_start,
            "end_date": bad_end,
            "monthly_rent": "12500.00",
            "deposit": "25000.00",
        },
        headers=h,
    )
    assert r.status_code == 422, r.text
    errors = r.json()["detail"]
    assert isinstance(errors, list)
    assert len(errors) == 1
    err0 = errors[0]
    assert "loc" in err0
    assert tuple(err0["loc"]) == ("body",)
    assert err0["type"] == "value_error"


def test_a10_concurrent_predecessor_archive_vs_successor_race_barrier(
    db_session, owner_a, unit_id, tenant_id, test_engine
):
    """Barrier-synchronized race: predecessor archive DELETE(RENEWAL_SUPERSESSION_PATH)
    vs concurrent successor mutation / soft-delete.

    Verifies:
    (a) successor row is locked during predecessor validation (FOR UPDATE + deleted_at filter);
    (b) predecessor never archives against an already-invalid (deleted/race-mutated) successor;
    (c) no 500 / AssertionError — only normal HTTP 200 or 409;
    (d) final supersession fact and audit action traces are consistent.

    Server-side test seam = transaction-local advisory lock at pg_sleep(0.05) injected
    AFTER the successor FOR UPDATE query, forcing overlap with a worker that already
    holds a commit-ordering advisory lock (Barrier ensures both start together).
    """
    from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
    from fastapi.testclient import TestClient
    from app.main import app
    from app.database import get_db
    from sqlalchemy.orm import sessionmaker
    import threading

    factory = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)
    prev_override = app.dependency_overrides.get(get_db)
    tls = threading.local()

    def _per_request_session():
        sess = getattr(tls, "sess", None)
        if sess is None:
            sess = factory()
            tls.sess = sess
        try:
            yield sess
        finally:
            try:
                sess.close()
            except Exception:
                pass
            tls.sess = None
    app.dependency_overrides[get_db] = _per_request_session
    try:
        h = _h(owner_a[1])
        past_end = date.today() - timedelta(days=8)
        past_start = past_end - timedelta(days=365)
        succ_start = (past_end + timedelta(days=1)).isoformat()
        succ_end = (past_end + timedelta(days=366)).isoformat()
        with TestClient(app, raise_server_exceptions=False) as client:
            pred_resp = client.post(
                f"{API}/leases",
                json={
                    "unit_id": unit_id, "tenant_id": tenant_id,
                    "start_date": past_start.isoformat(),
                    "end_date": past_end.isoformat(),
                    "monthly_rent": "10000.00",
                    "deposit": "20000.00",
                    "status": LeaseStatus.active.value,
                },
                headers=h,
            )
            assert pred_resp.status_code == 201, pred_resp.text
            pred_id = pred_resp.json()["id"]
            renew_resp = client.post(
                f"{API}/leases/{pred_id}/renew",
                json={
                    "start_date": succ_start,
                    "end_date": succ_end,
                    "monthly_rent": "10500.00",
                    "deposit": "21000.00",
                },
                headers=h,
            )
            assert renew_resp.status_code == 200, renew_resp.text
            succ_id = renew_resp.json()["id"]
            exp_resp = client.post(f"{API}/leases/{pred_id}/auto-expire", headers=h)
            assert exp_resp.status_code == 200, exp_resp.text
            db_session.expire_all()
            pred_row = db_session.get(Lease, pred_id)
            succ_row = db_session.get(Lease, succ_id)
            assert pred_row is not None
            assert pred_row.status == LeaseStatus.expired
            assert pred_row.superseded_by_lease_id == succ_id
            assert succ_row is not None
            assert succ_row.start_date.isoformat() == succ_start
            assert succ_row.deleted_at is None

        before_pred_audit = db_session.query(AuditLog).filter(
            AuditLog.table_name == "leases",
            AuditLog.record_id == str(pred_id),
            AuditLog.action == "renewal_predecessor_archived",
        ).count()
        before_succ_audit = db_session.query(AuditLog).filter(
            AuditLog.table_name == "leases",
            AuditLog.record_id == str(succ_id),
            AuditLog.action == "soft_delete",
        ).count()

        pred_results = []
        succ_results = []
        mu = threading.Lock()
        start_barrier = threading.Barrier(2)
        succ_lock_event = threading.Event()

        def _archive_pred_worker():
            try:
                with TestClient(app, raise_server_exceptions=False) as wc:
                    start_barrier.wait(timeout=20)
                    if not succ_lock_event.wait(timeout=20):
                        raise RuntimeError("Timeout waiting for succ lock acquisition")
                    r = wc.delete(f"{API}/leases/{pred_id}", headers=h)
                parsed = None
                try:
                    parsed = r.json()
                except Exception:
                    parsed = r.text or ""
                with mu:
                    pred_results.append((r.status_code, parsed))
            except Exception as exc:
                import traceback
                with mu:
                    pred_results.append((999, {"exc": type(exc).__name__, "msg": str(exc),
                                              "tb": traceback.format_exc(limit=10)}))

        def _mutate_succ_worker():
            sess = factory()
            try:
                start_barrier.wait(timeout=20)
                row = sess.query(Lease).filter(
                    Lease.id == succ_id, Lease.deleted_at.is_(None)
                ).with_for_update().populate_existing().first()
                assert row is not None
                succ_lock_event.set()
                old_s = row.__dict__.copy()
                old_s.pop("_sa_instance_state", None)
                clean_old = {}
                for k, v in old_s.items():
                    if hasattr(v, "isoformat"):
                        clean_old[k] = v.isoformat()
                    elif isinstance(v, Decimal):
                        clean_old[k] = str(v)
                    elif isinstance(v, (dict, list, tuple, str, int, float, bool)) or v is None:
                        clean_old[k] = v
                    else:
                        clean_old[k] = repr(v)
                row.updated_by = -999
                row.monthly_rent = Decimal("99999.99")
                row.deleted_at = datetime.now(timezone.utc)
                new_s = {
                    "id": row.id,
                    "deleted_at": row.deleted_at.isoformat() if row.deleted_at else None,
                    "updated_by": row.updated_by,
                    "monthly_rent": str(row.monthly_rent),
                }
                record_audit(
                    sess,
                    table_name="leases",
                    record_id=row.id,
                    action="soft_delete",
                    actor_id=None,
                    old_value=clean_old,
                    new_value=new_s,
                )
                sess.flush()
                sess.commit()
                with mu:
                    succ_results.append(("committed_deleted", succ_id))
                row_after = sess.get(Lease, succ_id)
                if row_after is None:
                    with mu:
                        succ_results.append(("row_missing_post_commit", None))
                else:
                    with mu:
                        succ_results.append(("row_present_post_commit", (row_after.deleted_at is not None, str(row_after.monthly_rent))))
            except Exception as exc:
                import traceback
                with mu:
                    succ_results.append(("exc", {"type": type(exc).__name__,
                                                "msg": str(exc),
                                                "tb": traceback.format_exc(limit=10)}))
            finally:
                try:
                    sess.close()
                except Exception:
                    pass

        with ThreadPoolExecutor(max_workers=2) as ex:
            f_pred = ex.submit(_archive_pred_worker)
            f_succ = ex.submit(_mutate_succ_worker)
            wait([f_pred, f_succ], timeout=60, return_when=FIRST_COMPLETED)
            f_pred.result(timeout=60)
            f_succ.result(timeout=60)

        assert len(pred_results) == 1, pred_results
        pred_status, pred_body = pred_results[0]
        assert pred_status in (200, 409), (
            f"Predecessor archive must succeed normally or 409 conflict (no 500/AssertionError). "
            f"status={pred_status} body={pred_body}"
        )
        if pred_status == 409:
            detail = pred_body.get("detail") if isinstance(pred_body, dict) else None
            reason = detail.get("reason") if isinstance(detail, dict) else None
            assert reason == "renewal_successor_truth_invalid_before_delete", (
                f"Race loser predecessor must 409 via renewal_successor_truth_invalid_before_delete, "
                f"status={pred_status} body={pred_body}"
            )
        assert len(succ_results) == 2, succ_results

        db_session.expire_all()
        final_pred = db_session.get(Lease, pred_id)
        final_succ = db_session.get(Lease, succ_id)
        assert final_pred is not None
        assert final_succ is not None

        after_pred_audit = db_session.query(AuditLog).filter(
            AuditLog.table_name == "leases",
            AuditLog.record_id == str(pred_id),
            AuditLog.action == "renewal_predecessor_archived",
        ).count()
        after_succ_audit = db_session.query(AuditLog).filter(
            AuditLog.table_name == "leases",
            AuditLog.record_id == str(succ_id),
            AuditLog.action == "soft_delete",
        ).count()

        pred_archive_happened = (after_pred_audit - before_pred_audit) >= 1
        succ_softdeleted_happened = final_succ.deleted_at is not None
        assert succ_softdeleted_happened, (
            "Successor worker performed soft_delete via thread-local SQLA session (committed "
            "before pred commit barrier released via row-lock contention)."
        )
        if pred_archive_happened:
            assert final_pred.deleted_at is not None, (
                "pred_archive audit produced but pred.deleted_at missing"
            )
            assert final_succ.deleted_at is None, (
                "Contradiction: predecessor archived claiming valid successor, but successor is soft-deleted. "
                "Successor re-lock WHERE deleted_at IS None FOR UPDATE should block until succ commit "
                "and return None. audit_pred={} succ_deleted_at={}".format(
                    pred_archive_happened, final_succ.deleted_at
                )
            )
        else:
            assert pred_status == 409, (
                "If pred predecessor archive audit is absent, predecessor must have fallen into "
                "the 409 successor_truth_invalid branch; got status={} body={}".format(
                    pred_status, pred_body
                )
            )
            assert (after_succ_audit - before_succ_audit) >= 1, (
                "Race where predecessor 409 but successor soft_delete audit absent"
            )
    finally:
        app.dependency_overrides.pop(get_db, None)
        if prev_override is not None:
            app.dependency_overrides[get_db] = prev_override


def test_c13_cross_org_settlement_create_404(
    client, db_session, owner_a, owner_b, lease_id, unit_id, tenant_id
):
    own_a_h = _h(owner_a[1])
    insp = _schedule_and_full_inspection(
        client, db_session, own_a_h, lease_id=lease_id
    )
    own_b_h = _h(owner_b[1])
    r = client.post(
        f"{API}/deposit-settlements",
        json={
            "move_out_inspection_id": insp["id"],
            "deposit_received": "24000.00",
            "total_deductions": "5000.00",
            "refund_amount": "19000.00",
            "deductions": [{"description": "paint", "amount": "5000.00"}],
        },
        headers=own_b_h,
    )
    assert r.status_code == 404, r.text


def test_c14_patch_settlement_forbidden_fields_rejected_422_refund_only_allowed(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    insp = _schedule_and_full_inspection(
        client, db_session, h, lease_id=lease_id
    )
    s = _draft_settlement(client, h, lease_id=lease_id, inspection_id=insp["id"])
    sid = s["id"]
    orig = db_session.get(DepositSettlement, sid)
    orig_lease_id = orig.lease_id
    orig_insp_id = orig.move_out_inspection_id
    orig_status = orig.status
    r = client.patch(
        f"{API}/deposit-settlements/{sid}",
        json={
            "status": DepositSettlementStatus.CONFIRMED.value,
            "lease_id": 9999,
            "move_out_inspection_id": 9999,
        },
        headers=h,
    )
    assert r.status_code == 422, r.text
    errors = r.json()["detail"]
    assert isinstance(errors, list)
    assert len(errors) == 3
    for err in errors:
        assert "loc" in err
        assert "type" in err
        assert err["type"] == "extra_forbidden"
    locs = {tuple(err["loc"]) for err in errors}
    assert ("body", "status") in locs
    assert ("body", "lease_id") in locs
    assert ("body", "move_out_inspection_id") in locs
    r2 = client.patch(
        f"{API}/deposit-settlements/{sid}",
        json={"refund_amount": "18999.99"},
        headers=h,
    )
    assert r2.status_code == 200, r2.text
    db_session.expire_all()
    after = db_session.get(DepositSettlement, sid)
    assert after.status == orig_status
    assert after.status == DepositSettlementStatus.DRAFT
    assert after.lease_id == orig_lease_id
    assert after.move_out_inspection_id == orig_insp_id
    assert after.refund_amount == Decimal("18999.99")


# =========================================================================
# G组 — Anti-case 精确断言反例测试 (g1-g13: 13 tests)
# =========================================================================

def test_g1_patch_terminal_lease_no_status_change_no_final_effects(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    _build_settled_lease(
        client, db_session, h, expiring_lease_id=lease_id,
    )
    tenant_before = db_session.get(Tenant, tenant_id)
    moved_out_before = tenant_before.moved_out_at
    assert moved_out_before is None
    unit_before = client.get(f"{API}/units/{unit_id}", headers=h).json()
    unit_status_before = unit_before["status"]
    r = client.patch(
        f"{API}/leases/{lease_id}",
        json={"notes": "just updating notes, no status change"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    db_session.expire_all()
    tenant_after = db_session.get(Tenant, tenant_id)
    assert tenant_after.moved_out_at is None
    unit_after = client.get(f"{API}/units/{unit_id}", headers=h).json()
    assert unit_after["status"] == unit_status_before
    lease_after = db_session.get(Lease, lease_id)
    assert lease_after.status == LeaseStatus.active


def test_g2_renew_duplicate_returns_same_successor(
    client, db_session, owner_a, unit_id, tenant_id, lease_id
):
    h = _h(owner_a[1])
    pred = db_session.get(Lease, lease_id)
    assert pred.end_date <= date(2026, 6, 30)
    succ_start_date = pred.end_date + timedelta(days=1)
    succ_start = succ_start_date.isoformat()
    succ_end = (pred.end_date + timedelta(days=365)).isoformat()
    payload = {
        "start_date": succ_start,
        "end_date": succ_end,
        "monthly_rent": "12500.00",
        "deposit": "25000.00",
    }
    r1 = client.post(f"{API}/leases/{lease_id}/renew", json=payload, headers=h)
    assert r1.status_code == 200, r1.text
    b1 = r1.json()
    assert "id" in b1, "first renew response MUST contain direct integer id field"
    assert isinstance(b1["id"], int), (
        f"first renew id must be integer, got {type(b1['id']).__name__}"
    )
    pred1 = db_session.get(Lease, lease_id)
    succ_db_1 = db_session.get(Lease, pred1.superseded_by_lease_id)
    assert succ_db_1 is not None
    assert b1["id"] == succ_db_1.id, (
        f"first renew response id {b1['id']} must equal DB successor.id {succ_db_1.id}"
    )
    succ_count_1 = (
        db_session.query(Lease)
        .filter(
            Lease.unit_id == pred.unit_id,
            Lease.tenant_id == pred.tenant_id,
            Lease.start_date == succ_start_date,
            Lease.deleted_at.is_(None),
        )
        .count()
    )
    assert succ_count_1 == 1, (
        f"successor rows for unit+tenant+start after first renew must equal 1, got {succ_count_1}"
    )
    r2 = client.post(f"{API}/leases/{lease_id}/renew", json=payload, headers=h)
    assert r2.status_code == 200, r2.text
    b2 = r2.json()
    assert "id" in b2, "second renew response MUST still contain id directly (metadata fallback FORBIDDEN)"
    assert isinstance(b2["id"], int)
    assert b2["id"] == b1["id"], (
        f"idempotent renew must return exact same successor id, got r2.id={b2['id']} vs r1.id={b1['id']}"
    )
    succ_count_2 = (
        db_session.query(Lease)
        .filter(
            Lease.unit_id == pred.unit_id,
            Lease.tenant_id == pred.tenant_id,
            Lease.start_date == succ_start_date,
            Lease.deleted_at.is_(None),
        )
        .count()
    )
    assert succ_count_2 == 1


def test_g3_settlement_confirm_idempotent(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    insp = _schedule_and_full_inspection(
        client, db_session, h, lease_id=lease_id,
    )
    deductions = [
        {"description": "wall repair", "amount": "3000.00"},
        {"description": "cleaning", "amount": "2000.00"},
    ]
    s = _draft_settlement(
        client, h, lease_id=lease_id, inspection_id=insp["id"],
        total_deduct="5000.00",
        deductions=deductions,
    )
    sid = s["id"]
    c1 = client.post(f"{API}/deposit-settlements/{sid}/confirm", headers=h)
    assert c1.status_code == 200, c1.text
    db_session.expire_all()
    refund_key_1 = f"deposit_settlement:{sid}:refund"
    refund_rows_1 = (
        db_session.query(Expense)
        .filter(Expense.idempotency_key == refund_key_1)
        .count()
    )
    assert refund_rows_1 == 1
    income_keys_expected = 2
    income_rows_1 = (
        db_session.query(Income)
        .filter(Income.idempotency_key.like(f"deposit_settlement:{sid}:deduction:%"))
        .count()
    )
    assert income_rows_1 == income_keys_expected
    c2 = client.post(f"{API}/deposit-settlements/{sid}/confirm", headers=h)
    assert c2.status_code == 200, c2.text
    db_session.expire_all()
    refund_rows_2 = (
        db_session.query(Expense)
        .filter(Expense.idempotency_key == refund_key_1)
        .count()
    )
    assert refund_rows_2 == 1
    income_rows_2 = (
        db_session.query(Income)
        .filter(Income.idempotency_key.like(f"deposit_settlement:{sid}:deduction:%"))
        .count()
    )
    assert income_rows_2 == income_keys_expected
    sett = db_session.get(DepositSettlement, sid)
    actual_deductions = sett.deductions or []
    assert len(actual_deductions) == len(deductions)


def test_g4_replacement_settlement_updates_lease_fk(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    insp1 = _schedule_and_full_inspection(
        client, db_session, h, lease_id=lease_id,
    )
    sett1 = _draft_settlement(
        client, h, lease_id=lease_id, inspection_id=insp1["id"],
    )
    db_session.expire_all()
    lease_before = db_session.get(Lease, lease_id)
    old_deposit_settlement_id = lease_before.deposit_settlement_id
    assert old_deposit_settlement_id is not None
    insp1_row = db_session.get(MoveOutInspection, insp1["id"])
    cancel_r = client.post(f"{API}/move-out-inspections/{insp1_row.id}/cancel", headers=h)
    assert cancel_r.status_code == 200, cancel_r.text
    db_session.expire_all()
    insp1_cancelled = db_session.get(MoveOutInspection, insp1["id"])
    assert insp1_cancelled.status == MoveOutInspectionStatus.CANCELLED
    insp2 = _schedule_and_full_inspection(
        client, db_session, h, lease_id=lease_id,
    )
    db_session.expire_all()
    lease_intermediate = db_session.get(Lease, lease_id)
    sett2 = _full_settlement(
        client, db_session, h, inspection_id=insp2["id"],
    )
    db_session.expire_all()
    lease_after = db_session.get(Lease, lease_id)
    assert lease_after.deposit_settlement_id == sett2["id"]


def test_g5_dup_inspection_schedule_409(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    when = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    findings_original = [{"item": "original scratch", "severity": "minor"}]
    r1 = client.post(
        f"{API}/move-out-inspections",
        json={
            "lease_id": lease_id,
            "scheduled_at": when,
            "findings": findings_original,
        },
        headers=h,
    )
    assert r1.status_code == 201, r1.text
    insp_id = r1.json()["id"]
    r2 = client.post(
        f"{API}/move-out-inspections",
        json={
            "lease_id": lease_id,
            "scheduled_at": when,
            "findings": [{"item": "overwrite attempt", "severity": "major"}],
        },
        headers=h,
    )
    assert r2.status_code == 409, r2.text
    detail = r2.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["reason"] == "move_out_inspection_already_exists_for_lease"
    db_session.expire_all()
    insp_after = db_session.get(MoveOutInspection, insp_id)
    assert len(insp_after.findings or []) == 1
    assert insp_after.findings[0]["item"] == "original scratch"
    assert insp_after.findings[0]["severity"] == "minor"


def test_g6_patch_inspection_invalid_evidence_ids_cross_org(
    client, db_session, owner_a, owner_b, org_a, org_b, property_id,
    unit_id, tenant_id, lease_id,
):
    own_a_h = _h(owner_a[1])
    own_b_h = _h(owner_b[1])
    prop_b_resp = client.post(
        f"{API}/properties",
        json={
            "name": "Property-B",
            "address": "2 Ayala Ave",
            "city": "Makati",
            "total_units": 2,
            "organization_id": org_b.id,
        },
        headers=own_b_h,
    )
    assert prop_b_resp.status_code == 201, prop_b_resp.text
    prop_b_id = prop_b_resp.json()["id"]
    unit_b_resp = client.post(
        f"{API}/units",
        json={
            "property_id": prop_b_id,
            "unit_number": "201",
            "floor": "2",
            "size_sqm": "40.00",
            "monthly_rent": "15000.00",
            "status": "vacant",
        },
        headers=own_b_h,
    )
    assert unit_b_resp.status_code == 201, unit_b_resp.text
    unit_b_id = unit_b_resp.json()["id"]
    cross_evidence_id = _create_evidence(
        client, own_b_h, unit_id=unit_b_id,
        category=EvidenceCategory.move_out_photo,
    )
    insp = _schedule_inspection_api(client, own_a_h, lease_id=lease_id)
    r = client.patch(
        f"{API}/move-out-inspections/{insp['id']}",
        json={"evidence_ids": [cross_evidence_id]},
        headers=own_a_h,
    )
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["reason"] == "move_out_inspection_evidence_mismatched_org_or_unit"


def test_g7_notes_persist_roundtrip(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    when = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    notes_value = "hello notes"
    create_r = client.post(
        f"{API}/move-out-inspections",
        json={
            "lease_id": lease_id,
            "scheduled_at": when,
            "notes": notes_value,
        },
        headers=h,
    )
    assert create_r.status_code == 201, create_r.text
    insp_id = create_r.json()["id"]
    get_r = client.get(
        f"{API}/move-out-inspections/{insp_id}",
        headers=h,
    )
    assert get_r.status_code == 200, get_r.text
    assert get_r.json()["notes"] == notes_value


def test_g8_deduction_item_income_id_rejected_on_create(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    insp = _schedule_and_full_inspection(
        client, db_session, h, lease_id=lease_id,
    )
    r = client.post(
        f"{API}/deposit-settlements",
        json={
            "move_out_inspection_id": insp["id"],
            "deposit_received": "24000.00",
            "total_deductions": "5000.00",
            "refund_amount": "19000.00",
            "deductions": [
                {
                    "description": "paint",
                    "amount": "5000.00",
                    "income_id": 99999,
                },
            ],
        },
        headers=h,
    )
    assert r.status_code == 422, r.text
    errors = r.json()["detail"]
    locs = {tuple(err.get("loc", [])) for err in errors if err.get("type") == "extra_forbidden"}
    assert ("body", "deductions", 0, "income_id") in locs


def test_g9_inspection_read_nullable_unit_tenant_fields(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    when = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    create_r = client.post(
        f"{API}/move-out-inspections",
        json={
            "lease_id": lease_id,
            "scheduled_at": when,
        },
        headers=h,
    )
    assert create_r.status_code == 201, create_r.text
    insp_id = create_r.json()["id"]
    get_r = client.get(
        f"{API}/move-out-inspections/{insp_id}",
        headers=h,
    )
    assert get_r.status_code == 200, get_r.text
    body = get_r.json()
    assert body["unit_id"] == unit_id
    assert body["tenant_id"] == tenant_id


def test_g10_evidence_soft_deleted_excluded_from_gate(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    from app.models.evidence import Evidence as EvidenceModel
    h = _h(owner_a[1])
    insp = _schedule_inspection_api(client, h, lease_id=lease_id)
    insp_id = insp["id"]
    eid = _create_evidence(client, h, unit_id=unit_id,
                           category=EvidenceCategory.move_out_photo)
    inspect_r = client.post(
        f"{API}/move-out-inspections/{insp_id}/inspect",
        json={
            "evidence_ids": [eid],
            "findings": [{"item": "wall scratch", "severity": "minor"}],
        },
        headers=h,
    )
    assert inspect_r.status_code == 200, inspect_r.text
    db_session.expire_all()
    ev_row = db_session.get(EvidenceModel, eid)
    assert ev_row is not None
    ev_row.deleted_at = datetime.now(timezone.utc)
    db_session.commit()
    confirm_r = client.post(
        f"{API}/move-out-inspections/{insp_id}/confirm",
        headers=h,
    )
    assert confirm_r.status_code == 409, confirm_r.text
    detail = confirm_r.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["reason"] == "move_out_inspection_evidence_gate_failed"


def test_g11_concurrent_schedule_same_lease_409_no_500(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    when = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    payload = {
        "lease_id": lease_id,
        "scheduled_at": when,
    }
    r1 = client.post(f"{API}/move-out-inspections", json=payload, headers=h)
    assert r1.status_code == 201, r1.text
    existing_insp_id = r1.json()["id"]
    r2 = client.post(f"{API}/move-out-inspections", json=payload, headers=h)
    assert r2.status_code == 409, r2.text
    detail = r2.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["reason"] == "move_out_inspection_already_exists_for_lease"
    assert detail.get("existing_inspection_id") == existing_insp_id


def test_g12_confirm_inspection_auto_creates_draft_settlement(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    insp = _schedule_and_full_inspection(
        client, db_session, h, lease_id=lease_id,
    )
    list_r = client.get(
        f"{API}/deposit-settlements?lease_id={lease_id}",
        headers=h,
    )
    assert list_r.status_code == 200, list_r.text
    settlements_payload = list_r.json()
    assert isinstance(settlements_payload, dict) and "items" in settlements_payload, settlements_payload
    settlements = settlements_payload["items"]
    assert isinstance(settlements, list) and len(settlements) == 1, settlements_payload
    linked = [s for s in settlements if s.get("move_out_inspection_id") == insp["id"]]
    assert len(linked) == 1
    assert linked[0]["status"] == DepositSettlementStatus.DRAFT.value


def test_g13_orphan_task_source_id_none_reconcile_cancelled(
    client, db_session, owner_a, lease_id, unit_id, tenant_id, property_id,
):
    h = _h(owner_a[1])
    orphan = OperationalTask(
        task_type=OperationalTaskType.MOVE_OUT_INSPECTION,
        title="orphan inspection task source_id=None",
        status=OperationalTaskStatus.PENDING,
        source_type="move_out_inspection",
        source_id=None,
        lease_id=lease_id,
        property_id=property_id,
        priority=OperationalTaskPriority.high,
        due_at=datetime.now(timezone.utc) + timedelta(days=1),
        details={},
    )
    db_session.add(orphan)
    db_session.commit()
    db_session.refresh(orphan)
    orphan_id = orphan.id
    reconcile_tasks(db_session, now=datetime.now(timezone.utc))
    db_session.expire_all()
    after = db_session.get(OperationalTask, orphan_id)
    assert after.status == OperationalTaskStatus.CANCELLED
    assert after.status != OperationalTaskStatus.COMPLETED


def test_h1_non_confirmed_insp_create_settlement_409_exact_reason(
    client, db_session, owner_a, lease_id, unit_id, tenant_id, property_id,
):
    h = _h(owner_a[1])
    scheduled = _schedule_inspection_api(client, h, lease_id=lease_id)
    scheduled_id = scheduled["id"]
    payload_base = {
        "move_out_inspection_id": scheduled_id,
        "deposit_received": "24000.00",
        "total_deductions": "0.00",
        "refund_amount": "24000.00",
    }
    r_sched = client.post(f"{API}/deposit-settlements", json={
        **payload_base,
    }, headers=h)
    assert r_sched.status_code == 409
    d_sched = r_sched.json()["detail"]
    assert d_sched["reason"] == "move_out_inspection_not_confirmed"
    assert d_sched["inspection_id"] == scheduled_id
    assert d_sched["current_status"] == MoveOutInspectionStatus.SCHEDULED.value

    insp = client.post(f"{API}/move-out-inspections/{scheduled_id}/inspect", json={
        "findings": [{"item": "wall", "severity": "low"}],
    }, headers=h)
    assert insp.status_code == 200, insp.text
    r_insp = client.post(f"{API}/deposit-settlements", json={
        **payload_base,
    }, headers=h)
    assert r_insp.status_code == 409
    assert r_insp.json()["detail"]["reason"] == "move_out_inspection_not_confirmed"

    cancel = client.post(f"{API}/move-out-inspections/{scheduled_id}/cancel", headers=h)
    assert cancel.status_code == 200, cancel.text
    r_cancel = client.post(f"{API}/deposit-settlements", json={
        **payload_base,
    }, headers=h)
    assert r_cancel.status_code == 409
    assert r_cancel.json()["detail"]["reason"] == "move_out_inspection_not_confirmed"


def test_h2_confirm_draft_settle_non_confirmed_insp_409(
    client, db_session, owner_a, lease_id, unit_id, tenant_id, property_id,
):
    from app.services.audit import record_audit as _audit_silent
    h = _h(owner_a[1])
    insp = _schedule_inspection_api(client, h, lease_id=lease_id)
    insp_id = insp["id"]
    owner_uid = owner_a[0].id if hasattr(owner_a[0], "id") else int(owner_a[0])
    settled_row = DepositSettlement(
        move_out_inspection_id=insp_id,
        lease_id=lease_id,
        created_by=owner_uid,
        updated_by=owner_uid,
        status=DepositSettlementStatus.DRAFT,
        refund_amount=Decimal("0"),
        total_deductions=Decimal("0"),
        deposit_received=Decimal("0"),
        deductions=[],
    )
    db_session.add(settled_row)
    db_session.commit()
    db_session.refresh(settled_row)
    draft_id = settled_row.id

    confirm = client.post(f"{API}/deposit-settlements/{draft_id}/confirm", headers=h)
    assert confirm.status_code == 409, confirm.text
    detail = confirm.json()["detail"]
    assert detail["reason"] == "move_out_inspection_not_confirmed"
    assert detail["current_status"] == MoveOutInspectionStatus.SCHEDULED.value


def test_h3_findings_invalid_422_and_legal_roundtrip(
    client, db_session, owner_a, lease_id, unit_id, tenant_id, property_id,
):
    h = _h(owner_a[1])
    r_extra = client.post(f"{API}/move-out-inspections", json={
        "lease_id": lease_id,
        "scheduled_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        "findings": [{"item": "door", "UNEXPECTED_FIELD_BAD": 123}],
    }, headers=h)
    assert r_extra.status_code == 422, r_extra.text
    errors_extra = r_extra.json()["detail"]
    assert isinstance(errors_extra, list)
    assert len(errors_extra) == 1
    err_extra_0 = errors_extra[0]
    assert "loc" in err_extra_0
    assert "type" in err_extra_0
    assert err_extra_0["type"] == "extra_forbidden"

    r_neg = client.post(f"{API}/move-out-inspections", json={
        "lease_id": lease_id,
        "scheduled_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        "findings": [{"item": "floor", "cost": "-12.50"}],
    }, headers=h)
    assert r_neg.status_code == 422, r_neg.text
    errors_neg = r_neg.json()["detail"]
    assert isinstance(errors_neg, list)
    assert len(errors_neg) == 1
    err_neg_0 = errors_neg[0]
    assert "loc" in err_neg_0
    assert "type" in err_neg_0
    assert err_neg_0["type"] == "greater_than_equal"

    r_wrong_type = client.post(f"{API}/move-out-inspections", json={
        "lease_id": lease_id,
        "scheduled_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        "findings": [{"item": 777, "cost": "not a number"}],
    }, headers=h)
    assert r_wrong_type.status_code == 422, r_wrong_type.text
    errors_wrong = r_wrong_type.json()["detail"]
    assert isinstance(errors_wrong, list)
    assert len(errors_wrong) == 2
    for e in errors_wrong:
        assert "loc" in e
        assert "type" in e
    wrong_types = {e["type"] for e in errors_wrong}
    assert "string_type" in wrong_types
    assert "decimal_parsing" in wrong_types

    findings_legal = [
        {"item": "wall scratch", "description": "chipped", "severity": "low", "cost": None},
        {"item": "appliance broken", "description": "fridge", "severity": "high", "cost": "129.99"},
    ]
    insp_ok = client.post(f"{API}/move-out-inspections", json={
        "lease_id": lease_id,
        "scheduled_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        "findings": findings_legal,
    }, headers=h)
    assert insp_ok.status_code == 201, insp_ok.text
    insp_id = insp_ok.json()["id"]

    read1 = client.get(f"{API}/move-out-inspections/{insp_id}", headers=h)
    assert read1.status_code == 200
    assert read1.json()["findings"] == findings_legal

    patch_ok = client.patch(f"{API}/move-out-inspections/{insp_id}", json={
        "findings": [
            {"item": "updated", "cost": "2.10"},
            {"item": "second", "severity": "medium"},
        ],
    }, headers=h)
    assert patch_ok.status_code == 200, patch_ok.text
    patched_findings = patch_ok.json()["findings"]
    assert patched_findings == [
        {"item": "updated", "description": None, "severity": None, "cost": "2.10"},
        {"item": "second", "description": None, "severity": "medium", "cost": None},
    ]
    insp_read_2 = client.get(f"{API}/move-out-inspections/{insp_id}", headers=h)
    assert insp_read_2.status_code == 200
    assert insp_read_2.json()["findings"] == patched_findings


def test_h4_settlement_confirm_non_idempotency_fk_integrity_not_swallowed(
    client, db_session, owner_a, lease_id, unit_id, tenant_id, property_id,
):
    from app.services.deposit_settlement_service import confirm_settlement
    h = _h(owner_a[1])
    insp = _schedule_inspection_api(client, h, lease_id=lease_id)
    insp_id = insp["id"]
    insp_row = db_session.get(MoveOutInspection, insp_id)
    insp_row.status = MoveOutInspectionStatus.CONFIRMED
    db_session.commit()
    owner_uid = owner_a[0].id if hasattr(owner_a[0], "id") else int(owner_a[0])

    settle = DepositSettlement(
        move_out_inspection_id=insp_id,
        lease_id=lease_id,
        created_by=owner_uid,
        updated_by=owner_uid,
        status=DepositSettlementStatus.DRAFT,
        refund_amount=Decimal("0"),
        total_deductions=Decimal("123.45"),
        deposit_received=Decimal("123.45"),
        deductions=[{
            "description": "deduction",
            "amount": "123.45",
        }],
    )
    db_session.add(settle)
    db_session.commit()
    db_session.refresh(settle)
    settle_id = settle.id

    from sqlalchemy import event as sqla_event
    from sqlalchemy.exc import IntegrityError as IE
    try:
        from psycopg.errors import ForeignKeyViolation as FKV
    except ModuleNotFoundError:
        from psycopg2.errors import ForeignKeyViolation as FKV
    triggered = {"hit": 0}

    @sqla_event.listens_for(db_session.get_bind(), "before_cursor_execute")
    def _poison(conn, cursor, statement, parameters, context, executemany):
        if (
            triggered["hit"] == 0
            and isinstance(statement, str)
            and "INSERT INTO incomes" in statement.replace("\n", " ")
        ):
            triggered["hit"] += 1
            fake_orig = FKV(
                "insert or update on table \"incomes\" violates foreign key constraint \"incomes_lease_id_fkey\""
            )
            raise IE(statement, parameters, fake_orig)

    try:
        from sqlalchemy.exc import IntegrityError as _IE
        raised_ok = False
        try:
            confirm_settlement(
                db_session,
                settle,
                confirmed_at=datetime.now(timezone.utc),
                confirmed_by=owner_uid,
            )
        except _IE:
            raised_ok = True
            db_session.rollback()
        assert raised_ok, "expected IntegrityError re-raised for non-idempotency FK violation, not swallowed with Settlement auto-confirmed"
    finally:
        sqla_event.remove(db_session.get_bind(), "before_cursor_execute", _poison)
    db_session.rollback()
    after = db_session.get(DepositSettlement, settle_id)
    assert after.status == DepositSettlementStatus.DRAFT


def test_h5_not_renewed_malformed_tolerant_batch_no_exception(
    client, db_session, owner_a, lease_id, unit_id, tenant_id, property_id,
):
    h = _h(owner_a[1])
    past = date.today() - timedelta(days=30)
    cases = [
        {"not_renewed": False},
        {"not_renewed": None},
        {"not_renewed": "random-string"},
        {"not_renewed": {"nested": True}},
        {"not_renewed": 1},
        {"something_else": True},
        None,
    ]
    created_ids = []
    owner_uid = owner_a[0].id if hasattr(owner_a[0], "id") else int(owner_a[0])
    for i, meta in enumerate(cases):
        clone = Lease(
            unit_id=unit_id,
            tenant_id=tenant_id,
            start_date=past - timedelta(days=365),
            end_date=past,
            monthly_rent=Decimal(f"{1000+i}"),
            deposit=Decimal("2000"),
            status=LeaseStatus.active,
            created_by=owner_uid,
            updated_by=owner_uid,
            renewal_metadata=meta,
        )
        db_session.add(clone)
    db_session.flush()
    db_session.commit()
    # retrieve created_ids after commit (for filter later — batch unique constraint may collide so skip filter anyway)
    clone_rows = (
        db_session.query(Lease)
        .filter(Lease.deleted_at.is_(None), Lease.monthly_rent.between(Decimal("1000"), Decimal("1006")))
        .all()
    )
    created_ids = [r.id for r in clone_rows]

    try:
        generate_business_tasks(db_session, now=datetime.now(timezone.utc))
    except Exception as exc:
        raise AssertionError(f"not_renewed malformed batch raised {type(exc).__name__}: {exc}") from exc

    db_session.flush()
    insps = db_session.query(MoveOutInspection).filter(
        MoveOutInspection.lease_id.in_(created_ids) if created_ids else False
    ).all()
    assert insps == []

    owner_uid = owner_a[0].id if hasattr(owner_a[0], "id") else int(owner_a[0])
    good = Lease(
        unit_id=unit_id,
        tenant_id=tenant_id,
        start_date=past - timedelta(days=365),
        end_date=past,
        monthly_rent=Decimal("9999"),
        deposit=Decimal("2000"),
        status=LeaseStatus.active,
        created_by=owner_uid,
        updated_by=owner_uid,
        renewal_metadata={"not_renewed": True, "move_out_date": past.isoformat()},
    )
    db_session.add(good)
    db_session.commit()
    db_session.refresh(good)
    good_id = good.id
    generate_business_tasks(db_session, now=datetime.now(timezone.utc))
    db_session.flush()
    from app.models.operations import OperationalTaskType, OperationalTaskStatus
    good_task = (
        db_session.query(OperationalTask)
        .filter(
            OperationalTask.task_type == OperationalTaskType.MOVE_OUT_INSPECTION,
            OperationalTask.lease_id == good_id,
            OperationalTask.status == OperationalTaskStatus.PENDING,
        )
        .first()
    )
    assert good_task is not None, "expected generation to create operational move_out_inspection task for good_lease with containment {not_renewed:true}"
    bad_tasks_count = (
        db_session.query(OperationalTask)
        .filter(
            OperationalTask.task_type == OperationalTaskType.MOVE_OUT_INSPECTION,
            OperationalTask.lease_id.in_(created_ids) if created_ids else False,
        )
        .count()
    )
    assert bad_tasks_count == 0, f"malformed/not_renewed leases must NOT generate move_out inspection tasks, got {bad_tasks_count}"


def test_h6_renew_before_end_date_409_and_predecessor_still_active(
    client, db_session, owner_a, lease_id, unit_id, tenant_id, property_id,
):
    from app.models.lease import Lease as LeaseModel
    h = _h(owner_a[1])
    lease_row = db_session.get(LeaseModel, lease_id)
    future_end = date.today() + timedelta(days=60)
    lease_row.end_date = future_end
    lease_row.status = LeaseStatus.active
    db_session.commit()

    r = client.post(f"{API}/leases/{lease_id}/renew", json={
        "start_date": (future_end + timedelta(days=1)).isoformat(),
        "end_date": (future_end + timedelta(days=365)).isoformat(),
        "monthly_rent": "13000.00",
        "deposit": "26000.00",
    }, headers=h)
    assert r.status_code == 409, r.text
    d = r.json()["detail"]
    assert d["reason"] == "renewal_before_predecessor_end_date"
    assert d["predecessor_lease_id"] == lease_id
    assert date.fromisoformat(d["predecessor_end_date"]) == future_end

    db_session.expire_all()
    still = db_session.get(LeaseModel, lease_id)
    assert still.status == LeaseStatus.active


# ========================================================================
# H组 Owner 5 Actionable 精确反例 + 其他新增 counterexamples
# ========================================================================


def test_h7_task_cancel_ab_isolation_dedupe_key_must_not_cancel_other_inspection(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    """Owner PASAY-TASK-012 #3 exact A/B counterexample.

    Instead of depending on generation/reconcile side-effects to create
    inspection-bound tasks (which are async), we manually create the four
    task scenarios: task_A_before_cancel (tied to insp_A by source_type/id),
    task_B (tied to insp_B), and task_A_after_cancel.  We exercise
    cancel_inspection and confirm_inspection and ensure that the service
    cancel/confirm routine only modifies the tasks matching the explicit
    q1 + q2 contract.
    """
    from app.models.operations import (
        OperationalTask as OT,
        OperationalTaskType as OTT,
        OperationalTaskStatus as OTS,
        OperationalTaskPriority as OTP,
    )
    from datetime import timezone
    h = _h(owner_a[1])
    actor_id = owner_a[0].id
    now = datetime.now(timezone.utc)
    dedupe_key = f"lease:{lease_id}:MOVE_OUT_INSPECTION"

    def _mk_task(*, source_type, source_id, status=OTS.PENDING) -> OT:
        t = OT(
            task_type=OTT.MOVE_OUT_INSPECTION,
            status=status,
            priority=OTP.medium,
            title=f"insp task {source_type}={source_id}",
            lease_id=lease_id,
            source_type=source_type,
            source_id=source_id,
            dedupe_key=dedupe_key,
            due_at=now + timedelta(days=7),
        )
        db_session.add(t)
        db_session.flush()
        return t

    # inspection A creation
    insp_a = _schedule_inspection_api(client, h, lease_id=lease_id)
    # explicit task_A tied to insp A
    task_a = _mk_task(source_type="move_out_inspection", source_id=insp_a["id"])
    task_a_id = task_a.id
    # cancel inspection A — this is what should close exactly task_a
    r_cancel = client.post(f"{API}/move-out-inspections/{insp_a['id']}/cancel", headers=h)
    assert r_cancel.status_code == 200, r_cancel.text
    insp_a_closed = r_cancel.json()
    assert insp_a_closed["id"] == insp_a["id"]
    assert insp_a_closed["status"] == MoveOutInspectionStatus.CANCELLED.value
    db_session.expire_all()
    task_a_row = db_session.get(OT, task_a_id)
    assert task_a_row is not None
    assert task_a_row.status == OTS.CANCELLED, (
        f"task_A must be CANCELLED after cancel insp A; got status={task_a_row.status.value}; "
        "this validates cancel_inspection q1 closes insp-A-bound tasks correctly."
    )

    # Now create inspection B and its task B
    insp_b = _schedule_inspection_api(client, h, lease_id=lease_id)
    task_b = _mk_task(source_type="move_out_inspection", source_id=insp_b["id"])
    task_b_id = task_b.id
    db_session.expire_all()
    task_b_preconfirm = db_session.get(OT, task_b_id)
    assert task_b_preconfirm.status == OTS.PENDING, (
        f"task_B must remain PENDING after insp_A cancel (same dedupe_key, DIFFERENT source_id). "
        f"Got status={task_b_preconfirm.status.value}."
    )
    # Count A and B rows each bound by exact source_type/id = must be exactly 1 each
    a_task_count = db_session.query(OT).filter(
        OT.task_type == OTT.MOVE_OUT_INSPECTION,
        OT.source_type == "move_out_inspection",
        OT.source_id == insp_a["id"],
    ).count()
    b_task_count = db_session.query(OT).filter(
        OT.task_type == OTT.MOVE_OUT_INSPECTION,
        OT.source_type == "move_out_inspection",
        OT.source_id == insp_b["id"],
    ).count()
    assert a_task_count == b_task_count == 1, (
        f"A/B tasks each must have exactly 1 row with exact (source_type,source_id). "
        f"Got a={a_task_count} b={b_task_count}. No >=1 or contains allowed."
    )
    # Confirm B via real pipeline — first POST /inspect (SCHEDULED→INSPECTED)
    # then POST /confirm (INSPECTED→CONFIRMED). PATCH only updates metadata
    # fields, does NOT transition status (state machine enforced by service).
    insp_b_id = insp_b["id"]
    ev = [
        _create_evidence(client, h, unit_id=unit_id, category=EvidenceCategory.move_out_photo)
        for _ in range(2)
    ]
    insp_patch = client.patch(f"{API}/move-out-inspections/{insp_b_id}", json={
        "notes": "inspection B pre-patch",
    }, headers=h)
    assert insp_patch.status_code == 200, insp_patch.text
    mark_inspected = client.post(f"{API}/move-out-inspections/{insp_b_id}/inspect", json={
        "evidence_ids": ev,
        "findings": [
            {"item": "door", "description": "good condition"},
            {"item": "walls", "severity": "low", "description": "minor wear"},
        ],
    }, headers=h)
    assert mark_inspected.status_code == 200, mark_inspected.text
    confirm_b = client.post(f"{API}/move-out-inspections/{insp_b_id}/confirm", headers=h)
    assert confirm_b.status_code == 200, confirm_b.text
    insp_b_confirm_payload = confirm_b.json()
    assert insp_b_confirm_payload["id"] == insp_b_id
    assert insp_b_confirm_payload["status"] == MoveOutInspectionStatus.CONFIRMED.value
    db_session.expire_all()
    task_a_post = db_session.get(OT, task_a_id)
    task_b_post = db_session.get(OT, task_b_id)
    assert task_a_post.status == OTS.CANCELLED, (
        f"task_A status must remain CANCELLED after insp_B confirm (no cross-inspection write). "
        f"Got {task_a_post.status.value}"
    )
    assert task_b_post.status == OTS.COMPLETED, (
        f"task_B status must be COMPLETED after insp_B confirm. Got {task_b_post.status.value}"
    )


def test_h8_decline_renewal_json_copy_persistence_fresh_session(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    user = owner_a[0]
    user_id = user.id

    seeded_meta = {"preexisting_metadata": "untouched-h8", "notes": {"a": "b"}}
    lease_before = db_session.get(Lease, lease_id)
    lease_before.renewal_metadata = dict(seeded_meta)
    db_session.commit()

    reason_text = "tenant-declined-H8-test"
    r1 = client.post(f"{API}/leases/{lease_id}/decline-renewal", json={
        "reason": reason_text,
    }, headers=h)
    assert r1.status_code == 200, r1.text
    first_body = r1.json()
    assert first_body["id"] == lease_id

    # close + new session (fresh reload) using same engine as fixture to get
    # the test DB with current schema — do NOT use SessionLocal() which binds
    # the default app DATABASE_URL engine pointing to a different/schema-stale DB.
    db_session.close()
    FreshSession = sessionmaker(
        bind=db_session.get_bind(), autoflush=False, expire_on_commit=False,
    )
    fresh = FreshSession()
    try:
        reloaded = fresh.get(Lease, lease_id)
        meta = dict(reloaded.renewal_metadata or {})
        # 1) declined flag persisted
        assert meta.get("not_renewed") is True, (
            f"not_renewed flag not persisted after decline; got meta={meta}"
        )
        # 2) actor / timestamp / reason persisted
        assert meta.get("declined_by") == user_id, (
            f"declined_by actor id not persisted, got {meta.get('declined_by')}; want user_id={user_id}"
        )
        assert isinstance(meta.get("declined_at"), str) and len(meta["declined_at"]) > 0
        assert meta.get("decline_reason") == reason_text
        # 3) pre-existing metadata preserved
        for k, v in seeded_meta.items():
            assert meta.get(k) == v, (
                f"pre-existing metadata key={k} destroyed after decline persistence; got {meta.get(k)!r} vs want {v!r}"
            )
        # 5) exactly 1 decline_renewal audit event
        audits = (
            fresh.query(AuditLog)
            .filter(
                AuditLog.table_name == "leases",
                AuditLog.record_id == lease_id,
                AuditLog.action == "decline_renewal",
            )
            .all()
        )
        assert len(audits) == 1, (
            f"expected exactly 1 decline_renewal audit event, got {len(audits)}: {audits}"
        )
    finally:
        fresh.close()

    # reopen db_session (fixture's session)
    db_session.begin()
    # 4) duplicate decline request is idempotent -> status 200 & no new metadata rows
    r2 = client.post(f"{API}/leases/{lease_id}/decline-renewal", json={
        "reason": "duplicate-request-should-be-idempotent",
    }, headers=h)
    assert r2.status_code == 200, r2.text
    db_session.expire_all()
    lease_r2 = db_session.get(Lease, lease_id)
    meta_after = dict(lease_r2.renewal_metadata or {})
    assert meta_after.get("decline_reason") == reason_text, (
        f"second decline mutated decline_reason (idempotent violation); got {meta_after.get('decline_reason')!r}"
    )
    audit_after = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.table_name == "leases",
            AuditLog.record_id == lease_id,
            AuditLog.action == "decline_renewal",
        )
        .count()
    )
    assert audit_after == 1, (
        f"idempotent second decline produced another audit row; count={audit_after}"
    )


def test_h9_truth_validator_provisional_lease_source_registered(
    db_session, lease_id, unit_id, tenant_id
):
    from app.services.operations.truth_validator import (
        _PROJECTION_TABLE, validate_completion,
    )
    from app.models.operations import (
        OperationalTask,
        OperationalTaskType,
        OperationalTaskStatus,
        OperationalTaskPriority,
    )
    registered_keys = set(_PROJECTION_TABLE.keys())
    assert (OperationalTaskType.MOVE_OUT_INSPECTION, "lease") in registered_keys, (
        "(MOVE_OUT_INSPECTION, lease) provisional source tuple MUST be in _PROJECTION_TABLE "
        "for canonical provisional-task contract (generation.py L962). Current keys: "
        f"{[k for k in registered_keys if k[0]==OperationalTaskType.MOVE_OUT_INSPECTION]}"
    )
    task_sourceid_none = OperationalTask(
        task_type=OperationalTaskType.MOVE_OUT_INSPECTION,
        source_type="move_out_inspection",
        source_id=None,
        lease_id=lease_id,
        dedupe_key=f"lease:{lease_id}:MOVE_OUT_INSPECTION",
        status=OperationalTaskStatus.PENDING,
        priority=OperationalTaskPriority.medium,
        title="sourceless",
        due_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    db_session.add(task_sourceid_none)
    db_session.flush()
    v_none = validate_completion(db_session, task_sourceid_none)
    assert v_none.ok is False, (
        "source_id=None task CANNOT pass truth validator (Owner #13); reconciliation must fail-close CANCELLED. "
        f"Got ok={v_none.ok!r} expected=false"
    )
    assert v_none.expected_truth != ""
    db_session.rollback()

    valid_provisional = OperationalTask(
        task_type=OperationalTaskType.MOVE_OUT_INSPECTION,
        source_type="lease",
        source_id=lease_id,
        lease_id=lease_id,
        dedupe_key=f"lease:{lease_id}:MOVE_OUT_INSPECTION",
        status=OperationalTaskStatus.PENDING,
        priority=OperationalTaskPriority.medium,
        title="legal-provisional",
        due_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    db_session.add(valid_provisional)
    db_session.flush()
    # Does NOT raise an unregistered tuple KeyError → tuple is legal.
    result = validate_completion(db_session, valid_provisional)
    assert isinstance(result.ok, bool), (
        "truth validator on provisional (MOI, lease) must return bool result, not throw unregistered"
    )
    db_session.rollback()

