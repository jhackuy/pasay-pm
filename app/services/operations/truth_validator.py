"""PASAY-MILESTONE-003 OP-TRUTH-003 Truth-First Task Completion Validator.

Canonical direction guard: an OperationalTask COMPLETED is a *projection* of real
business truth (Operation is truth -> Task is projection). A human cannot PATCH a task
COMPLETED unless the corresponding real-world business state says it is complete.

The HUMAN complete API endpoints (PATCH /operations/tasks/{id} status=COMPLETED and
POST /operations/tasks/{id}/complete) MUST route through this validator and raise 409
when the truth is missing. Fail-closed by default: any unknown (task_type, source_type)
combination that isn't explicitly mapped here produces a 409 with a structured
expected_truth / actual_truth / hint detail payload.

CUSTOM_TASK_COMPLETE policy (Owner configured per AGENTS contract, default = "A"):
  - "A": non-projection manual tasks (source_type empty or "conversation", and the
    task_type is not in the projection table) are considered non-projection user tasks
    and allowed through. A HUMAN principal can PATCH complete directly.
  - "B": strictly no HUMAN can PATCH complete any task; only the truth forward
    projection seam and system reconcile auto-close are allowed.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.deposit_settlement import DepositSettlement, DepositSettlementStatus
from app.models.evidence import Evidence, EvidenceCategory
from app.models.financial import Expense, ExpenseStatus, Income, IncomeStatus
from app.models.lease import Lease, LeaseStatus
from app.models.move_out import MoveOutInspection, MoveOutInspectionStatus
from app.models.operations import OperationalTask, OperationalTaskStatus, OperationalTaskType
from app.models.repair import RepairOperation, RepairOperationStatus
from app.services.expense_payment_truth import payment_truth as expense_payment_truth
from app.services.operations.rent_math import covered_periods, lease_periods
from app.services.rent_payment_truth import snapshot as rent_snapshot


@dataclass(frozen=True)
class TruthValidationResult:
    ok: bool
    expected_truth: str | None = None
    actual_truth: str | None = None
    hint: str | None = None


class TruthValidationError(HTTPException):
    """409 HTTP with canonical error JSON body so the bot/Mini App can render
    a specific truth-vs-expected instead of a generic conflict."""

    def __init__(self, task_id: int, result: TruthValidationResult):
        detail = {
            "reason": "task_completion_truth_missing",
            "task_id": task_id,
            "expected_truth": result.expected_truth,
            "actual_truth": result.actual_truth,
            "hint": result.hint,
        }
        super().__init__(status.HTTP_409_CONFLICT, detail)


CUSTOM_TASK_COMPLETE_POLICY = "A"


def _rent_fully_paid_for_task(db: Session, task: OperationalTask) -> TruthValidationResult:
    if task.source_type != "lease" or task.source_id is None:
        return TruthValidationResult(
            False,
            "source_type=lease with source_id",
            f"source_type={task.source_type!r} source_id={task.source_id!r}",
            "Rent tasks must bind a lease_id (source_id) and source_type='lease'.",
        )
    lease = db.get(Lease, task.source_id)
    if lease is None or lease.status != LeaseStatus.active or (lease.deleted_at is not None):
        return TruthValidationResult(
            False,
            "Active Lease row",
            "Lease missing/inactive",
            "The referenced lease was removed or is not active — reconcile will cancel this task.",
        )
    details = task.details or {}
    periods: list[str] = []
    if task.task_type == OperationalTaskType.RENT_OVERDUE:
        periods = list(details.get("periods") or [])
    else:
        single = details.get("period")
        if single:
            periods = [str(single)]
    if not periods:
        return TruthValidationResult(
            False,
            "task.details.period(s) present",
            "No period info on task.details",
            "Rent tasks require details.period / details.periods referencing the covered billing month(s).",
        )
    incomes = (
        db.query(Income)
        .filter(Income.lease_id == lease.id, Income.status == IncomeStatus.confirmed)
        .all()
    )
    periods_available = lease_periods(lease)
    covered = covered_periods(lease, periods_available, incomes)
    for p in periods:
        snap = rent_snapshot(db, lease.id, str(p))
        if p not in covered or not snap.is_fully_paid:
            return TruthValidationResult(
                False,
                f"RentPeriodTruth[{p}] fully paid",
                (
                    f"remaining={snap.remaining} "
                    f"verified_paid={snap.verified_paid_total} "
                    f"required={snap.required_amount}"
                ),
                "Use the canonical rent payment verification endpoint (create/verify a RentPaymentClaim) to close this item. PATCH COMPLETED cannot shortcut the truth.",
            )
    return TruthValidationResult(True)


def _lease_expiring_ok(db: Session, task: OperationalTask) -> TruthValidationResult:
    if task.source_type != "lease" or task.source_id is None:
        return TruthValidationResult(
            False,
            "source_type=lease + source_id",
            f"source_type={task.source_type!r}",
            "LEASE_EXPIRING tasks must bind a lease.",
        )
    lease = db.get(Lease, task.source_id)
    if lease is None:
        return TruthValidationResult(
            False,
            "Lease row",
            "Lease missing",
            "Reconcile will cancel this orphan task.",
        )
    renewed = (
        db.query(Lease)
        .filter(
            Lease.unit_id == lease.unit_id,
            Lease.id != lease.id,
            Lease.status == LeaseStatus.active,
            Lease.deleted_at.is_(None),
            Lease.end_date > lease.end_date,
            Lease.start_date <= lease.end_date,
        )
        .first()
    )
    if renewed is None:
        renewed = (
            db.query(Lease)
            .filter(
                Lease.unit_id == lease.unit_id,
                Lease.id != lease.id,
                Lease.status == LeaseStatus.active,
                Lease.deleted_at.is_(None),
                Lease.start_date >= lease.end_date,
            )
            .first()
        )
    if renewed is not None or lease.status != LeaseStatus.active or (lease.deleted_at is not None):
        return TruthValidationResult(True)
    return TruthValidationResult(
        False,
        "Lease renewed / cancelled",
        f"Lease still active through {lease.end_date}",
        "Either a renewal lease must exist, or the lease must be cancelled/inactive before this task can close.",
    )


def _approval_pending_ok(db: Session, task: OperationalTask) -> TruthValidationResult:
    if task.source_type != "expense" or task.source_id is None:
        return TruthValidationResult(
            False,
            "source_type=expense with source_id",
            f"source_type={task.source_type!r} source_id={task.source_id!r}",
            "Approval tasks must bind an expense_id (source_id).",
        )
    expense = db.get(Expense, task.source_id)
    if expense is None:
        return TruthValidationResult(
            False,
            "Expense row exists",
            "Expense missing",
            "Reconcile will cancel this orphan task.",
        )
    if expense.status in (
        ExpenseStatus.approved,
        ExpenseStatus.paid,
        ExpenseStatus.rejected,
        ExpenseStatus.reversed,
        ExpenseStatus.partially_paid,
        ExpenseStatus.payment_claimed,
    ):
        return TruthValidationResult(True)
    return TruthValidationResult(
        False,
        "Expense.status in {approved, paid, partially_paid, payment_claimed, rejected, reversed}",
        f"Expense.status={expense.status.value}",
        "Approve / reject the expense through the canonical expense decision endpoint. PATCH COMPLETED is not a substitute for the real approval.",
    )


def _payment_pending_ok(db: Session, task: OperationalTask) -> TruthValidationResult:
    if task.source_type != "expense" or task.source_id is None:
        return TruthValidationResult(
            False,
            "source_type=expense + source_id",
            f"source_type={task.source_type!r}",
            "Payment tasks must bind an expense_id (source_id).",
        )
    expense = db.get(Expense, task.source_id)
    if expense is None:
        return TruthValidationResult(
            False,
            "Expense row exists",
            "Expense missing",
            "Reconcile will cancel this orphan task.",
        )
    if expense.status in (ExpenseStatus.rejected, ExpenseStatus.reversed):
        return TruthValidationResult(True)
    truth = expense_payment_truth(db, expense)
    if truth.fully_paid:
        return TruthValidationResult(True)
    return TruthValidationResult(
        False,
        "ExpensePaymentTruth fully_paid OR status in {rejected, reversed}",
        (
            f"remaining={truth.remaining} verified_paid={truth.verified_paid} "
            f"expense.status={expense.status.value}"
        ),
        "Record and VERIFY an ExpensePaymentClaim through the canonical payment-verification endpoint; do not close the payment via PATCH COMPLETED shortcut.",
    )


def _repair_projection_ok(db: Session, task: OperationalTask) -> TruthValidationResult:
    repair_id = task.source_id
    if repair_id is None:
        dedupe = task.dedupe_key or ""
        if dedupe.startswith("repair:"):
            try:
                repair_id = int(dedupe.split(":", 2)[1])
            except (ValueError, IndexError):
                repair_id = None
    if repair_id is None:
        return TruthValidationResult(
            False,
            "Repair linked (source_id or dedupe_key=repair:<id>)",
            f"source_id={task.source_id!r} dedupe_key={task.dedupe_key!r}",
            "Repair maintenance/followup tasks must reference a RepairOperation via source_id or dedupe_key prefix 'repair:'.",
        )
    repair = db.get(RepairOperation, repair_id)
    if repair is None:
        return TruthValidationResult(
            False,
            "RepairOperation row exists",
            "Repair missing",
            "Reconcile will cancel this orphan task.",
        )
    if repair.status in (RepairOperationStatus.CLOSED, RepairOperationStatus.CANCELLED):
        return TruthValidationResult(True)
    return TruthValidationResult(
        False,
        "RepairOperation.status in {CLOSED, CANCELLED}",
        f"RepairOperation.status={repair.status.value}",
        "Close the repair operation through the canonical repair verification endpoint (RepairEvidenceGate: HUMAN_CONFIRMED or COMPLETION_EVENT with evidence). Expense payment alone does not close a repair.",
    )


def _move_out_inspection_ok(db: Session, task: OperationalTask) -> TruthValidationResult:
    inspection_id = task.source_id
    if inspection_id is None or (task.source_type or "") != "move_out_inspection":
        return TruthValidationResult(
            False,
            "source_type=move_out_inspection with source_id",
            f"source_type={task.source_type!r} source_id={inspection_id!r}",
            "Move-out inspection tasks must bind source_type='move_out_inspection' + inspection source_id.",
        )
    inspection = db.get(MoveOutInspection, inspection_id)
    if inspection is None:
        return TruthValidationResult(
            False,
            "MoveOutInspection row exists",
            "MoveOutInspection missing",
            "Reconcile will cancel this orphan task.",
        )
    if inspection.status in (MoveOutInspectionStatus.CONFIRMED, MoveOutInspectionStatus.CANCELLED):
        return TruthValidationResult(True)
    has_evidence = False
    if inspection.evidence_ids:
        eids = [eid for eid in inspection.evidence_ids if isinstance(eid, int)]
        if eids:
            evidence_rows = (
                db.query(Evidence)
                .filter(Evidence.id.in_(eids), Evidence.deleted_at.is_(None))
                .all()
            )
            has_evidence = any(
                e.category in (EvidenceCategory.move_out_photo, EvidenceCategory.move_out)
                for e in evidence_rows
            )
    actual = (
        f"status={inspection.status.value} "
        f"has_findings={'yes' if inspection.findings else 'no'} "
        f"has_move_out_photo={'yes' if has_evidence else 'no'}"
    )
    return TruthValidationResult(
        False,
        "MoveOutInspection.status in {CONFIRMED, CANCELLED} (or evidence+findings verified via canonical confirm endpoint)",
        actual,
        "Schedule then run the move-out inspection through the canonical PATCH inspect + confirm endpoints; do not PATCH COMPLETED the task as a substitute.",
    )


def _deposit_settlement_ok(db: Session, task: OperationalTask) -> TruthValidationResult:
    settlement_id = task.source_id
    if settlement_id is None or (task.source_type or "") != "deposit_settlement":
        return TruthValidationResult(
            False,
            "source_type=deposit_settlement with source_id",
            f"source_type={task.source_type!r} source_id={settlement_id!r}",
            "Deposit settlement tasks must bind source_type='deposit_settlement' + settlement source_id.",
        )
    settlement = db.get(DepositSettlement, settlement_id)
    if settlement is None:
        return TruthValidationResult(
            False,
            "DepositSettlement row exists",
            "DepositSettlement missing",
            "Reconcile will cancel this orphan task.",
        )
    if settlement.status in (DepositSettlementStatus.CONFIRMED, DepositSettlementStatus.RECONCILED):
        return TruthValidationResult(True)
    from decimal import Decimal
    gap = Decimal(str(settlement.deposit_received)) - (
        Decimal(str(settlement.total_deductions)) + Decimal(str(settlement.refund_amount))
    )
    return TruthValidationResult(
        False,
        "DepositSettlement.status in {CONFIRMED, RECONCILED} (amount conserved ≤ 1c)",
        f"status={settlement.status.value} conservation_gap={gap}",
        "Review deductions and refund, then PATCH confirm the settlement through the canonical endpoint (amount must match deposit_received within 1c tolerance).",
    )


CheckerFn = Callable[[Session, OperationalTask], TruthValidationResult]

_PROJECTION_TABLE: dict[tuple[OperationalTaskType | None, str | None], CheckerFn] = {
    (OperationalTaskType.RENT_DUE, "lease"): _rent_fully_paid_for_task,
    (OperationalTaskType.RENT_OVERDUE, "lease"): _rent_fully_paid_for_task,
    (OperationalTaskType.LEASE_EXPIRING, "lease"): _lease_expiring_ok,
    (OperationalTaskType.APPROVAL_PENDING, "expense"): _approval_pending_ok,
    (OperationalTaskType.PAYMENT_PENDING, "expense"): _payment_pending_ok,
    (OperationalTaskType.AC_MAINTENANCE, "repair"): _repair_projection_ok,
    (OperationalTaskType.FOLLOWUP, "repair"): _repair_projection_ok,
    (OperationalTaskType.MOVE_OUT_INSPECTION, "move_out_inspection"): _move_out_inspection_ok,
    # --- Owner PASAY-TASK-012 #7: explicit registration for the provisional
    # MOVE_OUT_INSPECTION + source_type=lease contract (provisional task
    # created by generation.py L962-964 before a real inspection row
    # exists). Truth validation consults the canonical lease row and
    # determines closed-ness from the real inspection pointed at by
    # lease.move_out_inspection_id, rather than fail-closed 409-ing this
    # tuple as an unregistered orphan.
    #
    # NOTE: source_id=None tasks NEVER reach this checker — they are still
    # routed to reconcile fail-closed CANCELLED (per Owner #13 contract).
    (OperationalTaskType.MOVE_OUT_INSPECTION, "lease"): _move_out_inspection_ok,
    (OperationalTaskType.DEPOSIT_SETTLEMENT, "deposit_settlement"): _deposit_settlement_ok,
}


def _table_has_match(task: OperationalTask) -> bool:
    key = (task.task_type, (task.source_type or None))
    if key in _PROJECTION_TABLE:
        return True
    wildcard_source = (task.task_type, None)
    if wildcard_source in _PROJECTION_TABLE:
        return True
    wildcard_type = (None, (task.source_type or None))
    return wildcard_type in _PROJECTION_TABLE


_NON_PROJECTION_SOURCE_TYPES = frozenset({
    "",
    "conversation",
    "recurring_rule",
    "manual",
    "task",
})


def _is_non_projection(task: OperationalTask) -> bool:
    """A task is 'non-projection' (human-created copilot/conversation,
    recurring_rule scheduled, manual operator, task-to-task followup) when
    source_type is in the explicit NON_PROJECTION_SOURCE_TYPES set above.

    All other source_types (rent, lease, expense, repair, settlement,
    income_lease, and any unknown/future custom integration value that has
    not been explicitly registered) are treated as projection integrations
    and MUST be registered in PROJECTION_TABLE — otherwise they fail-close
    (not schema-A)."""
    source_type = (task.source_type or "").strip()
    return source_type in _NON_PROJECTION_SOURCE_TYPES


def validate_completion(db: Session, task: OperationalTask) -> TruthValidationResult:
    """Validate the linked business truth before allowing a HUMAN principal to
    mark this task COMPLETED via the REST API. Returns the canonical result.

    Never call this from truth -> projection forward-writing paths such as
    expense_claims._finalize_paid, rent_claims._sync_period_tasks,
    repairs.verification._complete_linked, projection.close_active_projections.
    Those paths write the DB directly and bypass the HTTP API."""
    if task.status == OperationalTaskStatus.COMPLETED:
        return TruthValidationResult(True)
    key = (task.task_type, (task.source_type or None))
    checker = _PROJECTION_TABLE.get(key)
    if checker is not None:
        return checker(db, task)
    if _is_non_projection(task):
        if CUSTOM_TASK_COMPLETE_POLICY == "A":
            return TruthValidationResult(True)
        return TruthValidationResult(
            False,
            "Forward projection or system reconcile completion only (policy B)",
            "Manual task PATCH COMPLETED denied",
            "Schema B disallows any HUMAN PATCH COMPLETED on any task; must close via reconcile or a truth forward projection.",
        )
    tt = task.task_type.value if hasattr(task.task_type, "value") else repr(task.task_type)
    return TruthValidationResult(
        False,
        "Truth mapping registered for this (task_type, source_type) combination",
        f"task_type={tt} source_type={task.source_type!r}",
        "No business truth validator registered for this projection; contact Owner to register schema for this combination before HUMAN completion can be allowed.",
    )


def assert_completion_allowed(db: Session, task: OperationalTask) -> None:
    """Raise TruthValidationError (409) if the linked truth is missing.
    Idempotent no-op when validate_completion(...).ok."""
    result = validate_completion(db, task)
    if not result.ok:
        raise TruthValidationError(task.id, result)
