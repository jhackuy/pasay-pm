"""REPAIR-AI-EMPLOYEE-WORKFLOW-008A — Repair Verification gate.

A Repair Operation may ONLY be CLOSED after a real verification (008A §7).
Payment, proposal approval, reminder sent, or vendor being contacted are each
REQUIRED-but-NOT-SUFFICIENT and never fire closure on their own.

PASAY-MILESTONE-002 closures (invariants enforced here):
    * contractor says done ≠ repair closed: COMPLETION_EVENT closure-signal
      requires completion evidence_ids or structured evidence blob before
      we allow the gate to pass. Only HUMAN_CONFIRMED (Owner/Secretary
      on-site or explicit OK) is allowed without media evidence, because
      the human sign-off itself is the verification.
    * Task is only a human-action projection: once we pass the closure gate
      and move repair.status to CLOSED, we also mark linked OperationalTask
      rows COMPLETED (never the reverse — tasks never close a repair).

Allowed valid results (at least one):
- A Secretary explicitly confirms the repair is fixed/completed;
- The Owner explicitly confirms it;
- Repair-result evidence + human confirmation exists;
- A credibly structured completion event exists (source retained in
  ``verification_result`` / ``details``) WITH evidence if the signal is
  COMPLETION_EVENT (contractor-says-done is insufficient on its own).

Closure records ``verified_by``, ``verified_at``, ``verification_result`` and
``closure_reason`` on the Repair Operation.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.operations import (
    OperationalTask,
    OperationalTaskStatus,
    OperationalTaskType,
)
from app.models.repair import (
    RepairAction,
    RepairActionStatus,
    RepairOperation,
    RepairOperationStatus,
)
from app.services.audit import record_audit, serialize_row
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
    # Evidence must always be written (idempotently) regardless of status —
    # if pay-expense already moved the repair to VERIFYING before we record
    # the vendor's completion evidence, the guard still needs evidence to
    # satisfy the COMPLETION_EVENT gate.
    if evidence_ids:
        details = dict(repair.details or {})
        existing = details.get("completion_evidence_ids") or []
        merged = list({*existing, *evidence_ids})
        if merged != existing:
            details["completion_evidence_ids"] = merged
            repair.details = details
            repair.updated_at = now
            db.flush()
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
    repair.updated_at = now
    db.flush()
    return repair


# Statuses a human-completed repair can VERIFY from.
_COMPLETABLE = frozenset(
    {"OPEN", "IN_PROGRESS", "WAITING_HUMAN", "WAITING_VENDOR", "WAITING_APPROVAL", "WAITING_PAYMENT"}
)


def _evidence_present_for_close(repair: RepairOperation) -> bool:
    """Return True if completion evidence is present for closure.

    Evidence comes from:
    (a) repair.details['completion_evidence_ids'] being a non-empty list, OR
    (b) repair.evidence (JSONB evidence blob) being truthy with at least one
        media/record item (we accept any non-empty dict / non-empty list).
    """
    d = repair.details or {}
    ce = d.get("completion_evidence_ids")
    if isinstance(ce, list) and len(ce) > 0:
        return True
    ev = repair.evidence
    if isinstance(ev, (list, tuple)) and len(ev) > 0:
        return True
    if isinstance(ev, dict) and len(ev) > 0:
        return True
    return False


def _complete_linked_operational_tasks(
    db: Session,
    repair: RepairOperation,
    *,
    resolved_by: int | None,
    now: datetime,
) -> None:
    """Sync (closure truth → projection): any PENDING / IN_PROGRESS
    OperationalTask associated with the repair transitions to COMPLETED.

    The task is the PROJECTION (human-action to-do), not truth. The repair
    closing IS truth. Never the reverse.
    """
    candidates: list[OperationalTask] = []
    # (a) Direct operational_task_id link on repair row.
    if repair.operational_task_id is not None:
        t = db.get(OperationalTask, repair.operational_task_id)
        if t is not None:
            candidates.append(t)
    # (b) Tasks whose details JSONB contain repair_id, or dedupe_key
    #     references the repair_id (existing legacy projections).

    str_rid = str(repair.id)
    for t in (
        db.query(OperationalTask)
        .filter(
            OperationalTask.status.in_(
                (
                    OperationalTaskStatus.PENDING,
                    OperationalTaskStatus.IN_PROGRESS,
                )
            ),
            OperationalTask.task_type.in_(
                (
                    OperationalTaskType.APPROVAL_PENDING,
                    OperationalTaskType.PAYMENT_PENDING,
                    OperationalTaskType.FOLLOWUP,
                    OperationalTaskType.AC_MAINTENANCE,
                    OperationalTaskType.RENT_OVERDUE,
                )
            ),
        )
        .all()
    ):
        matched = False
        if t.dedupe_key and f"repair:{repair.id}" in t.dedupe_key:
            matched = True
        elif t.details:
            details_rid = t.details.get("repair_id")
            if details_rid is not None and str(details_rid) == str_rid:
                matched = True
        if matched:
            candidates.append(t)
    for task in candidates:
        if task.status == OperationalTaskStatus.COMPLETED:
            continue
        old_status = task.status.value if hasattr(task.status, "value") else str(task.status)
        task.status = OperationalTaskStatus.COMPLETED
        task.completed_at = now
        task.completed_by = resolved_by
        task.updated_by = resolved_by
        task.updated_at = now
        record_audit(
            db,
            table_name="operational_tasks",
            record_id=task.id,
            action="repair_closure_task_completed",
            actor_id=resolved_by,
            changed_fields={
                "status": [old_status, OperationalTaskStatus.COMPLETED.value],
                "repair_id": [None, repair.id],
            },
            old_value=serialize_row(task),
            new_value=serialize_row(task),
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

    PASAY-MILESTONE-002 closure evidence gate:
      * COMPLETION_EVENT (e.g. contractor says done / auto-event) —
        evidence_ids OR repair.evidence blob required; otherwise the gate
        raises VerificationError (contractor says done ≠ repair closed).
      * HUMAN_CONFIRMED (Owner/Secretary explicit sign-off) — evidence is
        recommended but the human is the authority on the real-world outcome;
        pass.

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
    # --- closure evidence gate (M002 invariant: contractor says done ≠ closed)
    # COMPLETION_EVENT (= structured "vendor/contractor completion signal" — ALWAYS requires
    # evidence regardless of verified_by; Owner sign-off. A human Owner/Secretary who wants
    # to close without evidence should use HUMAN_CONFIRMED (no evidence needed) instead.
    if (
        closure_signal == ClosureSignal.COMPLETION_EVENT.value
        and not _evidence_present_for_close(repair)
    ):
        raise VerificationError(
            "Closure signal COMPLETION_EVENT requires completion evidence_ids "
            "attached to the repair — contractor says done alone is not "
            "sufficient to close a repair. Use HUMAN_CONFIRMED if a human "
            "authority overrides without evidence."
        )
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
    # Sync truth → task projection (M002 truth consistency).
    _complete_linked_operational_tasks(
        db, repair, resolved_by=verified_by, now=now
    )
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
