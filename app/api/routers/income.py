from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, manager_or_admin, owner_subject_only
from app.database import get_db
from app.models.financial import Income, IncomeStatus
from app.models.lease import Lease
from app.models.user import User
from app.schemas.financial import IncomeCreate, IncomeRead, IncomeUpdate
from app.services.audit import field_changes, record_audit, serialize_row
from app.services.organization_scope import (
    CrossOrgReference,
    OwnerRequired,
    ScopeBlocked,
    lease_org_id,
    resolve_org_membership,
    scoped_get_income,
    scoped_list_incomes,
)

router = APIRouter(prefix="/incomes", tags=["incomes"])


def _scope_exception_to_http(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, (ScopeBlocked, OwnerRequired)):
        return HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    if isinstance(exc, CrossOrgReference):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, type(exc).__name__)


def _check_lease(db: Session, lease_id: int | None) -> None:
    if lease_id is None:
        return
    lease = db.query(Lease).filter(Lease.id == lease_id, Lease.deleted_at.is_(None)).first()
    if lease is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lease not found")


def _assert_lease_co_org(db: Session, user: User, lease_id: int | None) -> None:
    if lease_id is None:
        return
    object_org_id = lease_org_id(db, lease_id)
    if object_org_id is None:
        raise CrossOrgReference(
            f"Lease id={lease_id} not found or has no organization"
        )
    try:
        resolve_org_membership(db, user.id, object_org_id)
    except ScopeBlocked:
        raise CrossOrgReference(
            f"Lease id={lease_id} does not belong to the caller's organization"
        ) from None


@router.get("", response_model=list[IncomeRead])
def list_incomes(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    try:
        return scoped_list_incomes(db, for_user_id=user.id)
    except Exception as exc:
        raise _scope_exception_to_http(exc) from exc


@router.post("", response_model=IncomeRead, status_code=status.HTTP_201_CREATED)
def create_income(
    payload: IncomeCreate,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(manager_or_admin),
):
    if payload.status not in (IncomeStatus.pending, IncomeStatus.confirmed):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Income can only be created as pending or confirmed",
        )
    _check_lease(db, payload.lease_id)
    try:
        _assert_lease_co_org(db, user, payload.lease_id)
    except Exception as exc:
        raise _scope_exception_to_http(exc) from exc
    if payload.idempotency_key:
        existing = (
            db.query(Income)
            .filter(Income.idempotency_key == payload.idempotency_key)
            .first()
        )
        if existing is not None:
            response.status_code = status.HTTP_200_OK
            return existing
    obj = Income(**payload.model_dump())
    obj.created_by = user.id
    obj.updated_by = user.id
    if obj.status == IncomeStatus.confirmed:
        obj.confirmed_by = user.id
        obj.confirmed_at = datetime.now(timezone.utc)
    db.add(obj)
    try:
        db.flush()
        record_audit(
            db,
            table_name="incomes",
            record_id=obj.id,
            action="create",
            actor_id=user.id,
            new_value=serialize_row(obj),
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        if payload.idempotency_key:
            existing = (
                db.query(Income)
                .filter(Income.idempotency_key == payload.idempotency_key)
                .first()
            )
            if existing is not None:
                response.status_code = status.HTTP_200_OK
                return existing
        raise
    db.refresh(obj)
    return obj


@router.get("/{income_id}", response_model=IncomeRead)
def get_income(
    income_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    try:
        obj, _membership = scoped_get_income(db, income_id, for_user_id=user.id)
    except Exception as exc:
        raise _scope_exception_to_http(exc) from exc
    return obj


@router.patch("/{income_id}", response_model=IncomeRead)
def update_income(
    income_id: int,
    payload: IncomeUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(manager_or_admin),
):
    try:
        obj, _membership = scoped_get_income(db, income_id, for_user_id=user.id)
    except Exception as exc:
        raise _scope_exception_to_http(exc) from exc
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return obj
    if obj.status == IncomeStatus.reversed:
        raise HTTPException(status.HTTP_409_CONFLICT, "Cannot edit a reversed income")
    if obj.status == IncomeStatus.confirmed and "amount" in updates and updates["amount"] != obj.amount:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Cannot change the amount of a confirmed income"
        )
    if "lease_id" in updates:
        _check_lease(db, updates["lease_id"])
        try:
            _assert_lease_co_org(db, user, updates["lease_id"])
        except Exception as exc:
            raise _scope_exception_to_http(exc) from exc
    old = serialize_row(obj)
    changed = field_changes(obj, updates)
    for field, value in updates.items():
        setattr(obj, field, value)
    obj.updated_by = user.id
    record_audit(
        db,
        table_name="incomes",
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


@router.post("/{income_id}/confirm", response_model=IncomeRead)
def confirm_income(
    income_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(owner_subject_only),
):
    try:
        obj, _membership = scoped_get_income(db, income_id, for_user_id=user.id)
    except Exception as exc:
        raise _scope_exception_to_http(exc) from exc
    old = serialize_row(obj)
    result = db.execute(
        update(Income)
        .where(Income.id == income_id, Income.status == IncomeStatus.pending)
        .values(
            status=IncomeStatus.confirmed,
            confirmed_by=user.id,
            confirmed_at=datetime.now(timezone.utc),
            updated_by=user.id,
            updated_at=func.now(),
        ),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount == 1:
        db.refresh(obj)
        record_audit(
            db,
            table_name="incomes",
            record_id=obj.id,
            action="confirm",
            actor_id=user.id,
            old_value=old,
            new_value=serialize_row(obj),
        )
        db.commit()
        db.refresh(obj)
        return obj
    db.rollback()
    try:
        current, _m = scoped_get_income(db, income_id, for_user_id=user.id)
    except Exception as exc:
        raise _scope_exception_to_http(exc) from exc
    if current.status == IncomeStatus.confirmed:
        return current
    raise HTTPException(status.HTTP_409_CONFLICT, "Only pending income can be confirmed")


@router.post("/{income_id}/reverse", response_model=IncomeRead)
def reverse_income(
    income_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(owner_subject_only),
):
    try:
        obj, _membership = scoped_get_income(db, income_id, for_user_id=user.id)
    except Exception as exc:
        raise _scope_exception_to_http(exc) from exc
    old = serialize_row(obj)
    result = db.execute(
        update(Income)
        .where(Income.id == income_id, Income.status == IncomeStatus.confirmed)
        .values(
            status=IncomeStatus.reversed,
            updated_by=user.id,
            updated_at=func.now(),
        ),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount == 1:
        db.refresh(obj)
        record_audit(
            db,
            table_name="incomes",
            record_id=obj.id,
            action="reverse",
            actor_id=user.id,
            old_value=old,
            new_value=serialize_row(obj),
        )
        db.commit()
        db.refresh(obj)
        return obj
    db.rollback()
    try:
        current, _m = scoped_get_income(db, income_id, for_user_id=user.id)
    except Exception as exc:
        raise _scope_exception_to_http(exc) from exc
    if current.status == IncomeStatus.reversed:
        return current
    raise HTTPException(status.HTTP_409_CONFLICT, "Only confirmed income can be reversed")
