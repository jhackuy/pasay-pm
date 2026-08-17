"""PASAY-EXPENSE-OPERATION-003B — Expense Payment Claim lifecycle.

Service that owns the claim state machine and the expense payment truth it
feeds:

  PENDING -- created (idempotent); payment reported only, NOT verified
  VERIFIED -- only a successful real-world verification moves the claimed
              amount into the verified aggregate (E3/E4). Amount mismatch
              (over-claim) is flagged, never auto-PAID/truncated (E6).
  FAILED  -- verification failed; never enters the aggregate (E7).
  REVERSED -- a previously VERIFIED claim is legitimately reversed; its
              verified amount leaves the aggregate and remaining recomputes
              (E13). The reversal record is a NEW mutation on the SAME row
              (audit + status flip); history is preserved, never deleted.

Idempotency (section 6 / E5): claim creation uses a deterministic
``idempotency_key`` guarded by a DB unique index, so replaying the same claim
(Telegram callback retry / API retry / browser refresh / worker retry / double
click) returns the existing PENDING row instead of double-counting.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.expense_claim import ClaimStatus, ExpensePaymentClaim
from app.models.financial import Expense, ExpenseStatus
from app.services.audit import record_audit, serialize_row
from app.services.expense_payment_truth import _d2, payment_truth, sync_expense_status

_MONEY = Decimal("0.01")


class ClaimError(Exception):
    """A claim transition was refused (preserved as a 409 upstream)."""


def claim_idempotency_key(expense_id: int, actor: int | None, tag: str) -> str:
    """Deterministic DB-level key for one logical claim submission.

    ``tag`` separates genuinely-distinct payments from replays of the SAME one
    (e.g. the Secretary's partial-payment tag vs the Owner-complete-remaining
    tag). Repeating the same (expense, actor, tag) is the SAME logical claim and
    is deduped by the DB unique index (section 6 / E5).
    """
    raw = f"expense:{expense_id}:claim:{actor or 'anon'}:{tag}"
    return "claim:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_claim(
    db: Session,
    expense: Expense,
    *,
    claimed_amount: Decimal,
    claimed_by: int | None,
    verification_note: str | None = None,
    evidence_ids: list | None = None,
    idempotency_key: str | None = None,
    now: datetime | None = None,
) -> tuple[ExpensePaymentClaim, bool]:
    """Create ONE PENDING payment claim (idempotent, concurrency-safe).
    Returns ``(claim, created)``; ``created`` is False when the same logical
    claim already exists (replay/race), so the caller must never double-count.

    The insert uses ``ON CONFLICT DO NOTHING`` on the unique idempotency_key
    index, so concurrent replays of the same (expense, actor, tag) converge to
    ONE row instead of double-counting or erroring (section 6 / E5).

    Cannot claim on an already fully-paid / reversed / rejected expense — there
    is nothing left to pay.
    """
    if expense.status in (ExpenseStatus.paid, ExpenseStatus.reversed):
        raise ClaimError(
            f"Expense is already {expense.status.value}; nothing more to claim"
        )
    if expense.status == ExpenseStatus.rejected:
        raise ClaimError("Cannot claim payment on a rejected expense")
    amount = _d2(claimed_amount)
    if amount <= 0:
        raise ClaimError("Claimed amount must be positive")
    now = now or datetime.now(timezone.utc)
    key = idempotency_key
    if key:
        existing = (
            db.query(ExpensePaymentClaim)
            .filter(
                ExpensePaymentClaim.expense_id == expense.id,
                ExpensePaymentClaim.idempotency_key == key,
            )
            .first()
        )
        if existing is not None:
            return existing, False
    fields = {
        "expense_id": expense.id,
        "claimed_amount": amount,
        "claimed_by": claimed_by,
        "claimed_at": now,
        "status": ClaimStatus.PENDING,
        "evidence_ids": evidence_ids or [],
        "verification_note": verification_note,
        "idempotency_key": key,
        "created_by": claimed_by,
        "updated_by": claimed_by,
    }
    stmt = (
        pg_insert(ExpensePaymentClaim)
        .values(**fields)
        .on_conflict_do_nothing(
            index_elements=["idempotency_key"],
            index_where=text("idempotency_key IS NOT NULL"),
        )
        .returning(ExpensePaymentClaim.id)
    )
    row = db.execute(stmt).first()
    if row is None:
        # A concurrent caller won the insert; return the same-key claim so the
        # caller can proceed without double-counting (no error).
        if key:
            claim = (
                db.query(ExpensePaymentClaim)
                .filter(
                    ExpensePaymentClaim.expense_id == expense.id,
                    ExpensePaymentClaim.idempotency_key == key,
                )
                .first()
            )
            if claim is not None:
                return claim, False
        raise ClaimError("Payment claim was concurrently created but could not be resolved; retry")
    claim = db.get(ExpensePaymentClaim, row[0])
    record_audit(
        db,
        table_name="expense_payment_claims",
        record_id=claim.id,
        action="expense_claim_created",
        actor_id=claimed_by,
        changed_fields={"status": [None, "PENDING"]},
        new_value=serialize_row(claim),
    )
    # A PENDING claim moves the expense to payment_claimed (never paid).
    sync_expense_status(db, expense, actor_id=claimed_by, now=now)
    return claim, True


def _get_claim_or_error(db: Session, claim_id: int) -> ExpensePaymentClaim:
    claim = db.get(ExpensePaymentClaim, claim_id)
    if claim is None:
        raise ClaimError("Payment claim not found")
    return claim


def verify_claim(
    db: Session,
    expense: Expense,
    claim_id: int,
    *,
    verified_by: int | None,
    verified_amount: Decimal | None = None,
    result: str | None = None,
    now: datetime | None = None,
) -> ExpensePaymentClaim:
    """Verify a PENDING claim. On success the claim enters the verified
    aggregate and the expense status reconciles (partial/full paid). An
    over-claim (verified_amount approaches/matches claimed_amount beyond the
    remaining) is flagged as a mismatch and never auto-PAIDs.

    By default the verified amount adopts the claimed amount (full credit);
    ``verified_amount`` may be supplied to admit a different verified figure
    (e.g. bank confirmed only a partial)."""
    claim = _get_claim_or_error(db, claim_id)
    if claim.expense_id != expense.id:
        raise ClaimError("Claim does not belong to this expense")
    if claim.status == ClaimStatus.VERIFIED:
        # Idempotent replay of an already-verified claim (Owner-verifies path /
        # concurrent replay) is a no-op returning the same verified record —
        # never double-counts (section 6 / E5, matches the old pay replay).
        return claim
    if claim.status != ClaimStatus.PENDING:
        raise ClaimError(
            f"Only PENDING claims can be verified (claim is {claim.status.value})"
        )
    now = now or datetime.now(timezone.utc)
    admitted = _d2(verified_amount) if verified_amount is not None else _d2(claim.claimed_amount)
    truth = payment_truth(db, expense)
    total = _d2(expense.amount)
    already_verified = truth.verified_paid
    new_total = already_verified + admitted

    # Amount mismatch: admitting this claim would exceed the expense total OR
    # the claimed amount is larger than what still remains. Surface it, never
    # auto-PAID / truncate / drop the surplus.
    if new_total > total:
        claim.mismatch = True
        claim.mismatch_reason = (
            f"Claim {_d2(claim.claimed_amount)} would make verified "
            f"{_d2(new_total)} exceed expense total {_d2(total)}; "
            "over-payment requires resolution."
        )
        claim.failure_reason = "OVERPAYMENT_MISMATCH"
        claim.status = ClaimStatus.FAILED
        claim.verified_amount = None
        claim.verification_note = result or claim.verification_note
        claim.updated_by = verified_by
        claim.updated_at = now
        db.flush()
        record_audit(
            db,
            table_name="expense_payment_claims",
            record_id=claim.id,
            action="expense_amount_mismatch",
            actor_id=verified_by,
            changed_fields={
                "status": ["PENDING", "FAILED"],
                "mismatch": [False, True],
                "verified_amount": [None, None],
            },
            old_value=serialize_row(claim),
            new_value=serialize_row(claim),
        )
        return claim

    claim.status = ClaimStatus.VERIFIED
    claim.verified_amount = admitted
    claim.verified_by = verified_by
    claim.verified_at = now
    claim.verification_note = result or claim.verification_note
    claim.updated_by = verified_by
    claim.updated_at = now
    db.flush()
    record_audit(
        db,
        table_name="expense_payment_claims",
        record_id=claim.id,
        action="expense_claim_verified",
        actor_id=verified_by,
        changed_fields={
            "status": ["PENDING", "VERIFIED"],
            "verified_amount": [None, str(admitted)],
        },
        old_value=serialize_row(claim),
        new_value=serialize_row(claim),
    )
    sync_expense_status(db, expense, actor_id=verified_by, now=now)
    return claim


def fail_claim(
    db: Session,
    expense: Expense,
    claim_id: int,
    *,
    failed_by: int | None,
    reason: str,
    now: datetime | None = None,
) -> ExpensePaymentClaim:
    """Fail a PENDING claim — its amount NEVER enters the verified aggregate
    (E7). The failure is preserved in history."""
    claim = _get_claim_or_error(db, claim_id)
    if claim.expense_id != expense.id:
        raise ClaimError("Claim does not belong to this expense")
    if claim.status != ClaimStatus.PENDING:
        raise ClaimError(f"Only PENDING claims can be failed (claim is {claim.status.value})")
    now = now or datetime.now(timezone.utc)
    claim.status = ClaimStatus.FAILED
    claim.failure_reason = reason
    claim.updated_by = failed_by
    claim.updated_at = now
    db.flush()
    record_audit(
        db,
        table_name="expense_payment_claims",
        record_id=claim.id,
        action="expense_claim_failed",
        actor_id=failed_by,
        changed_fields={"status": ["PENDING", "FAILED"]},
        old_value=serialize_row(claim),
        new_value=serialize_row(claim),
    )
    # A FAILED claim must not flip the expense to payment_claimed if no other
    # pending claim remains; recompute from verified truth.
    sync_expense_status(db, expense, actor_id=failed_by, now=now)
    return claim


def reverse_claim(
    db: Session,
    expense: Expense,
    claim_id: int,
    *,
    reversed_by: int | None,
    reason: str,
    now: datetime | None = None,
) -> ExpensePaymentClaim:
    """Reverse a VERIFIED claim — its verified amount leaves the aggregate,
    remaining recomputes, and a previously-fully-paid expense re-enters a
    payable state (E13). History is preserved (claim row kept, status flipped,
    audit chain); never a physical delete."""
    claim = _get_claim_or_error(db, claim_id)
    if claim.expense_id != expense.id:
        raise ClaimError("Claim does not belong to this expense")
    if claim.status != ClaimStatus.VERIFIED:
        raise ClaimError(f"Only VERIFIED claims can be reversed (claim is {claim.status.value})")
    now = now or datetime.now(timezone.utc)
    claim.status = ClaimStatus.REVERSED
    claim.failure_reason = reason
    claim.verified_amount = None  # exclude from the aggregate from now on
    claim.updated_by = reversed_by
    claim.updated_at = now
    db.flush()
    record_audit(
        db,
        table_name="expense_payment_claims",
        record_id=claim.id,
        action="expense_claim_reversed",
        actor_id=reversed_by,
        changed_fields={
            "status": ["VERIFIED", "REVERSED"],
            "verified_amount": [str(claim.claimed_amount), None],
        },
        old_value=serialize_row(claim),
        new_value=serialize_row(claim),
    )
    sync_expense_status(db, expense, actor_id=reversed_by, now=now)
    return claim


def list_claims(db: Session, expense_id: int) -> list[ExpensePaymentClaim]:
    return (
        db.query(ExpensePaymentClaim)
        .filter(ExpensePaymentClaim.expense_id == expense_id)
        .order_by(ExpensePaymentClaim.claimed_at.asc(), ExpensePaymentClaim.id.asc())
        .all()
    )
