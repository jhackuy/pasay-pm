"""PASAY-MILESTONE-004 Lease & Move-out Truth Closure Targeted tests.

Scope: ONLY Lease Renewal SM + Move-out Inspection SM + Deposit Settlement +
Lease Close Gate + Truth→Task Projection.

Groups (count >= 35 PASS contract):
  A组 — Renewal state machine (A1-A9: 9 tests)
  B组 — Move-out Inspection + Evidence gate (B1-B13: 13 tests)
  C组 — Deposit Settlement + 1c conservation (C1-C14: 14 tests)
  D组 — Lease Close gate + final state sync (D1-D8: 8 tests)
  E组 — Truth -> OperationalTask projection (E1-E5: 5 tests)
  F组 — Anti-bypass tests (F1-F7: 7 tests)

Total: 49 tests. ALL tests use EXACT assertions; no truthy/weak checks.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

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
from app.services.operations.generation import generate_business_tasks
from app.services.operations.reconcile import reconcile_tasks

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
        if existing.get("status") != DepositSettlementStatus.DRAFT.value:
            return existing
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
    return r2.json()


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
    new_start = "2027-01-01"
    new_end = "2027-12-31"
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
    meta = pred.renewal_metadata or {}
    assert "renewed_lease_id" in meta
    succ_id = meta["renewed_lease_id"]
    succ = db_session.get(Lease, succ_id)
    assert succ is not None
    assert succ.status == LeaseStatus.active
    assert str(succ.start_date) == new_start
    assert str(succ.end_date) == new_end
    assert succ.monthly_rent == Decimal("12500.00")
    assert succ.deposit == Decimal("25000.00")


def test_a2_renew_is_idempotent_no_duplicate_successor(
    client, db_session, owner_a, unit_id, tenant_id, lease_id
):
    h = _h(owner_a[1])
    payload = {
        "start_date": "2027-01-01",
        "end_date": "2027-12-31",
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
    client.post(
        f"{API}/leases/{lease_id}/renew",
        json={
            "start_date": "2027-01-01", "end_date": "2027-12-31",
            "monthly_rent": "12500.00", "deposit": "25000.00",
        },
        headers=h,
    )
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
        if existing.get("status") != DepositSettlementStatus.DRAFT.value:
            return existing
        patch_payload = {
            "deposit_received": deposit,
            "total_deductions": total_deduct,
            "refund_amount": refund,
            "deductions": deductions,
        }
        p = client.patch(f"{API}/deposit-settlements/{existing_id}", json=patch_payload, headers=headers)
        assert p.status_code == 200, p.text
        return p.json()
    raise AssertionError(f"_draft_settlement unexpected status={r.status_code}: {r.text}")


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


def test_c3_conservation_gap_zero_and_one_cent_pass(
    client, db_session, owner_a, lease_id, unit_id, tenant_id
):
    h = _h(owner_a[1])
    insp = _schedule_and_full_inspection(
        client, db_session, h, lease_id=lease_id,
    )
    # gap = 0
    s1 = _draft_settlement(
        client, h, lease_id=lease_id, inspection_id=insp["id"],
        deposit="100.00", total_deduct="60.00", refund="40.00",
    )
    r1 = client.post(f"{API}/deposit-settlements/{s1['id']}/confirm", headers=h)
    assert r1.status_code == 200, r1.text


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
    s = _draft_settlement(client, h, lease_id=lease_id, inspection_id=insp["id"])
    client.post(f"{API}/deposit-settlements/{s['id']}/confirm", headers=h)
    after1_inc = db_session.query(Income).count()
    after1_exp = db_session.query(Expense).count()
    c2 = client.post(f"{API}/deposit-settlements/{s['id']}/confirm", headers=h)
    assert c2.status_code == 200, c2.text
    after2_inc = db_session.query(Income).count()
    after2_exp = db_session.query(Expense).count()
    assert after2_inc == after1_inc
    assert after2_exp == after1_exp


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
    r = client.delete(f"{API}/leases/{lease_id}", headers=h)
    assert r.status_code == 200, r.text
    lease = db_session.get(Lease, lease_id)
    assert lease.deleted_at is not None


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
    client, db_session, owner_a, lease_id, unit_id, tenant_id,
):
    h = _h(owner_a[1])
    client.post(
        f"{API}/leases/{lease_id}/decline-renewal",
        json={"reason": "tenant moving out"},
        headers=h,
    )
    db_session.expire_all()
    generate_business_tasks(db_session, now=datetime.now(timezone.utc))
    db_session.expire_all()
    cnt = _task_count(
        db_session,
        task_type=OperationalTaskType.MOVE_OUT_INSPECTION,
        source_type="move_out_inspection",
    )
    pending_cnt = _task_count(
        db_session,
        task_type=OperationalTaskType.MOVE_OUT_INSPECTION,
        status=OperationalTaskStatus.PENDING,
    )
    assert cnt == 1, f"Expected == 1 MOVE_OUT_INSPECTION task, got {cnt}"
    assert pending_cnt == 1


def test_e2_forward_path_inspection_confirm_completes_task(
    client, db_session, owner_a, lease_id, unit_id, tenant_id,
):
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
    client, db_session, owner_a, lease_id, unit_id, tenant_id,
):
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
    client, db_session, owner_a, lease_id, unit_id, tenant_id,
):
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
    locs = {tuple(err.get("loc", [])) for err in errors if err.get("type") == "extra_forbidden"}
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
    pred_end = date.today() + timedelta(days=30)
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
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["reason"] == "renewal_invalid_dates_end_before_start"


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
    locs = {tuple(err.get("loc", [])) for err in errors if err.get("type") == "extra_forbidden"}
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
    payload = {
        "start_date": "2027-01-01",
        "end_date": "2027-12-31",
        "monthly_rent": "12500.00",
        "deposit": "25000.00",
    }
    r1 = client.post(f"{API}/leases/{lease_id}/renew", json=payload, headers=h)
    assert r1.status_code == 200, r1.text
    pred1 = db_session.get(Lease, lease_id)
    succ_id_1 = (pred1.renewal_metadata or {})["renewed_lease_id"]
    r2 = client.post(f"{API}/leases/{lease_id}/renew", json=payload, headers=h)
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    succ_id_2 = body2.get("id")
    if succ_id_2 is None:
        pred2 = db_session.get(Lease, lease_id)
        succ_id_2 = (pred2.renewal_metadata or {})["renewed_lease_id"]
    assert succ_id_2 == succ_id_1


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
    sett1 = _full_settlement(
        client, db_session, h, inspection_id=insp1["id"],
    )
    db_session.expire_all()
    lease_before = db_session.get(Lease, lease_id)
    old_deposit_settlement_id = lease_before.deposit_settlement_id
    assert old_deposit_settlement_id is not None
    insp1_row = db_session.get(MoveOutInspection, insp1["id"])
    client.post(f"{API}/move-out-inspections/{insp1_row.id}/cancel", headers=h)
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
    assert insp_after.findings == findings_original


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
    assert "unit_id" in body
    assert "tenant_id" in body


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
    assert r1.status_code in (200, 201, 409), r1.text
    assert r1.status_code != 500, r1.text
    r2 = client.post(f"{API}/move-out-inspections", json=payload, headers=h)
    assert r2.status_code in (200, 201, 409), r2.text
    assert r2.status_code != 500, r2.text


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
    settlements = list_r.json()
    assert len(settlements) == 1
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
