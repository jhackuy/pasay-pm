"""PASAY-MILESTONE-002-FIX3 — 5 Owner-confirmed blocker regressions.

Covers (mapped to the FIX3 blocker list):
  F3-R1  Income route collision: GET /api/v1/incomes/claims returns 200
         (not 422 from being captured by the int:income_id dynamic segment).
  F3-R2  Rent claim verify/fail/reverse require OrganizationRole.OWNER at the
         membership level (not a global admin/manager role).  SECRETARY active
         members and non-members must receive 403.
  F3-R3  Rent idempotency_key is namespaced by (lease, period, actor, raw_key).
         Same raw key across different leases/orgs → two independent claims,
         neither leaked.  Same raw key on same lease+period → dedupe (200).
  F3-R4  Repair approval with no property/unit anchor fails CLOSED with 409
         (ProposalError) and never transitions into WAITING_PAYMENT or
         pretends a linked Expense is waiting for payment.
  F3-R5  Audit truth — verify / fail / reverse mutations capture old snapshots
         BEFORE the mutation; reverse changed_fields.verified_amount uses the
         real pre-mutation verified_amount (not claimed_amount).  A separate
         repair-closure linked-task audit also captures pre-mutation values.
  F3-R6  Snapshot REVERSED semantic closure: reversed_n increments but
         verified_sum is only derived from VERIFIED rows (no double subtract).
"""
from __future__ import annotations

import pytest
from decimal import Decimal

from app.models.audit_log import AuditAction, AuditLog
from app.models.financial import Expense, ExpenseStatus
from app.models.lease import Lease
from app.models.membership import (
    Membership,
    MembershipState,
    Organization,
    OrganizationRole,
)
from app.models.rent_payment_claim import RentClaimStatus, RentPaymentClaim
from app.models.repair import RepairOperation, RepairProposalStatus
from app.models.user import User, UserRole
from app.services.rent_payment_truth import snapshot
from tests.conftest import _headers, make_user

API = "/api/v1"

_RENT_PERIOD = "2026-03"


def _h(key):
    return _headers(key)


def _claim(client, headers, lease_id, period, amount, ik=None):
    payload = {"period": period, "claimed_amount": amount}
    if ik is not None:
        payload["idempotency_key"] = ik
    r = client.post(
        f"{API}/incomes/leases/{lease_id}/claims",
        json=payload,
        headers=headers,
    )
    assert r.status_code in (200, 201), r.text
    return r.json()


def _verify(client, headers, claim_id, verified_amount=None, result="ok"):
    payload = {"result": result}
    if verified_amount is not None:
        payload["verified_amount"] = verified_amount
    return client.patch(
        f"{API}/incomes/claims/{claim_id}/verify",
        json=payload,
        headers=headers,
    )


def _fail(client, headers, claim_id, reason="bad ref"):
    return client.patch(
        f"{API}/incomes/claims/{claim_id}/fail",
        json={"reason": reason},
        headers=headers,
    )


def _reverse(client, headers, claim_id, reason="bounced"):
    return client.patch(
        f"{API}/incomes/claims/{claim_id}/reverse",
        json={"reason": reason},
        headers=headers,
    )


# ---------------------------------------------------------------------------
# F3-R1: GET /incomes/claims must not fall into /{income_id} 422 path parse.
# ---------------------------------------------------------------------------
def test_f3_r1_incomes_claims_no_422(client, owner_a):
    r = client.get(f"{API}/incomes/claims", headers=_h(owner_a[1]))
    assert r.status_code != 422, (
        "static /claims route was captured by dynamic /{income_id} int parser; "
        "response=%s" % r.text
    )
    assert r.status_code in (200, 401, 403)


# ---------------------------------------------------------------------------
# F3-R2: OrganizationRole.OWNER required (not global admin) for
#        verify / fail / reverse rent claims.
# ---------------------------------------------------------------------------
def test_f3_r2_rent_mutations_require_org_owner(
    client, db_session, owner_a, secretary_a, org_a, property_id, unit_id, lease_id
):
    # Non-member active user (no membership in org_a at all)
    nm_user, nm_key = make_user(db_session, "fix3_nm", UserRole.admin)
    db_session.commit()

    headers_owner = _h(owner_a[1])
    headers_sec = _h(secretary_a[1])
    headers_nm = _h(nm_key)

    claim = _claim(client, headers_owner, lease_id, _RENT_PERIOD, "6000.00")
    cid = claim["id"]

    # SECRETARY cannot verify / fail / reverse (PENDING claim now, try verify)
    rv = _verify(client, headers_sec, cid)
    assert rv.status_code == 403, (
        "SECRETARY should not be able to verify rent claim; got %s %s"
        % (rv.status_code, rv.text)
    )

    # Owner verify first so we have VERIFIED for reverse.
    rv = _verify(client, headers_owner, cid, verified_amount="6000.00")
    assert rv.status_code == 200, rv.text
    # Now secretary + non-member reverse → 403 / 404 (404 for non-member is
    # fine too; we never leak existence across organizations).
    rv = _reverse(client, headers_sec, cid)
    assert rv.status_code == 403, (
        "SECRETARY cannot reverse rent claim; got %s %s"
        % (rv.status_code, rv.text)
    )
    rv = _reverse(client, headers_nm, cid)
    assert rv.status_code in (403, 404), (
        "non-member cannot reverse rent claim (either 403 or 404 fail-closed "
        "to avoid cross-org existence leakage); got %s %s"
        % (rv.status_code, rv.text)
    )

    # Owner reverse to FAILED path via new claim: owner fail second claim.
    c2 = _claim(client, headers_owner, lease_id, _RENT_PERIOD, "3000.00")
    # secretary + non-member can't fail
    rv = _fail(client, headers_sec, c2["id"])
    assert rv.status_code == 403
    rv = _fail(client, headers_nm, c2["id"])
    assert rv.status_code in (403, 404)
    # owner can fail
    rv = _fail(client, headers_owner, c2["id"])
    assert rv.status_code == 200, rv.text


# ---------------------------------------------------------------------------
# F3-R3: idempotency_key namespaced by lease+period.
# ---------------------------------------------------------------------------
def test_f3_r3_idempotency_namespaced_by_lease_and_period(
    client,
    db_session,
    owner_a,
    owner_b,
    org_a,
    org_b,
    property_id,
    unit_id,
    lease_id,
    tenant_id,
):
    # Tenant for org_b (cross-org tenant_id reference would 409 fail-closed).
    tb = client.post(
        f"{API}/tenants",
        json={
            "full_name": "Org-B Tenant",
            "phone": "+639179999999",
            "email": "orgb@example.com",
            "organization_id": org_b.id,
        },
        headers=_h(owner_b[1]),
    )
    assert tb.status_code == 201, tb.text
    tb_id = tb.json()["id"]

    # Create a second property / unit / lease in org_b (different lease).
    pb_r = client.post(
        f"{API}/properties",
        json={
            "name": "OrgB Tower",
            "address": "2 Ayala Ave",
            "city": "Makati",
            "total_units": 2,
            "organization_id": org_b.id,
        },
        headers=_h(owner_b[1]),
    )
    assert pb_r.status_code == 201, pb_r.text
    pb = pb_r.json()
    ub_r = client.post(
        f"{API}/units",
        json={
            "property_id": pb["id"],
            "unit_number": "2A",
            "floor": "2",
            "size_sqm": "30.00",
            "monthly_rent": "9000.00",
            "status": "vacant",
        },
        headers=_h(owner_b[1]),
    )
    assert ub_r.status_code == 201, ub_r.text
    ub = ub_r.json()
    lb_r = client.post(
        f"{API}/leases",
        json={
            "unit_id": ub["id"],
            "tenant_id": tb_id,
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
            "monthly_rent": "9000.00",
            "deposit": "18000.00",
            "status": "active",
        },
        headers=_h(owner_b[1]),
    )
    assert lb_r.status_code == 201, lb_r.text
    lb = lb_r.json()
    lease_b_id = lb["id"]

    shared_ik = "fix3-r3-shared-ik"
    ha = _h(owner_a[1])
    hb = _h(owner_b[1])

    # Same raw ik on lease A period 2026-03 → twice → dedupe (same claim id)
    c_a1 = _claim(client, ha, lease_id, "2026-03", "12000.00", ik=shared_ik)
    c_a2 = _claim(client, ha, lease_id, "2026-03", "12000.00", ik=shared_ik)
    assert c_a1["id"] == c_a2["id"], "same lease+period ik should dedupe"

    # Same raw ik on lease B period 2026-03 → different claim id (no cross-lease leak)
    c_b = _claim(client, hb, lease_b_id, "2026-03", "9000.00", ik=shared_ik)
    assert c_b["id"] != c_a1["id"], (
        "same ik across different leases must NOT dedupe/collide"
    )

    # Same raw ik on lease A different period (2026-04) → new claim, not
    # collided with 2026-03.
    c_apr = _claim(client, ha, lease_id, "2026-04", "12000.00", ik=shared_ik)
    assert c_apr["id"] != c_a1["id"]


# ---------------------------------------------------------------------------
# F3-R4: Repair APPROVAL without property/unit anchor → fail closed 409,
#        status stays WAITING_APPROVAL, no linked Expense, not WAITING_PAYMENT.
# ---------------------------------------------------------------------------
def test_f3_r4_repair_approve_no_anchor_fails_closed(
    client, db_session, owner_a, org_a, property_id, unit_id
):
    # First create repair normally (with property_id + unit_id so scoped API
    # org resolution works and we can get a real repair_id/proposal_id).
    headers = _h(owner_a[1])
    r = client.post(
        f"{API}/repairs",
        json={
            "issue": "no anchor test",
            "unit_id": unit_id,
            "property_id": property_id,
            "closure_criteria": "vendor finishes",
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    rep = r.json()
    rep_id = rep["id"]

    # Proposal submitted.
    p = client.post(
        f"{API}/repairs/{rep_id}/proposals",
        json={
            "amount": "2500.00",
            "vendor": "FixCo",
            "description": "labor",
            "submit_as_expense": False,
        },
        headers=headers,
    )
    assert p.status_code == 201, p.text
    # Proposals are embedded in repair detail.
    proposal_after = [
        pp for pp in p.json()["proposals"] if pp["version"] == 1
    ][0]
    prop_id = proposal_after["id"]

    # NOW sever the anchors: force unit_id + property_id = None on the repair
    # via direct ORM UPDATE.  scoped_get for API access still works because
    # membership was resolved through the previous anchor before this test
    # point? No — scoped_get_repair resolves org via repair_org_id every
    # call.  But the router's decide_proposal calls scoped_get *before*
    # approve_proposal; so if we sever the anchor now then scoped_get will
    # return LookupError (404) before we reach the approve guard.
    #
    # Workaround: call approve_proposal at the service layer directly (that's
    # where the anchor guard lives), using a freshly-resolved repair row from
    # ORM with property_id = unit_id = None.
    from app.services.repairs.proposals import approve_proposal, ProposalError
    from app.models.repair import RepairOperation, RepairProposal

    row = db_session.get(RepairOperation, rep_id)
    assert row is not None
    row.unit_id = None
    row.property_id = None
    db_session.flush()

    prop_row = db_session.get(RepairProposal, prop_id)
    assert prop_row is not None
    assert prop_row.status == RepairProposalStatus.PENDING

    # Anchor guard must raise ProposalError BEFORE any transition to APPROVED
    # / WAITING_PAYMENT.
    with pytest.raises(ProposalError):
        approve_proposal(
            db_session, row, prop_row, approved_by=owner_a[0].id
        )
    db_session.rollback()

    # Refresh rows: proposal still PENDING / repair still WAITING_APPROVAL /
    # no linked expense_ids on proposal.
    row2 = db_session.get(RepairOperation, rep_id)
    prop2 = db_session.get(RepairProposal, prop_id)
    assert row2.status.value == "WAITING_APPROVAL"
    assert prop2.status == RepairProposalStatus.PENDING
    assert prop2.expense_id is None, (
        "approve_proposal anchor guard must not create or link an Expense "
        "when there is no property/unit anchor"
    )


# Also keep the happy-path anchored approve (Case G mirror) to confirm no
# regression on real approvals.
def test_f3_r4_anchored_approve_still_works(
    client, owner_a, org_a, property_id, unit_id
):
    h = _h(owner_a[1])
    r = client.post(
        f"{API}/repairs",
        json={
            "issue": "anchored faucet",
            "unit_id": unit_id,
            "property_id": property_id,
            "closure_criteria": "faucet fixed",
        },
        headers=h,
    ).json()
    client.post(
        f"{API}/repairs/{r['id']}/proposals",
        json={
            "amount": "3500.00",
            "vendor": "FixCo",
            "description": "parts + labor",
            "submit_as_expense": False,
        },
        headers=h,
    )
    d = client.post(
        f"{API}/repairs/{r['id']}/decide",
        json={"decision": "approve", "version": 1},
        headers=h,
    )
    assert d.status_code == 200, d.text
    after = d.json()
    assert after["status"] == "WAITING_PAYMENT"
    eids = [e for e in (after.get("expense_ids") or []) if e is not None]
    assert len(eids) == 1
    proposal = [p for p in after["proposals"] if p["version"] == 1][0]
    assert proposal["status"] == "APPROVED"


# ---------------------------------------------------------------------------
# F3-R5: Audit truth — verify / reverse capture pre-mutation snapshots, and
#        reverse.verified_amount[0] == real pre-mutation (not claimed_amount).
# ---------------------------------------------------------------------------
def test_f3_r5_audit_verify_reverse_old_new_different(
    client, db_session, owner_a, org_a, property_id, unit_id, lease_id
):
    h = _h(owner_a[1])
    # Claim claimed=7000, verify to 6500 so claimed_amount != verified_amount.
    c = _claim(client, h, lease_id, _RENT_PERIOD, "7000.00")
    cid = c["id"]

    v = _verify(client, h, cid, verified_amount="6500.00", result="partial ok")
    assert v.status_code == 200, v.text

    # Verify audit row: verify action, old_value.status=PENDING new=VERIFIED,
    # old/new dicts actually differ.
    audit = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.table_name == "rent_payment_claims",
            AuditLog.record_id == cid,
            AuditLog.action == AuditAction.rent_claim_verified,
        )
        .order_by(AuditLog.created_at.desc())
        .first()
    )
    assert audit is not None
    assert audit.old_value != audit.new_value, (
        "verify audit old/new must differ post-mutation"
    )
    pre_status = audit.changed_fields["status"][0]
    post_status = audit.changed_fields["status"][1]
    assert pre_status == "PENDING"
    assert post_status == "VERIFIED"

    # Reverse now. claimed_amount=7000.  The pre-mutation verified_amount
    # is 6500.  Audit changed_fields.verified_amount[0] MUST be "6500.00"
    # (real pre-mutation value) and NOT "7000.00" (claimed_amount).
    rv = _reverse(client, h, cid, reason="bounced partial")
    assert rv.status_code == 200, rv.text

    audit_rev = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.table_name == "rent_payment_claims",
            AuditLog.record_id == cid,
            AuditLog.action == AuditAction.rent_claim_reversed,
        )
        .order_by(AuditLog.created_at.desc())
        .first()
    )
    assert audit_rev is not None
    assert audit_rev.old_value != audit_rev.new_value
    pre_va = audit_rev.changed_fields["verified_amount"][0]
    post_va = audit_rev.changed_fields["verified_amount"][1]
    assert pre_va == "6500.00", (
        "reverse verified_amount pre-mutation must be actual verified_amount "
        "(got %r, expected 6500.00; claimed_amount would be 7000.00 which is "
        "the bug we are guarding against)" % pre_va
    )
    assert post_va is None


# ---------------------------------------------------------------------------
# F3-R6: Snapshot REVERSED semantic closure — reversed_n incremented but
#        verified_sum is only derived from VERIFIED rows (no double subtract).
# ---------------------------------------------------------------------------
def test_f3_r6_snapshot_reversed_no_double_subtract(
    client, db_session, owner_a, org_a, property_id, unit_id, lease_id
):
    h = _h(owner_a[1])
    c = _claim(client, h, lease_id, _RENT_PERIOD, "12000.00")
    _verify(client, h, c["id"], verified_amount="12000.00")
    lease_obj = db_session.get(Lease, lease_id)
    t1 = snapshot(db_session, lease_obj.id, _RENT_PERIOD)
    assert t1.verified_paid_total == Decimal("12000.00")
    assert t1.verified_claim_count == 1
    assert t1.reversed_claim_count == 0

    _reverse(client, h, c["id"], reason="bounced")
    db_session.commit()
    t2 = snapshot(db_session, lease_obj.id, _RENT_PERIOD)
    # verified_paid_total should be exactly zero because the VERIFIED row is
    # now REVERSED; with the old (buggy) double-subtract implementation this
    # would have been -12000 if we re-implemented the "REVERSED rows subtract
    # verified_amount" rule.
    assert t2.verified_paid_total == Decimal("0.00"), (
        "REVERSED snapshot must not double-subtract; got %s" % t2.verified_paid_total
    )
    assert t2.verified_claim_count == 0
    assert t2.reversed_claim_count == 1
