import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.database import get_db
from app.models.expense_claim import ClaimStatus
from app.models.financial import Expense, ExpenseStatus
from app.models.operations import (
    OperationalTask,
    OperationalTaskPriority,
    OperationalTaskStatus,
    OperationalTaskType,
)

logger = logging.getLogger(__name__)
from app.models.property import Property, Unit
from app.models.user import User
from app.schemas.common import Paginated
from app.schemas.financial import (
    ExpenseCreate,
    ExpenseDetailOut,
    ExpenseRead,
    ExpenseUpdate,
    PaymentClaimIn,
    PaymentClaimOut,
)
from app.services.audit import field_changes, record_audit, serialize_row
from app.services.expense_claims import (
    create_claim,
    fail_claim,
    list_claims,
    reverse_claim,
    verify_claim,
    ClaimError,
)
from app.services.expense_payment_truth import (
    expense_finance_payload,
    payment_truth,
    clear_approval,
)
from app.services.expense_timeline import build_expense_timeline
from app.services.operations import projection as task_projection
from app.services.organization_scope import (
    CrossOrgReference,
    OrganizationRole,
    OwnerRequired,
    ScopeBlocked,
    assert_co_org,
    property_org_id,
    resolve_org_membership,
    scope_exception_to_http,
    scoped_get_expense,
    scoped_list_expenses,
    unit_org_id,
)

router = APIRouter(prefix="/expenses", tags=["expenses"])

_CREATABLE = {ExpenseStatus.pending, ExpenseStatus.approved}

_CRITICAL_FIELDS = ("amount", "payee", "category", "description", "unit_id", "property_id", "payer_user_id")


class _ResubmitPayload(ExpenseUpdate):
    idempotency_key: str | None = None


def _check_unit(db: Session, unit_id: int | None) -> None:
    if unit_id is None:
        return
    unit = (
        db.query(Unit)
        .filter(Unit.id == unit_id, Unit.deleted_at.is_(None))
        .first()
    )
    if unit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unit not found")


def _check_property(db: Session, property_id: int | None) -> None:
    if property_id is None:
        return
    prop = (
        db.query(Property)
        .filter(Property.id == property_id, Property.deleted_at.is_(None))
        .first()
    )
    if prop is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Property not found")


def _resolve_expense_property_id(db: Session, unit_id: int | None, property_id: int | None) -> int:
    """Resolve the final expense.property_id with canonical ownership rules.

    * At least one of unit_id or property_id MUST be present.
    * If both present: unit.property_id MUST equal property_id, else CrossOrgReference (409).
    * Returns the property_id to store on Expense (NOT NULL).
    """
    resolved_from_unit: int | None = None
    if unit_id is not None:
        unit_row = db.query(Unit.property_id).filter(Unit.id == unit_id, Unit.deleted_at.is_(None)).one_or_none()
        if unit_row is None:
            raise LookupError("Unit not found")
        resolved_from_unit = unit_row[0]
    if property_id is not None and resolved_from_unit is not None:
        if property_id != resolved_from_unit:
            raise CrossOrgReference(
                "Expense unit_id and property_id must belong to the same property"
            )
    final_property_id = resolved_from_unit if resolved_from_unit is not None else property_id
    if final_property_id is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Expense requires at least unit_id or property_id (canonical ownership anchor)",
        )
    return final_property_id


def _resolve_expense_org_id(db: Session, unit_id: int | None, property_id: int | None) -> int:
    """Resolve canonical organization_id for an Expense via both channels (fail closed)."""
    final_property_id = _resolve_expense_property_id(db, unit_id, property_id)
    oid = property_org_id(db, final_property_id)
    if oid is None:
        raise CrossOrgReference(
            f"Expense property id={final_property_id} not found or has no organization"
        )
    return oid


def _check_payer(db: Session, payer_user_id: int | None) -> None:
    if payer_user_id is None:
        return
    from app.models.user import User

    user = db.query(User).filter(User.id == payer_user_id, User.is_active.is_(True)).first()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payer user not found")


def _guard_edit(db: Session, obj: Expense, updates: dict, user: User) -> None:
    if obj.status == ExpenseStatus.reversed:
        raise HTTPException(status.HTTP_409_CONFLICT, "Cannot edit a reversed expense")
    if obj.status == ExpenseStatus.rejected:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Rejected expense is edited via POST /expenses/{id}/resubmit (preserves the rejected version)",
        )
    critical_changed = {f for f in _CRITICAL_FIELDS if f in updates and updates[f] != getattr(obj, f)}
    if obj.status in (ExpenseStatus.approved, ExpenseStatus.paid, ExpenseStatus.partially_paid, ExpenseStatus.payment_claimed):
        if critical_changed:
            clear_approval(db, obj, actor_id=user.id,
                           reason="Critical financial field changed after approval "
                                  f"({', '.join(sorted(critical_changed))})")


def _complete_linked_approval_task(
    db: Session,
    expense: Expense,
    *,
    actor_id: int,
    reason: str,
) -> None:
    task = (
        db.query(OperationalTask)
        .filter(
            OperationalTask.source_type == "expense",
            OperationalTask.source_id == expense.id,
            OperationalTask.task_type == OperationalTaskType.APPROVAL_PENDING,
            OperationalTask.status.in_(
                [OperationalTaskStatus.PENDING, OperationalTaskStatus.IN_PROGRESS]
            ),
        )
        .first()
    )
    if task is None:
        return
    audit_action = (
        "task_completed_via_approval"
        if reason == "approval"
        else "task_completed_via_rejection"
    )
    task_projection.close_active_projections(
        db,
        tasks=[task],
        actor_id=actor_id,
        reason=reason,
        source_domain="expense.approval",
        audit_action=audit_action,
    )


def _complete_linked_payment_tasks(
    db: Session,
    expense: Expense,
    *,
    actor_id: int,
) -> None:
    tasks = (
        db.query(OperationalTask)
        .filter(
            OperationalTask.source_type == "expense",
            OperationalTask.source_id == expense.id,
            OperationalTask.task_type == OperationalTaskType.PAYMENT_PENDING,
            OperationalTask.status.in_(
                [OperationalTaskStatus.PENDING, OperationalTaskStatus.IN_PROGRESS]
            ),
        )
        .all()
    )
    if not tasks:
        return
    task_projection.close_active_projections(
        db,
        tasks=tasks,
        actor_id=actor_id,
        reason="expense_paid",
        source_domain="expense.payment",
        audit_action="task_completed_via_payment",
    )


def _claim_out(c, expense_id: int) -> dict:
    return {
        "id": c.id,
        "expense_id": expense_id,
        "claimed_amount": str(c.claimed_amount),
        "claimed_by": c.claimed_by,
        "claimed_at": c.claimed_at,
        "status": c.status.value,
        "evidence_ids": c.evidence_ids or [],
        "verification_note": c.verification_note,
        "verified_amount": str(c.verified_amount) if c.verified_amount is not None else None,
        "verified_by": c.verified_by,
        "verified_at": c.verified_at,
        "mismatch": bool(c.mismatch),
        "mismatch_reason": c.mismatch_reason,
        "failure_reason": c.failure_reason,
    }


def _build_detail(db: Session, expense: Expense) -> ExpenseDetailOut:
    claims = list_claims(db, expense.id)
    base = ExpenseRead.model_validate(expense).model_dump()
    evidence = _evidence_for_expense(db, expense, claims)
    return ExpenseDetailOut(
        **base,
        payment=expense_finance_payload(db, expense, claims),
        claims=[PaymentClaimOut(**_claim_out(c, expense.id)) for c in claims],
        evidence=evidence,
        timeline=build_expense_timeline(db, expense, claims),
        reviewed={
            "approved_by": expense.approved_by,
            "approved_at": expense.approved_at,
            "rejection_reason": expense.rejection_reason,
            "reapproval_reason": expense.reapproval_reason,
            "version": expense.version,
            "parent_expense_id": expense.parent_expense_id,
        },
    )


def _evidence_for_expense(db: Session, expense: Expense, claims) -> dict:
    from app.models.evidence import Evidence

    out = {}
    for c in claims:
        ids = [int(i) for i in (c.evidence_ids or [])]
        rows = (
            db.query(Evidence).filter(Evidence.id.in_(ids)).all()
            if ids else []
        )
        out[str(c.id)] = [
            {
                "id": e.id,
                "category": e.category.value if hasattr(e.category, "value") else e.category,
                "media_type": e.media_type,
                "external_file_id": e.external_file_id,
                "filename": e.filename,
            }
            for e in rows
        ]
    if expense.receipt_attachment_id:
        out["receipt_attachment_id"] = expense.receipt_attachment_id
    return out


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

@router.get("", response_model=Paginated[ExpenseRead])
def list_expenses(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    try:
        rows = scoped_list_expenses(db, for_user_id=user.id)
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    total = len(rows)
    paged = rows[offset:offset + limit]
    return Paginated(items=paged, total=total, limit=limit, offset=offset)


@router.get("/{expense_id}", response_model=ExpenseRead)
def get_expense(
    expense_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    try:
        obj, _membership = scoped_get_expense(db, expense_id, for_user_id=user.id)
        return obj
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc


@router.get("/{expense_id}/detail", response_model=ExpenseDetailOut)
def expense_detail(
    expense_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        obj, _membership = scoped_get_expense(db, expense_id, for_user_id=user.id)
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc
    return _build_detail(db, obj)


# ---------------------------------------------------------------------------
# Create / update
# ---------------------------------------------------------------------------

@router.post("", response_model=ExpenseRead, status_code=status.HTTP_201_CREATED)
def create_expense(
    payload: ExpenseCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if payload.status not in _CREATABLE:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Expense can only be created as pending or approved",
        )

    membership = None
    try:
        final_property_id = _resolve_expense_property_id(db, payload.unit_id, payload.property_id)
        e_org_id = _resolve_expense_org_id(db, payload.unit_id, payload.property_id)
        membership = resolve_org_membership(
            db, user.id, e_org_id, role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY]
        )
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc

    if payload.status == ExpenseStatus.approved and membership.role != OrganizationRole.OWNER:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Only OWNER can create an approved expense"
        )
    _check_unit(db, payload.unit_id)
    _check_property(db, payload.property_id)
    _check_payer(db, payload.payer_user_id)
    data = payload.model_dump()
    data["property_id"] = final_property_id
    obj = Expense(**data, version=1)
    obj.created_by = user.id
    obj.updated_by = user.id
    if obj.status == ExpenseStatus.approved:
        obj.approved_by = user.id
        obj.approved_at = datetime.now(timezone.utc)
    db.add(obj)
    db.flush()
    record_audit(
        db,
        table_name="expenses",
        record_id=obj.id,
        action="create",
        actor_id=user.id,
        new_value=serialize_row(obj),
    )
    db.commit()
    db.refresh(obj)
    return obj


@router.patch("/{expense_id}", response_model=ExpenseRead)
def update_expense(
    expense_id: int,
    payload: ExpenseUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        obj, membership = scoped_get_expense(
            db, expense_id, for_user_id=user.id,
            role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY],
        )
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return obj
    _guard_edit(db, obj, updates, user)

    effective_unit_id = updates.get("unit_id", obj.unit_id)
    effective_property_id = updates.get("property_id", obj.property_id)
    try:
        final_property_id = _resolve_expense_property_id(db, effective_unit_id, effective_property_id)
        new_org_id = _resolve_expense_org_id(db, effective_unit_id, effective_property_id)
        assert_co_org(
            db,
            user_org_id=membership.organization_id,
            object_org_id=new_org_id,
            object_kind="Expense (after update)",
            object_id=expense_id,
        )
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc

    _check_unit(db, updates.get("unit_id"))
    _check_property(db, updates.get("property_id"))
    if "payer_user_id" in updates:
        _check_payer(db, updates.get("payer_user_id"))
    old = serialize_row(obj)
    changed = field_changes(obj, updates)
    for field, value in updates.items():
        if field != "status":
            setattr(obj, field, value)
    obj.property_id = final_property_id
    obj.updated_by = user.id
    record_audit(
        db,
        table_name="expenses",
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


# ---------------------------------------------------------------------------
# Approval / Rejection / Resubmit
# ---------------------------------------------------------------------------

@router.post("/{expense_id}/approve", response_model=ExpenseRead)
def approve_expense(
    expense_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        obj, membership = scoped_get_expense(
            db, expense_id, for_user_id=user.id, role=OrganizationRole.OWNER
        )
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc

    if obj.created_by == user.id and membership.role != OrganizationRole.OWNER:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Cannot approve an expense you created"
        )
    old = serialize_row(obj)
    result = db.execute(
        update(Expense)
        .where(Expense.id == expense_id, Expense.status == ExpenseStatus.pending)
        .values(
            status=ExpenseStatus.approved,
            approved_by=user.id,
            approved_at=datetime.now(timezone.utc),
            updated_by=user.id,
            updated_at=func.now(),
        ),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount == 1:
        db.refresh(obj)
        _complete_linked_approval_task(db, obj, actor_id=user.id, reason="approval")
        record_audit(
            db,
            table_name="expenses",
            record_id=obj.id,
            action="approve",
            actor_id=user.id,
            old_value=old,
            new_value=serialize_row(obj),
        )
        db.commit()
        db.refresh(obj)
        return obj
    db.rollback()
    current, _m = scoped_get_expense(db, expense_id, for_user_id=user.id)
    if current.status == ExpenseStatus.approved:
        return current
    raise HTTPException(status.HTTP_409_CONFLICT, "Only pending expenses can be approved")


@router.post("/{expense_id}/reject", response_model=ExpenseRead)
def reject_expense(
    expense_id: int,
    payload: PaymentClaimIn | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        obj, membership = scoped_get_expense(
            db, expense_id, for_user_id=user.id, role=OrganizationRole.OWNER
        )
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc

    if obj.created_by == user.id and membership.role != OrganizationRole.OWNER:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Cannot reject an expense you created"
        )
    old = serialize_row(obj)
    result = db.execute(
        update(Expense)
        .where(Expense.id == expense_id, Expense.status == ExpenseStatus.pending)
        .values(
            status=ExpenseStatus.rejected,
            rejection_reason=(payload.reason if payload else None) or obj.rejection_reason,
            updated_by=user.id,
            updated_at=func.now(),
        ),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount == 1:
        db.refresh(obj)
        _complete_linked_approval_task(db, obj, actor_id=user.id, reason="rejection")
        record_audit(
            db,
            table_name="expenses",
            record_id=obj.id,
            action="reject",
            actor_id=user.id,
            changed_fields={
                "status": ["pending", "rejected"],
                "rejection_reason": [None, obj.rejection_reason],
            },
            old_value=old,
            new_value=serialize_row(obj),
        )
        db.commit()
        db.refresh(obj)
        return obj
    db.rollback()
    current, _m = scoped_get_expense(db, expense_id, for_user_id=user.id)
    if current.status == ExpenseStatus.rejected:
        return current
    raise HTTPException(status.HTTP_409_CONFLICT, "Only pending expenses can be rejected")


@router.post("/{expense_id}/resubmit", response_model=ExpenseRead, status_code=status.HTTP_201_CREATED)
def resubmit_expense(
    expense_id: int,
    payload: _ResubmitPayload,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        prior, membership = scoped_get_expense(
            db, expense_id, for_user_id=user.id,
            role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY],
        )
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc

    if prior.status != ExpenseStatus.rejected:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Only a rejected expense can be resubmitted as the next version",
        )
    updates = payload.model_dump(exclude_unset=True)
    effective_unit_id = updates.get("unit_id", prior.unit_id)
    effective_property_id = updates.get("property_id", prior.property_id)
    try:
        final_property_id = _resolve_expense_property_id(db, effective_unit_id, effective_property_id)
        new_org_id = _resolve_expense_org_id(db, effective_unit_id, effective_property_id)
        assert_co_org(db, user_org_id=membership.organization_id, object_org_id=new_org_id, object_kind="Expense (resubmit)", object_id=expense_id)
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc

    _check_unit(db, effective_unit_id)
    _check_property(db, effective_property_id)
    effective_payer = updates.get("payer_user_id", prior.payer_user_id)
    if effective_payer:
        _check_payer(db, effective_payer)
    data = {
        "expense_date": updates.get("expense_date", prior.expense_date),
        "due_date": updates.get("due_date", prior.due_date),
        "category": updates.get("category", prior.category),
        "amount": updates.get("amount", prior.amount),
        "payee": updates.get("payee", prior.payee),
        "description": updates.get("description", prior.description),
        "unit_id": effective_unit_id,
        "property_id": final_property_id,
        "payer_user_id": effective_payer,
        "status": ExpenseStatus.pending,
        "version": (prior.version or 1) + 1,
        "parent_expense_id": prior.id,
    }
    obj = Expense(**data)
    obj.created_by = user.id
    obj.updated_by = user.id
    db.add(obj)
    db.flush()
    record_audit(
        db,
        table_name="expenses",
        record_id=obj.id,
        action="expense_resubmitted",
        actor_id=user.id,
        changed_fields={"version": [prior.version, data["version"]], "status": [None, "pending"]},
        old_value={"parent_id": prior.id, "rejection_reason": prior.rejection_reason},
        new_value=serialize_row(obj),
    )
    db.commit()
    db.refresh(obj)
    return obj


# ---------------------------------------------------------------------------
# Payment claims
# ---------------------------------------------------------------------------

@router.post("/{expense_id}/claims", response_model=PaymentClaimOut, status_code=status.HTTP_201_CREATED)
def create_payment_claim(
    expense_id: int,
    payload: PaymentClaimIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        expense, _membership = scoped_get_expense(
            db, expense_id, for_user_id=user.id,
            role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY],
        )
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc

    if payload.claimed_amount is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "claimed_amount is required")
    try:
        claim, _created = create_claim(
            db,
            expense,
            claimed_amount=payload.claimed_amount,
            claimed_by=user.id,
            verification_note=payload.verification_note,
            evidence_ids=payload.evidence_ids,
            idempotency_key=payload.idempotency_key,
        )
    except ClaimError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    db.commit()
    db.refresh(claim)
    return PaymentClaimOut(**_claim_out(claim, expense_id))


@router.get("/{expense_id}/claims", response_model=list[PaymentClaimOut])
def list_payment_claims(
    expense_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        _expense, _membership = scoped_get_expense(db, expense_id, for_user_id=user.id)
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc
    return [PaymentClaimOut(**__claim_out(c)) for c in list_claims(db, expense_id)]


def __claim_out(c) -> dict:
    return {
        "id": c.id,
        "expense_id": c.expense_id,
        "claimed_amount": str(c.claimed_amount),
        "claimed_by": c.claimed_by,
        "claimed_at": c.claimed_at,
        "status": c.status.value,
        "evidence_ids": c.evidence_ids or [],
        "verification_note": c.verification_note,
        "verified_amount": str(c.verified_amount) if c.verified_amount is not None else None,
        "verified_by": c.verified_by,
        "verified_at": c.verified_at,
        "mismatch": bool(c.mismatch),
        "mismatch_reason": c.mismatch_reason,
        "failure_reason": c.failure_reason,
    }


@router.post("/{expense_id}/claims/{claim_id}/verify", response_model=PaymentClaimOut)
def verify_payment_claim(
    expense_id: int,
    claim_id: int,
    payload: PaymentClaimIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        expense, _membership = scoped_get_expense(
            db, expense_id, for_user_id=user.id, role=OrganizationRole.OWNER
        )
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc

    try:
        claim = verify_claim(
            db,
            expense,
            claim_id,
            verified_by=user.id,
            verified_amount=payload.verified_amount,
            result=payload.result,
        )
    except ClaimError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    _finalize_paid(expense, db, actor_id=user.id)
    db.commit()
    db.refresh(claim)
    return PaymentClaimOut(**_claim_out(claim, expense_id))


@router.post("/{expense_id}/claims/{claim_id}/fail", response_model=PaymentClaimOut)
def fail_payment_claim(
    expense_id: int,
    claim_id: int,
    payload: PaymentClaimIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        expense, _membership = scoped_get_expense(
            db, expense_id, for_user_id=user.id, role=OrganizationRole.OWNER
        )
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc

    now = datetime.now(timezone.utc)
    try:
        claim = fail_claim(
            db, expense, claim_id,
            failed_by=user.id, reason=payload.reason or payload.result or "verification failed",
            now=now,
        )
    except ClaimError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    dedupe_key = f"expense:{expense_id}:claim_fail:{claim_id}"
    existing_active = (
        db.query(OperationalTask)
        .filter(
            OperationalTask.dedupe_key == dedupe_key,
            OperationalTask.status.in_(
                [OperationalTaskStatus.PENDING, OperationalTaskStatus.IN_PROGRESS]
            ),
        )
        .first()
    )
    mismatch = bool(getattr(claim, "mismatch", False))
    failure_label = "金额不一致" if mismatch else "FAILED"
    summary = (
        f"Owner Expense Claim {failure_label}决策："
        f"Expense #{expense_id} Claim #{claim_id} 需Owner决定后续处理方式"
    )
    from app.services.operations.generation import create_operational_task

    scoped_org_id = _membership.organization_id
    if existing_active is None:
        try:
            with db.begin_nested():
                create_operational_task(
                    db,
                    fields={
                        "task_type": OperationalTaskType.FOLLOWUP,
                        "title": f"Expense Claim {failure_label}待处理",
                        "description": summary,
                        "source_type": "expense",
                        "source_id": expense_id,
                        "property_id": expense.property_id,
                        "assigned_user_id": user.id,
                        "priority": OperationalTaskPriority.high,
                        "status": OperationalTaskStatus.PENDING,
                        "due_at": now + timedelta(days=3),
                        "remind_at": now,
                        "next_check_at": now,
                        "next_action": "PAYMENT_CLAIM_DECISION",
                        "dedupe_key": dedupe_key,
                        "details": {
                            "expense_id": expense_id,
                            "claim_id": claim_id,
                            "claim_status": "FAILED",
                            "mismatch": mismatch,
                            "failure_reason": getattr(claim, "failure_reason", None),
                            "next_actor": "OWNER",
                            "organization_id": scoped_org_id,
                            "summary": summary,
                        },
                    },
                    now=now,
                    actor_id=user.id,
                )
        except Exception:
            logger.exception(
                "failed to create claim-failure followup task "
                "expense_id=%s claim_id=%s dedupe_key=%s",
                expense_id, claim_id, dedupe_key,
            )
    db.commit()
    db.refresh(claim)
    return PaymentClaimOut(**_claim_out(claim, expense_id))


@router.post("/{expense_id}/claims/{claim_id}/reverse", response_model=PaymentClaimOut)
def reverse_payment_claim(
    expense_id: int,
    claim_id: int,
    payload: PaymentClaimIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        expense, _membership = scoped_get_expense(
            db, expense_id, for_user_id=user.id, role=OrganizationRole.OWNER
        )
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc

    try:
        claim = reverse_claim(
            db, expense, claim_id,
            reversed_by=user.id, reason=payload.reason or payload.result or "reversed",
        )
    except ClaimError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    db.commit()
    db.refresh(claim)
    return PaymentClaimOut(**_claim_out(claim, expense_id))


# ---------------------------------------------------------------------------
# Pay / Reverse (Owner final verification)
# ---------------------------------------------------------------------------

def _finalize_paid(expense: Expense, db: Session, actor_id: int | None = None) -> None:
    truth = payment_truth(db, expense)
    if truth.fully_paid:
        _complete_linked_payment_tasks(db, expense, actor_id=actor_id or expense.updated_by)
        _schedule_repair_verification_followup(db, expense, actor_id=actor_id or expense.updated_by)


def _evidence_present_for_repair_close(repair) -> bool:
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


def _schedule_repair_verification_followup(
    db: Session, expense: Expense, *, actor_id: int | None
) -> None:
    from app.models.repair import RepairOperation, RepairOperationStatus, RepairProposal
    from app.services.organization_scope import property_org_id, repair_org_id
    from app.services.operations.generation import create_operational_task

    proposal_row = (
        db.query(RepairProposal.repair_id)
        .filter(RepairProposal.expense_id == expense.id)
        .first()
    )
    if proposal_row is None:
        return
    repair_id = proposal_row[0]
    if repair_id is None:
        return
    repair = db.get(RepairOperation, repair_id)
    if repair is None:
        return
    if repair.status == RepairOperationStatus.CLOSED:
        return
    if repair.status != RepairOperationStatus.VERIFYING:
        return
    exp_org = property_org_id(db, expense.property_id)
    rep_org = repair_org_id(db, repair_id)
    if exp_org is None or rep_org is None or exp_org != rep_org:
        return
    now = datetime.now(timezone.utc)
    dedupe_key = f"repair:{repair_id}:verification_followup_from_expense_{expense.id}"
    existing_active = (
        db.query(OperationalTask)
        .filter(
            OperationalTask.dedupe_key == dedupe_key,
            OperationalTask.status.in_(
                [OperationalTaskStatus.PENDING, OperationalTaskStatus.IN_PROGRESS]
            ),
        )
        .first()
    )
    if existing_active is not None:
        return
    summary = (
        "Repair verification follow-up: linked expense E{} fully paid. "
        "Please perform canonical repair verification (evidence review & "
        "explicit HUMAN_CONFIRMED or rejection) via the repairs router — "
        "payment alone does not constitute verification."
    ).format(expense.id)
    repair_property_id = repair.property_id
    if repair.unit_id is not None and repair_property_id is None:
        unit_row = (
            db.query(Unit.property_id)
            .filter(Unit.id == repair.unit_id, Unit.deleted_at.is_(None))
            .one_or_none()
        )
        if unit_row is not None:
            repair_property_id = unit_row[0]
    try:
        with db.begin_nested():
            create_operational_task(
                db,
                fields={
                    "task_type": OperationalTaskType.FOLLOWUP,
                    "title": f"Repair #{repair_id} 核销跟进（关联支出已支付）",
                    "description": summary,
                    "source_type": "repair",
                    "source_id": repair_id,
                    "property_id": repair_property_id,
                    "assigned_user_id": actor_id,
                    "priority": OperationalTaskPriority.high if _evidence_present_for_repair_close(repair) else OperationalTaskPriority.medium,
                    "status": OperationalTaskStatus.PENDING,
                    "due_at": now + timedelta(days=3),
                    "remind_at": now,
                    "next_check_at": now,
                    "next_action": "CANONICAL_REPAIR_VERIFICATION",
                    "dedupe_key": dedupe_key,
                    "details": {
                        "repair_id": repair_id,
                        "trigger_expense_id": expense.id,
                        "repair_status": repair.status.value,
                        "evidence_present": _evidence_present_for_repair_close(repair),
                        "next_actor": "OWNER",
                        "organization_id": rep_org,
                        "warning": "Payment is not verification. Do NOT close the repair from the expense path.",
                    },
                },
                now=now,
                actor_id=actor_id,
            )
    except Exception:
        logger.exception(
            "failed to create repair verification followup after paid expense=%s repair_id=%s",
            expense.id, repair_id,
        )


@router.post("/{expense_id}/pay", response_model=ExpenseRead)
def pay_expense(
    expense_id: int,
    payload: PaymentClaimIn | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        obj, _membership = scoped_get_expense(
            db, expense_id, for_user_id=user.id, role=OrganizationRole.OWNER
        )
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc

    obj = (
        db.query(Expense)
        .filter(Expense.id == expense_id)
        .with_for_update()
        .first()
    )
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Expense not found")
    if obj.status in (ExpenseStatus.pending, ExpenseStatus.rejected, ExpenseStatus.reversed):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Expense must be approved before payment ({obj.status.value}); cannot pay",
        )
    truth = payment_truth(db, obj)
    if truth.fully_paid:
        _finalize_paid(obj, db, actor_id=user.id)
        db.commit()
        db.refresh(obj)
        return obj
    now = datetime.now(timezone.utc)
    from app.services.expense_claims import claim_idempotency_key

    key = payload.idempotency_key if payload and payload.idempotency_key else claim_idempotency_key(
        obj.id, user.id, "owner-complete-remaining"
    )
    try:
        claim, was_created = create_claim(
            db,
            obj,
            claimed_amount=truth.remaining if truth.remaining > 0 else obj.amount,
            claimed_by=user.id,
            verification_note=(payload.verification_note if payload else None),
            evidence_ids=(payload.evidence_ids if payload else None),
            idempotency_key=key,
            now=now,
        )
        verify_claim(
            db, obj, claim.id, verified_by=user.id,
            verified_amount=None, result=(payload.result if payload else None), now=now,
        )
    except ClaimError as exc:
        db.rollback()
        db.expire_all()
        db.refresh(obj)
        post = payment_truth(db, obj)
        if post.fully_paid:
            _finalize_paid(obj, db, actor_id=user.id)
            db.commit()
            db.refresh(obj)
            return obj
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if was_created:
        record_audit(
            db,
            table_name="expenses",
            record_id=obj.id,
            action="pay",
            actor_id=user.id,
            changed_fields={"status": ["approved", obj.status.value]},
        )
    _finalize_paid(obj, db, actor_id=user.id)
    db.commit()
    db.refresh(obj)
    return obj


@router.post("/{expense_id}/reverse", response_model=ExpenseRead)
def reverse_expense(
    expense_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        obj, _membership = scoped_get_expense(
            db, expense_id, for_user_id=user.id, role=OrganizationRole.OWNER
        )
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc

    obj = (
        db.query(Expense)
        .filter(Expense.id == expense_id)
        .with_for_update()
        .first()
    )
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Expense not found")
    if obj.status == ExpenseStatus.reversed:
        db.commit()
        db.refresh(obj)
        return obj
    claims = [c for c in list_claims(db, obj.id) if c.status == ClaimStatus.VERIFIED]
    if not claims:
        db.refresh(obj)
        if obj.status == ExpenseStatus.reversed:
            db.commit()
            db.refresh(obj)
            return obj
        raise HTTPException(status.HTTP_409_CONFLICT, "No verified payments to reverse")
    now = datetime.now(timezone.utc)
    for c in claims:
        try:
            reverse_claim(db, obj, c.id, reversed_by=user.id, reason="expense_reversed", now=now)
        except ClaimError as exc:
            db.rollback()
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    reverse_window_earliest = now - timedelta(hours=72)
    completed_payment_task_rows = (
        db.query(OperationalTask.id)
        .filter(
            OperationalTask.source_type == "expense",
            OperationalTask.source_id == obj.id,
            OperationalTask.task_type == OperationalTaskType.PAYMENT_PENDING,
            OperationalTask.status == OperationalTaskStatus.COMPLETED,
            OperationalTask.completed_at >= reverse_window_earliest,
        )
        .all()
    )
    completed_payment_task_ids = [row[0] for row in completed_payment_task_rows]
    if completed_payment_task_ids:
        task_projection.reopen_closed_projections(
            db,
            task_ids=completed_payment_task_ids,
            actor_id=user.id,
            source_domain="expense.reverse",
            reason="expense_reversed_reopen_payment_pending_within_72h",
            now=now,
        )
    old_status = obj.status
    obj.status = ExpenseStatus.reversed
    obj.updated_by = user.id
    obj.updated_at = now
    db.flush()
    record_audit(
        db,
        table_name="expenses",
        record_id=obj.id,
        action="reverse",
        actor_id=user.id,
        changed_fields={"status": [old_status.value if old_status else None, "reversed"]},
        old_value=serialize_row(obj),
        new_value=serialize_row(obj),
    )
    db.commit()
    db.refresh(obj)
    return obj
