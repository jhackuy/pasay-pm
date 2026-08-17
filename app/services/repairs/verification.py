"""REPAIR-AI-EMPLOYEE-WORKFLOW-008A — Repair Verification gate.

A Repair Operation may ONLY be CLOSED after a real verification (008A §7).
Payment, proposal approval, reminder sent, or vendor being contacted are each
REQUIRED-but-NOT-SUFFICIENT and never fire closure on their own.

Allowed valid results (at least one):
- A Secretary explicitly confirms the repair is fixed/completed;
- The Owner explicitly confirms it;
- Repair-result evidence + human confirmation exists;
- A credibly structured completion event exists (source retained in
  ``verification_result`` / ``details``).

Closure records ``verified_by``, ``verified_at``, ``verification_result`` and
``closure_reason`` on the Repair Operation.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.repair import (
    RepairAction,
    RepairActionStatus,
    RepairOperation,
    RepairOperationStatus,
    RepairProposal,
)
from app.services.repairs.state import ClosureSignal, ensure_closable_via_verification


class VerificationError(Exception):
    """Verification could not be recorded (e.g. invalid closure signal)."""


def mark_repair_completed(
    db: Session,
    repair: RepairOperation,
    *,
    confirmed_by: int | None = None,
    verification_result: str | None = None,
    evidence_ids: list[int] | None = None,
    source: str | None = None,
    now: datetime | None = None,
) -> RepairOperation:
    """A human confirms the real-world repair work is DONE.

    Only moves OPEN/IN_PROGRESS/WAITING_* repairs into VERIFYING — it does NOT
    close them. The verification pass that follows may close it. Idempotent for
    already-VERIFYING repairs; refused for terminal statuses."""
    now = now or datetime.now(timezone.utc)
    if repair.status == RepairOperationStatus.VERIFYING:
        # Re-stating completion is a no-op (already awaiting verification).
        return repair
    if repair.status == RepairOperationStatus.CLOSED:
        raise VerificationError("Repair is already CLOSED")
    if repair.status == RepairOperationStatus.CANCELLED:
        raise VerificationError("Repair is cancelled")
    if repair.status.value not in _COMPLETABLE:
        raise VerificationError(
            f"Repair {repair.status.value} cannot be marked completed directly"
        )
    repair.status = RepairOperationStatus.VERIFYING
    repair.next_action = (
        "Awaiting verification: confirm the problem is actually fixed before closing."
    )
    repair.waiting_on = "secretary"
    repair.verified_by = confirmed_by
    repair.verified_at = now
    repair.verification_result = verification_result or source or "completed"
    if evidence_ids:
        details = dict(repair.details or {})
        details["completion_evidence_ids"] = evidence_ids
        repair.details = details
    repair.updated_at = now
    db.flush()
    return repair


# Statuses a human-completed repair can VERIFY from.
_COMPLETABLE = frozenset(
    {"OPEN", "IN_PROGRESS", "WAITING_HUMAN", "WAITING_VENDOR", "WAITING_APPROVAL", "WAITING_PAYMENT"}
)


def verify_and_close(
    db: Session,
    repair: RepairOperation,
    *,
    verified_by: int | None = None,
    verification_result: str | None = None,
    closure_signal: str = ClosureSignal.HUMAN_CONFIRMED.value,
    source: str | None = None,
    now: datetime | None = None,
) -> RepairOperation:
    """Record a REAL verification and CLOSE the repair.

    Guarded: an invalid closure signal is refused so the only path into CLOSED
    is verification. Records verified_by/verified_at/result + closure reason.

    NOTE: this must never be called by the payment path, the approve path, the
    reminder path, or the vendor-contact path. Calling it from those points is
    a P0 regression.
    """
    now = now or datetime.now(timezone.utc)
    ensure_closable_via_verification(closure_signal)
    if repair.status == RepairOperationStatus.CLOSED:
        return repair  # idempotent
    if repair.status == RepairOperationStatus.CANCELLED:
        raise VerificationError("Cannot close a cancelled repair")
    repair.status = RepairOperationStatus.CLOSED
    repair.verified_by = verified_by
    repair.verified_at = now
    repair.verification_result = verification_result or source or "verified"
    repair.closure_reason = closure_signal
    repair.closed_at = now
    repair.next_action = "Repair closed."
    repair.waiting_on = None
    repair.blocked_reason = None
    repair.updated_at = now
    db.flush()
    # Complete any active VERIFY/RECORD_RESULT actions so they no longer pend.
    _close_result_actions(db, repair.id, resolved_by=verified_by, now=now)
    return repair


def _close_result_actions(
    db: Session, repair_id: int, *, resolved_by: int | None, now: datetime
) -> None:
    actions = (
        db.query(RepairAction)
        .filter(
            RepairAction.repair_id == repair_id,
            RepairAction.status.in_(
                [RepairActionStatus.PENDING, RepairActionStatus.IN_PROGRESS]
            ),
        )
        .all()
    )
    for action in actions:
        action.status = RepairActionStatus.COMPLETED
        action.resolved_at = now
        action.resolved_by = resolved_by
        action.updated_at = now


def reopen_to_in_progress(
    db: Session,
    repair: RepairOperation,
    *,
    reason: str | None = None,
    by: int | None = None,
    now: datetime | None = None,
) -> RepairOperation:
    """A VERIFYING repair that is discovered NOT actually fixed drops back to
    IN_PROGRESS for rework (never closes)."""
    now = now or datetime.now(timezone.utc)
    if repair.status != RepairOperationStatus.VERIFYING:
        raise VerificationError("Only a VERIFYING repair can be sent back to work")
    repair.status = RepairOperationStatus.IN_PROGRESS
    repair.next_action = "Rework needed — mark the repair completed again when fixed."
    repair.waiting_on = "vendor"
    repair.blocked_reason = reason or "Verification found the problem is not resolved"
    repair.updated_at = now
    db.flush()
    return repair
