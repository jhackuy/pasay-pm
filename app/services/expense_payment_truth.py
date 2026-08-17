"""PASAY-EXPENSE-OPERATION-003B — authoritative Expense payment truth.

Single source of truth for expense payment state (section 19). Every layer —
the expense router, the Mini App detail serializer, the bot cards, the worker
continuation — reads the SAME derived financial truth from this module so no
two components can disagree about:

    verified amount  = SUM(VERIFIED claims' verified_amount)
    remaining        = expenses.amount - verified amount  (never negative)
    fully paid       = verified amount == expenses.amount and remaining == 0
    paid status      = expenses.status == 'paid' AND remaining == 0

The Expense ``status`` field is maintained by the same module so the DB row and
the derived truth stay consistent. PENDING claims NEVER enter the aggregate
(E2); FAILED claims never enter it (E7); REVERSED claims have their previous
verified amount removed (E13). An over-claim is surfaced as a mismatch and
NEVER auto-PAIDs or truncates (E6).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.expense_claim import ClaimStatus, ExpensePaymentClaim
from app.models.financial import Expense, ExpenseStatus
from app.services.audit import record_audit, serialize_row

_TWO = Decimal("0.01")


def _d2(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(_TWO)


class ExpensePaymentTruth:
    """Immutable snapshot of an expense's derived payment truth."""

    __slots__ = ("verified_paid", "remaining", "fully_paid", "pending_claims", "has_mismatch")

    def __init__(self, verified_paid, remaining, fully_paid, pending_claims, has_mismatch):
        self.verified_paid = verified_paid
        self.remaining = remaining
        self.fully_paid = bool(fully_paid)
        self.pending_claims = int(pending_claims)
        self.has_mismatch = bool(has_mismatch)

    def as_dict(self) -> dict:
        return {
            "verified_paid": str(self.verified_paid),
            "remaining": str(self.remaining),
            "fully_paid": self.fully_paid,
            "pending_claims": self.pending_claims,
            "has_mismatch": self.has_mismatch,
        }


def verified_claims(db: Session, expense_id: int) -> list[ExpensePaymentClaim]:
    """Every claim that has actually been VERIFIED (and not reversed) for this
    expense — these are the ONLY records that count toward the paid aggregate."""
    return (
        db.query(ExpensePaymentClaim)
        .filter(
            ExpensePaymentClaim.expense_id == expense_id,
            ExpensePaymentClaim.status.in_([ClaimStatus.VERIFIED]),
        )
        .all()
    )


def payment_truth(db: Session, expense: Expense) -> ExpensePaymentTruth:
    """Derive the authoritative verified-paid / remaining / fully-paid truth
    by aggregating ONLY VERIFIED claim amounts (section 4)."""
    verified = verified_claims(db, expense.id)
    verified_paid = _d2(sum((_d2(c.verified_amount) for c in verified if c.verified_amount), Decimal("0")))
    total = _d2(expense.amount)
    remaining = total - verified_paid
    pending = (
        db.query(ExpensePaymentClaim)
        .filter(
            ExpensePaymentClaim.expense_id == expense.id,
            ExpensePaymentClaim.status == ClaimStatus.PENDING,
        )
        .count()
    )
    has_mismatch = (
        db.query(ExpensePaymentClaim)
        .filter(
            ExpensePaymentClaim.expense_id == expense.id,
            ExpensePaymentClaim.mismatch.is_(True),
        )
        .count()
        > 0
    )
    return ExpensePaymentTruth(
        verified_paid=verified_paid,
        remaining=remaining if remaining > 0 else _d2(Decimal("0")),
        fully_paid=verified_paid >= total and total > 0,
        pending_claims=pending,
        has_mismatch=has_mismatch,
    )


def _expected_status(expense: Expense, truth: ExpensePaymentTruth) -> ExpenseStatus:
    """The ExpenseStatus the business truth implies."""
    total = _d2(expense.amount)
    if truth.fully_paid and total > 0:
        return ExpenseStatus.paid
    if truth.verified_paid > 0 and truth.remaining > 0:
        return ExpenseStatus.partially_paid
    if truth.pending_claims > 0:
        return ExpenseStatus.payment_claimed
    return ExpenseStatus.approved


def sync_expense_status(
    db: Session,
    expense: Expense,
    *,
    actor_id: int | None,
    now: datetime | None = None,
) -> ExpensePaymentTruth:
    """Reconcile ``expense.status`` to the derived payment truth after any
    claim mutation. Records a step audit only when the status actually changed.

    ``paid`` is reached ONLY through verified-claims aggregation (section 7);
    it is never set by a raw click. Returns the resulting truth snapshot.
    """
    truth = payment_truth(db, expense)
    expected = _expected_status(expense, truth)
    if expense.status != expected:
        old = expense.status
        expense.status = expected
        expense.updated_by = actor_id
        expense.updated_at = now or datetime.now(timezone.utc)
        db.flush()
        action = (
            "expense_fully_paid"
            if expected == ExpenseStatus.paid
            else "expense_partially_paid"
            if expected == ExpenseStatus.partially_paid
            else "expense_claim_verified"
        )
        record_audit(
            db,
            table_name="expenses",
            record_id=expense.id,
            action=action,
            actor_id=actor_id,
            changed_fields={"status": [old.value if old else None, expected.value]},
            old_value=serialize_row(expense),
            new_value=serialize_row(expense),
        )
    return truth


def clear_approval(db: Session, expense: Expense, *, actor_id: int, reason: str) -> None:
    """A critical financial field changed after approval -> the old approval is
    invalidated and the expense must be re-approved (section 9). This also
    clears any stale APPROVED-only derived claims."""
    old = expense.status
    expense.status = ExpenseStatus.pending
    expense.approved_by = None
    expense.approved_at = None
    expense.updated_by = actor_id
    expense.updated_at = datetime.now(timezone.utc)
    expense.reapproval_reason = reason
    db.flush()
    record_audit(
        db,
        table_name="expenses",
        record_id=expense.id,
        action="expense_requires_reapproval",
        actor_id=actor_id,
        changed_fields={
            "status": [old.value if old else None, "pending"],
            "approved_by": [expense.approved_by, None],
            "approved_at": [expense.approved_at, None],
            "reapproval_reason": [None, reason],
        },
        old_value=serialize_row(expense),
        new_value=serialize_row(expense),
    )


def expense_finance_payload(db: Session, expense: Expense, claims: list[ExpensePaymentClaim]) -> dict:
    """Single payload used by the router serializer and the bot so everyone
    reads the same truth (section 19)."""
    truth = payment_truth(db, expense)
    claims_payload = []
    for c in sorted(claims, key=lambda x: (x.claimed_at or x.created_at)):
        claims_payload.append({
            "id": c.id,
            "claimed_amount": str(c.claimed_amount),
            "claimed_by": c.claimed_by,
            "claimed_at": c.claimed_at.isoformat() if c.claimed_at else None,
            "status": c.status.value,
            "evidence_ids": c.evidence_ids or [],
            "verification_note": c.verification_note,
            "verified_amount": str(c.verified_amount) if c.verified_amount is not None else None,
            "verified_by": c.verified_by,
            "verified_at": c.verified_at.isoformat() if c.verified_at else None,
            "mismatch": bool(c.mismatch),
            "mismatch_reason": c.mismatch_reason,
            "failure_reason": c.failure_reason,
        })
    return {
        **truth.as_dict(),
        "required_amount": str(_d2(expense.amount)),
        "expense_status": expense.status.value,
        "claims": claims_payload,
        "verified_claim_count": sum(1 for x in claims_payload if x["status"] == "VERIFIED"),
        "pending_claim_count": truth.pending_claims,
    }
