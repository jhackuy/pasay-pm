from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.database import get_db
from app.models.lease import Lease, LeaseStatus
from app.models.property import Unit, UnitStatus
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.common import MessageResponse
from app.schemas.lease import (
    LeaseAutoExpireResponse,
    LeaseCreate,
    LeaseDeclineRenewalRequest,
    LeaseRead,
    LeaseRenewalRequest,
    LeaseUpdate,
)
from app.services.audit import field_changes, record_audit, serialize_row
from app.services.move_out_workflow import (
    apply_settled_lease_final_state,
    enforce_lease_terminal_immutable,
    validate_lease_closeable,
)
from app.services.shared import sync_unit_status as _sync_unit_status
from app.services.organization_scope import (
    CrossOrgReference,
    OrganizationRole,
    OwnerRequired,
    ScopeBlocked,
    assert_co_org,
    resolve_org_membership,
    scope_exception_to_http,
    scoped_get_lease,
    scoped_list_leases,
    tenant_org_id,
    unit_org_id,
)

router = APIRouter(prefix="/leases", tags=["leases"])


@router.get("", response_model=list[LeaseRead])
def list_leases(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    try:
        return scoped_list_leases(db, for_user_id=user.id)
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc


@router.post("", response_model=LeaseRead, status_code=status.HTTP_201_CREATED)
def create_lease(
    payload: LeaseCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        u_org_id = unit_org_id(db, payload.unit_id)
        if u_org_id is None:
            raise LookupError("Unit not found")
        membership = resolve_org_membership(
            db, user.id, u_org_id, role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY]
        )
        t_org_id = tenant_org_id(db, payload.tenant_id)
        if t_org_id is None:
            raise LookupError("Tenant not found")
        assert_co_org(db, user_org_id=membership.organization_id, object_org_id=t_org_id, object_kind="Tenant", object_id=payload.tenant_id)
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc

    unit = db.query(Unit).filter(Unit.id == payload.unit_id, Unit.deleted_at.is_(None)).first()
    if unit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unit not found")
    tenant = db.query(Tenant).filter(Tenant.id == payload.tenant_id, Tenant.deleted_at.is_(None)).first()
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tenant not found")
    if payload.status == LeaseStatus.active and unit.status == UnitStatus.occupied:
        raise HTTPException(status.HTTP_409_CONFLICT, "Unit is already occupied")

    obj = Lease(**payload.model_dump())
    obj.created_by = user.id
    obj.updated_by = user.id
    db.add(obj)
    db.flush()
    if obj.status == LeaseStatus.active:
        unit.status = UnitStatus.occupied
        unit.updated_by = user.id
    record_audit(
        db,
        table_name="leases",
        record_id=obj.id,
        action="create",
        actor_id=user.id,
        new_value=serialize_row(obj),
    )
    db.commit()
    db.refresh(obj)
    return obj


@router.get("/{lease_id}", response_model=LeaseRead)
def get_lease(
    lease_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        obj, _membership = scoped_get_lease(db, lease_id, for_user_id=user.id)
        return obj
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc


@router.patch("/{lease_id}", response_model=LeaseRead)
def update_lease(
    lease_id: int,
    payload: LeaseUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        obj, membership = scoped_get_lease(
            db, lease_id, for_user_id=user.id,
            role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY],
        )
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return obj
    target_status_raw = updates.get("status")
    target_status: LeaseStatus | None = None
    if target_status_raw is not None:
        if isinstance(target_status_raw, str):
            target_status = LeaseStatus(target_status_raw)
        else:
            target_status = target_status_raw
    enforce_lease_terminal_immutable(db, obj, target_status=target_status)

    if {"accounting_start_date", "start_date", "end_date"} & updates.keys():
        effective_start = updates.get("start_date", obj.start_date)
        effective_end = updates.get("end_date", obj.end_date)
        effective_accounting_start = updates.get(
            "accounting_start_date", obj.accounting_start_date
        )
        if effective_accounting_start is not None and (
            effective_accounting_start < effective_start
            or effective_accounting_start > effective_end
        ):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "accounting_start_date must be within [start_date, end_date]",
            )

    try:
        if updates.get("unit_id") is not None and updates["unit_id"] != obj.unit_id:
            new_unit_org = unit_org_id(db, updates["unit_id"])
            if new_unit_org is None:
                raise LookupError("Unit not found")
            assert_co_org(db, user_org_id=membership.organization_id, object_org_id=new_unit_org, object_kind="Unit", object_id=updates["unit_id"])
        if updates.get("tenant_id") is not None and updates["tenant_id"] != obj.tenant_id:
            new_tenant_org = tenant_org_id(db, updates["tenant_id"])
            if new_tenant_org is None:
                raise LookupError("Tenant not found")
            assert_co_org(db, user_org_id=membership.organization_id, object_org_id=new_tenant_org, object_kind="Tenant", object_id=updates["tenant_id"])
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc

    if updates.get("unit_id") is not None and updates["unit_id"] != obj.unit_id:
        unit = db.query(Unit).filter(Unit.id == updates["unit_id"], Unit.deleted_at.is_(None)).first()
        if unit is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Unit not found")
    if updates.get("tenant_id") is not None and updates["tenant_id"] != obj.tenant_id:
        tenant = db.query(Tenant).filter(Tenant.id == updates["tenant_id"], Tenant.deleted_at.is_(None)).first()
        if tenant is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Tenant not found")

    unit = db.query(Unit).filter(Unit.id == obj.unit_id, Unit.deleted_at.is_(None)).first()
    new_status = updates.get("status", obj.status)
    status_changed = new_status != obj.status

    eff_start = updates.get("start_date", obj.start_date)
    eff_end = updates.get("end_date", obj.end_date)
    acc_start = updates.get("accounting_start_date", obj.accounting_start_date)
    if eff_end < eff_start:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "lease_end_before_start",
                "start_date": eff_start.isoformat(),
                "end_date": eff_end.isoformat(),
            },
        )
    if acc_start is not None and acc_start < eff_start:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "lease_accounting_before_effective_start",
                "effective_start_date": eff_start.isoformat(),
                "accounting_start_date": acc_start.isoformat(),
            },
        )

    was_terminal = obj.status in (LeaseStatus.terminated, LeaseStatus.expired)
    no_transition = not status_changed
    if was_terminal and no_transition:
        old = serialize_row(obj)
        changed = field_changes(obj, updates)
        for field, value in updates.items():
            setattr(obj, field, value)
        obj.updated_by = user.id
        db.flush()
        record_audit(
            db,
            table_name="leases",
            record_id=obj.id,
            action="update",
            actor_id=user.id,
            changed_fields=changed,
            old_value=old,
            new_value=serialize_row(obj),
        )
        db.commit()
        db.refresh(obj)
        return obj

    if status_changed and new_status in (LeaseStatus.terminated, LeaseStatus.expired):
        ok, expected, actual = validate_lease_closeable(db, obj, expected_target_status=new_status)
        if not ok:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "reason": "lease_closeable_truth_missing",
                    "lease_id": obj.id,
                    "target_status": new_status.value,
                    "expected_truth": expected,
                    "actual_truth": actual,
                    "hint": "Run move-out inspection + deposit settlement pipeline first; inspection evidence gate and settlement conservation must both pass.",
                },
            )
    if new_status == LeaseStatus.active:
        conflicting = (
            db.query(Lease)
            .filter(
                Lease.unit_id == obj.unit_id,
                Lease.status == LeaseStatus.active,
                Lease.id != obj.id,
                Lease.deleted_at.is_(None),
            )
            .first()
        )
        if conflicting is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "Unit is already occupied by another active lease")

    old = serialize_row(obj)
    changed = field_changes(obj, updates)
    for field, value in updates.items():
        setattr(obj, field, value)
    obj.updated_by = user.id
    db.flush()
    if unit is not None:
        if new_status == LeaseStatus.active:
            unit.status = UnitStatus.occupied
            unit.updated_by = user.id
        else:
            _sync_unit_status(db, unit)
    if status_changed and new_status in (LeaseStatus.terminated, LeaseStatus.expired):
        apply_settled_lease_final_state(db, obj, actor_id=user.id, now=datetime.now(timezone.utc))
    record_audit(
        db,
        table_name="leases",
        record_id=obj.id,
        action="update",
        actor_id=user.id,
        changed_fields=changed,
        old_value=old,
        new_value=serialize_row(obj),
    )
    db.commit()
    db.refresh(obj)
    return obj


@router.delete("/{lease_id}", response_model=MessageResponse)
def delete_lease(
    lease_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        obj, _membership = scoped_get_lease(
            db, lease_id, for_user_id=user.id,
            role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY],
        )
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc

    if obj.status == LeaseStatus.active:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Terminate the lease before deleting it"
        )
    if obj.moved_out_settled_at is None and obj.deleted_at is None:
        ok, expected, actual = validate_lease_closeable(db, obj)
        if not ok:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "reason": "lease_closeable_truth_missing_before_delete",
                    "lease_id": obj.id,
                    "expected_truth": expected,
                    "actual_truth": actual,
                    "hint": "Close the inspection + settlement pipeline before soft-deleting; or mark the lease as settled first.",
                },
            )

    obj.deleted_at = datetime.now(timezone.utc)
    obj.updated_by = user.id
    if obj.moved_out_settled_at is None:
        apply_settled_lease_final_state(db, obj, actor_id=user.id, now=datetime.now(timezone.utc))
    unit = db.query(Unit).filter(Unit.id == obj.unit_id, Unit.deleted_at.is_(None)).first()
    if unit is not None:
        _sync_unit_status(db, unit)
    record_audit(
        db,
        table_name="leases",
        record_id=obj.id,
        action="soft_delete",
        actor_id=user.id,
        old_value=serialize_row(obj),
    )
    db.commit()
    return MessageResponse(detail="Lease deleted")


@router.post("/{lease_id}/renew", response_model=LeaseRead)
def renew_lease(
    lease_id: int,
    payload: LeaseRenewalRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        obj, membership = scoped_get_lease(
            db, lease_id, for_user_id=user.id,
            role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY],
        )
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc

    # --- #12 Concurrency: lock predecessor + unit FOR UPDATE ---
    from datetime import timedelta
    locked_predecessor = (
        db.query(Lease)
        .filter(Lease.id == obj.id)
        .with_for_update()
        .first()
    )
    if locked_predecessor is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lease not found")
    obj = locked_predecessor
    locked_unit = None
    if obj.unit_id is not None:
        locked_unit = (
            db.query(Unit)
            .filter(Unit.id == obj.unit_id)
            .with_for_update()
            .first()
        )
    # --- Check renewed_lease_id AFTER acquiring lock ---
    existing_meta = obj.renewal_metadata or {}
    if existing_meta.get("renewed_lease_id"):
        successor_id = existing_meta["renewed_lease_id"]
        successor = db.get(Lease, successor_id)
        if successor is not None and successor.deleted_at is None:
            return successor
        if successor is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "reason": "renewal_concurrent_successor_unavailable",
                    "predecessor_lease_id": obj.id,
                    "renewed_lease_id": successor_id,
                },
            )
        return successor

    # --- #13 Capture old_obj audit row IMMEDIATELY after locks, BEFORE any mutation ---
    old_obj = serialize_row(obj)

    # --- #11 All guards BEFORE any mutation / db.add(successor) ---
    # Guard: status == active
    if obj.status != LeaseStatus.active:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "renewal_requires_active_lease",
                "lease_id": obj.id,
                "current_status": obj.status.value,
            },
        )
    # Guard: not_renewed
    if existing_meta.get("not_renewed"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "lease_renewal_already_declined",
                "lease_id": obj.id,
            },
        )

    # Guard (redundant defensive; schema #9 validator already covers this at API boundary
    if payload.start_date > payload.end_date:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "renewal_invalid_dates_end_before_start",
                "start_date": payload.start_date.isoformat(),
                "end_date": payload.end_date.isoformat(),
            },
        )

    # --- #10 Seamless renew rules: TODAY >= pred.end && start == pred.end + 1 ---
    today = datetime.now(timezone.utc).date()
    if today < obj.end_date:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "renewal_before_predecessor_end_date",
                "predecessor_lease_id": obj.id,
                "predecessor_end_date": obj.end_date.isoformat(),
                "today": today.isoformat(),
                "hint": f"Renewal transitions the predecessor lease to EXPIRED only on/after the predecessor end_date ({obj.end_date.isoformat()}). Do not renew before the predecessor contract has finished performance.",
            },
        )
    pred_end_plus_1 = obj.end_date + timedelta(days=1)
    if payload.start_date < obj.end_date:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "renewal_overlaps_predecessor",
                "predecessor_end_date": obj.end_date.isoformat(),
                "successor_start_date": payload.start_date.isoformat(),
            },
        )
    if payload.start_date > pred_end_plus_1:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "renewal_gap_between_periods",
                "predecessor_end_date": obj.end_date.isoformat(),
                "successor_start_date": payload.start_date.isoformat(),
                "expected_start_date": pred_end_plus_1.isoformat(),
                "hint": "Successor start_date must equal predecessor end_date + 1 day exactly for seamless renewal (no gap, no overlap).",
            },
        )

    successor_unit_id = payload.unit_id or obj.unit_id
    successor_tenant_id = payload.tenant_id or obj.tenant_id

    # Guard: org scope checks
    try:
        if payload.unit_id is not None and payload.unit_id != obj.unit_id:
            s_u_org = unit_org_id(db, successor_unit_id)
            if s_u_org is None:
                raise LookupError("Unit not found")
            assert_co_org(db, user_org_id=membership.organization_id, object_org_id=s_u_org, object_kind="Unit", object_id=successor_unit_id)
        if payload.tenant_id is not None and payload.tenant_id != obj.tenant_id:
            s_t_org = tenant_org_id(db, successor_tenant_id)
            if s_t_org is None:
                raise LookupError("Tenant not found")
            assert_co_org(db, user_org_id=membership.organization_id, object_org_id=s_t_org, object_kind="Tenant", object_id=successor_tenant_id)
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc

    # Guard: unit / tenant existence
    s_unit = db.query(Unit).filter(Unit.id == successor_unit_id, Unit.deleted_at.is_(None)).first()
    if s_unit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unit not found")
    s_tenant = db.query(Tenant).filter(Tenant.id == successor_tenant_id, Tenant.deleted_at.is_(None)).first()
    if s_tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tenant not found")

    obj = db.query(Lease).filter(Lease.id == obj.id).with_for_update().one()

    # Guard: unit occupied conflicting (other active lease)
    conflicting_active = (
        db.query(Lease)
        .filter(
            Lease.unit_id == successor_unit_id,
            Lease.status == LeaseStatus.active,
            Lease.id != obj.id,
            Lease.deleted_at.is_(None),
        )
        .with_for_update()
        .first()
    )
    if conflicting_active is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Successor unit is already occupied by another active lease")

    # --- ALL GUARDS PASSED — NOW create successor + mutations ---
    successor = Lease(
        unit_id=successor_unit_id,
        tenant_id=successor_tenant_id,
        start_date=payload.start_date,
        end_date=payload.end_date,
        accounting_start_date=None,
        monthly_rent=payload.monthly_rent,
        deposit=payload.deposit,
        status=LeaseStatus.active,
        due_day=payload.due_day or obj.due_day,
        renewal_notice_period_days=payload.renewal_notice_period_days or obj.renewal_notice_period_days,
        management_fee_included=obj.management_fee_included,
        special_terms=obj.special_terms,
    )
    successor.created_by = user.id
    successor.updated_by = user.id
    db.add(successor)
    db.flush()
    if s_unit.status != UnitStatus.occupied:
        s_unit.status = UnitStatus.occupied
        s_unit.updated_by = user.id
    existing_meta = obj.renewal_metadata or {}
    if not existing_meta.get("renewed_lease_id"):
        existing_meta["renewed_lease_id"] = successor.id
        existing_meta["renewed_at"] = datetime.now(timezone.utc).isoformat()
        obj.renewal_metadata = existing_meta
        obj.updated_by = user.id
        record_audit(
            db,
            table_name="leases",
            record_id=obj.id,
            action="renewal_linked",
            actor_id=user.id,
            changed_fields={"renewal_metadata": ["old", "renewed -> successor #%d" % successor.id]},
            old_value=old_obj,
            new_value=serialize_row(obj),
        )
    record_audit(
        db,
        table_name="leases",
        record_id=successor.id,
        action="create_renewal_successor",
        actor_id=user.id,
        new_value=serialize_row(successor),
    )
    from app.models.operations import OperationalTask, OperationalTaskStatus, OperationalTaskType
    now = datetime.now(timezone.utc)
    tasks = (
        db.query(OperationalTask)
        .filter(
            OperationalTask.task_type == OperationalTaskType.LEASE_EXPIRING,
            OperationalTask.source_type == "lease",
            OperationalTask.source_id == obj.id,
            OperationalTask.status == OperationalTaskStatus.PENDING,
        )
        .all()
    )
    for t in tasks:
        old_row = serialize_row(t)
        t.status = OperationalTaskStatus.COMPLETED
        t.updated_at = now
        t.completed_at = now
        t.completed_by = user.id
        t.reminder_generation = t.reminder_generation + 1
        record_audit(
            db, table_name="operational_tasks", record_id=t.id, action="task_auto_completed",
            actor_id=None,
            changed_fields={"status": ["PENDING", "COMPLETED"], "reason": "lease_renewed_successor_created"},
            old_value=old_row, new_value=serialize_row(t),
        )
    # Predecessor -> expired (seamless checks all passed)
    if obj.status == LeaseStatus.active:
        obj.status = LeaseStatus.expired
        obj.updated_by = user.id
        old_unit = db.query(Unit).filter(Unit.id == obj.unit_id, Unit.deleted_at.is_(None)).first()
        if old_unit is not None:
            _sync_unit_status(db, old_unit)

    db.flush()
    db.refresh(obj)
    successor_id_in_meta = (obj.renewal_metadata or {}).get("renewed_lease_id")
    if successor_id_in_meta and successor_id_in_meta != successor.id:
        db.rollback()
        winner = db.query(Lease).filter(Lease.id == successor_id_in_meta, Lease.deleted_at.is_(None)).first()
        if winner is not None:
            db.refresh(winner)
            return winner
        winner = db.query(Lease).filter(Lease.id == successor_id_in_meta).first()
        return winner
    db.commit()
    db.refresh(successor)
    return successor


@router.post("/{lease_id}/decline-renewal", response_model=LeaseRead)
def decline_renewal(
    lease_id: int,
    payload: LeaseDeclineRenewalRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        obj, _membership = scoped_get_lease(
            db, lease_id, for_user_id=user.id,
            role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY],
        )
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc
    existing_meta = obj.renewal_metadata or {}
    if existing_meta.get("renewed_lease_id"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "lease_already_renewed",
                "lease_id": obj.id,
                "renewed_lease_id": existing_meta["renewed_lease_id"],
                "hint": "Cannot decline renewal after a successor lease has been created; use PATCH status=terminated instead.",
            },
        )
    if existing_meta.get("not_renewed"):
        return obj
    existing_meta["not_renewed"] = True
    existing_meta["declined_at"] = datetime.now(timezone.utc).isoformat()
    if payload.reason is not None:
        existing_meta["decline_reason"] = payload.reason
    if payload.move_out_date is not None:
        existing_meta["move_out_date"] = payload.move_out_date.isoformat()
    old = serialize_row(obj)
    obj.renewal_metadata = existing_meta
    obj.updated_by = user.id
    db.flush()
    record_audit(
        db,
        table_name="leases",
        record_id=obj.id,
        action="decline_renewal",
        actor_id=user.id,
        changed_fields={"renewal_metadata": ["old", "not_renewed=true"]},
        old_value=old,
        new_value=serialize_row(obj),
    )
    db.commit()
    db.refresh(obj)
    return obj


@router.post("/{lease_id}/auto-expire", response_model=LeaseAutoExpireResponse)
def auto_expire(
    lease_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        obj, _membership = scoped_get_lease(
            db, lease_id, for_user_id=user.id,
            role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY],
        )
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc
    old_status = obj.status.value
    today = datetime.now(timezone.utc).date()
    if obj.status != LeaseStatus.active:
        return LeaseAutoExpireResponse(
            id=obj.id,
            status=obj.status.value,
            old_status=old_status,
            new_status=obj.status.value,
            already_expired=True,
        )
    existing_meta = obj.renewal_metadata or {}
    if existing_meta.get("renewed_lease_id"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "lease_already_renewed_successor_detected",
                "lease_id": obj.id,
                "renewed_lease_id": existing_meta["renewed_lease_id"],
                "hint": "A successor active lease exists; use PATCH status=terminated after move-out settlement instead.",
            },
        )
    if not existing_meta.get("not_renewed"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "decline_renewal_required_not_renewed_missing",
                "lease_id": obj.id,
                "not_renewed": False,
                "hint": "Call /decline-renewal first to flag not_renewed=true before auto-expire.",
            },
        )
    if obj.end_date > today:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "lease_not_yet_expired",
                "lease_id": obj.id,
                "end_date": obj.end_date.isoformat(),
                "today": today.isoformat(),
                "hint": "auto-expire is allowed only after the lease end_date has passed.",
            },
        )
    from app.services.operations.reconcile import _lease_renewed
    renewed = _lease_renewed(db, obj)
    if renewed:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "lease_already_renewed_successor_detected",
                "lease_id": obj.id,
                "hint": "A successor active lease exists for the same unit with a later end_date; use PATCH status=terminated after settlement instead.",
            },
        )
    ok, expected, actual = validate_lease_closeable(
        db, obj, expected_target_status=LeaseStatus.expired,
    )
    if not ok:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "lease_closeable_truth_missing",
                "expected": expected,
                "actual": actual,
                "hint": "Schedule MoveOutInspection -> confirm; then create DepositSettlement with amount conservation <=1c, -> confirm -> reconcile, before auto-expire.",
            },
        )
    old_row = serialize_row(obj)
    obj.status = LeaseStatus.expired
    obj.updated_by = user.id
    db.flush()
    apply_settled_lease_final_state(db, obj, actor_id=user.id, now=datetime.now(timezone.utc))
    record_audit(
        db, table_name="leases", record_id=obj.id, action="auto_expire",
        actor_id=user.id,
        changed_fields={"status": [old_status, LeaseStatus.expired.value]},
        old_value=old_row, new_value=serialize_row(obj),
    )
    db.commit()
    db.refresh(obj)
    return LeaseAutoExpireResponse(
        id=obj.id,
        status=LeaseStatus.expired.value,
        old_status=old_status,
        new_status=LeaseStatus.expired.value,
        already_expired=False,
    )
