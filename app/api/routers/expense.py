from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.api.deps import admin_only, get_current_user, manager_or_admin
from app.database import get_db
from app.models.financial import Expense, ExpenseStatus
from app.models.property import Unit
from app.models.user import User
from app.schemas.financial import ExpenseCreate, ExpenseRead, ExpenseUpdate
from app.services.audit import field_changes, record_audit, serialize_row

router = APIRouter(prefix="/expenses", tags=["expenses"])

_CREATABLE = {ExpenseStatus.pending, ExpenseStatus.approved}


def _get_or_404(db: Session, expense_id: int) -> Expense:
    obj = db.query(Expense).filter(Expense.id == expense_id).first()
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Expense not found")
    return obj


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


def _guard_edit(obj: Expense, updates: dict) -> None:
    if obj.status == ExpenseStatus.reversed:
        raise HTTPException(status.HTTP_409_CONFLICT, "Cannot edit a reversed expense")
    if obj.status in (ExpenseStatus.approved, ExpenseStatus.paid) and "amount" in updates:
        if updates["amount"] != obj.amount:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Cannot change the amount of an approved/paid expense; reject or reverse first",
            )


@router.get("", response_model=list[ExpenseRead])
def list_expenses(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return db.query(Expense).order_by(Expense.id).all()


@router.post("", response_model=ExpenseRead, status_code=status.HTTP_201_CREATED)
def create_expense(
    payload: ExpenseCreate,
    db: Session = Depends(get_db),
    user: User = Depends(manager_or_admin),
):
    if payload.status not in _CREATABLE:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Expense can only be created as pending or approved",
        )
    if payload.status == ExpenseStatus.approved and user.role != "admin":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Only admin can create an approved expense"
        )
    _check_unit(db, payload.unit_id)
    obj = Expense(**payload.model_dump())
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


@router.get("/{expense_id}", response_model=ExpenseRead)
def get_expense(
    expense_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)
):
    return _get_or_404(db, expense_id)


@router.patch("/{expense_id}", response_model=ExpenseRead)
def update_expense(
    expense_id: int,
    payload: ExpenseUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(manager_or_admin),
):
    obj = _get_or_404(db, expense_id)
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return obj
    _guard_edit(obj, updates)
    _check_unit(db, updates.get("unit_id"))
    old = serialize_row(obj)
    changed = field_changes(obj, updates)
    for field, value in updates.items():
        setattr(obj, field, value)
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


@router.post("/{expense_id}/approve", response_model=ExpenseRead)
def approve_expense(
    expense_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(manager_or_admin),
):
    obj = _get_or_404(db, expense_id)
    if user.role == "manager" and obj.created_by == user.id:
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
    current = _get_or_404(db, expense_id)
    if current.status == ExpenseStatus.approved:
        return current
    raise HTTPException(status.HTTP_409_CONFLICT, "Only pending expenses can be approved")


@router.post("/{expense_id}/reject", response_model=ExpenseRead)
def reject_expense(
    expense_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
):
    obj = _get_or_404(db, expense_id)
    # Owner (admin) is the final authority and may handle an expense they
    # recorded themselves (the Owner-records-then-approves flow). Only a
    # manager rejecting their own creation is blocked, mirroring approve.
    if user.role == "manager" and obj.created_by == user.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Cannot reject an expense you created"
        )
    old = serialize_row(obj)
    result = db.execute(
        update(Expense)
        .where(Expense.id == expense_id, Expense.status == ExpenseStatus.pending)
        .values(
            status=ExpenseStatus.rejected,
            updated_by=user.id,
            updated_at=func.now(),
        ),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount == 1:
        db.refresh(obj)
        record_audit(
            db,
            table_name="expenses",
            record_id=obj.id,
            action="reject",
            actor_id=user.id,
            old_value=old,
            new_value=serialize_row(obj),
        )
        db.commit()
        db.refresh(obj)
        return obj
    db.rollback()
    current = _get_or_404(db, expense_id)
    if current.status == ExpenseStatus.rejected:
        return current
    raise HTTPException(status.HTTP_409_CONFLICT, "Only pending expenses can be rejected")


@router.post("/{expense_id}/pay", response_model=ExpenseRead)
def pay_expense(
    expense_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
):
    obj = _get_or_404(db, expense_id)
    old = serialize_row(obj)
    result = db.execute(
        update(Expense)
        .where(Expense.id == expense_id, Expense.status == ExpenseStatus.approved)
        .values(
            status=ExpenseStatus.paid,
            updated_by=user.id,
            updated_at=func.now(),
        ),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount == 1:
        db.refresh(obj)
        record_audit(
            db,
            table_name="expenses",
            record_id=obj.id,
            action="pay",
            actor_id=user.id,
            old_value=old,
            new_value=serialize_row(obj),
        )
        db.commit()
        db.refresh(obj)
        return obj
    db.rollback()
    current = _get_or_404(db, expense_id)
    if current.status == ExpenseStatus.paid:
        return current
    raise HTTPException(status.HTTP_409_CONFLICT, "Only approved expenses can be paid")


@router.post("/{expense_id}/reverse", response_model=ExpenseRead)
def reverse_expense(
    expense_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
):
    obj = _get_or_404(db, expense_id)
    old = serialize_row(obj)
    result = db.execute(
        update(Expense)
        .where(Expense.id == expense_id, Expense.status == ExpenseStatus.paid)
        .values(
            status=ExpenseStatus.reversed,
            updated_by=user.id,
            updated_at=func.now(),
        ),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount == 1:
        db.refresh(obj)
        record_audit(
            db,
            table_name="expenses",
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
    current = _get_or_404(db, expense_id)
    if current.status == ExpenseStatus.reversed:
        return current
    raise HTTPException(status.HTTP_409_CONFLICT, "Only paid expenses can be reversed")
