"""Repair service — single source of truth for the repair lifecycle.

AGENTS.md §4 invariants enforced here:

- Operation is Truth, Task is Projection. A Task can never resolve an
  Operation by itself. The Operation only resolves (REPORT.state=COMPLETED
  + Operation.state=resolved) when the OWNER explicitly verifies a real
  completion claim.
- Money is Decimal only (parse_money rejects float/bool with MoneyError).
- Idempotency keys are opaque and case-preserving; same key + same payload
  returns the same report/quote; same key + different payload raises
  IdempotencyConflictError.
- Org-scope is enforced via require_org_scope at the top of every method
  (fail-closed).
- Report/Quote/Work/CompletionClaim/Verification are FIVE separate tables.
  They never collapse into one.
- **Linked Expense/Payment does NOT close Repair.** Repair is a physical
  problem; the related expense is a money flow. Closure is gated by the
  OWNER's verification of a completion claim only.
- A reversal of a verified decision reopens the report to
  COMPLETION_CLAIMED and reopens the Operation to in_progress.
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
from app.v1.models.repair import (
    OPERATION_KIND_REPAIR,
    OPERATION_SUBJECT_REPAIR_REPORT,
    REPAIR_CATEGORIES,
    REPAIR_QUOTE_DECISIONS,
    REPAIR_SEVERITIES,
    REPAIR_TECHNICIAN_SOURCES,
    REPAIR_WORK_STATES,
    RepairActivity,
    RepairActivityKind,
    RepairCategory,
    RepairCompletionClaim,
    RepairQuote,
    RepairQuoteDecision,
    RepairReport,
    RepairSeverity,
    RepairState,
    RepairTechnicianSource,
    RepairVerification,
    RepairVerificationDecision,
    RepairWork,
    RepairWorkState,
    TASK_KIND_REPAIR_FOLLOW_UP,
)
from app.v1.models.rent_payment import Operation, Task
from app.v1.services.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)


# ---------- result types ----------


@dataclass(frozen=True)
class ReportResult:
    """Result of opening a repair report.

    ``replayed=True`` means an identical (org_id, idempotency_key,
    payload_hash) was already stored; the existing report is returned.
    The router maps this to 200 OK instead of 201 Created.
    """

    replayed: bool
    report: RepairReport


@dataclass(frozen=True)
class QuoteResult:
    """Result of submitting a quote. Replay semantics mirror ReportResult."""

    replayed: bool
    quote: RepairQuote


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
    report_id: Optional[int],
    quote_id: Optional[int] = None,
    work_id: Optional[int] = None,
    claim_id: Optional[int] = None,
    kind: RepairActivityKind,
    actor_user_id: Optional[int],
    detail: Optional[str] = None,
) -> None:
    db.add(
        RepairActivity(
            org_id=org_id,
            report_id=report_id,
            quote_id=quote_id,
            work_id=work_id,
            claim_id=claim_id,
            kind=kind.value,
            detail=detail,
            actor_user_id=actor_user_id,
            occurred_at=utcnow(),
        )
    )


def _verified_count(db: Session, *, report_id: int) -> int:
    """Count of verified decisions on this report that are still active.

    Only ``RepairVerification`` rows with ``decision='VERIFIED'`` AND
    ``reversed_by_verification_id IS NULL`` contribute. This excludes
    verifications that have been superseded by a later REVERSED row.
    """
    return (
        db.query(RepairVerification)
        .filter(
            RepairVerification.report_id == report_id,
            RepairVerification.decision == (
                RepairVerificationDecision.VERIFIED.value
            ),
            RepairVerification.reversed_by_verification_id.is_(None),
        )
        .count()
    )


def _close_operation(
    db: Session,
    *,
    report: RepairReport,
    operation: Operation,
    actor_user_id: Optional[int],
) -> None:
    """Closure gate. Idempotent: safe to call multiple times.

    Sets report.state=COMPLETED + Operation.state=resolved. Cancels any
    open follow-up Task projections. Logs a COMPLETED activity entry.

    The gate is fired ONLY when:
      - the report has at least one CompletionClaim, AND
      - that claim has at least one active VERIFIED decision.

    This is the single closure path. NO other code path may resolve
    the Operation or set the report to COMPLETED.
    """
    claim_count = (
        db.query(RepairCompletionClaim)
        .filter(RepairCompletionClaim.report_id == report.id)
        .count()
    )
    if claim_count == 0:
        return  # No claim yet — cannot close.
    if _verified_count(db, report_id=report.id) < 1:
        return  # No active verified decision — cannot close.
    if report.state == RepairState.COMPLETED.value:
        return  # Already closed — idempotent no-op.
    report.state = RepairState.COMPLETED.value
    report.completed_at = utcnow()
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
        org_id=report.org_id,
        report_id=report.id,
        kind=RepairActivityKind.COMPLETED,
        actor_user_id=actor_user_id,
    )
    db.flush()


def _reopen(
    db: Session,
    *,
    report: RepairReport,
    operation: Operation,
    actor_user_id: Optional[int],
) -> None:
    """Reopen a previously-closed repair when a verified decision is reversed.

    Restores the report to COMPLETION_CLAIMED and the Operation to
    in_progress. Clears report.completed_at and operation.resolved_at.
    """
    report.state = RepairState.COMPLETION_CLAIMED.value
    report.completed_at = None
    operation.state = OperationState.IN_PROGRESS.value
    operation.resolved_at = None
    _log_activity(
        db,
        org_id=report.org_id,
        report_id=report.id,
        kind=RepairActivityKind.REOPENED,
        actor_user_id=actor_user_id,
    )
    db.flush()


def _bump_to_in_progress(operation: Operation) -> bool:
    """OPEN → IN_PROGRESS. Never advances to RESOLVED."""
    if operation.state == OperationState.OPEN.value:
        operation.state = OperationState.IN_PROGRESS.value
        return True
    return False


# ---------- service ----------


class RepairService:
    """Cohesive application/domain service for the repair lifecycle."""

    def __init__(self, db: Session) -> None:
        self.db = db

    # ---- read helpers ----

    def get_report(
        self,
        principal: Principal,
        *,
        org_id: int,
        report_id: int,
    ) -> RepairReport:
        require_org_scope(principal, org_id)
        report = self.db.get(RepairReport, report_id)
        if report is None or report.org_id != org_id:
            raise NotFoundError(
                f"repair report {report_id} not found in org {org_id}",
            )
        return report

    def get_operation(
        self,
        principal: Principal,
        *,
        org_id: int,
        report_id: int,
    ) -> Operation:
        require_org_scope(principal, org_id)
        self.get_report(principal, org_id=org_id, report_id=report_id)
        op = (
            self.db.query(Operation)
            .filter(
                Operation.org_id == org_id,
                Operation.subject_type == OPERATION_SUBJECT_REPAIR_REPORT,
                Operation.subject_id == report_id,
            )
            .first()
        )
        if op is None:
            raise NotFoundError(
                f"operation for repair report {report_id} not found",
            )
        return op

    def list_reports(
        self,
        principal: Principal,
        *,
        org_id: int,
        state: Optional[str] = None,
        category: Optional[str] = None,
    ) -> list[RepairReport]:
        require_org_scope(principal, org_id)
        q = self.db.query(RepairReport).filter(RepairReport.org_id == org_id)
        if state is not None:
            q = q.filter(RepairReport.state == state)
        if category is not None:
            q = q.filter(RepairReport.category == category)
        return q.order_by(RepairReport.id.asc()).all()

    def list_quotes(
        self,
        principal: Principal,
        *,
        org_id: int,
        report_id: int,
    ) -> list[RepairQuote]:
        require_org_scope(principal, org_id)
        self.get_report(principal, org_id=org_id, report_id=report_id)
        return (
            self.db.query(RepairQuote)
            .filter(
                RepairQuote.org_id == org_id,
                RepairQuote.report_id == report_id,
            )
            .order_by(RepairQuote.id.asc())
            .all()
        )

    def list_work(
        self,
        principal: Principal,
        *,
        org_id: int,
        report_id: int,
    ) -> list[RepairWork]:
        require_org_scope(principal, org_id)
        self.get_report(principal, org_id=org_id, report_id=report_id)
        return (
            self.db.query(RepairWork)
            .filter(
                RepairWork.org_id == org_id,
                RepairWork.report_id == report_id,
            )
            .order_by(RepairWork.id.asc())
            .all()
        )

    def list_completion_claims(
        self,
        principal: Principal,
        *,
        org_id: int,
        report_id: int,
    ) -> list[RepairCompletionClaim]:
        require_org_scope(principal, org_id)
        self.get_report(principal, org_id=org_id, report_id=report_id)
        return (
            self.db.query(RepairCompletionClaim)
            .filter(
                RepairCompletionClaim.org_id == org_id,
                RepairCompletionClaim.report_id == report_id,
            )
            .order_by(RepairCompletionClaim.id.asc())
            .all()
        )

    def list_verifications(
        self,
        principal: Principal,
        *,
        org_id: int,
        report_id: int,
    ) -> list[RepairVerification]:
        require_org_scope(principal, org_id)
        self.get_report(principal, org_id=org_id, report_id=report_id)
        return (
            self.db.query(RepairVerification)
            .filter(
                RepairVerification.org_id == org_id,
                RepairVerification.report_id == report_id,
            )
            .order_by(RepairVerification.id.asc())
            .all()
        )

    def list_activity(
        self,
        principal: Principal,
        *,
        org_id: int,
        report_id: int,
    ) -> list[RepairActivity]:
        require_org_scope(principal, org_id)
        self.get_report(principal, org_id=org_id, report_id=report_id)
        return (
            self.db.query(RepairActivity)
            .filter(
                RepairActivity.org_id == org_id,
                RepairActivity.report_id == report_id,
            )
            .order_by(RepairActivity.occurred_at.asc(), RepairActivity.id.asc())
            .all()
        )

    # ---- report lifecycle ----

    def open_report(
        self,
        principal: Principal,
        *,
        org_id: int,
        unit_id: int,
        title: str,
        description: str,
        category: str,
        severity: str,
        linked_expense_payment_id: Optional[int],
        idempotency_key: str,
    ) -> ReportResult:
        """Open a new repair report. Idempotent on (org_id, key)."""
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER, Role.SECRETARY)
        key = normalize_idempotency_key(idempotency_key)
        if category not in REPAIR_CATEGORIES:
            raise ValidationError(
                f"unknown category {category!r} "
                f"(must be one of: {list(REPAIR_CATEGORIES)})",
            )
        if severity not in REPAIR_SEVERITIES:
            raise ValidationError(
                f"unknown severity {severity!r} "
                f"(must be one of: {list(REPAIR_SEVERITIES)})",
            )
        # Confirm the unit belongs to this org.
        from app.v1.models.property import Unit
        unit = self.db.get(Unit, unit_id)
        if unit is None or unit.org_id != org_id:
            raise NotFoundError(f"unit {unit_id} not found in org {org_id}")
        # Compute payload_hash from the canonical report input.
        payload = {
            "unit_id": unit_id,
            "title": title,
            "description": description,
            "category": category,
            "severity": severity,
            "linked_expense_payment_id": linked_expense_payment_id,
        }
        payload_hash = compute_payload_hash(payload)
        existing = (
            self.db.query(RepairReport)
            .filter(
                RepairReport.org_id == org_id,
                RepairReport.idempotency_key == key,
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
                report_id=existing.id,
                kind=RepairActivityKind.REPORT_REPLAYED,
                actor_user_id=principal.user_id,
            )
            self.db.commit()
            return ReportResult(replayed=True, report=existing)
        report = RepairReport(
            org_id=org_id,
            unit_id=unit_id,
            title=title,
            description=description,
            category=category,
            severity=severity,
            state=RepairState.REPORTED.value,
            reported_by_user_id=principal.user_id,
            idempotency_key=key,
            payload_hash=payload_hash,
            linked_expense_payment_id=linked_expense_payment_id,
        )
        self.db.add(report)
        self.db.flush()
        # Create the linked Operation polymorphically.
        operation = Operation(
            org_id=org_id,
            kind=OPERATION_KIND_REPAIR,
            subject_type=OPERATION_SUBJECT_REPAIR_REPORT,
            subject_id=report.id,
            state=OperationState.OPEN.value,
        )
        self.db.add(operation)
        self.db.flush()
        _log_activity(
            self.db,
            org_id=org_id,
            report_id=report.id,
            kind=RepairActivityKind.REPORTED,
            actor_user_id=principal.user_id,
        )
        self.db.commit()
        return ReportResult(replayed=False, report=report)

    def confirm_report(
        self,
        principal: Principal,
        *,
        org_id: int,
        report_id: int,
    ) -> RepairReport:
        """OWNER acknowledges the report is real. REPORTED → CONFIRMED."""
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER)
        report = self.get_report(principal, org_id=org_id, report_id=report_id)
        if report.state != RepairState.REPORTED.value:
            raise ConflictError(
                f"cannot confirm a non-REPORTED report "
                f"(state={report.state})",
            )
        report.state = RepairState.CONFIRMED.value
        op = self.get_operation(principal, org_id=org_id, report_id=report_id)
        _bump_to_in_progress(op)
        _log_activity(
            self.db,
            org_id=org_id,
            report_id=report.id,
            kind=RepairActivityKind.CONFIRMED,
            actor_user_id=principal.user_id,
        )
        self.db.commit()
        return report

    def assign_technician(
        self,
        principal: Principal,
        *,
        org_id: int,
        report_id: int,
        technician_name: str,
        technician_source: str,
        technician_eta_at: Optional[datetime],
    ) -> RepairReport:
        """Attach a technician. CONFIRMED → AWAITING_TECHNICIAN.

        External (third-party) technicians are allowed; the report
        then sits in AWAITING_TECHNICIAN until the technician
        physically arrives (recorded as a work event with
        state=STARTED by the route handler in the next step).
        """
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER, Role.SECRETARY)
        if technician_source not in REPAIR_TECHNICIAN_SOURCES:
            raise ValidationError(
                f"unknown technician_source {technician_source!r} "
                f"(must be one of: {list(REPAIR_TECHNICIAN_SOURCES)})",
            )
        report = self.get_report(principal, org_id=org_id, report_id=report_id)
        if report.state != RepairState.CONFIRMED.value:
            raise ConflictError(
                f"cannot assign a technician to a non-CONFIRMED report "
                f"(state={report.state})",
            )
        report.technician_name = technician_name
        report.technician_source = technician_source
        report.technician_eta_at = technician_eta_at
        report.state = RepairState.AWAITING_TECHNICIAN.value
        _log_activity(
            self.db,
            org_id=org_id,
            report_id=report.id,
            kind=RepairActivityKind.TECHNICIAN_ASSIGNED,
            actor_user_id=principal.user_id,
            detail=(
                f"{technician_name} ({technician_source})"
                if technician_eta_at is None
                else (
                    f"{technician_name} ({technician_source}) "
                    f"ETA={technician_eta_at.isoformat()}"
                )
            ),
        )
        self.db.commit()
        return report

    def request_quote(
        self,
        principal: Principal,
        *,
        org_id: int,
        report_id: int,
    ) -> RepairReport:
        """Mark the report as needing a quote. AWAITING_TECHNICIAN → QUOTE_REQUESTED."""
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER, Role.SECRETARY)
        report = self.get_report(principal, org_id=org_id, report_id=report_id)
        if report.state not in (
            RepairState.AWAITING_TECHNICIAN.value,
            RepairState.QUOTE_REQUESTED.value,  # idempotent re-request
        ):
            raise ConflictError(
                f"cannot request a quote for a report in state "
                f"{report.state!r}",
            )
        report.state = RepairState.QUOTE_REQUESTED.value
        _log_activity(
            self.db,
            org_id=org_id,
            report_id=report.id,
            kind=RepairActivityKind.QUOTE_REQUESTED,
            actor_user_id=principal.user_id,
        )
        self.db.commit()
        return report

    def submit_quote(
        self,
        principal: Principal,
        *,
        org_id: int,
        report_id: int,
        amount: Decimal | str | int,
        description: str,
        technician_name: str,
        idempotency_key: str,
    ) -> QuoteResult:
        """Technician submits a quote. Idempotent on (org_id, key)."""
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER, Role.SECRETARY)
        key = normalize_idempotency_key(idempotency_key)
        amount_dec = parse_money(amount)
        report = self.get_report(principal, org_id=org_id, report_id=report_id)
        if report.state not in (
            RepairState.QUOTE_REQUESTED.value,
            RepairState.QUOTE_RECEIVED.value,  # allow re-quote before approve
        ):
            raise ConflictError(
                f"cannot submit a quote for a report in state "
                f"{report.state!r}",
            )
        payload = {
            "report_id": report_id,
            "amount": str(amount_dec),
            "description": description,
            "technician_name": technician_name,
        }
        payload_hash = compute_payload_hash(payload)
        # Idempotency scoped to (org_id, key) globally — same key with
        # different payload across the whole org is a conflict.
        existing = (
            self.db.query(RepairQuote)
            .filter(
                RepairQuote.org_id == org_id,
                RepairQuote.idempotency_key == key,
            )
            .first()
        )
        if existing is not None:
            if existing.payload_hash != payload_hash:
                raise IdempotencyConflictError(
                    f"idempotency key {key!r} reused with a different payload",
                )
            self.db.commit()
            return QuoteResult(replayed=True, quote=existing)
        quote = RepairQuote(
            org_id=org_id,
            report_id=report.id,
            amount=amount_dec,
            description=description,
            decision=RepairQuoteDecision.SUBMITTED.value,
            technician_name=technician_name,
            submitted_by_user_id=principal.user_id,
        )
        quote.idempotency_key = key
        quote.payload_hash = payload_hash
        self.db.add(quote)
        self.db.flush()
        report.state = RepairState.QUOTE_RECEIVED.value
        report.quoted_amount = amount_dec
        _log_activity(
            self.db,
            org_id=org_id,
            report_id=report.id,
            quote_id=quote.id,
            kind=RepairActivityKind.QUOTE_SUBMITTED,
            actor_user_id=principal.user_id,
            detail=f"{amount_dec} by {technician_name}",
        )
        self.db.commit()
        return QuoteResult(replayed=False, quote=quote)

    def approve_quote(
        self,
        principal: Principal,
        *,
        org_id: int,
        report_id: int,
        quote_id: int,
    ) -> RepairReport:
        """OWNER approves a quote. QUOTE_RECEIVED → QUOTE_APPROVED.

        Does NOT start work yet. Work is recorded via ``record_work``,
        which advances to IN_PROGRESS.
        """
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER)
        report = self.get_report(principal, org_id=org_id, report_id=report_id)
        if report.state != RepairState.QUOTE_RECEIVED.value:
            raise ConflictError(
                f"cannot approve a quote for a report in state "
                f"{report.state!r}",
            )
        quote = self.db.get(RepairQuote, quote_id)
        if quote is None or quote.org_id != org_id or quote.report_id != report.id:
            raise NotFoundError(
                f"quote {quote_id} not found in org {org_id} for report {report_id}",
            )
        if quote.decision != RepairQuoteDecision.SUBMITTED.value:
            raise ConflictError(
                f"quote {quote_id} is not pending (decision={quote.decision})",
            )
        quote.decision = RepairQuoteDecision.APPROVED.value
        quote.decided_by_user_id = principal.user_id
        quote.decided_at = utcnow()
        report.state = RepairState.QUOTE_APPROVED.value
        _log_activity(
            self.db,
            org_id=org_id,
            report_id=report.id,
            quote_id=quote.id,
            kind=RepairActivityKind.QUOTE_APPROVED,
            actor_user_id=principal.user_id,
        )
        self.db.commit()
        return report

    def reject_quote(
        self,
        principal: Principal,
        *,
        org_id: int,
        report_id: int,
        quote_id: int,
        reason: str,
    ) -> RepairReport:
        """OWNER rejects a quote. The report returns to QUOTE_REQUESTED.

        Rejection of a quote is NOT rejection of the repair. The repair
        is still open; the technician (or a new one) can submit a
        new quote via ``submit_quote`` (with a new idempotency key).
        """
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER)
        if not reason or not reason.strip():
            raise ValidationError("rejection reason is required")
        report = self.get_report(principal, org_id=org_id, report_id=report_id)
        if report.state != RepairState.QUOTE_RECEIVED.value:
            raise ConflictError(
                f"cannot reject a quote for a report in state "
                f"{report.state!r}",
            )
        quote = self.db.get(RepairQuote, quote_id)
        if quote is None or quote.org_id != org_id or quote.report_id != report.id:
            raise NotFoundError(
                f"quote {quote_id} not found in org {org_id} for report {report_id}",
            )
        if quote.decision != RepairQuoteDecision.SUBMITTED.value:
            raise ConflictError(
                f"quote {quote_id} is not pending (decision={quote.decision})",
            )
        quote.decision = RepairQuoteDecision.REJECTED.value
        quote.decided_by_user_id = principal.user_id
        quote.decided_at = utcnow()
        quote.reason = reason
        # Return to QUOTE_REQUESTED so a new quote can be submitted.
        report.state = RepairState.QUOTE_REQUESTED.value
        report.quoted_amount = None
        _log_activity(
            self.db,
            org_id=org_id,
            report_id=report.id,
            quote_id=quote.id,
            kind=RepairActivityKind.QUOTE_REJECTED,
            actor_user_id=principal.user_id,
            detail=reason,
        )
        self.db.commit()
        return report

    def record_work(
        self,
        principal: Principal,
        *,
        org_id: int,
        report_id: int,
        state: str,
        note: str,
    ) -> RepairWork:
        """Append a work progress event. Advances the report state.

        - First STARTED on a QUOTE_APPROVED report advances to IN_PROGRESS.
        - Any other state on an IN_PROGRESS report stays in IN_PROGRESS.
        - DONE_ON_SITE on an IN_PROGRESS report advances to ... still
          IN_PROGRESS. The report only reaches COMPLETION_CLAIMED when
          a CompletionClaim is recorded (next method).
        """
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER, Role.SECRETARY)
        if state not in REPAIR_WORK_STATES:
            raise ValidationError(
                f"unknown work state {state!r} "
                f"(must be one of: {list(REPAIR_WORK_STATES)})",
            )
        report = self.get_report(principal, org_id=org_id, report_id=report_id)
        if report.state not in (
            RepairState.QUOTE_APPROVED.value,
            RepairState.IN_PROGRESS.value,
        ):
            raise ConflictError(
                f"cannot record work for a report in state "
                f"{report.state!r}",
            )
        work = RepairWork(
            org_id=org_id,
            report_id=report.id,
            state=state,
            note=note,
            actor_user_id=principal.user_id,
        )
        self.db.add(work)
        self.db.flush()
        if state == RepairWorkState.STARTED.value and (
            report.state == RepairState.QUOTE_APPROVED.value
        ):
            report.state = RepairState.IN_PROGRESS.value
            kind = RepairActivityKind.WORK_STARTED
        elif state == RepairWorkState.BLOCKED.value:
            kind = RepairActivityKind.WORK_BLOCKED
        elif state == RepairWorkState.PROGRESS.value:
            kind = RepairActivityKind.WORK_PROGRESS
        elif state == RepairWorkState.DONE_ON_SITE.value:
            # CompletionClaim drives COMPLETION_CLAIMED; DONE_ON_SITE
            # alone does NOT move the report state.
            kind = RepairActivityKind.WORK_DONE_ON_SITE
        else:
            kind = RepairActivityKind.WORK_PROGRESS
        _log_activity(
            self.db,
            org_id=org_id,
            report_id=report.id,
            work_id=work.id,
            kind=kind,
            actor_user_id=principal.user_id,
            detail=note,
        )
        self.db.commit()
        return work

    def claim_completion(
        self,
        principal: Principal,
        *,
        org_id: int,
        report_id: int,
        summary: str,
    ) -> RepairCompletionClaim:
        """Technician / secretary says the work is done on-site.

        Records a completion claim. The report advances to
        COMPLETION_CLAIMED. The Operation only resolves when the OWNER
        verifies a real completion via ``verify_completion``.
        """
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER, Role.SECRETARY)
        report = self.get_report(principal, org_id=org_id, report_id=report_id)
        if report.state not in (
            RepairState.IN_PROGRESS.value,
            RepairState.COMPLETION_CLAIMED.value,  # allow re-claim
        ):
            raise ConflictError(
                f"cannot claim completion for a report in state "
                f"{report.state!r}",
            )
        claim = RepairCompletionClaim(
            org_id=org_id,
            report_id=report.id,
            summary=summary,
            claimed_by_user_id=principal.user_id,
        )
        self.db.add(claim)
        self.db.flush()
        report.state = RepairState.COMPLETION_CLAIMED.value
        _log_activity(
            self.db,
            org_id=org_id,
            report_id=report.id,
            claim_id=claim.id,
            kind=RepairActivityKind.COMPLETION_CLAIMED,
            actor_user_id=principal.user_id,
            detail=summary,
        )
        self.db.commit()
        return claim

    def verify_completion(
        self,
        principal: Principal,
        *,
        org_id: int,
        report_id: int,
        reason: str,
    ) -> RepairReport:
        """OWNER verifies a real completion. Closure gate.

        The Operation resolves and the report reaches COMPLETED only
        when this method records an active VERIFIED decision. Rejecting
        keeps the Operation open and returns the report to
        COMPLETION_CLAIMED.
        """
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER)
        if not reason or not reason.strip():
            raise ValidationError("verification reason is required")
        report = self.get_report(principal, org_id=org_id, report_id=report_id)
        if report.state != RepairState.COMPLETION_CLAIMED.value:
            raise ConflictError(
                f"cannot verify a report in state {report.state!r}",
            )
        # Record the VERIFIED decision (append-only).
        verification = RepairVerification(
            org_id=org_id,
            report_id=report.id,
            decision=RepairVerificationDecision.VERIFIED.value,
            verifier_user_id=principal.user_id,
            reason=reason,
        )
        self.db.add(verification)
        self.db.flush()
        op = self.get_operation(principal, org_id=org_id, report_id=report_id)
        # Trigger the closure gate.
        _close_operation(
            self.db,
            report=report,
            operation=op,
            actor_user_id=principal.user_id,
        )
        _log_activity(
            self.db,
            org_id=org_id,
            report_id=report.id,
            kind=RepairActivityKind.VERIFIED,
            actor_user_id=principal.user_id,
            detail=reason,
        )
        self.db.commit()
        return report

    def reject_completion(
        self,
        principal: Principal,
        *,
        org_id: int,
        report_id: int,
        reason: str,
    ) -> RepairReport:
        """OWNER rejects a completion claim (work not actually done).

        The report returns to IN_PROGRESS and the Operation stays open.
        A new CompletionClaim may be filed later.
        """
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER)
        if not reason or not reason.strip():
            raise ValidationError("rejection reason is required")
        report = self.get_report(principal, org_id=org_id, report_id=report_id)
        if report.state != RepairState.COMPLETION_CLAIMED.value:
            raise ConflictError(
                f"cannot reject a report in state {report.state!r}",
            )
        verification = RepairVerification(
            org_id=org_id,
            report_id=report.id,
            decision=RepairVerificationDecision.REJECTED.value,
            verifier_user_id=principal.user_id,
            reason=reason,
        )
        self.db.add(verification)
        self.db.flush()
        # Back to IN_PROGRESS so more work can be recorded.
        report.state = RepairState.IN_PROGRESS.value
        op = self.get_operation(principal, org_id=org_id, report_id=report_id)
        if op.state == OperationState.RESOLVED.value:
            op.state = OperationState.IN_PROGRESS.value
            op.resolved_at = None
        _log_activity(
            self.db,
            org_id=org_id,
            report_id=report.id,
            kind=RepairActivityKind.REJECTED,
            actor_user_id=principal.user_id,
            detail=reason,
        )
        self.db.commit()
        return report

    def reverse_verification(
        self,
        principal: Principal,
        *,
        org_id: int,
        report_id: int,
        reason: str,
    ) -> RepairReport:
        """OWNER reverses a previous VERIFIED decision. Reopens the repair.

        The original VERIFIED row stays in the audit log but is
        superseded (``reversed_by_verification_id`` is set on each
        previously active VERIFIED row to the new REVERSED row id).
        The closure gate is no longer satisfied, so the report returns
        to COMPLETION_CLAIMED and the Operation returns to in_progress.
        """
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER)
        if not reason or not reason.strip():
            raise ValidationError("reversal reason is required")
        report = self.get_report(principal, org_id=org_id, report_id=report_id)
        if report.state != RepairState.COMPLETED.value:
            raise ConflictError(
                f"cannot reverse a non-COMPLETED report (state={report.state})",
            )
        reversal = RepairVerification(
            org_id=org_id,
            report_id=report.id,
            decision=RepairVerificationDecision.REVERSED.value,
            verifier_user_id=principal.user_id,
            reason=reason,
        )
        self.db.add(reversal)
        self.db.flush()  # ensure reversal.id is populated
        # Mark all currently-active VERIFIED rows as superseded.
        active_verified = (
            self.db.query(RepairVerification)
            .filter(
                RepairVerification.report_id == report.id,
                RepairVerification.decision == (
                    RepairVerificationDecision.VERIFIED.value
                ),
                RepairVerification.reversed_by_verification_id.is_(None),
            )
            .all()
        )
        for v in active_verified:
            v.reversed_by_verification_id = reversal.id
        op = self.get_operation(principal, org_id=org_id, report_id=report_id)
        _reopen(
            self.db,
            report=report,
            operation=op,
            actor_user_id=principal.user_id,
        )
        _log_activity(
            self.db,
            org_id=org_id,
            report_id=report.id,
            kind=RepairActivityKind.REVERSED,
            actor_user_id=principal.user_id,
            detail=reason,
        )
        self.db.commit()
        return report

    def cancel_report(
        self,
        principal: Principal,
        *,
        org_id: int,
        report_id: int,
        reason: str,
    ) -> RepairReport:
        """Terminal CANCELLED state. No further transitions allowed."""
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER)
        if not reason or not reason.strip():
            raise ValidationError("cancellation reason is required")
        report = self.get_report(principal, org_id=org_id, report_id=report_id)
        if report.state in (
            RepairState.COMPLETED.value,
            RepairState.CANCELLED.value,
        ):
            raise ConflictError(
                f"cannot cancel a terminal report (state={report.state})",
            )
        report.state = RepairState.CANCELLED.value
        op = self.get_operation(principal, org_id=org_id, report_id=report_id)
        op.state = OperationState.CANCELLED.value
        op.resolved_at = utcnow()
        # Cancel any open Task projections.
        open_tasks = (
            self.db.query(Task)
            .filter(
                Task.operation_id == op.id,
                Task.state == "open",
            )
            .all()
        )
        for t in open_tasks:
            t.state = "cancelled"
        _log_activity(
            self.db,
            org_id=org_id,
            report_id=report.id,
            kind=RepairActivityKind.CANCELLED,
            actor_user_id=principal.user_id,
            detail=reason,
        )
        self.db.commit()
        return report

    # ---- follow-up (Task projection) ----

    def create_follow_up(
        self,
        principal: Principal,
        *,
        org_id: int,
        report_id: int,
        title: str,
        due_at: Optional[datetime] = None,
    ) -> Task:
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER, Role.SECRETARY)
        report = self.get_report(principal, org_id=org_id, report_id=report_id)
        op = self.get_operation(principal, org_id=org_id, report_id=report_id)
        # Creating a follow-up is non-trivial business activity but does
        # NOT advance to RESOLVED.
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
            kind=TASK_KIND_REPAIR_FOLLOW_UP,
            title=title,
            state="open",
            due_at=due_at,
        )
        self.db.add(task)
        self.db.flush()
        _log_activity(
            self.db,
            org_id=org_id,
            report_id=report.id,
            kind=RepairActivityKind.FOLLOW_UP_CREATED,
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
        report_id: int,
    ) -> list[Task]:
        require_org_scope(principal, org_id)
        op = self.get_operation(principal, org_id=org_id, report_id=report_id)
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
        """Mark a follow-up Task as done.

        NEVER closes the linked Operation / report. Operation closure is
        gated exclusively by ``verify_completion``.
        """
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
        op = self.db.get(Operation, task.operation_id)
        report_id: Optional[int] = None
        if op is not None and op.subject_type == (
            OPERATION_SUBJECT_REPAIR_REPORT
        ):
            report_id = op.subject_id
        _log_activity(
            self.db,
            org_id=org_id,
            report_id=report_id,
            kind=RepairActivityKind.FOLLOW_UP_DONE,
            actor_user_id=principal.user_id,
            detail=task.title,
        )
        self.db.commit()
        return task


__all__ = [
    "QuoteResult",
    "ReportResult",
    "RepairService",
]
