"""PASAY-MILESTONE-002 — Rent Payment Claim Truth + Closure tests.

Mirror pattern of test_expense_003b_payment_truth.py (E1..E6):

  R1 — pending claim ≠ paid
  R2 — verified partial ≠ paid (partial still has remaining)
  R3 — verified full amount → fully_paid truth + RENT_DUE/RENT_OVERDUE tasks COMPLETED
  R4 — idempotency key dedupe: 30 replays → single claim, single verified amount
  R5 — over-claim mismatch FAILED, never auto-paid (aggregate unchanged)
  R6 — failed claim never touches aggregate
  R7 — reversed verified claim reopens aggregate + reopens COMPLETED task +
       legacy Income projection NO LONGER confirmed (amount/status/confirmed_*
       reflect zero verified truth).
  R8 — cross-org: owner_b cannot claim/verify against org_a's lease (404/409)
  R9 — period detail endpoint mirrors snapshot truth
  R10 — natural-month bucket lookup: 28/29/30/31 end-of-month dates correctly
        stay inside the same period bucket (anti-truncation 28-day bug)
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from app.models.financial import Income, IncomeStatus
from app.models.operations import (
    OperationalTask,
    OperationalTaskPriority,
    OperationalTaskStatus,
    OperationalTaskType,
)

API = "/api/v1"

_RENT_PERIOD = "2026-03"


def _period_detail(client, headers, lease_id, period=_RENT_PERIOD):
    r = client.get(
        f"{API}/incomes/leases/{lease_id}/periods/{period}", headers=headers
    )
    assert r.status_code == 200, r.text
    return r.json()


def _claim(client, headers, lease_id, period, amount, ik=None, evidence=None):
    payload = {
        "period": period,
        "claimed_amount": amount,
    }
    if ik is not None:
        payload["idempotency_key"] = ik
    if evidence is not None:
        payload["evidence_ids"] = evidence
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
    r = client.patch(
        f"{API}/incomes/claims/{claim_id}/verify",
        json=payload,
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _fail(client, headers, claim_id, reason="bad ref"):
    r = client.patch(
        f"{API}/incomes/claims/{claim_id}/fail",
        json={"reason": reason},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _reverse(client, headers, claim_id, reason="bounced check"):
    r = client.patch(
        f"{API}/incomes/claims/{claim_id}/reverse",
        json={"reason": reason},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _create_rent_task_db(db, unit_id, lease_id, period, kind="RENT_DUE"):
    """Insert a task directly into the DB. Using ORM bypasses API constraints
    (TaskCreateIn intentionally has no details field — the metadata column is
    only set by internal projection/creation paths)."""
    tt = (
        OperationalTaskType.RENT_DUE
        if kind == "RENT_DUE"
        else OperationalTaskType.RENT_OVERDUE
    )
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    t = OperationalTask(
        task_type=tt,
        title=f"{kind} {period}",
        status=OperationalTaskStatus.PENDING,
        dedupe_key=f"{kind}:{lease_id}:{period}",
        priority=OperationalTaskPriority.high,
        due_at=now + timedelta(days=3),
        source_type="rent_closure_test",
        metadata={
            "lease_id": lease_id,
            "unit_id": unit_id,
            "period": period,
        },
        created_by=None,
        updated_by=None,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _get_task_status_db(db, task_id):
    t = db.get(OperationalTask, task_id)
    assert t is not None
    return t.status


# R1: pending claim ≠ paid
def test_r1_pending_claim_not_paid(
    client, owner_a, org_a, property_id, unit_id, lease_id
):
    headers = {"Authorization": f"Bearer {owner_a[1]}"}
    _claim(client, headers, lease_id, _RENT_PERIOD, "6000.00")
    truth = _period_detail(client, headers, lease_id)["truth"]
    assert truth["verified_paid"] == "0.00"
    assert truth["remaining"] == "12000.00"
    assert truth["fully_paid"] is False
    assert truth["partially_paid"] is False
    assert truth["pending_claim_count"] == 1
    assert truth["pending_claimed_total"] == "6000.00"


# R2: verified partial → partially_paid with remaining, not fully paid
def test_r2_partial_verified_not_paid(
    client, owner_a, org_a, property_id, unit_id, lease_id
):
    headers = {"Authorization": f"Bearer {owner_a[1]}"}
    c = _claim(client, headers, lease_id, _RENT_PERIOD, "6000.00")
    _verify(client, headers, c["id"])
    truth = _period_detail(client, headers, lease_id)["truth"]
    assert truth["verified_paid"] == "6000.00"
    assert truth["remaining"] == "6000.00"
    assert truth["partially_paid"] is True
    assert truth["fully_paid"] is False


# R3: fully verified → fully_paid + COMPLETES RENT_DUE/RENT_OVERDUE tasks
def test_r3_full_paid_closes_task(
    client, owner_a, org_a, property_id, unit_id, lease_id, db_session
):
    headers = {"Authorization": f"Bearer {owner_a[1]}"}
    t_due = _create_rent_task_db(
        db_session, unit_id, lease_id, _RENT_PERIOD, "RENT_DUE"
    )
    t_overdue = _create_rent_task_db(
        db_session, unit_id, lease_id, _RENT_PERIOD, "RENT_OVERDUE"
    )
    assert _get_task_status_db(db_session, t_due.id) == OperationalTaskStatus.PENDING
    assert _get_task_status_db(db_session, t_overdue.id) == OperationalTaskStatus.PENDING
    c1 = _claim(client, headers, lease_id, _RENT_PERIOD, "7000.00")
    c2 = _claim(client, headers, lease_id, _RENT_PERIOD, "5000.00")
    _verify(client, headers, c1["id"])
    _verify(client, headers, c2["id"])
    truth = _period_detail(client, headers, lease_id)["truth"]
    assert truth["fully_paid"] is True
    assert truth["verified_paid"] == "12000.00"
    assert truth["remaining"] == "0.00"
    db_session.commit()
    assert _get_task_status_db(db_session, t_due.id) == OperationalTaskStatus.COMPLETED
    assert _get_task_status_db(db_session, t_overdue.id) == OperationalTaskStatus.COMPLETED


# R4: idempotency 30 replays → one row, one verified
def test_r4_idempotency_dedupe(
    client, owner_a, org_a, property_id, unit_id, lease_id
):
    headers = {"Authorization": f"Bearer {owner_a[1]}"}
    ik = "rent-m2-r4-once"
    first = None
    for i in range(30):
        c = _claim(
            client,
            headers,
            lease_id,
            _RENT_PERIOD,
            "12000.00",
            ik=ik,
        )
        if first is None:
            first = c
        else:
            assert c["id"] == first["id"], "idempotency dedupe failed at %d" % i
    claims = client.get(
        f"{API}/incomes/leases/{lease_id}/claims?period={_RENT_PERIOD}",
        headers=headers,
    ).json()
    assert len([c for c in claims if c["status"] == "PENDING"]) == 1
    _verify(client, headers, first["id"])
    truth = _period_detail(client, headers, lease_id)["truth"]
    assert truth["verified_claim_count"] == 1
    assert truth["verified_paid"] == "12000.00"
    assert truth["fully_paid"] is True


# R5: over-claim mismatch FAILED, aggregate untouched
def test_r5_overclaim_mismatch(
    client, owner_a, org_a, property_id, unit_id, lease_id
):
    headers = {"Authorization": f"Bearer {owner_a[1]}"}
    # Admit 7k first so remaining is 5k
    c1 = _claim(client, headers, lease_id, _RENT_PERIOD, "7000.00")
    _verify(client, headers, c1["id"])
    # Claim 10k (would exceed) → verify should FAILED with mismatch
    c2 = _claim(client, headers, lease_id, _RENT_PERIOD, "10000.00")
    r = _verify(client, headers, c2["id"])
    assert r["status"] == "FAILED"
    assert r["mismatch"] is True
    # aggregate stays at exactly 7,000
    truth = _period_detail(client, headers, lease_id)["truth"]
    assert truth["verified_paid"] == "7000.00"
    assert truth["remaining"] == "5000.00"
    assert truth["fully_paid"] is False


# R6: failed claim never touches aggregate
def test_r6_failed_claim_no_effect(
    client, owner_a, org_a, property_id, unit_id, lease_id
):
    headers = {"Authorization": f"Bearer {owner_a[1]}"}
    c = _claim(client, headers, lease_id, _RENT_PERIOD, "6000.00")
    _fail(client, headers, c["id"], reason="GCash ref not found")
    truth = _period_detail(client, headers, lease_id)["truth"]
    assert truth["verified_paid"] == "0.00"
    assert truth["remaining"] == "12000.00"
    assert truth["failed_claim_count"] == 1


# R7: reversed verified claim reopens aggregate + reopens COMPLETED task +
# legacy Income projection NO LONGER confirmed (amount=0 / status=pending /
# confirmed_by=None / confirmed_at=None)
def test_r7_reversed_claim_reopens(
    client, owner_a, org_a, property_id, unit_id, lease_id, db_session
):
    headers = {"Authorization": f"Bearer {owner_a[1]}"}
    t = _create_rent_task_db(
        db_session, unit_id, lease_id, _RENT_PERIOD, "RENT_DUE"
    )
    c = _claim(client, headers, lease_id, _RENT_PERIOD, "12000.00")
    _verify(client, headers, c["id"])
    db_session.commit()
    assert _get_task_status_db(db_session, t.id) == OperationalTaskStatus.COMPLETED
    truth1 = _period_detail(client, headers, lease_id)["truth"]
    assert truth1["fully_paid"] is True
    # After verify → Income projection must be fully confirmed (truth mirror).
    # We use exact same period bucket as `_ensure_income_matches_truth`:
    # received_date in [2026-03-01, 2026-04-01) on same lease_id.
    bucket_before = (
        db_session.query(Income)
        .filter(
            Income.lease_id == lease_id,
            Income.received_date >= date(2026, 3, 1),
            Income.received_date < date(2026, 4, 1),
        )
        .order_by(Income.id.asc())
        .first()
    )
    assert bucket_before is not None
    assert bucket_before.status == IncomeStatus.confirmed
    assert bucket_before.amount == Decimal("12000.00")
    assert bucket_before.confirmed_by is not None
    assert bucket_before.confirmed_at is not None
    # Now reverse (bounced check / clawback)
    _reverse(client, headers, c["id"])
    truth2 = _period_detail(client, headers, lease_id)["truth"]
    assert truth2["verified_paid"] == "0.00"
    assert truth2["fully_paid"] is False
    assert truth2["reversed_claim_count"] == 1
    # FIX1 — Income projection MUST reflect zero verified truth:
    # amount=0, status=pending, confirmed_by/confirmed_at cleared.
    db_session.commit()
    db_session.refresh(bucket_before)
    assert bucket_before.status == IncomeStatus.pending
    assert bucket_before.amount == Decimal("0.00")
    assert bucket_before.confirmed_by is None
    assert bucket_before.confirmed_at is None
    # COMPLETED task should be reopened to PENDING
    status_after = _get_task_status_db(db_session, t.id)
    assert status_after in (
        OperationalTaskStatus.PENDING,
        OperationalTaskStatus.IN_PROGRESS,
    ), f"expected reopened task, got status {status_after}"


# R8: cross-org fail-closed (owner_b vs org_a lease)
def test_r8_cross_org_fail_closed(
    client, owner_a, owner_b, org_a, org_b, property_id, unit_id, lease_id
):
    headers_a = {"Authorization": f"Bearer {owner_a[1]}"}
    headers_b = {"Authorization": f"Bearer {owner_b[1]}"}
    # Claim against org_a's lease using owner_b — 404 fail-closed
    r = client.post(
        f"{API}/incomes/leases/{lease_id}/claims",
        json={"period": _RENT_PERIOD, "claimed_amount": "12000.00"},
        headers=headers_b,
    )
    assert r.status_code == 404, r.text
    # Even if a claim existed, owner_b can't see it
    c_a = _claim(client, headers_a, lease_id, _RENT_PERIOD, "6000.00")
    r = client.get(f"{API}/incomes/claims/{c_a['id']}", headers=headers_b)
    assert r.status_code == 404, r.text
    # owner_b cannot verify via direct endpoint either
    r = client.patch(
        f"{API}/incomes/claims/{c_a['id']}/verify",
        json={"result": "stolen attempt"},
        headers=headers_b,
    )
    assert r.status_code == 404, r.text


# R9: period detail mirrors snapshot truth
def test_r9_period_detail_snapshot(
    client, owner_a, org_a, property_id, unit_id, lease_id
):
    headers = {"Authorization": f"Bearer {owner_a[1]}"}
    c1 = _claim(client, headers, lease_id, _RENT_PERIOD, "5000.00")
    c2 = _claim(client, headers, lease_id, _RENT_PERIOD, "4000.00")
    _verify(client, headers, c1["id"])
    # leave c2 pending
    d = _period_detail(client, headers, lease_id)
    truth = d["truth"]
    assert truth["required_amount"] == "12000.00"
    assert truth["verified_paid"] == "5000.00"
    assert truth["remaining"] == "7000.00"
    assert truth["pending_claim_count"] == 1
    assert truth["verified_claim_count"] == 1
    assert len(d["claims"]) == 2


# ---- R10: natural-month Income period bucket anti-truncation --------------
# The FIX1 bug was: old code used received_date < replace(day=28) for every
# non-December month, so any legacy Income row on 28/29/30/31 was missed and
# the truth projection built a NEW bucket while the old row remained stale
# (and appeared as "unaccounted income" in ledgers).
# We write a helper that seeds a legacy Income bucket with received_date at
# the LAST calendar day of four representative months, then run a verify so
# projection walks.  If the 28-day bug is present, the query will not locate
# these rows and will create a second bucket.  We assert that exactly 1
# bucket exists per period and that its (confirmed/amount) fields reflect the
# new truth (so the lookup genuinely covered the end-of-month date).

def _seed_bucket_and_verify(db_session, client, headers, lease_id, period,
                           last_day_date: date):
    """Create a legacy Income bucket at the tail of the month and then
    run a rent verify so projection must walk it.
    Returns the bucket count before + after, plus the bucket object.
    """
    # 1. Seed legacy Income (mimics a manual old entry created on the LAST
    # day of the month that previously would have been excluded by
    # replace(day=28) for 30-day months, leap-day FEB, 31-day months, etc.).
    pre = Income(
        lease_id=lease_id,
        amount=Decimal("999.99"),  # placeholder that SHOULD be overwritten
        received_date=last_day_date,
        status=IncomeStatus.pending,
        created_by=1,
        updated_by=1,
    )
    db_session.add(pre)
    db_session.flush()
    pre_id = pre.id
    db_session.commit()
    # 2. Claim + verify full rent
    c = _claim(client, headers, lease_id, period, "12000.00")
    _verify(client, headers, c["id"])
    db_session.commit()
    # 3. Count all Income rows for this lease + natural month.
    year_month = (last_day_date.year, last_day_date.month)
    if year_month[1] == 12:
        end_excl = date(year_month[0] + 1, 1, 1)
    else:
        end_excl = date(year_month[0], year_month[1] + 1, 1)
    start_inc = date(year_month[0], year_month[1], 1)
    rows = (
        db_session.query(Income)
        .filter(
            Income.lease_id == lease_id,
            Income.received_date >= start_inc,
            Income.received_date < end_excl,
        )
        .order_by(Income.id.asc())
        .all()
    )
    return pre_id, rows


def test_r10_period_bucket_feb28_non_leap(
    client, owner_a, org_a, property_id, unit_id, lease_id, db_session
):
    """FEB non-leap: last natural day = 28 (was included even by old bug,
    but check anyway for symmetry)."""
    headers = {"Authorization": f"Bearer {owner_a[1]}"}
    period = "2025-02"
    pre_id, rows = _seed_bucket_and_verify(db_session, client, headers, lease_id, period, date(2025, 2, 28))
    # Old bug (<28) would EXCLUDE Feb 28.  FIX1 uses <Mar 1, so included.
    assert len(rows) == 1, f"expected 1 bucket (old bug would have 2), got ids={[r.id for r in rows]}"
    assert rows[0].id == pre_id
    assert rows[0].status == IncomeStatus.confirmed
    assert rows[0].amount == Decimal("12000.00")
    assert rows[0].confirmed_by is not None
    assert rows[0].confirmed_at is not None


def test_r10_period_bucket_feb29_leap(
    client, owner_a, org_a, property_id, unit_id, lease_id, db_session
):
    """FEB leap: last natural day = 29.  Old bug (<28) would EXCLUDE it."""
    headers = {"Authorization": f"Bearer {owner_a[1]}"}
    period = "2024-02"
    pre_id, rows = _seed_bucket_and_verify(db_session, client, headers, lease_id, period, date(2024, 2, 29))
    assert len(rows) == 1, f"expected 1 bucket (old bug would have 2), got ids={[r.id for r in rows]}"
    assert rows[0].id == pre_id
    assert rows[0].status == IncomeStatus.confirmed
    assert rows[0].amount == Decimal("12000.00")


def test_r10_period_bucket_apr30(
    client, owner_a, org_a, property_id, unit_id, lease_id, db_session
):
    """APR: last natural day = 30.  Old bug (<28) would EXCLUDE it."""
    headers = {"Authorization": f"Bearer {owner_a[1]}"}
    period = "2026-04"
    pre_id, rows = _seed_bucket_and_verify(db_session, client, headers, lease_id, period, date(2026, 4, 30))
    assert len(rows) == 1, f"expected 1 bucket (old bug would have 2), got ids={[r.id for r in rows]}"
    assert rows[0].id == pre_id
    assert rows[0].status == IncomeStatus.confirmed
    assert rows[0].amount == Decimal("12000.00")


def test_r10_period_bucket_mar31(
    client, owner_a, org_a, property_id, unit_id, lease_id, db_session
):
    """MAR: last natural day = 31.  Old bug (<28) would EXCLUDE it."""
    headers = {"Authorization": f"Bearer {owner_a[1]}"}
    period = "2026-03"
    pre_id, rows = _seed_bucket_and_verify(db_session, client, headers, lease_id, period, date(2026, 3, 31))
    assert len(rows) == 1, f"expected 1 bucket (old bug would have 2), got ids={[r.id for r in rows]}"
    assert rows[0].id == pre_id
    assert rows[0].status == IncomeStatus.confirmed
    assert rows[0].amount == Decimal("12000.00")
