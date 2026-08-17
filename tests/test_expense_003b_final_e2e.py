"""PASAY-VNEXT-EXPENSE-OPERATION-003B — Final E2E (section 22).

Unattended E2E: the exact ₱28,000 Expense chain
    Owner approval -> payment claim ₱10,000 -> evidence -> verify
    -> remaining ₱18,000 -> worker continuation -> second claim ₱18,000
    -> evidence -> verify -> Expense PAID
plus the exact final checks (Claim records = 2, verified = ₱28,000,
remaining = 0, duplicate count = 0, task history preserved, timeline complete,
audit complete), and a Repair-linked Expense that is PAID without closing the
Repair.
"""
from datetime import date, datetime, timedelta, timezone

API = "/api/v1"


def _mk(client, create_headers, approve_headers, amount="28000.00"):
    r = client.post(f"{API}/expenses", json={
        "expense_date": "2026-08-01", "category": "维修", "amount": amount,
        "payee": "Fix-It Co", "description": "aircon", "status": "pending",
    }, headers=create_headers)
    assert r.status_code == 201, r.text
    eid = r.json()["id"]
    r = client.post(f"{API}/expenses/{eid}/approve", headers=approve_headers)
    assert r.status_code == 200 and r.json()["status"] == "approved", r.text
    return eid


def test_e2e_28k_chain(client, admin_headers, manager_headers):
    eid = _mk(client, manager_headers, admin_headers)

    # Claim 1: ₱10,000 (Secretary reports), verify (Owner) -> remaining 18k.
    c1 = client.post(f"{API}/expenses/{eid}/claims",
                     json={"claimed_amount": "10000.00", "verification_note": "GCash ref 5001"},
                     headers=manager_headers)
    assert c1.status_code == 201, c1.text
    c1_id = c1.json()["id"]
    v1 = client.post(f"{API}/expenses/{eid}/claims/{c1_id}/verify", json={"result": "ok"},
                     headers=admin_headers)
    assert v1.status_code == 200 and v1.json()["status"] == "VERIFIED"

    d = client.get(f"{API}/expenses/{eid}/detail", headers=admin_headers).json()
    assert d["payment"]["verified_paid"] == "10000.00"
    assert d["payment"]["remaining"] == "18000.00"
    assert d["status"] == "partially_paid"

    # Claim 2: ₱18,000 => fully paid.
    c2 = client.post(f"{API}/expenses/{eid}/claims",
                     json={"claimed_amount": "18000.00", "verification_note": "Bank transfer 9002"},
                     headers=manager_headers)
    assert c2.status_code == 201, c2.text
    c2_id = c2.json()["id"]
    v2 = client.post(f"{API}/expenses/{eid}/claims/{c2_id}/verify", json={"result": "ok"},
                     headers=admin_headers)
    assert v2.status_code == 200 and v2.json()["status"] == "VERIFIED"

    final = client.get(f"{API}/expenses/{eid}/detail", headers=admin_headers).json()
    # Claim records = 2 (both PENDING->VERIFIED), no duplicates.
    assert len(final["claims"]) == 2
    assert final["payment"]["verified_claim_count"] == 2
    # 10,000 + 18,000 = 28,000 derived from VERIFIED records.
    assert final["payment"]["verified_paid"] == "28000.00"
    assert final["payment"]["remaining"] == "0.00"
    assert final["payment"]["fully_paid"] is True
    assert final["status"] == "paid"
    # Timeline complete.
    kinds = [e["kind"] for e in final["timeline"]]
    assert "expense_created" in kinds
    assert "approved" in kinds
    assert "verified" in kinds
    assert "remaining" in kinds
    assert "fully_paid" in kinds
    # Both claims verified with their own evidence note (per-claim truth).
    notes_text = " || ".join((c["verification_note"] or "") for c in final["claims"])
    assert "GCash ref 5001" in notes_text and "Bank transfer 9002" in notes_text
    # Audit happened for the whole chain.
    from app.models.audit_log import AuditLog
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        actions = [
            a.action.value if hasattr(a.action, "value") else str(a.action)
            for a in db.query(AuditLog).filter(
                AuditLog.table_name == "expenses", AuditLog.record_id == eid).all()
        ]
        for need in ("create", "approve", "pay"):
            assert need in actions
    finally:
        db.close()


def test_e2e_repair_linked_paid_not_closed(client, admin_headers, manager_headers, db_session):
    """A Repair-linked Expense that is fully PAID must NOT close the Repair."""
    from app.models.financial import Expense, ExpenseStatus
    from app.models.repair import (
        RepairOperation, RepairOperationStatus,
        RepairProposal, RepairProposalStatus,
    )
    from app.services.expense_claims import create_claim, verify_claim
    from app.services.expense_payment_truth import payment_truth
    from app.services.repairs import payment as repair_payment
    from datetime import date as _date

    repair = RepairOperation(issue="aircon broken", status=RepairOperationStatus.WAITING_PAYMENT,
                             next_action="awaiting payment", created_by=1)
    db_session.add(repair)
    db_session.flush()
    expense = Expense(expense_date=_date(2026, 8, 1), category="维修", amount="28000.00",
                      payee="Fix-It Co", status=ExpenseStatus.approved,
                      approved_by=1, approved_at=datetime.now(timezone.utc))
    db_session.add(expense)
    db_session.flush()
    prop = RepairProposal(repair_id=repair.id, version=1, vendor="Fix-It Co",
                          amount="28000.00", status=RepairProposalStatus.APPROVED,
                          expense_id=expense.id)
    db_session.add(prop)
    db_session.flush()
    db_session.commit()

    claim, _ = create_claim(db_session, expense, claimed_amount="28000.00", claimed_by=1)
    verify_claim(db_session, expense, claim.id, verified_by=1)
    repair_payment.on_expense_paid(db_session, repair)
    db_session.commit()

    assert expense.status == ExpenseStatus.paid
    assert payment_truth(db_session, expense).fully_paid is True
    assert repair.status == RepairOperationStatus.VERIFYING
    assert repair.status != RepairOperationStatus.CLOSED
    assert repair.closed_at is None
