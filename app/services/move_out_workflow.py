"""M004 Lease & Move-out Truth Closure: Move-out Inspection Workflow.

Implements:
 1. MoveOutInspection scheduling (idempotent, one active per lease)
 2. Evidence gate for CONFIRMED transition (at least 1 move_out_photo/move_out Evidence)
 3. Forward projection sync: CONFIRMED -> complete linked MOVE_OUT_INSPECTION OperationalTask
 4. Auto-create DRAFT DepositSettlement after inspection CONFIRMED
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Lease, LeaseStatus, Tenant, Unit
from app.models.deposit_settlement import DepositSettlement, DepositSettlementStatus
from app.models.evidence import Evidence, EvidenceCategory
from app.models.move_out import MoveOutInspection, MoveOutInspectionStatus
from app.models.operations import OperationalTask, OperationalTaskStatus, OperationalTaskType
from app.models.property import UnitLifecycleEvent, UnitStatus
from app.services.audit import record_audit, serialize_row
from app.services.organization_scope import property_org_id
from app.services.shared import sync_unit_status


def _close_tasks_by_query(
    db: Session,
    query,
    *,
    actor_id: int | None,
    now: datetime,
    target_status: OperationalTaskStatus,
    reason: str,
    extra_pre: callable | None = None,
    exclude_ids: set[int] | None = None,
) -> None:
    """Common helper: close every PENDING task returned by ``query`` with a
    task_auto_completed / task_auto_cancelled audit row. ``extra_pre(task)`` is
    invoked for each row before status changes (e.g. to patch source_id from
    provisional -> concrete). Rows whose .id is in ``exclude_ids`` are
    skipped.
    """
    tasks = query.all()
    for t in tasks:
        if exclude_ids and t.id in exclude_ids:
            continue
        old_row = serialize_row(t)
        if extra_pre is not None:
            extra_pre(t)
        t.status = target_status
        t.updated_at = now
        t.completed_at = now if target_status == OperationalTaskStatus.COMPLETED else None
        t.completed_by = actor_id
        t.reminder_generation = t.reminder_generation + 1
        action = "task_auto_cancelled" if target_status == OperationalTaskStatus.CANCELLED else "task_auto_completed"
        record_audit(
            db,
            table_name="operational_tasks",
            record_id=t.id,
            action=action,
            actor_id=None,
            changed_fields={
                "status": [OperationalTaskStatus.PENDING.value, target_status.value],
                "reason": reason,
            },
            old_value=old_row,
            new_value=serialize_row(t),
        )


def validate_evidence_ids(
    db: Session,
    evidence_ids: list[int] | None,
    lease: Lease,
    membership,
) -> None:
    if not evidence_ids:
        return
    evidence_rows = (
        db.query(Evidence)
        .filter(Evidence.id.in_(evidence_ids), Evidence.deleted_at.is_(None))
        .all()
    )
    found_ids = {e.id for e in evidence_rows}
    missing_ids = [eid for eid in evidence_ids if eid not in found_ids]
    if missing_ids:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "move_out_inspection_evidence_not_found",
                "missing_evidence_ids": missing_ids,
            },
        )
    for ev in evidence_rows:
        if ev.unit_id != lease.unit_id:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "reason": "move_out_inspection_evidence_mismatched_org_or_unit",
                    "evidence_id": ev.id,
                    "expected_unit_id": lease.unit_id,
                    "actual_unit_id": ev.unit_id,
                },
            )
        if ev.property_id is not None:
            ev_org_id = property_org_id(db, ev.property_id)
            if ev_org_id != membership.organization_id:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    detail={
                        "reason": "move_out_inspection_evidence_mismatched_org_or_unit",
                        "evidence_id": ev.id,
                    },
                )


_ALLOWED_INSPECTION_TRANSITIONS: dict[MoveOutInspectionStatus, set[MoveOutInspectionStatus]] = {
    MoveOutInspectionStatus.SCHEDULED: {MoveOutInspectionStatus.INSPECTED, MoveOutInspectionStatus.CANCELLED},
    MoveOutInspectionStatus.INSPECTED: {MoveOutInspectionStatus.CONFIRMED, MoveOutInspectionStatus.CANCELLED, MoveOutInspectionStatus.SCHEDULED},
    MoveOutInspectionStatus.CONFIRMED: {MoveOutInspectionStatus.CANCELLED},
    MoveOutInspectionStatus.CANCELLED: set(),
}


def validate_inspection_transition(current: MoveOutInspectionStatus, target: MoveOutInspectionStatus) -> None:
    allowed = _ALLOWED_INSPECTION_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "inspection_transition_invalid",
                "from_status": current.value,
                "to_status": target.value,
                "hint": f"Cannot transition {current.value} -> {target.value}. Valid targets: {sorted(s.value for s in allowed)}",
            },
        )


def evidence_gate_passed(db: Session, inspection: MoveOutInspection) -> tuple[bool, str]:
    """Return (passed, reason). CONFIRMED requires at least 1 move-out photo evidence."""
    eids = inspection.evidence_ids or []
    valid_ids = [eid for eid in eids if isinstance(eid, int)]
    if not valid_ids:
        return False, "No evidence_ids linked to this inspection (need at least 1 move_out_photo / move_out category)."
    evidence_rows = db.query(Evidence).filter(Evidence.id.in_(valid_ids), Evidence.deleted_at.is_(None)).all()
    move_out_categories = {EvidenceCategory.move_out, EvidenceCategory.move_out_photo}
    has_photo = any(e.category in move_out_categories for e in evidence_rows)
    if not has_photo:
        cats = sorted({e.category.value for e in evidence_rows if e.category is not None})
        return False, f"No move-out evidence found (got categories={cats}). Require at least 1 Evidence with category=move_out or move_out_photo."
    findings = inspection.findings or []
    if not findings:
        return False, "No inspection findings recorded. Findings (list of items) are required before CONFIRMED."
    return True, "ok"


def schedule_inspection(
    db: Session,
    *,
    lease_id: int,
    unit_id: int | None,
    tenant_id: int | None,
    scheduled_at: datetime,
    actor_id: int,
) -> tuple[MoveOutInspection, bool]:
    """Idempotent: returns existing SCHEDULED/INSPECTED row for this lease without creating a duplicate.

    Returns (obj, created: bool) — created=True if a new row was inserted, False if existing returned.
    """
    existing = (
        db.query(MoveOutInspection)
        .filter(
            MoveOutInspection.lease_id == lease_id,
            MoveOutInspection.status.in_([MoveOutInspectionStatus.SCHEDULED, MoveOutInspectionStatus.INSPECTED]),
        )
        .first()
    )
    if existing is not None:
        return existing, False
    obj = MoveOutInspection(
        lease_id=lease_id,
        unit_id=unit_id,
        tenant_id=tenant_id,
        scheduled_at=scheduled_at,
    )
    obj.created_by = actor_id
    obj.updated_by = actor_id
    db.add(obj)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        existing = (
            db.query(MoveOutInspection)
            .filter(
                MoveOutInspection.lease_id == lease_id,
                MoveOutInspection.status.in_([MoveOutInspectionStatus.SCHEDULED, MoveOutInspectionStatus.INSPECTED]),
            )
            .first()
        )
        if existing is not None:
            return existing, False
        raise
    lease = db.get(Lease, lease_id)
    if lease is not None and lease.move_out_inspection_id is None:
        lease.move_out_inspection_id = obj.id
        lease.updated_by = actor_id
    record_audit(
        db,
        table_name="move_out_inspections",
        record_id=obj.id,
        action="create",
        actor_id=actor_id,
        new_value=serialize_row(obj),
    )
    return obj, True


def mark_inspected(
    db: Session,
    inspection: MoveOutInspection,
    *,
    findings: list[dict] | None,
    evidence_ids: list[int] | None,
    inspected_at: datetime,
    actor_id: int,
) -> MoveOutInspection:
    target = MoveOutInspectionStatus.INSPECTED
    validate_inspection_transition(inspection.status, target)
    old = serialize_row(inspection)
    if findings is not None:
        inspection.findings = findings
    if evidence_ids is not None:
        inspection.evidence_ids = evidence_ids
    inspection.inspected_at = inspected_at
    inspection.status = target
    inspection.updated_by = actor_id
    changed = {"status": [old.get("status"), target.value]}
    if findings is not None:
        changed["findings"] = "updated"
    if evidence_ids is not None:
        changed["evidence_ids"] = "updated"
    record_audit(
        db,
        table_name="move_out_inspections",
        record_id=inspection.id,
        action="update",
        actor_id=actor_id,
        changed_fields=changed,
        old_value=old,
        new_value=serialize_row(inspection),
    )
    return inspection


def confirm_inspection(
    db: Session,
    inspection: MoveOutInspection,
    *,
    confirmed_at: datetime,
    actor_id: int,
) -> MoveOutInspection:
    target = MoveOutInspectionStatus.CONFIRMED
    locked = (
        db.query(MoveOutInspection)
        .filter(MoveOutInspection.id == inspection.id)
        .with_for_update()
        .first()
    )
    if locked is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={
                "reason": "move_out_inspection_not_found",
                "inspection_id": inspection.id,
            },
        )
    if locked.status == target:
        return locked
    validate_inspection_transition(locked.status, target)
    passed, reason = evidence_gate_passed(db, locked)
    if not passed:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "move_out_inspection_evidence_gate_failed",
                "inspection_id": locked.id,
                "detail": reason,
                "hint": "PATCH /move-out-inspections/{id} with evidence_ids and findings first, then retry confirm.",
            },
        )
    lease = db.get(Lease, locked.lease_id)
    if locked.unit_id is None and lease is not None:
        locked.unit_id = lease.unit_id
    if locked.tenant_id is None and lease is not None:
        locked.tenant_id = lease.tenant_id
    old = serialize_row(locked)
    locked.status = target
    locked.confirmed_at = confirmed_at
    locked.confirmed_by = actor_id
    locked.updated_by = actor_id
    record_audit(
        db,
        table_name="move_out_inspections",
        record_id=locked.id,
        action="confirm",
        actor_id=actor_id,
        changed_fields={
            "status": [old.get("status"), target.value],
            "confirmed_at": [None, confirmed_at.isoformat()],
        },
        old_value=old,
        new_value=serialize_row(locked),
    )
    # --- Forward sync: close any PENDING MOVE_OUT_INSPECTION task for this inspection ---
    _close_projection_tasks_for_inspection(db, locked, actor_id, confirmed_at)
    _ensure_draft_settlement_for(db, locked, actor_id)
    return locked


def cancel_inspection(
    db: Session,
    inspection: MoveOutInspection,
    *,
    cancelled_at: datetime,
    cancelled_by: int,
) -> MoveOutInspection:
    target = MoveOutInspectionStatus.CANCELLED
    lease = db.get(Lease, inspection.lease_id)
    if inspection.status == MoveOutInspectionStatus.CONFIRMED and lease is not None:
        if lease.status in {LeaseStatus.expired, LeaseStatus.terminated}:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "reason": "move_out_inspection_cancel_lease_already_terminal",
                    "inspection_id": inspection.id,
                    "lease_id": lease.id,
                    "lease_status": lease.status.value,
                },
            )
    if inspection.status == MoveOutInspectionStatus.CONFIRMED:
        settl = (
            db.query(DepositSettlement)
            .filter(DepositSettlement.move_out_inspection_id == inspection.id)
            .first()
        )
        if settl is not None and settl.status in {DepositSettlementStatus.CONFIRMED, DepositSettlementStatus.RECONCILED}:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "reason": "move_out_inspection_cancel_settlement_already_confirmed",
                    "inspection_id": inspection.id,
                    "settlement_id": settl.id,
                    "settlement_status": settl.status.value,
                },
            )
    if inspection.status == MoveOutInspectionStatus.CONFIRMED and lease is not None and lease.moved_out_settled_at is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "move_out_inspection_immutable_lease_settled",
                "inspection_id": inspection.id,
                "lease_id": lease.id,
            },
        )
    validate_inspection_transition(inspection.status, target)
    old = serialize_row(inspection)
    inspection.status = target
    inspection.cancelled_at = cancelled_at
    inspection.cancelled_by = cancelled_by
    inspection.updated_by = cancelled_by
    record_audit(
        db,
        table_name="move_out_inspections",
        record_id=inspection.id,
        action="cancel",
        actor_id=cancelled_by,
        changed_fields={"status": [old.get("status"), target.value]},
        old_value=old,
        new_value=serialize_row(inspection),
    )
    _close_projection_tasks_for_inspection(db, inspection, cancelled_by, cancelled_at, cancelled=True)
    return inspection


def _close_projection_tasks_for_inspection(
    db: Session,
    inspection: MoveOutInspection,
    actor_id: int | None,
    now: datetime,
    *,
    cancelled: bool = False,
) -> None:
    target_status = OperationalTaskStatus.CANCELLED if cancelled else OperationalTaskStatus.COMPLETED
    reason = "move_out_inspection_cancelled" if cancelled else "move_out_inspection_confirmed_forward_sync"
    q1 = db.query(OperationalTask).filter(
        OperationalTask.task_type == OperationalTaskType.MOVE_OUT_INSPECTION,
        OperationalTask.source_type == "move_out_inspection",
        OperationalTask.source_id == inspection.id,
        OperationalTask.status == OperationalTaskStatus.PENDING,
    )
    already_closed_ids: set[int] = set()
    tasks1 = q1.all()
    for t in tasks1:
        already_closed_ids.add(t.id)
    _close_tasks_by_query(
        db, q1,
        actor_id=actor_id, now=now,
        target_status=target_status, reason=reason,
    )
    # Also dedupe_key route: lease:{id}:MOVE_OUT_INSPECTION (may still have source_id=None at generation time before inspection created)
    def _patch_provisional(t: OperationalTask) -> None:
        if t.source_id is None:
            t.source_id = inspection.id
            t.source_type = "move_out_inspection"
    q2 = db.query(OperationalTask).filter(
        OperationalTask.task_type == OperationalTaskType.MOVE_OUT_INSPECTION,
        OperationalTask.status == OperationalTaskStatus.PENDING,
        OperationalTask.dedupe_key == f"lease:{inspection.lease_id}:MOVE_OUT_INSPECTION",
    )
    _close_tasks_by_query(
        db, q2,
        actor_id=actor_id, now=now,
        target_status=target_status, reason=reason,
        extra_pre=_patch_provisional,
        exclude_ids=already_closed_ids,
    )


def _close_projection_tasks_for_settlement(
    db: Session,
    settlement: DepositSettlement,
    actor_id: int | None,
    now: datetime,
    *,
    cancelled: bool = False,
) -> None:
    """Shared helper future-use: close DEPOSIT_SETTLEMENT projection tasks
    for a confirmed/cancelled settlement. Currently unused but exists so
    settlement forward-sync can reuse the same pattern as inspection.
    """
    target_status = OperationalTaskStatus.CANCELLED if cancelled else OperationalTaskStatus.COMPLETED
    reason = "deposit_settlement_cancelled" if cancelled else "deposit_settlement_confirmed_forward_sync"
    q1 = db.query(OperationalTask).filter(
        OperationalTask.task_type == OperationalTaskType.DEPOSIT_SETTLEMENT,
        OperationalTask.source_type == "deposit_settlement",
        OperationalTask.source_id == settlement.id,
        OperationalTask.status == OperationalTaskStatus.PENDING,
    )
    _close_tasks_by_query(
        db, q1,
        actor_id=actor_id, now=now,
        target_status=target_status, reason=reason,
    )
    if settlement.move_out_inspection_id is not None:
        insp = db.get(MoveOutInspection, settlement.move_out_inspection_id)
        if insp is not None:
            q2 = db.query(OperationalTask).filter(
                OperationalTask.task_type == OperationalTaskType.DEPOSIT_SETTLEMENT,
                OperationalTask.status == OperationalTaskStatus.PENDING,
                OperationalTask.dedupe_key == f"deposit_settlement:{settlement.id}:DEPOSIT_SETTLEMENT",
            )
            _close_tasks_by_query(
                db, q2,
                actor_id=actor_id, now=now,
                target_status=target_status, reason=reason,
            )


def _ensure_draft_settlement_for(
    db: Session,
    inspection: MoveOutInspection,
    actor_id: int,
) -> DepositSettlement:
    existing = (
        db.query(DepositSettlement)
        .filter(DepositSettlement.move_out_inspection_id == inspection.id)
        .first()
    )
    if existing is not None:
        return existing
    lease = db.get(Lease, inspection.lease_id)
    deposit_received = Decimal("0.00")
    if lease is not None and lease.deposit_received is not None:
        deposit_received = Decimal(str(lease.deposit_received))
    elif lease is not None:
        deposit_received = Decimal(str(lease.deposit))
    obj = DepositSettlement(
        lease_id=inspection.lease_id,
        move_out_inspection_id=inspection.id,
        deposit_received=deposit_received,
        total_deductions=Decimal("0.00"),
        refund_amount=deposit_received,
        status=DepositSettlementStatus.DRAFT,
    )
    obj.created_by = actor_id
    obj.updated_by = actor_id
    db.add(obj)
    db.flush()
    if lease is not None:
        should_set_fk = False
        if lease.deposit_settlement_id is None:
            should_set_fk = True
        elif inspection.id == lease.move_out_inspection_id:
            should_set_fk = True
        else:
            existing_sett = db.get(DepositSettlement, lease.deposit_settlement_id)
            if existing_sett is not None:
                existing_insp = db.get(MoveOutInspection, existing_sett.move_out_inspection_id)
                if existing_insp is not None and existing_insp.status == MoveOutInspectionStatus.CANCELLED:
                    should_set_fk = True
        if should_set_fk:
            lease.deposit_settlement_id = obj.id
            lease.updated_by = actor_id
    record_audit(
        db,
        table_name="deposit_settlements",
        record_id=obj.id,
        action="create_draft",
        actor_id=actor_id,
        new_value=serialize_row(obj),
    )
    return obj


def validate_lease_closeable(
    db: Session,
    lease: Lease,
    *,
    expected_target_status: LeaseStatus | None = None,
) -> tuple[bool, str | None, str | None]:
    """Validate whether a Lease can reach its final closed state.

    Returns (ok, expected_truth, actual_truth).
    Conditions:
      (a) Lease.status must NOT be active (must be terminated/expired first),
          BUT if ``expected_target_status`` is passed (the intended post-PATCH
          state from the caller), we treat it as authoritative for condition
          (a) so the gate can be evaluated BEFORE the in-place mutation.
      (b) A CONFIRMED MoveOutInspection must exist
      (c) A CONFIRMED DepositSettlement must exist with amount conservation (already enforced at confirm time, re-checked here for API safety)
    """
    effective_status = expected_target_status if expected_target_status is not None else lease.status
    effective_deleted = lease.deleted_at
    if effective_status == LeaseStatus.active and (effective_deleted is None):
        return (
            False,
            "Lease.status in {terminated, expired} (not active)",
            f"status={effective_status.value}",
        )
    inspection = None
    if lease.move_out_inspection_id is not None:
        inspection = db.get(MoveOutInspection, lease.move_out_inspection_id)
    if inspection is None:
        inspection = (
            db.query(MoveOutInspection)
            .filter(MoveOutInspection.lease_id == lease.id)
            .order_by(MoveOutInspection.id.desc())
            .first()
        )
    if inspection is None or inspection.status != MoveOutInspectionStatus.CONFIRMED:
        return (
            False,
            "MoveOutInspection.status = CONFIRMED (evidence gate passed)",
            f"inspection_id={inspection.id if inspection else None} status={inspection.status.value if inspection else 'MISSING'}",
        )
    if inspection.lease_id != lease.id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "lease_closeable_inspection_lease_mismatch",
                "inspection_id": inspection.id,
                "inspection_lease_id": inspection.lease_id,
                "lease_id": lease.id,
            },
        )
    settlement = None
    if lease.deposit_settlement_id is not None:
        settlement = db.get(DepositSettlement, lease.deposit_settlement_id)
    if settlement is None:
        settlement = (
            db.query(DepositSettlement)
            .filter(DepositSettlement.lease_id == lease.id)
            .order_by(DepositSettlement.id.desc())
            .first()
        )
    if settlement is None or settlement.status not in (DepositSettlementStatus.CONFIRMED, DepositSettlementStatus.RECONCILED):
        return (
            False,
            "DepositSettlement.status in {CONFIRMED, RECONCILED} (amount conserved within 1c)",
            f"settlement_id={settlement.id if settlement else None} status={settlement.status.value if settlement else 'MISSING'}",
        )
    if settlement.lease_id != lease.id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "lease_closeable_settlement_lease_mismatch",
                "settlement_id": settlement.id,
                "settlement_lease_id": settlement.lease_id,
                "lease_id": lease.id,
            },
        )
    if settlement.move_out_inspection_id != inspection.id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "lease_closeable_settlement_inspection_mismatch",
                "settlement_id": settlement.id,
                "settlement_move_out_inspection_id": settlement.move_out_inspection_id,
                "inspection_id": inspection.id,
            },
        )
    gap = abs(
        Decimal(str(settlement.deposit_received))
        - (Decimal(str(settlement.total_deductions)) + Decimal(str(settlement.refund_amount)))
    )
    if gap > Decimal("0.01"):
        return (
            False,
            "Conservation: deposit_received = total_deductions + refund_amount (gap <= 0.01)",
            f"gap={gap} exceeds 1c tolerance",
        )
    return True, None, None


def apply_settled_lease_final_state(
    db: Session,
    lease: Lease,
    *,
    actor_id: int,
    now: datetime,
) -> None:
    """Post-condition: write final state AFTER validate_lease_closeable returns True.

    - lease.moved_out_settled_at = now
    - Unit.status = vacant (via existing _sync_unit_status + lifecycle event)
    - Tenant.moved_out_at = now (only for THIS lease's tenant, never blanket deactivate)
    """
    if lease.moved_out_settled_at is not None:
        return  # Idempotent: never double-apply
    lease.moved_out_settled_at = now
    lease.updated_by = actor_id
    # --- Unit status ---
    unit = db.get(Unit, lease.unit_id)
    if unit is not None:
        old_unit = serialize_row(unit)
        sync_unit_status(db, unit)
        unit.updated_by = actor_id
        if old_unit.get("status") != unit.status:
            record_audit(
                db,
                table_name="units",
                record_id=unit.id,
                action="update",
                actor_id=actor_id,
                changed_fields={"status": [old_unit.get("status"), unit.status]},
                old_value=old_unit,
                new_value=serialize_row(unit),
            )
        evt = UnitLifecycleEvent(
            unit_id=unit.id,
            from_status=old_unit.get("status"),
            to_status=unit.status,
            reason="move_out_settled",
            occurred_at=now,
        )
        evt.created_by = actor_id
        db.add(evt)
    # --- Tenant move-out timestamp (idempotent by lease.tenant_id) ---
    tenant = db.get(Tenant, lease.tenant_id)
    if tenant is not None and tenant.moved_out_at is None:
        old_tenant = serialize_row(tenant)
        tenant.moved_out_at = now
        tenant.updated_by = actor_id
        record_audit(
            db,
            table_name="tenants",
            record_id=tenant.id,
            action="update",
            actor_id=actor_id,
            changed_fields={"moved_out_at": [None, now.isoformat()]},
            old_value=old_tenant,
            new_value=serialize_row(tenant),
        )
    # --- Forward sync: close LEASE_EXPIRING task for this settled lease ---
    tasks = (
        db.query(OperationalTask)
        .filter(
            OperationalTask.task_type == OperationalTaskType.LEASE_EXPIRING,
            OperationalTask.source_type == "lease",
            OperationalTask.source_id == lease.id,
            OperationalTask.status == OperationalTaskStatus.PENDING,
        )
        .all()
    )
    for t in tasks:
        old_row = serialize_row(t)
        t.status = OperationalTaskStatus.COMPLETED
        t.updated_at = now
        t.completed_at = now
        t.completed_by = actor_id
        t.reminder_generation = t.reminder_generation + 1
        record_audit(
            db,
            table_name="operational_tasks",
            record_id=t.id,
            action="task_auto_completed",
            actor_id=None,
            changed_fields={"status": ["PENDING", "COMPLETED"], "reason": "lease_moved_out_settled"},
            old_value=old_row,
            new_value=serialize_row(t),
        )


_TERMINAL_LEASE_STATUSES = {
    LeaseStatus.terminated,
    LeaseStatus.expired,
}


def enforce_lease_terminal_immutable(
    db: Session,
    lease: Lease,
    *,
    target_status: LeaseStatus | None = None,
) -> None:
    """Prevent a terminal (expired/terminated) lease from reverting to active.

    Raises HTTP 409 if:
      - Current status is terminal AND target_status would move it back to non-terminal.
    If target_status is None (no status change), this is a no-op regardless of current state
    (since no mutation is being attempted).
    """
    del db
    if target_status is None:
        return
    current_is_terminal = lease.status in _TERMINAL_LEASE_STATUSES
    target_is_terminal = target_status in _TERMINAL_LEASE_STATUSES
    if current_is_terminal and not target_is_terminal:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "lease_terminal_immutable_cannot_revert",
                "lease_id": lease.id,
                "current_status": lease.status.value,
                "target_status": target_status.value,
                "hint": "Terminal leases (expired/terminated) cannot be reverted to active. Create a new successor lease instead.",
            },
        )
