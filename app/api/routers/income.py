from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import admin_only, get_current_user, manager_or_admin
from app.database import get_db
from app.models.financial import Income, IncomeStatus
from app.models.lease import Lease
from app.models.user import User
from app.schemas.financial import IncomeCreate, IncomeRead, IncomeUpdate
from app.services.audit import field_changes, record_audit, serialize_row

router = APIRouter(prefix="/incomes", tags=["incomes"])


def _get_or_404(db: Session, income_id: int) -> Income:
    obj = db.query(Income).filter(Income.id == income_id).first()
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Income not found")
    return obj


def _check_lease(db: Session, lease_id: int | None) -> None:
    if lease_id is None:
        return
    lease = db.query(Lease).filter(Lease.id == lease_id, Lease.deleted_at.is_(None)).first()
    if lease is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lease not found")


@router.get("", response_model=list[IncomeRead])
def list_incomes(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return db.query(Income).order_by(Income.id).all()


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
    if payload.idempotency_key:
        existing = (
            db.query(Income)
            .filter(Income.idempotency_key == payload.idempotency_key)
            .first()
        )
        if existing is not None:
            # Idempotent replay: the create already landed -> return it.
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
        # UNIQUE(idempotency_key) is the atomic backstop: a concurrent
        # create with the same key won the race. Re-read and return it.
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
    income_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)
):
    return _get_or_404(db, income_id)


@router.patch("/{income_id}", response_model=IncomeRead)
def update_income(
    income_id: int,
    payload: IncomeUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(manager_or_admin),
):
    obj = _get_or_404(db, income_id)
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
    user: User = Depends(manager_or_admin),
):
    obj = _get_or_404(db, income_id)
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
    # rowcount == 0 -> no transition happened. Replay (already confirmed)
    # returns the current state; any other state is a genuine conflict.
    db.rollback()
    current = _get_or_404(db, income_id)
    if current.status == IncomeStatus.confirmed:
        return current
    raise HTTPException(status.HTTP_409_CONFLICT, "Only pending income can be confirmed")


@router.post("/{income_id}/reverse", response_model=IncomeRead)
def reverse_income(
    income_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
):
    obj = _get_or_404(db, income_id)
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
    current = _get_or_404(db, income_id)
    if current.status == IncomeStatus.reversed:
        return current
    raise HTTPException(status.HTTP_409_CONFLICT, "Only confirmed income can be reversed")
