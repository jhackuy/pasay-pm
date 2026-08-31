"""Rent/Payment service — single source of truth for the rent-collection cycle.

AGENTS.md §4 invariants enforced here:
- Operation is Truth, Task is Projection. A Task can never resolve an
  Operation by itself. The Operation only resolves when the rent cycle is
  fully VERIFIED (remaining_balance reaches 0).
- Money is Decimal only (parse_money rejects float/bool with MoneyError).
- Idempotency keys are opaque and case-preserving; same key + same payload
  returns the same payment (replayed=True); same key + different payload
  raises IdempotencyConflictError.
- Org-scope is enforced via require_org_scope at the top of every method
  (fail-closed).
- Verify/reject/reverse are OWNER-only.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
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
from app.v1.models.rent_payment import (
    EVIDENCE_KINDS,
    OPERATION_KIND_RENT,
    OPERATION_SUBJECT_RENT_DUE,
    TASK_KIND_RENT_FOLLOW_UP,
    Operation,
    RentActivity,
    RentActivityKind,
    RentDueSchedule,
    RentDueState,
    RentEvidence,
    RentPayment,
    RentVerification,
    Task,
    VerificationDecision,
)
from app.v1.models.tenant_lease import Lease
from app.v1.services.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)


# ---------- result types ----------


@dataclass(frozen=True)
class ClaimResult:
    """The result of recording a payment claim.

    ``replayed=True`` means an identical (org_id, idempotency_key,
    payload_hash) was already stored; the existing payment is returned.
    The router maps this to 200 OK instead of 201 Created.
    """

    replayed: bool
    payment: RentPayment


@dataclass(frozen=True)
class RentBalanceSnapshot:
    """Read-only balance projection for a due schedule."""

    due_schedule_id: int
    amount_due: Decimal
    verified_total: Decimal
    remaining_balance: Decimal
    is_paid: bool


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
    due_schedule_id: Optional[int],
    rent_payment_id: Optional[int],
    kind: RentActivityKind,
    actor_user_id: Optional[int],
    detail: Optional[str] = None,
) -> None:
    db.add(
        RentActivity(
            org_id=org_id,
            due_schedule_id=due_schedule_id,
            rent_payment_id=rent_payment_id,
            kind=kind.value,
            detail=detail,
            actor_user_id=actor_user_id,
            occurred_at=utcnow(),
        )
    )


def _verified_total(db: Session, *, due_schedule_id: int) -> Decimal:
    """Sum of verified amounts on payments linked to this schedule.

    Only RentPayment rows with status='VERIFIED' contribute. This is the
    single source of truth for how much rent actually arrived.
    """
    rows = (
        db.query(RentPayment)
        .filter(
            RentPayment.due_schedule_id == due_schedule_id,
            RentPayment.status == "VERIFIED",
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
    schedule: RentDueSchedule,
    operation: Operation,
    actor_user_id: Optional[int],
) -> bool:
    """Recompute the schedule/operation closure after a verification.

    Returns True iff the schedule is now fully PAID (Operation resolved).
    Idempotent — safe to call multiple times.

    The session uses ``autoflush=False`` for explicit control over the
    flush boundary, so we MUST flush before reading ``_verified_total``
    to make the just-mutated ``RentPayment.status`` rows visible to the
    SQL aggregate query. Without this flush, a partial verify followed by
    a full verify would only see the first row and never settle.
    """
    db.flush()
    amount_due = Decimal(schedule.amount_due)
    total = _verified_total(db, due_schedule_id=schedule.id)
    if total >= amount_due and schedule.state != RentDueState.PAID.value:
        schedule.state = RentDueState.PAID.value
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
            org_id=schedule.org_id,
            due_schedule_id=schedule.id,
            rent_payment_id=None,
            kind=RentActivityKind.PAID,
            actor_user_id=actor_user_id,
        )
        db.flush()
        return True
    if total < amount_due and schedule.state != RentDueState.PAID.value:
        if operation.state == OperationState.OPEN.value:
            operation.state = OperationState.IN_PROGRESS.value
        db.flush()
    return False


def _bump_to_in_progress(operation: Operation) -> bool:
    """Transition an Operation from OPEN → IN_PROGRESS.

    Called whenever the schedule/operation receives non-trivial business
    activity (follow-up creation, claim rejection). The Operation only
    advances to RESOLVED via ``_settle`` when verified_total >= amount_due.

    Returns True if the state was changed.
    """
    if operation.state == OperationState.OPEN.value:
        operation.state = OperationState.IN_PROGRESS.value
        return True
    return False


def _reopen(
    db: Session,
    *,
    schedule: RentDueSchedule,
    operation: Operation,
    actor_user_id: Optional[int],
    due_date: date,
) -> None:
    """Reopen a previously-resolved Operation when a verified payment is reversed."""
    operation.state = OperationState.IN_PROGRESS.value
    operation.resolved_at = None
    if due_date < date.today():
        schedule.state = RentDueState.OVERDUE.value
    else:
        schedule.state = RentDueState.DUE.value
    _log_activity(
        db,
        org_id=schedule.org_id,
        due_schedule_id=schedule.id,
        rent_payment_id=None,
        kind=RentActivityKind.REOPENED,
        actor_user_id=actor_user_id,
    )
    db.flush()


# ---------- service ----------


class RentPaymentService:
    """Cohesive application/domain service for the rent-collection cycle."""

    def __init__(self, db: Session) -> None:
        self.db = db

    # ---- due / overdue ----

    def create_due_schedule(
        self,
        principal: Principal,
        *,
        org_id: int,
        lease_id: int,
        period_start: date,
        due_date: date,
        amount_due: Decimal | str | int,
    ) -> RentDueSchedule:
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER, Role.SECRETARY)
        amount = parse_money(amount_due)
        if due_date < period_start:
            raise ValidationError(
                "due_date must be on or after period_start",
            )
        lease = self.db.get(Lease, lease_id)
        if lease is None or lease.org_id != org_id:
            raise NotFoundError(
                f"lease {lease_id} not found in org {org_id}",
            )
        schedule = RentDueSchedule(
            org_id=org_id,
            lease_id=lease_id,
            period_start=period_start,
            due_date=due_date,
            amount_due=amount,
            state=RentDueState.DUE.value,
        )
        self.db.add(schedule)
        self.db.flush()
        operation = Operation(
            org_id=org_id,
            kind=OPERATION_KIND_RENT,
            subject_type=OPERATION_SUBJECT_RENT_DUE,
            subject_id=schedule.id,
            state=OperationState.OPEN.value,
            due_at=datetime.combine(due_date, datetime.min.time(), tzinfo=__import__("datetime").timezone.utc),
        )
        self.db.add(operation)
        self.db.flush()
        _log_activity(
            self.db,
            org_id=org_id,
            due_schedule_id=schedule.id,
            rent_payment_id=None,
            kind=RentActivityKind.DUE_CREATED,
            actor_user_id=principal.user_id,
        )
        self.db.commit()
        return schedule

    def list_due_schedules(
        self,
        principal: Principal,
        *,
        org_id: int,
        lease_id: Optional[int] = None,
        state: Optional[str] = None,
    ) -> list[RentDueSchedule]:
        require_org_scope(principal, org_id)
        q = self.db.query(RentDueSchedule).filter(RentDueSchedule.org_id == org_id)
        if lease_id is not None:
            q = q.filter(RentDueSchedule.lease_id == lease_id)
        if state is not None:
            q = q.filter(RentDueSchedule.state == state)
        return q.order_by(RentDueSchedule.due_date.asc()).all()

    def list_overdue(
        self,
        principal: Principal,
        *,
        org_id: int,
        as_of: Optional[date] = None,
    ) -> list[RentDueSchedule]:
        require_org_scope(principal, org_id)
        cutoff = as_of or date.today()
        return (
            self.db.query(RentDueSchedule)
            .filter(
                RentDueSchedule.org_id == org_id,
                RentDueSchedule.state.in_(("DUE", "OVERDUE")),
                RentDueSchedule.due_date < cutoff,
            )
            .order_by(RentDueSchedule.due_date.asc())
            .all()
        )

    def mark_overdue(
        self,
        principal: Principal,
        *,
        org_id: int,
        as_of: Optional[date] = None,
    ) -> list[RentDueSchedule]:
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER, Role.SECRETARY)
        cutoff = as_of or date.today()
        schedules = (
            self.db.query(RentDueSchedule)
            .filter(
                RentDueSchedule.org_id == org_id,
                RentDueSchedule.state == RentDueState.DUE.value,
                RentDueSchedule.due_date < cutoff,
            )
            .all()
        )
        for s in schedules:
            s.state = RentDueState.OVERDUE.value
            _log_activity(
                self.db,
                org_id=s.org_id,
                due_schedule_id=s.id,
                rent_payment_id=None,
                kind=RentActivityKind.MARKED_OVERDUE,
                actor_user_id=principal.user_id,
            )
        self.db.commit()
        return schedules

    def get_due_schedule(
        self,
        principal: Principal,
        *,
        org_id: int,
        due_schedule_id: int,
    ) -> RentDueSchedule:
        require_org_scope(principal, org_id)
        schedule = self.db.get(RentDueSchedule, due_schedule_id)
        if schedule is None or schedule.org_id != org_id:
            # Cross-org read returns 404 (fail-closed), per Issue #99.
            raise NotFoundError(
                f"due schedule {due_schedule_id} not found in org {org_id}",
            )
        return schedule

    def remaining_balance(
        self,
        principal: Principal,
        *,
        org_id: int,
        due_schedule_id: int,
    ) -> RentBalanceSnapshot:
        require_org_scope(principal, org_id)
        schedule = self.get_due_schedule(
            principal, org_id=org_id, due_schedule_id=due_schedule_id,
        )
        total = _verified_total(self.db, due_schedule_id=due_schedule_id)
        amount_due = Decimal(schedule.amount_due)
        remaining = amount_due - total
        is_paid = remaining <= Decimal("0") or schedule.state == RentDueState.PAID.value
        return RentBalanceSnapshot(
            due_schedule_id=schedule.id,
            amount_due=amount_due,
            verified_total=total,
            remaining_balance=remaining if remaining > Decimal("0") else Decimal("0"),
            is_paid=is_paid,
        )

    def get_operation(
        self,
        principal: Principal,
        *,
        org_id: int,
        due_schedule_id: int,
    ) -> Operation:
        require_org_scope(principal, org_id)
        schedule = self.get_due_schedule(
            principal, org_id=org_id, due_schedule_id=due_schedule_id,
        )
        op = (
            self.db.query(Operation)
            .filter(
                Operation.org_id == org_id,
                Operation.subject_type == OPERATION_SUBJECT_RENT_DUE,
                Operation.subject_id == schedule.id,
            )
            .first()
        )
        if op is None:
            raise NotFoundError(
                f"operation for due schedule {due_schedule_id} not found",
            )
        return op

    def list_activity(
        self,
        principal: Principal,
        *,
        org_id: int,
        due_schedule_id: int,
    ) -> list[RentActivity]:
        require_org_scope(principal, org_id)
        self.get_due_schedule(
            principal, org_id=org_id, due_schedule_id=due_schedule_id,
        )
        return (
            self.db.query(RentActivity)
            .filter(
                RentActivity.org_id == org_id,
                RentActivity.due_schedule_id == due_schedule_id,
            )
            .order_by(RentActivity.occurred_at.asc(), RentActivity.id.asc())
            .all()
        )

    # ---- follow-up (Task projection) ----

    def create_follow_up(
        self,
        principal: Principal,
        *,
        org_id: int,
        due_schedule_id: int,
        title: str,
        due_at: Optional[datetime] = None,
    ) -> Task:
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER, Role.SECRETARY)
        schedule = self.get_due_schedule(
            principal, org_id=org_id, due_schedule_id=due_schedule_id,
        )
        op = self.get_operation(
            principal, org_id=org_id, due_schedule_id=due_schedule_id,
        )
        # Follow-up creation is non-trivial business activity; the
        # Operation moves from OPEN → IN_PROGRESS. The Operation may
        # only resolve via the verified-balance gate in ``_settle``.
        _bump_to_in_progress(op)
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
            kind=TASK_KIND_RENT_FOLLOW_UP,
            title=title,
            state="open",
            due_at=due_at,
        )
        self.db.add(task)
        self.db.flush()
        _log_activity(
            self.db,
            org_id=org_id,
            due_schedule_id=schedule.id,
            rent_payment_id=None,
            kind=RentActivityKind.FOLLOW_UP_CREATED,
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
        due_schedule_id: int,
    ) -> list[Task]:
        require_org_scope(principal, org_id)
        self.get_due_schedule(
            principal, org_id=org_id, due_schedule_id=due_schedule_id,
        )
        op = self.get_operation(
            principal, org_id=org_id, due_schedule_id=due_schedule_id,
        )
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
        _log_activity(
            self.db,
            org_id=org_id,
            due_schedule_id=None,
            rent_payment_id=None,
            kind=RentActivityKind.FOLLOW_UP_DONE,
            actor_user_id=principal.user_id,
            detail=task.title,
        )
        # Completing a follow-up NEVER resolves the Operation. Operation
        # closure is gated by full verification, not by follow-up tasks.
        self.db.commit()
        return task

    # ---- claim / evidence ----

    def claim_payment(
        self,
        principal: Principal,
        *,
        org_id: int,
        due_schedule_id: int,
        claimed_amount: Decimal | str | int,
        idempotency_key: str,
        evidence: list[dict[str, Any]],
    ) -> ClaimResult:
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER, Role.SECRETARY)
        # normalize_idempotency_key raises IdempotencyKeyError on bad input;
        # router maps that to 400.
        key = normalize_idempotency_key(idempotency_key)
        amount = parse_money(claimed_amount)
        schedule = self.get_due_schedule(
            principal, org_id=org_id, due_schedule_id=due_schedule_id,
        )
        # Compute payload_hash from the canonical claim input.
        payload = {
            "due_schedule_id": due_schedule_id,
            "claimed_amount": str(amount),
            "evidence": evidence,
        }
        payload_hash = compute_payload_hash(payload)
        # Idempotency replay: same (org_id, key) → return existing.
        existing = (
            self.db.query(RentPayment)
            .filter(
                RentPayment.org_id == org_id,
                RentPayment.idempotency_key == key,
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
                due_schedule_id=due_schedule_id,
                rent_payment_id=existing.id,
                kind=RentActivityKind.CLAIM_REPLAYED,
                actor_user_id=principal.user_id,
            )
            self.db.commit()
            return ClaimResult(replayed=True, payment=existing)
        payment = RentPayment(
            org_id=org_id,
            due_schedule_id=due_schedule_id,
            claimed_amount=amount,
            verified_amount=None,
            status="PENDING",
            claimed_by_user_id=principal.user_id,
            idempotency_key=key,
            payload_hash=payload_hash,
        )
        self.db.add(payment)
        self.db.flush()
        # Persist evidence rows attached to the claim.
        for ev in evidence:
            kind = ev.get("kind", "")
            reference = ev.get("reference", "")
            if kind not in EVIDENCE_KINDS:
                raise ValidationError(
                    f"unknown evidence kind {kind!r} "
                    f"(must be one of: {list(EVIDENCE_KINDS)})",
                )
            if not reference:
                raise ValidationError("evidence reference must be non-empty")
            self.db.add(
                RentEvidence(
                    org_id=org_id,
                    rent_payment_id=payment.id,
                    kind=kind,
                    reference=reference,
                    uploaded_by_user_id=principal.user_id,
                )
            )
        _log_activity(
            self.db,
            org_id=org_id,
            due_schedule_id=due_schedule_id,
            rent_payment_id=payment.id,
            kind=RentActivityKind.CLAIM_CREATED,
            actor_user_id=principal.user_id,
        )
        self.db.commit()
        return ClaimResult(replayed=False, payment=payment)

    def list_payments(
        self,
        principal: Principal,
        *,
        org_id: int,
        due_schedule_id: int,
    ) -> list[RentPayment]:
        require_org_scope(principal, org_id)
        self.get_due_schedule(
            principal, org_id=org_id, due_schedule_id=due_schedule_id,
        )
        return (
            self.db.query(RentPayment)
            .filter(
                RentPayment.org_id == org_id,
                RentPayment.due_schedule_id == due_schedule_id,
            )
            .order_by(RentPayment.id.asc())
            .all()
        )

    def list_all_payments(
        self,
        principal: Principal,
        *,
        org_id: int,
        status: Optional[str] = None,
        due_schedule_id: Optional[int] = None,
    ) -> list[RentPayment]:
        """List every RentPayment in the org (used by /rent/claims).

        Filters are optional. Org scope is fail-closed (require_org_scope).
        """
        require_org_scope(principal, org_id)
        q = self.db.query(RentPayment).filter(RentPayment.org_id == org_id)
        if status is not None:
            q = q.filter(RentPayment.status == status)
        if due_schedule_id is not None:
            q = q.filter(RentPayment.due_schedule_id == due_schedule_id)
        return q.order_by(RentPayment.id.desc()).all()

    def add_evidence(
        self,
        principal: Principal,
        *,
        org_id: int,
        rent_payment_id: int,
        kind: str,
        reference: str,
    ) -> RentEvidence:
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER, Role.SECRETARY)
        payment = self.db.get(RentPayment, rent_payment_id)
        if payment is None or payment.org_id != org_id:
            raise NotFoundError(
                f"rent payment {rent_payment_id} not found in org {org_id}",
            )
        if payment.status != "PENDING":
            raise ConflictError(
                f"cannot add evidence to a non-PENDING payment "
                f"(status={payment.status})",
            )
        if kind not in EVIDENCE_KINDS:
            raise ValidationError(
                f"unknown evidence kind {kind!r} "
                f"(must be one of: {list(EVIDENCE_KINDS)})",
            )
        if not reference:
            raise ValidationError("evidence reference must be non-empty")
        ev = RentEvidence(
            org_id=org_id,
            rent_payment_id=payment.id,
            kind=kind,
            reference=reference,
            uploaded_by_user_id=principal.user_id,
        )
        self.db.add(ev)
        self.db.flush()
        _log_activity(
            self.db,
            org_id=org_id,
            due_schedule_id=payment.due_schedule_id,
            rent_payment_id=payment.id,
            kind=RentActivityKind.EVIDENCE_ADDED,
            actor_user_id=principal.user_id,
        )
        self.db.commit()
        return ev

    def list_evidence(
        self,
        principal: Principal,
        *,
        org_id: int,
        rent_payment_id: int,
    ) -> list[RentEvidence]:
        require_org_scope(principal, org_id)
        payment = self.db.get(RentPayment, rent_payment_id)
        if payment is None or payment.org_id != org_id:
            raise NotFoundError(
                f"rent payment {rent_payment_id} not found in org {org_id}",
            )
        return (
            self.db.query(RentEvidence)
            .filter(
                RentEvidence.org_id == org_id,
                RentEvidence.rent_payment_id == rent_payment_id,
            )
            .order_by(RentEvidence.id.asc())
            .all()
        )

    # ---- verification ----

    def verify_payment(
        self,
        principal: Principal,
        *,
        org_id: int,
        rent_payment_id: int,
        verified_amount: Optional[Decimal | str | int] = None,
    ) -> RentPayment:
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER)
        payment = self.db.get(RentPayment, rent_payment_id)
        if payment is None or payment.org_id != org_id:
            raise NotFoundError(
                f"rent payment {rent_payment_id} not found in org {org_id}",
            )
        if payment.status != "PENDING":
            raise ConflictError(
                f"cannot verify a non-PENDING payment "
                f"(status={payment.status})",
            )
        # Verification requires at least one evidence row.
        ev_count = (
            self.db.query(RentEvidence)
            .filter(RentEvidence.rent_payment_id == payment.id)
            .count()
        )
        if ev_count == 0:
            raise ValidationError(
                "verification requires at least one evidence row",
            )
        if verified_amount is None:
            verified = Decimal(payment.claimed_amount)
        else:
            verified = parse_money(verified_amount)
        payment.status = "VERIFIED"
        payment.verified_amount = verified
        self.db.add(
            RentVerification(
                org_id=org_id,
                rent_payment_id=payment.id,
                decision=VerificationDecision.VERIFIED.value,
                verified_amount=verified,
                verifier_user_id=principal.user_id,
            )
        )
        schedule = self.db.get(RentDueSchedule, payment.due_schedule_id)
        op = (
            self.db.query(Operation)
            .filter(
                Operation.subject_type == OPERATION_SUBJECT_RENT_DUE,
                Operation.subject_id == schedule.id,
                Operation.org_id == org_id,
            )
            .first()
        )
        is_partial = not _settle(
            self.db,
            schedule=schedule,
            operation=op,
            actor_user_id=principal.user_id,
        )
        _log_activity(
            self.db,
            org_id=org_id,
            due_schedule_id=payment.due_schedule_id,
            rent_payment_id=payment.id,
            kind=(
                RentActivityKind.PARTIAL_VERIFIED
                if is_partial
                else RentActivityKind.VERIFIED
            ),
            actor_user_id=principal.user_id,
        )
        self.db.commit()
        return payment

    def reject_payment(
        self,
        principal: Principal,
        *,
        org_id: int,
        rent_payment_id: int,
        reason: str,
    ) -> RentPayment:
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER)
        if not reason or not reason.strip():
            raise ValidationError("rejection reason is required")
        payment = self.db.get(RentPayment, rent_payment_id)
        if payment is None or payment.org_id != org_id:
            raise NotFoundError(
                f"rent payment {rent_payment_id} not found in org {org_id}",
            )
        if payment.status != "PENDING":
            raise ConflictError(
                f"cannot reject a non-PENDING payment "
                f"(status={payment.status})",
            )
        payment.status = "FAILED"
        payment.verified_amount = None
        self.db.add(
            RentVerification(
                org_id=org_id,
                rent_payment_id=payment.id,
                decision=VerificationDecision.REJECTED.value,
                verified_amount=None,
                verifier_user_id=principal.user_id,
                reason=reason,
            )
        )
        # Rejection is non-trivial business activity on the Operation; bump
        # the Operation from OPEN → IN_PROGRESS. The Operation never resolves
        # via rejection — only via ``_settle`` once verified_total >=
        # amount_due.
        schedule = self.db.get(RentDueSchedule, payment.due_schedule_id)
        op = (
            self.db.query(Operation)
            .filter(
                Operation.subject_type == OPERATION_SUBJECT_RENT_DUE,
                Operation.subject_id == schedule.id,
                Operation.org_id == org_id,
            )
            .first()
        )
        if op is not None:
            _bump_to_in_progress(op)
        _log_activity(
            self.db,
            org_id=org_id,
            due_schedule_id=payment.due_schedule_id,
            rent_payment_id=payment.id,
            kind=RentActivityKind.REJECTED,
            actor_user_id=principal.user_id,
            detail=reason,
        )
        # Rejection NEVER closes the Operation. FAILED is a no-op for the
        # Operation/Schedule state machine.
        self.db.commit()
        return payment

    def reverse_payment(
        self,
        principal: Principal,
        *,
        org_id: int,
        rent_payment_id: int,
        reason: str,
    ) -> RentPayment:
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER)
        if not reason or not reason.strip():
            raise ValidationError("reversal reason is required")
        payment = self.db.get(RentPayment, rent_payment_id)
        if payment is None or payment.org_id != org_id:
            raise NotFoundError(
                f"rent payment {rent_payment_id} not found in org {org_id}",
            )
        if payment.status != "VERIFIED":
            raise ConflictError(
                f"cannot reverse a non-VERIFIED payment "
                f"(status={payment.status})",
            )
        payment.status = "REVERSED"
        payment.verified_amount = None
        self.db.add(
            RentVerification(
                org_id=org_id,
                rent_payment_id=payment.id,
                decision=VerificationDecision.REVERSED.value,
                verified_amount=None,
                verifier_user_id=principal.user_id,
                reason=reason,
            )
        )
        schedule = self.db.get(RentDueSchedule, payment.due_schedule_id)
        op = (
            self.db.query(Operation)
            .filter(
                Operation.subject_type == OPERATION_SUBJECT_RENT_DUE,
                Operation.subject_id == schedule.id,
                Operation.org_id == org_id,
            )
            .first()
        )
        # If the Operation was previously resolved (PAID), reopen it.
        if op is not None and op.state == OperationState.RESOLVED.value:
            _reopen(
                self.db,
                schedule=schedule,
                operation=op,
                actor_user_id=principal.user_id,
                due_date=schedule.due_date,
            )
        _log_activity(
            self.db,
            org_id=org_id,
            due_schedule_id=payment.due_schedule_id,
            rent_payment_id=payment.id,
            kind=RentActivityKind.REVERSED,
            actor_user_id=principal.user_id,
            detail=reason,
        )
        self.db.commit()
        return payment

    def list_verifications(
        self,
        principal: Principal,
        *,
        org_id: int,
        rent_payment_id: int,
    ) -> list[RentVerification]:
        require_org_scope(principal, org_id)
        payment = self.db.get(RentPayment, rent_payment_id)
        if payment is None or payment.org_id != org_id:
            raise NotFoundError(
                f"rent payment {rent_payment_id} not found in org {org_id}",
            )
        return (
            self.db.query(RentVerification)
            .filter(
                RentVerification.org_id == org_id,
                RentVerification.rent_payment_id == rent_payment_id,
            )
            .order_by(RentVerification.id.asc())
            .all()
        )


__all__ = [
    "ClaimResult",
    "RentBalanceSnapshot",
    "RentPaymentService",
]