"""Expense service — single source of truth for the expense-claim cycle.

AGENTS.md §4 invariants enforced here:

- Operation is Truth, Task is Projection. A Task can never resolve an
  Operation by itself. The Operation only resolves (claim → SETTLED) when
  the verified amount covers the claim.
- Money is Decimal only (parse_money rejects float/bool with MoneyError).
- Idempotency keys are opaque and case-preserving; same key + same payload
  returns the same claim (replayed=True); same key + different payload
  raises IdempotencyConflictError.
- Org-scope is enforced via require_org_scope at the top of every method
  (fail-closed).
- Verify/reject/reverse are OWNER-only.
- Claim ≠ Evidence ≠ Verification: three separate tables, never
  collapsed.
- Approval ≠ Payment: verify is the OWNER's decision, not cash movement;
  amount mismatch is recorded explicitly, not faked as success.
- Reminder ≠ Completion: completing a follow-up NEVER closes the
  Operation; only full verification can.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.core.idempotency import (
    IdempotencyConflictError,
    compute_payload_hash,
    normalize_idempotency_key,
)
from app.core.money import parse_money
from app.core.permissions import (
    PermissionDenied,
    Principal,
    Role,
    require_org_scope,
)
from app.core.time import utcnow
from app.v1.models.base import OperationState
from app.v1.models.expense import (
    EXPENSE_CATEGORIES,
    EXPENSE_RECEIPT_KINDS,
    ExpenseActivity,
    ExpenseActivityKind,
    ExpenseClaim,
    ExpenseClaimStatus,
    ExpenseReceipt,
    ExpenseVerification,
    ExpenseVerificationDecision,
    OPERATION_KIND_EXPENSE,
    OPERATION_SUBJECT_EXPENSE_CLAIM,
    TASK_KIND_EXPENSE_FOLLOW_UP,
)
from app.v1.models.rent_payment import Operation, Task
from app.v1.services.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)


# ---------- result types ----------


@dataclass(frozen=True)
class ClaimResult:
    """The result of opening an expense claim.

    ``replayed=True`` means an identical (org_id, idempotency_key,
    payload_hash) was already stored; the existing claim is returned.
    The router maps this to 200 OK instead of 201 Created.
    """

    replayed: bool
    claim: ExpenseClaim


@dataclass(frozen=True)
class ExpenseBalanceSnapshot:
    """Read-only balance projection for a claim."""

    claim_id: int
    claimed_amount: Decimal
    verified_total: Decimal
    remaining_amount: Decimal
    is_settled: bool


# ---------- helpers ----------


def _ensure_role(principal: Principal, *allowed: Role) -> None:
    if principal.role not in allowed:
        raise PermissionDenied(
            f"role {principal.role.value} not allowed "
            f"(must be one of: {[r.value for r in allowed]})"
        )


def _log_activity(
    db: Session,
    *,
    org_id: int,
    claim_id: Optional[int],
    receipt_id: Optional[int],
    kind: ExpenseActivityKind,
    actor_user_id: Optional[int],
    detail: Optional[str] = None,
) -> None:
    db.add(
        ExpenseActivity(
            org_id=org_id,
            claim_id=claim_id,
            receipt_id=receipt_id,
            kind=kind.value,
            detail=detail,
            actor_user_id=actor_user_id,
            occurred_at=utcnow(),
        )
    )


def _verified_total(db: Session, *, claim_id: int) -> Decimal:
    """Sum of verified amounts recorded on this claim.

    Only ExpenseVerification rows with decision='VERIFIED' contribute.
    This is the single source of truth for how much was actually approved.
    """
    rows = (
        db.query(ExpenseVerification)
        .filter(
            ExpenseVerification.claim_id == claim_id,
            ExpenseVerification.decision == "VERIFIED",
        )
        .all()
    )
    total = Decimal("0")
    for r in rows:
        if r.verified_amount is not None:
            total += Decimal(r.verified_amount)
    return total


def _settle(
    db: Session,
    *,
    claim: ExpenseClaim,
    operation: Operation,
    actor_user_id: Optional[int],
) -> bool:
    """Recompute the claim/operation closure after a verification.

    Returns True iff the claim is now fully SETTLED (Operation resolved).
    Idempotent — safe to call multiple times.

    The closure gate is the ONLY path that can move the Operation to
    ``resolved`` for expenses. Verification of an amount smaller than the
    claim leaves the claim at status=VERIFIED but does NOT close the
    Operation (and does NOT change the claim to SETTLED); the remaining
    gap is exposed via ``ExpenseBalanceSnapshot.remaining_amount``.
    """
    claimed = Decimal(claim.claimed_amount)
    total = _verified_total(db, claim_id=claim.id)
    if total >= claimed and claim.status != ExpenseClaimStatus.SETTLED.value:
        claim.status = ExpenseClaimStatus.SETTLED.value
        operation.state = OperationState.RESOLVED.value
        operation.resolved_at = utcnow()
        # Cancel any remaining open follow-ups (Task is Projection).
        open_tasks = (
            db.query(Task)
            .filter(
                Task.operation_id == operation.id,
                Task.state == "open",
            )
            .all()
        )
        for t in open_tasks:
            t.state = "cancelled"
        _log_activity(
            db,
            org_id=claim.org_id,
            claim_id=claim.id,
            receipt_id=None,
            kind=ExpenseActivityKind.SETTLED,
            actor_user_id=actor_user_id,
        )
        db.flush()
        return True
    return False


def _reopen(
    db: Session,
    *,
    claim: ExpenseClaim,
    operation: Operation,
    actor_user_id: Optional[int],
) -> None:
    """Reopen a previously-settled Operation when a verified decision is reversed.

    The claim goes back to ``VERIFIED`` (the prior verified rows are still
    in the audit log); the Operation returns to ``in_progress`` and a
    ``REOPENED`` activity entry is recorded.
    """
    claim.status = ExpenseClaimStatus.VERIFIED.value
    operation.state = OperationState.IN_PROGRESS.value
    operation.resolved_at = None
    _log_activity(
        db,
        org_id=claim.org_id,
        claim_id=claim.id,
        receipt_id=None,
        kind=ExpenseActivityKind.REOPENED,
        actor_user_id=actor_user_id,
    )
    db.flush()


# ---------- service ----------


class ExpenseClaimService:
    """Cohesive application/domain service for the expense-claim cycle."""

    def __init__(self, db: Session) -> None:
        self.db = db

    # ---- claim / receipts ----

    def open_claim(
        self,
        principal: Principal,
        *,
        org_id: int,
        title: str,
        category: str,
        claimed_amount: Decimal | str | int,
        idempotency_key: str,
        receipts: list[dict[str, Any]],
    ) -> ClaimResult:
        """Open a new expense claim. Idempotent on (org_id, key)."""
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER, Role.SECRETARY)
        # normalize_idempotency_key raises IdempotencyKeyError on bad input;
        # router maps that to 400.
        key = normalize_idempotency_key(idempotency_key)
        amount = parse_money(claimed_amount)
        if category not in EXPENSE_CATEGORIES:
            raise ValidationError(
                f"unknown category {category!r} "
                f"(must be one of: {list(EXPENSE_CATEGORIES)})",
            )
        # Compute payload_hash from the canonical claim input.
        payload = {
            "title": title,
            "category": category,
            "claimed_amount": str(amount),
            "receipts": receipts,
        }
        payload_hash = compute_payload_hash(payload)
        # Idempotency replay: same (org_id, key) → return existing.
        existing = (
            self.db.query(ExpenseClaim)
            .filter(
                ExpenseClaim.org_id == org_id,
                ExpenseClaim.idempotency_key == key,
            )
            .first()
        )
        if existing is not None:
            if existing.payload_hash != payload_hash:
                raise IdempotencyConflictError(
                    f"idempotency key {key!r} reused with a different payload",
                )
            _log_activity(
                self.db,
                org_id=org_id,
                claim_id=existing.id,
                receipt_id=None,
                kind=ExpenseActivityKind.CLAIM_REPLAYED,
                actor_user_id=principal.user_id,
            )
            self.db.commit()
            return ClaimResult(replayed=True, claim=existing)
        claim = ExpenseClaim(
            org_id=org_id,
            title=title,
            category=category,
            claimed_amount=amount,
            verified_amount=None,
            status=ExpenseClaimStatus.OPEN.value,
            opened_by_user_id=principal.user_id,
            idempotency_key=key,
            payload_hash=payload_hash,
        )
        self.db.add(claim)
        self.db.flush()
        # Create the linked Operation (polymorphic subject) up-front so
        # follow-ups (Task projections) always have an Operation to attach to.
        operation = Operation(
            org_id=org_id,
            kind=OPERATION_KIND_EXPENSE,
            subject_type=OPERATION_SUBJECT_EXPENSE_CLAIM,
            subject_id=claim.id,
            state=OperationState.OPEN.value,
        )
        self.db.add(operation)
        self.db.flush()
        # Persist initial receipt rows.
        for r in receipts:
            kind = r.get("kind", "")
            reference = r.get("reference", "")
            if kind not in EXPENSE_RECEIPT_KINDS:
                raise ValidationError(
                    f"unknown receipt kind {kind!r} "
                    f"(must be one of: {list(EXPENSE_RECEIPT_KINDS)})",
                )
            if not reference:
                raise ValidationError("receipt reference must be non-empty")
            self.db.add(
                ExpenseReceipt(
                    org_id=org_id,
                    claim_id=claim.id,
                    kind=kind,
                    reference=reference,
                    uploaded_by_user_id=principal.user_id,
                )
            )
        # If receipts were supplied, mark SUBMITTED.
        if receipts:
            claim.status = ExpenseClaimStatus.SUBMITTED.value
            _log_activity(
                self.db,
                org_id=org_id,
                claim_id=claim.id,
                receipt_id=None,
                kind=ExpenseActivityKind.SUBMITTED,
                actor_user_id=principal.user_id,
            )
        _log_activity(
            self.db,
            org_id=org_id,
            claim_id=claim.id,
            receipt_id=None,
            kind=ExpenseActivityKind.CLAIM_OPENED,
            actor_user_id=principal.user_id,
        )
        self.db.commit()
        return ClaimResult(replayed=False, claim=claim)

    def list_claims(
        self,
        principal: Principal,
        *,
        org_id: int,
        status: Optional[str] = None,
        category: Optional[str] = None,
    ) -> list[ExpenseClaim]:
        require_org_scope(principal, org_id)
        q = self.db.query(ExpenseClaim).filter(ExpenseClaim.org_id == org_id)
        if status is not None:
            q = q.filter(ExpenseClaim.status == status)
        if category is not None:
            q = q.filter(ExpenseClaim.category == category)
        return q.order_by(ExpenseClaim.id.asc()).all()

    def get_claim(
        self,
        principal: Principal,
        *,
        org_id: int,
        claim_id: int,
    ) -> ExpenseClaim:
        require_org_scope(principal, org_id)
        claim = self.db.get(ExpenseClaim, claim_id)
        if claim is None or claim.org_id != org_id:
            # Cross-org read returns 404 (fail-closed), per Issue #99.
            raise NotFoundError(
                f"expense claim {claim_id} not found in org {org_id}",
            )
        return claim

    def add_receipt(
        self,
        principal: Principal,
        *,
        org_id: int,
        claim_id: int,
        kind: str,
        reference: str,
    ) -> ExpenseReceipt:
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER, Role.SECRETARY)
        claim = self.get_claim(principal, org_id=org_id, claim_id=claim_id)
        if claim.status not in (
            ExpenseClaimStatus.OPEN.value,
            ExpenseClaimStatus.SUBMITTED.value,
        ):
            raise ConflictError(
                f"cannot add receipt to a non-open claim "
                f"(status={claim.status})",
            )
        if kind not in EXPENSE_RECEIPT_KINDS:
            raise ValidationError(
                f"unknown receipt kind {kind!r} "
                f"(must be one of: {list(EXPENSE_RECEIPT_KINDS)})",
            )
        if not reference:
            raise ValidationError("receipt reference must be non-empty")
        receipt = ExpenseReceipt(
            org_id=org_id,
            claim_id=claim.id,
            kind=kind,
            reference=reference,
            uploaded_by_user_id=principal.user_id,
        )
        self.db.add(receipt)
        self.db.flush()
        # First receipt flips OPEN → SUBMITTED.
        if claim.status == ExpenseClaimStatus.OPEN.value:
            claim.status = ExpenseClaimStatus.SUBMITTED.value
            _log_activity(
                self.db,
                org_id=org_id,
                claim_id=claim.id,
                receipt_id=None,
                kind=ExpenseActivityKind.SUBMITTED,
                actor_user_id=principal.user_id,
            )
        _log_activity(
            self.db,
            org_id=org_id,
            claim_id=claim.id,
            receipt_id=receipt.id,
            kind=ExpenseActivityKind.RECEIPT_ADDED,
            actor_user_id=principal.user_id,
        )
        self.db.commit()
        return receipt

    def list_receipts(
        self,
        principal: Principal,
        *,
        org_id: int,
        claim_id: int,
    ) -> list[ExpenseReceipt]:
        require_org_scope(principal, org_id)
        self.get_claim(principal, org_id=org_id, claim_id=claim_id)
        return (
            self.db.query(ExpenseReceipt)
            .filter(
                ExpenseReceipt.org_id == org_id,
                ExpenseReceipt.claim_id == claim_id,
            )
            .order_by(ExpenseReceipt.id.asc())
            .all()
        )

    # ---- follow-up (Task projection) ----

    def create_follow_up(
        self,
        principal: Principal,
        *,
        org_id: int,
        claim_id: int,
        title: str,
        due_at: Optional[datetime] = None,
    ) -> Task:
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER, Role.SECRETARY)
        claim = self.get_claim(principal, org_id=org_id, claim_id=claim_id)
        op = self.get_operation(principal, org_id=org_id, claim_id=claim_id)
        existing_open = (
            self.db.query(Task)
            .filter(
                Task.operation_id == op.id,
                Task.state == "open",
            )
            .first()
        )
        if existing_open is not None:
            raise ConflictError(
                f"an open follow-up already exists for operation {op.id}",
            )
        task = Task(
            org_id=org_id,
            operation_id=op.id,
            kind=TASK_KIND_EXPENSE_FOLLOW_UP,
            title=title,
            state="open",
            due_at=due_at,
        )
        self.db.add(task)
        self.db.flush()
        _log_activity(
            self.db,
            org_id=org_id,
            claim_id=claim.id,
            receipt_id=None,
            kind=ExpenseActivityKind.FOLLOW_UP_CREATED,
            actor_user_id=principal.user_id,
            detail=title,
        )
        self.db.commit()
        return task

    def list_follow_ups(
        self,
        principal: Principal,
        *,
        org_id: int,
        claim_id: int,
    ) -> list[Task]:
        require_org_scope(principal, org_id)
        op = self.get_operation(principal, org_id=org_id, claim_id=claim_id)
        return (
            self.db.query(Task)
            .filter(Task.operation_id == op.id)
            .order_by(Task.id.asc())
            .all()
        )

    def complete_follow_up(
        self,
        principal: Principal,
        *,
        org_id: int,
        task_id: int,
    ) -> Task:
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER, Role.SECRETARY)
        task = self.db.get(Task, task_id)
        if task is None or task.org_id != org_id:
            raise NotFoundError(f"task {task_id} not found in org {org_id}")
        if task.state != "open":
            raise ConflictError(
                f"task {task_id} is not open (state={task.state})",
            )
        task.state = "done"
        task.done_at = utcnow()
        # Find the linked claim (Task → Operation → claim) for the activity entry.
        op = self.db.get(Operation, task.operation_id)
        claim_id: Optional[int] = None
        if op is not None and op.subject_type == OPERATION_SUBJECT_EXPENSE_CLAIM:
            claim_id = op.subject_id
        _log_activity(
            self.db,
            org_id=org_id,
            claim_id=claim_id,
            receipt_id=None,
            kind=ExpenseActivityKind.FOLLOW_UP_DONE,
            actor_user_id=principal.user_id,
            detail=task.title,
        )
        # Completing a follow-up NEVER resolves the Operation. Operation
        # closure is gated by full verification, not by follow-up tasks.
        self.db.commit()
        return task

    # ---- read projections ----

    def get_operation(
        self,
        principal: Principal,
        *,
        org_id: int,
        claim_id: int,
    ) -> Operation:
        require_org_scope(principal, org_id)
        self.get_claim(principal, org_id=org_id, claim_id=claim_id)
        op = (
            self.db.query(Operation)
            .filter(
                Operation.org_id == org_id,
                Operation.subject_type == OPERATION_SUBJECT_EXPENSE_CLAIM,
                Operation.subject_id == claim_id,
            )
            .first()
        )
        if op is None:
            raise NotFoundError(
                f"operation for expense claim {claim_id} not found",
            )
        return op

    def remaining_balance(
        self,
        principal: Principal,
        *,
        org_id: int,
        claim_id: int,
    ) -> ExpenseBalanceSnapshot:
        require_org_scope(principal, org_id)
        claim = self.get_claim(principal, org_id=org_id, claim_id=claim_id)
        total = _verified_total(self.db, claim_id=claim_id)
        claimed = Decimal(claim.claimed_amount)
        remaining = claimed - total
        is_settled = (
            remaining <= Decimal("0")
            or claim.status == ExpenseClaimStatus.SETTLED.value
        )
        return ExpenseBalanceSnapshot(
            claim_id=claim.id,
            claimed_amount=claimed,
            verified_total=total,
            remaining_amount=(
                remaining if remaining > Decimal("0") else Decimal("0")
            ),
            is_settled=is_settled,
        )

    def list_activity(
        self,
        principal: Principal,
        *,
        org_id: int,
        claim_id: int,
    ) -> list[ExpenseActivity]:
        require_org_scope(principal, org_id)
        self.get_claim(principal, org_id=org_id, claim_id=claim_id)
        return (
            self.db.query(ExpenseActivity)
            .filter(
                ExpenseActivity.org_id == org_id,
                ExpenseActivity.claim_id == claim_id,
            )
            .order_by(ExpenseActivity.occurred_at.asc(), ExpenseActivity.id.asc())
            .all()
        )

    def list_verifications(
        self,
        principal: Principal,
        *,
        org_id: int,
        claim_id: int,
    ) -> list[ExpenseVerification]:
        require_org_scope(principal, org_id)
        self.get_claim(principal, org_id=org_id, claim_id=claim_id)
        return (
            self.db.query(ExpenseVerification)
            .filter(
                ExpenseVerification.org_id == org_id,
                ExpenseVerification.claim_id == claim_id,
            )
            .order_by(ExpenseVerification.id.asc())
            .all()
        )

    # ---- verification ----

    def verify_claim(
        self,
        principal: Principal,
        *,
        org_id: int,
        claim_id: int,
        verified_amount: Optional[Decimal | str | int] = None,
    ) -> ExpenseClaim:
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER)
        claim = self.get_claim(principal, org_id=org_id, claim_id=claim_id)
        if claim.status not in (
            ExpenseClaimStatus.OPEN.value,
            ExpenseClaimStatus.SUBMITTED.value,
        ):
            raise ConflictError(
                f"cannot verify a non-open claim "
                f"(status={claim.status})",
            )
        # Verification requires at least one receipt row.
        receipt_count = (
            self.db.query(ExpenseReceipt)
            .filter(ExpenseReceipt.claim_id == claim.id)
            .count()
        )
        if receipt_count == 0:
            raise ValidationError(
                "verification requires at least one receipt row",
            )
        if verified_amount is None:
            verified = Decimal(claim.claimed_amount)
        else:
            verified = parse_money(verified_amount)
        claim.status = ExpenseClaimStatus.VERIFIED.value
        claim.verified_amount = verified
        self.db.add(
            ExpenseVerification(
                org_id=org_id,
                claim_id=claim.id,
                decision=ExpenseVerificationDecision.VERIFIED.value,
                verified_amount=verified,
                verifier_user_id=principal.user_id,
            )
        )
        op = self.get_operation(principal, org_id=org_id, claim_id=claim_id)
        is_settled = _settle(
            self.db,
            claim=claim,
            operation=op,
            actor_user_id=principal.user_id,
        )
        # Record amount mismatch as an activity entry (does NOT change closure).
        if verified != Decimal(claim.claimed_amount):
            _log_activity(
                self.db,
                org_id=org_id,
                claim_id=claim.id,
                receipt_id=None,
                kind=ExpenseActivityKind.AMOUNT_MISMATCH,
                actor_user_id=principal.user_id,
                detail=(
                    f"claimed={claim.claimed_amount} "
                    f"verified={verified}"
                ),
            )
        _log_activity(
            self.db,
            org_id=org_id,
            claim_id=claim.id,
            receipt_id=None,
            kind=(
                ExpenseActivityKind.SETTLED
                if is_settled
                else ExpenseActivityKind.VERIFIED
            ),
            actor_user_id=principal.user_id,
        )
        self.db.commit()
        return claim

    def reject_claim(
        self,
        principal: Principal,
        *,
        org_id: int,
        claim_id: int,
        reason: str,
    ) -> ExpenseClaim:
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER)
        if not reason or not reason.strip():
            raise ValidationError("rejection reason is required")
        claim = self.get_claim(principal, org_id=org_id, claim_id=claim_id)
        if claim.status not in (
            ExpenseClaimStatus.OPEN.value,
            ExpenseClaimStatus.SUBMITTED.value,
        ):
            raise ConflictError(
                f"cannot reject a non-open claim "
                f"(status={claim.status})",
            )
        claim.status = ExpenseClaimStatus.FAILED.value
        claim.verified_amount = None
        self.db.add(
            ExpenseVerification(
                org_id=org_id,
                claim_id=claim.id,
                decision=ExpenseVerificationDecision.REJECTED.value,
                verified_amount=None,
                verifier_user_id=principal.user_id,
                reason=reason,
            )
        )
        _log_activity(
            self.db,
            org_id=org_id,
            claim_id=claim.id,
            receipt_id=None,
            kind=ExpenseActivityKind.REJECTED,
            actor_user_id=principal.user_id,
            detail=reason,
        )
        # Rejection NEVER closes the Operation. FAILED is a no-op for the
        # Operation/Claim state machine.
        self.db.commit()
        return claim

    def reverse_claim(
        self,
        principal: Principal,
        *,
        org_id: int,
        claim_id: int,
        reason: str,
    ) -> ExpenseClaim:
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER)
        if not reason or not reason.strip():
            raise ValidationError("reversal reason is required")
        claim = self.get_claim(principal, org_id=org_id, claim_id=claim_id)
        if claim.status != ExpenseClaimStatus.SETTLED.value:
            raise ConflictError(
                f"cannot reverse a non-SETTLED claim "
                f"(status={claim.status})",
            )
        # The verified row remains in the audit log; the claim is
        # recomputed by _settle() which will see verified_total < claimed
        # again and reopen the Operation.
        op = self.get_operation(principal, org_id=org_id, claim_id=claim_id)
        self.db.add(
            ExpenseVerification(
                org_id=org_id,
                claim_id=claim.id,
                decision=ExpenseVerificationDecision.REVERSED.value,
                verified_amount=None,
                verifier_user_id=principal.user_id,
                reason=reason,
            )
        )
        # Restore Operation to in_progress; the existing VERIFIED audit row
        # remains but the claim is no longer at SETTLED until verified_total
        # covers the claim again.
        _reopen(
            self.db,
            claim=claim,
            operation=op,
            actor_user_id=principal.user_id,
        )
        _log_activity(
            self.db,
            org_id=org_id,
            claim_id=claim.id,
            receipt_id=None,
            kind=ExpenseActivityKind.REVERSED,
            actor_user_id=principal.user_id,
            detail=reason,
        )
        self.db.commit()
        return claim


__all__ = [
    "ClaimResult",
    "ExpenseBalanceSnapshot",
    "ExpenseClaimService",
]