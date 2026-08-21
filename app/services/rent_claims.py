"""PASAY-MILESTONE-002 — Rent Payment Claim lifecycle.

Mirror pattern of ``app.services.expense_claims``: a claim is not a verified
payment, and a verified partial does not mean the lease period is paid.

State machine:
  PENDING  -- reported (tenant / secretary claims paid). Never enters the
              verified aggregate; only surfaces as "pending claim".
  VERIFIED -- real-world verification succeeded (bank slip / GCash ref /
              Owner confirmation of deposit). ``verified_amount`` enters
              the aggregate; over-claim mismatch is flagged but never
              auto-paid.
  FAILED   -- verification failed. Never enters the aggregate.
  REVERSED -- a previously VERIFIED claim was legitimately reversed
              (bounced check / clawback). Verified aggregate subtracts it.

Idempotency: deterministic ``idempotency_key`` with DB-level partial unique
index (see migration m2a000000001).

The associated Income row (legacy aggregate bucket) is ONLY populated after
verification, and its ``confirmed`` status flows from the verified-claim
aggregate rather than a manual flip (claim ≠ verified payment).
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.financial import Income, IncomeStatus
from app.models.lease import Lease
from app.models.operations import (
    OperationalTaskStatus,
    OperationalTaskType,
)
from app.models.rent_payment_claim import RentClaimStatus, RentPaymentClaim
from app.services.audit import record_audit, serialize_row
from app.services.rent_payment_truth import RentPeriodTruth, snapshot

_MONEY = Decimal("0.01")


class RentClaimError(Exception):
    """A claim transition was refused (preserved as 409 upstream)."""


def _d2(v: Decimal | None) -> Decimal:
    return Decimal("0.00") if v is None else Decimal(v).quantize(_MONEY)


def claim_idempotency_key(
    lease_id: int, period: str, actor: int | None, tag: str
) -> str:
    raw = f"rent:{lease_id}:{period}:claim:{actor or 'anon'}:{tag}"
    return "rentclaim:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_claim(
    db: Session,
    lease: Lease,
    period: str,
    *,
    claimed_amount: Decimal,
    claimed_by: int | None,
    received_date: date | None = None,
    verification_note: str | None = None,
    evidence_ids: list[int] | None = None,
    idempotency_key: str | None = None,
    income_id: int | None = None,
    now: datetime | None = None,
) -> tuple[RentPaymentClaim, bool]:
    """Create ONE PENDING rent claim idempotently. Returns (claim, created).

    A PENDING claim does NOT enter the verified-paid aggregate and does NOT
    auto-confirm any Income. It is purely a reported-payment projection.
    """
    amount = _d2(claimed_amount)
    if amount <= 0:
        raise RentClaimError("Claimed amount must be positive")
    if not period or len(period) != 7 or period[4] != "-":
        raise RentClaimError(
            "period must be YYYY-MM (e.g. 2026-01)"
        )
    now = now or datetime.now(timezone.utc)
    # FIX3: client-supplied idempotency keys are namespaced by
    # (lease_id, period) before they touch the DB.  This guarantees:
    #   * same client key on same lease+period → dedupe hit
    #   * same client key on different lease/org → independent names,
    #     no cross-lease collisions (and no "does this key exist"
    #     leak between leases / orgs)
    # If the caller does not pass a key we continue without one so
    # legitimate retries still get through the uniqueness constraints
    # on (status transitions + claim_id).
    raw_client_key = idempotency_key
    if raw_client_key:
        key = claim_idempotency_key(lease.id, period, claimed_by, raw_client_key)
    else:
        key = None
    if key:
        existing = (
            db.query(RentPaymentClaim)
            .filter(
                RentPaymentClaim.lease_id == lease.id,
                RentPaymentClaim.period == period,
                RentPaymentClaim.idempotency_key == key,
            )
            .first()
        )
        if existing is not None:
            return existing, False
    fields = {
        "lease_id": lease.id,
        "period": period,
        "income_id": income_id,
        "claimed_amount": amount,
        "claimed_by": claimed_by,
        "claimed_at": now,
        "received_date": (
            datetime(received_date.year, received_date.month, received_date.day, tzinfo=timezone.utc)
            if received_date is not None
            else None
        ),
        "status": RentClaimStatus.PENDING,
        "evidence_ids": list(evidence_ids) if evidence_ids else [],
        "verification_note": verification_note,
        "idempotency_key": key,
        "created_by": claimed_by,
        "updated_by": claimed_by,
    }
    stmt = (
        pg_insert(RentPaymentClaim)
        .values(**fields)
        .on_conflict_do_nothing(
            index_elements=["idempotency_key"],
            index_where=text("idempotency_key IS NOT NULL"),
        )
        .returning(RentPaymentClaim.id)
    )
    row = db.execute(stmt).first()
    if row is None:
        if key:
            claim = (
                db.query(RentPaymentClaim)
                .filter(
                    RentPaymentClaim.lease_id == lease.id,
                    RentPaymentClaim.period == period,
                    RentPaymentClaim.idempotency_key == key,
                )
                .first()
            )
            if claim is not None:
                return claim, False
        raise RentClaimError(
            "Rent payment claim was concurrently created but could not be resolved; retry"
        )
    claim = db.get(RentPaymentClaim, row[0])
    record_audit(
        db,
        table_name="rent_payment_claims",
        record_id=claim.id,
        action="rent_claim_created",
        actor_id=claimed_by,
        changed_fields={"status": [None, "PENDING"]},
        new_value=serialize_row(claim),
    )
    return claim, True


def _get_claim_or_error(db: Session, claim_id: int) -> RentPaymentClaim:
    claim = db.get(RentPaymentClaim, claim_id)
    if claim is None:
        raise RentClaimError("Rent payment claim not found")
    return claim


def _sync_period_tasks_on_verify(
    db: Session,
    lease: Lease,
    period: str,
    truth_after: RentPeriodTruth,
    *,
    actor_id: int | None,
    now: datetime,
) -> None:
    """After a claim moves to VERIFIED / REVERSED, sync the truth and:

    * If the period is now fully paid, complete any PENDING RENT_DUE or
      RENT_OVERDUE tasks whose dedupe_key matches this period.
    * If the period WAS fully paid and is now NOT fully paid (reversal
      subtracted enough to reopen), mark the task as PENDING again.
    """
    from app.models.operations import OperationalTask

    unit_id = lease.unit_id
    # RENT_DUE / RENT_OVERDUE tasks reference (unit, period) via details
    # JSONB and/or dedupe_key. We match by both the explicit dedupe_key
    # pattern and by details.period / details.lease_id as a fallback so
    # older tasks are still closed correctly.
    target_keys = {
        f"RENT_DUE:{lease.id}:{period}",
        f"RENT_OVERDUE:{lease.id}:{period}",
        f"RENT_DUE:{unit_id}:{period}" if unit_id else None,
        f"RENT_OVERDUE:{unit_id}:{period}" if unit_id else None,
    }
    target_keys.discard(None)
    task_types = (OperationalTaskType.RENT_DUE, OperationalTaskType.RENT_OVERDUE)
    tasks = (
        db.query(OperationalTask)
        .filter(
            OperationalTask.task_type.in_(task_types),
            OperationalTask.status.in_(
                (OperationalTaskStatus.PENDING, OperationalTaskStatus.IN_PROGRESS, OperationalTaskStatus.COMPLETED)
            ),
        )
        .all()
    )
    for t in tasks:
        matched = t.dedupe_key in target_keys
        if not matched and t.details:
            if (
                str(t.details.get("lease_id")) == str(lease.id)
                and t.details.get("period") == period
            ) or unit_id and (
                str(t.details.get("unit_id")) == str(unit_id)
                and t.details.get("period") == period
            ):
                matched = True
        if not matched:
            continue
        if truth_after.is_fully_paid:
            if t.status != OperationalTaskStatus.COMPLETED:
                old_status = t.status.value
                t.status = OperationalTaskStatus.COMPLETED
                t.completed_at = now
                t.completed_by = actor_id
                t.updated_by = actor_id
                t.updated_at = now
                record_audit(
                    db,
                    table_name="operational_tasks",
                    record_id=t.id,
                    action="task_auto_completed",
                    actor_id=actor_id,
                    changed_fields={
                        "status": [old_status, OperationalTaskStatus.COMPLETED.value]
                    },
                )
        else:
            # REVERSAL / partial-reopen path: if the period is NOT fully
            # paid any more and the task was COMPLETED, reopen it.
            if t.status == OperationalTaskStatus.COMPLETED:
                t.status = OperationalTaskStatus.PENDING
                t.completed_at = None
                t.completed_by = None
                t.updated_by = actor_id
                t.updated_at = now
                record_audit(
                    db,
                    table_name="operational_tasks",
                    record_id=t.id,
                    action="task_reopened",
                    actor_id=actor_id,
                    changed_fields={
                        "status": [OperationalTaskStatus.COMPLETED.value, OperationalTaskStatus.PENDING.value]
                    },
                )


def verify_claim(
    db: Session,
    lease: Lease,
    claim_id: int,
    *,
    verified_by: int | None,
    verified_amount: Decimal | None = None,
    result: str | None = None,
    now: datetime | None = None,
) -> RentPaymentClaim:
    """Verify a PENDING claim and reconcile the period's verified aggregate.

    OVER-CLAIM GUARD (Mismatch invariant): if admitting the verified
    amount would push the period's verified-total strictly ABOVE the
    required period rent, we mark the claim FAILED with mismatch=True.
    The owner must split or adjust manually — never auto-paid and never
    silently truncated.
    """
    claim = _get_claim_or_error(db, claim_id)
    if claim.lease_id != lease.id:
        raise RentClaimError("Claim does not belong to this lease")
    if claim.status == RentClaimStatus.VERIFIED:
        return claim
    if claim.status != RentClaimStatus.PENDING:
        raise RentClaimError(
            f"Only PENDING claims can be verified (claim is {claim.status})"
        )
    now = now or datetime.now(timezone.utc)
    admitted = (
        _d2(verified_amount)
        if verified_amount is not None
        else _d2(claim.claimed_amount)
    )
    truth_before = snapshot(db, lease.id, claim.period)
    new_total = truth_before.verified_paid_total + admitted
    required = truth_before.required_amount

    if required > 0 and new_total > required + _MONEY:
        # Strict over-claim beyond a 1c rounding tolerance → FAILED + mismatch.
        # FIX5: capture pre-mutation snapshot BEFORE setting status/mismatch/verified_amount.
        old_snap = serialize_row(claim)
        pre_status = str(claim.status.value if hasattr(claim.status, "value") else claim.status)
        pre_mismatch = bool(claim.mismatch)
        pre_verified_amount = (
            str(claim.verified_amount)
            if claim.verified_amount is not None
            else None
        )
        claim.status = RentClaimStatus.FAILED
        claim.mismatch = True
        claim.mismatch_reason = (
            f"Admitting {admitted} would make verified-paid {new_total} "
            f"exceed period requirement {required}; over-payment "
            "requires manual resolution."
        )
        claim.failure_reason = "OVERCLAIM_MISMATCH"
        claim.verified_amount = None
        if result:
            claim.verification_note = (
                f"{claim.verification_note} · {result}"
                if claim.verification_note
                else result
            )
        claim.updated_by = verified_by
        claim.updated_at = now
        db.flush()
        record_audit(
            db,
            table_name="rent_payment_claims",
            record_id=claim.id,
            action="rent_amount_mismatch",
            actor_id=verified_by,
            changed_fields={
                "status": [pre_status, "FAILED"],
                "mismatch": [pre_mismatch, True],
                "verified_amount": [pre_verified_amount, None],
            },
            old_value=old_snap,
            new_value=serialize_row(claim),
        )
        return claim

    old_snap = serialize_row(claim)
    pre_status = str(claim.status.value if hasattr(claim.status, "value") else claim.status)
    pre_verified_amount = (
        str(claim.verified_amount)
        if claim.verified_amount is not None
        else None
    )
    claim.status = RentClaimStatus.VERIFIED
    claim.verified_amount = admitted
    claim.verified_by = verified_by
    claim.verified_at = now
    if result:
        claim.verification_note = (
            f"{claim.verification_note} · {result}"
            if claim.verification_note
            else result
        )
    claim.updated_by = verified_by
    claim.updated_at = now
    db.flush()
    record_audit(
        db,
        table_name="rent_payment_claims",
        record_id=claim.id,
        action="rent_claim_verified",
        actor_id=verified_by,
        changed_fields={
            "status": [pre_status, "VERIFIED"],
            "verified_amount": [pre_verified_amount, str(admitted)],
        },
        old_value=old_snap,
        new_value=serialize_row(claim),
    )
    truth_after = snapshot(db, lease.id, claim.period)
    _sync_period_tasks_on_verify(
        db, lease, claim.period, truth_after, actor_id=verified_by, now=now
    )
    _ensure_income_matches_truth(
        db, lease, claim.period, truth_after, actor_id=verified_by, now=now
    )
    return claim


def fail_claim(
    db: Session,
    lease: Lease,
    claim_id: int,
    *,
    failed_by: int | None,
    reason: str,
    now: datetime | None = None,
) -> RentPaymentClaim:
    claim = _get_claim_or_error(db, claim_id)
    if claim.lease_id != lease.id:
        raise RentClaimError("Claim does not belong to this lease")
    if claim.status != RentClaimStatus.PENDING:
        raise RentClaimError(
            f"Only PENDING claims can be failed (claim is {claim.status})"
        )
    now = now or datetime.now(timezone.utc)
    old_snap = serialize_row(claim)
    pre_status = (
        str(claim.status.value)
        if hasattr(claim.status, "value")
        else str(claim.status)
    )
    claim.status = RentClaimStatus.FAILED
    claim.failure_reason = reason
    claim.updated_by = failed_by
    claim.updated_at = now
    db.flush()
    record_audit(
        db,
        table_name="rent_payment_claims",
        record_id=claim.id,
        action="rent_claim_failed",
        actor_id=failed_by,
        changed_fields={"status": [pre_status, "FAILED"]},
        old_value=old_snap,
        new_value=serialize_row(claim),
    )
    return claim


def reverse_claim(
    db: Session,
    lease: Lease,
    claim_id: int,
    *,
    reversed_by: int | None,
    reason: str,
    now: datetime | None = None,
) -> RentPaymentClaim:
    """Reverse a VERIFIED claim (bounced check / clawback).

    The aggregate is reduced via the snapshot helper: REVERSED rows simply
    increment reversed_n (verified_sum is derived exclusively from VERIFIED
    rows); OperationalTasks for the period are reopened (if they were
    COMPLETED and the period is no longer fully paid).
    """
    claim = _get_claim_or_error(db, claim_id)
    if claim.lease_id != lease.id:
        raise RentClaimError("Claim does not belong to this lease")
    if claim.status != RentClaimStatus.VERIFIED:
        raise RentClaimError(
            f"Only VERIFIED claims can be reversed (claim is {claim.status})"
        )
    now = now or datetime.now(timezone.utc)
    old_snap = serialize_row(claim)
    pre_status = (
        str(claim.status.value)
        if hasattr(claim.status, "value")
        else str(claim.status)
    )
    pre_verified_amount_str = (
        str(claim.verified_amount)
        if claim.verified_amount is not None
        else None
    )
    claim.status = RentClaimStatus.REVERSED
    claim.failure_reason = reason
    claim.verified_amount = None
    claim.updated_by = reversed_by
    claim.updated_at = now
    db.flush()
    record_audit(
        db,
        table_name="rent_payment_claims",
        record_id=claim.id,
        action="rent_claim_reversed",
        actor_id=reversed_by,
        changed_fields={
            "status": [pre_status, "REVERSED"],
            "verified_amount": [pre_verified_amount_str, None],
        },
        old_value=old_snap,
        new_value=serialize_row(claim),
    )
    truth_after = snapshot(db, lease.id, claim.period)
    _sync_period_tasks_on_verify(
        db, lease, claim.period, truth_after, actor_id=reversed_by, now=now
    )
    _ensure_income_matches_truth(
        db, lease, claim.period, truth_after, actor_id=reversed_by, now=now
    )
    return claim


def list_claims(
    db: Session, lease_id: int, *, period: str | None = None
) -> list[RentPaymentClaim]:
    q = db.query(RentPaymentClaim).filter(
        RentPaymentClaim.lease_id == lease_id
    )
    if period:
        q = q.filter(RentPaymentClaim.period == period)
    return q.order_by(
        RentPaymentClaim.period.asc(),
        RentPaymentClaim.claimed_at.asc(),
        RentPaymentClaim.id.asc(),
    ).all()


def _ensure_income_matches_truth(
    db: Session,
    lease: Lease,
    period: str,
    truth: RentPeriodTruth,
    *,
    actor_id: int | None,
    now: datetime,
) -> None:
    """Projection-only: keep legacy Income rows consistent with the new
    verified-claim truth.

    ``Income`` is NOT the authoritative record of a paid period — the
    verified claims are. But for backward compatibility with the existing
    rent_math helper and financial ledger queries we keep a mirrored
    ``Income`` row whose ``confirmed_by`` / ``confirmed_at`` / ``status``
    reflects the aggregate truth:

    * verified_paid_total == 0            → no Income (or a pending one is
                                           marked NOT confirmed).
    * verified_paid_total in (0, required) → status=confirmed with
                                           amount=verified_paid_total;
                                           NOT marked as fully-paid (caller
                                           still sees remaining via the
                                           snapshot helper).
    * verified_paid_total >= required     → status=confirmed amount=required
                                           (any overpaid remains in snapshot
                                           overpaid field).
    """
    from app.services.audit import record_audit as audit2

    period_date_start = datetime(
        int(period[:4]), int(period[5:7]), 1, tzinfo=timezone.utc
    ).date()
    # Locate the existing Income row(s) for this lease and period.
    # FIX1 — Correct natural-month boundary (prev: day=28 truncation which
    # missed the 29/30/31 tail). Always compute end_of_month date:
    #   period_next = (start + 32 days).replace(day=1)
    #   end_inclusive_lower_bound_exclusive = period_next
    _year = period_date_start.year
    _month = period_date_start.month
    if _month == 12:
        period_date_end_excl = date(_year + 1, 1, 1)
    else:
        period_date_end_excl = date(_year, _month + 1, 1)
    existing = (
        db.query(Income)
        .filter(
            Income.lease_id == lease.id,
            Income.received_date >= period_date_start,
            Income.received_date < period_date_end_excl,
        )
        .order_by(Income.id.asc())
        .all()
    )
    # Pick the earliest (lowest id) as the canonical projection bucket;
    # extra rows are left untouched (they represent legacy manual entries
    # the user may still want to inspect).
    bucket: Income | None = None
    for inc in existing:
        if bucket is None:
            bucket = inc
        else:
            continue
    # FIX1 — Strict truth reflection:
    #   verified_paid_total == 0 → Income status = pending (NOT confirmed)
    #                            → amount = 0
    #                            → confirmed_by = confirmed_at = None
    #   verified_paid_total in (0, required) → status = confirmed,
    #                            amount = verified_paid_total
    #   verified_paid_total >= required → status = confirmed,
    #                            amount = required (cap)
    target_status: IncomeStatus
    target_amount: Decimal
    if truth.verified_paid_total <= 0:
        target_status = IncomeStatus.pending
        target_amount = Decimal(0)
    elif truth.is_fully_paid:
        target_status = IncomeStatus.confirmed
        target_amount = truth.required_amount
    else:
        target_status = IncomeStatus.confirmed
        target_amount = truth.verified_paid_total
    if bucket is None and truth.verified_paid_total <= 0:
        # No projection needed when there is nothing paid and no legacy bucket.
        return
    if bucket is None:
        bucket = Income(
            lease_id=lease.id,
            amount=target_amount,
            received_date=period_date_start,
            status=target_status,
            created_by=actor_id,
            updated_by=actor_id,
        )
        if target_status == IncomeStatus.confirmed:
            bucket.confirmed_by = actor_id
            bucket.confirmed_at = now
        db.add(bucket)
        db.flush()
    else:
        changed = {}
        if bucket.amount != target_amount:
            changed["amount"] = [str(bucket.amount), str(target_amount)]
            bucket.amount = target_amount
        if bucket.status != target_status:
            changed["status"] = [bucket.status.value, target_status.value]
            bucket.status = target_status
        # FIX1 — confirmed_by/confirmed_at always align with target_status:
        # never keep confirmed_* populated when target_status is not confirmed.
        if target_status == IncomeStatus.confirmed:
            if bucket.confirmed_at is None:
                bucket.confirmed_by = actor_id
                bucket.confirmed_at = now
                changed["confirmed_at"] = [None, now.isoformat()]
                changed["confirmed_by"] = [None, str(actor_id)]
            else:
                # Still update actor_id/date if the entry was stale, but only
                # audit if they actually changed.
                if bucket.confirmed_by != actor_id:
                    old = None if bucket.confirmed_by is None else str(bucket.confirmed_by)
                    bucket.confirmed_by = actor_id
                    changed["confirmed_by"] = [old, str(actor_id)]
        else:  # target is PENDING (verified_paid == 0) → must NOT be confirmed
            if bucket.confirmed_at is not None or bucket.confirmed_by is not None:
                changed["confirmed_at"] = [
                    bucket.confirmed_at.isoformat() if bucket.confirmed_at else None,
                    None,
                ]
                changed["confirmed_by"] = [
                    None if bucket.confirmed_by is None else str(bucket.confirmed_by),
                    None,
                ]
                bucket.confirmed_by = None
                bucket.confirmed_at = None
        bucket.updated_by = actor_id
        bucket.updated_at = now
        if changed:
            audit2(
                db,
                table_name="incomes",
                record_id=bucket.id,
                action="income_truth_projected",
                actor_id=actor_id,
                changed_fields=changed,
                old_value=serialize_row(bucket),
                new_value=serialize_row(bucket),
            )
