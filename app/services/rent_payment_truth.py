"""PASAY-MILESTONE-002 — Rent Payment Truth.

Mirror pattern of ``app.services.expense_payment_truth``: a claim is not a
verified payment, and verified partial does not mean the period is fully
paid.  The authoritative aggregate for a lease period comes from the
VERIFIED claims, *not* from:

* the status of pending claims (claim ≠ paid),
* any ``incomes.status=confirmed`` rows that were never associated with a
  verified claim,
* OperationalTask status (which is only the human-projection projection).

Canonical invariants enforced here:
    1. PENDING claims never enter the aggregate.
    2. FAILED claims never enter the aggregate.
    3. REVERSED claims that were previously VERIFIED are *subtracted* from
       the aggregate via the ``verified_amount`` column.
    4. Claim-level ``verified_amount`` is what aggregates (not
       ``claimed_amount``) so an over-claim with mismatch can be recorded
       without polluting the period's true paid amount.
    5. A period's ``paid < required`` is PARTIAL, ``paid == required`` is
       FULL, ``paid > required`` is OVER but still FULL (with an overage
       flag).
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import and_, select
from sqlalchemy.orm import Session, noload

from app.models.lease import Lease
from app.models.rent_payment_claim import RentClaimStatus, RentPaymentClaim


@dataclass(frozen=True)
class RentPeriodTruth:
    """Authoritative snapshot of a lease period's claim/verification state."""

    lease_id: int
    period: str  # YYYY-MM
    required_amount: Decimal
    pending_claim_count: int = 0
    verified_claim_count: int = 0
    failed_claim_count: int = 0
    reversed_claim_count: int = 0
    pending_claimed_total: Decimal = Decimal("0.00")
    verified_paid_total: Decimal = Decimal("0.00")
    has_mismatch: bool = False
    overclaimed_total: Decimal = Decimal("0.00")

    @property
    def remaining(self) -> Decimal:
        delta = self.required_amount - self.verified_paid_total
        if delta < 0:
            return Decimal("0.00")
        return delta.quantize(Decimal("0.01"))

    @property
    def overpaid(self) -> Decimal:
        delta = self.verified_paid_total - self.required_amount
        if delta <= 0:
            return Decimal("0.00")
        return delta.quantize(Decimal("0.01"))

    @property
    def is_partially_paid(self) -> bool:
        return Decimal(0) < self.verified_paid_total < self.required_amount

    @property
    def is_fully_paid(self) -> bool:
        return (
            self.required_amount > 0
            and self.verified_paid_total >= self.required_amount
        )

    @property
    def has_pending_claims(self) -> bool:
        return self.pending_claim_count > 0


def _zero(d: Decimal | None) -> Decimal:
    return Decimal("0.00") if d is None else Decimal(d).quantize(Decimal("0.01"))


def _q(d: Decimal) -> Decimal:
    return Decimal(d).quantize(Decimal("0.01"))


def snapshot(
    db: Session,
    lease_id: int,
    period: str,
    claims: Iterable[RentPaymentClaim] | None = None,
) -> RentPeriodTruth:
    """Aggregate verified-claim totals for one lease period.

    If ``claims`` is provided the aggregate is built in-process (cheaper for
    endpoints that already loaded them); otherwise the DB is queried with
    the same filter used by ``sync_rent_period_income_status`` to keep the
    two paths numerically identical.
    """
    if claims is None:
        rows = db.execute(
            select(RentPaymentClaim)
            .options(noload("*"))
            .where(
                and_(
                    RentPaymentClaim.lease_id == lease_id,
                    RentPaymentClaim.period == period,
                )
            )
        ).scalars()
    else:
        rows = [
            c
            for c in claims
            if c.lease_id == lease_id and c.period == period
        ]

    pending_n = 0
    verified_n = 0
    failed_n = 0
    reversed_n = 0
    pending_sum = Decimal("0.00")
    verified_sum = Decimal("0.00")
    mismatch = False
    overclaimed_total = Decimal("0.00")

    for c in rows:
        status = c.status
        if isinstance(status, RentClaimStatus):
            s = status.value
        else:
            s = str(status)
        if s == RentClaimStatus.PENDING.value:
            pending_n += 1
            pending_sum += _zero(c.claimed_amount)
        elif s == RentClaimStatus.VERIFIED.value:
            verified_n += 1
            verified_sum += _zero(c.verified_amount)
            if c.mismatch:
                mismatch = True
            claimed = _zero(c.claimed_amount)
            verified = _zero(c.verified_amount)
            if claimed > verified:
                overclaimed_total += claimed - verified
        elif s == RentClaimStatus.FAILED.value:
            failed_n += 1
        elif s == RentClaimStatus.REVERSED.value:
            reversed_n += 1
            # REVERSED claims *remove* previously counted verified amounts.
            # The service-level ``verify`` path already sets verified_amount
            # on reversal to -|original|; if the caller left it as a positive
            # number we subtract explicitly rather than rely on convention.
            amt = _zero(c.verified_amount)
            if amt < 0:
                verified_sum += amt
            else:
                verified_sum -= amt

    lease = db.get(Lease, lease_id)
    if lease is None or lease.monthly_rent is None:
        required = Decimal("0.00")
    else:
        required = _q(lease.monthly_rent)

    return RentPeriodTruth(
        lease_id=lease_id,
        period=period,
        required_amount=required,
        pending_claim_count=pending_n,
        verified_claim_count=verified_n,
        failed_claim_count=failed_n,
        reversed_claim_count=reversed_n,
        pending_claimed_total=_q(pending_sum),
        verified_paid_total=_q(verified_sum),
        has_mismatch=mismatch,
        overclaimed_total=_q(overclaimed_total),
    )


def _claim_status_for_aggregate(claim: RentPaymentClaim) -> str:
    s = claim.status
    if isinstance(s, RentClaimStatus):
        return s.value
    return str(s)


def claim_counts_for_lease_periods(
    db: Session,
    lease_id: int,
    periods: list[str],
) -> dict[str, RentPeriodTruth]:
    """Bulk snapshot helper for listing views.

    Builds a per-period snapshot in one query plus in-memory bucketing.
    """
    if not periods:
        return {}
    rows = db.execute(
        select(RentPaymentClaim)
        .options(noload("*"))
        .where(
            and_(
                RentPaymentClaim.lease_id == lease_id,
                RentPaymentClaim.period.in_(periods),
            )
        )
    ).scalars()
    buckets: dict[str, list[RentPaymentClaim]] = {p: [] for p in periods}
    for r in rows:
        buckets.setdefault(r.period, []).append(r)
    return {
        p: snapshot(db, lease_id, p, claims=buckets[p])
        for p in periods
    }
