from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.api.deps import admin_only, get_current_user, manager_or_admin
from app.database import get_db
from app.models.financial import Expense, ExpenseStatus
from app.models.operations import (
    OperationalTask,
    OperationalTaskStatus,
    OperationalTaskType,
)
from app.models.property import Unit
from app.models.user import User
from app.schemas.financial import ExpenseCreate, ExpenseRead, ExpenseUpdate
from app.services.audit import field_changes, record_audit, serialize_row
from app.services.operations.redelivery import suppress_pending_redeliveries

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


def _complete_linked_approval_task(
    db: Session,
    expense: Expense,
    *,
    actor_id: int,
    reason: str,
) -> None:
    """PASAY-V2-FOUNDATION-001: closing an expense approval also closes the
    linked APPROVAL_PENDING operational task atomically in the same
    transaction (single source of truth; the bot never does this itself)."""
    now = datetime.now(timezone.utc)
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
    old = serialize_row(task)
    task.status = OperationalTaskStatus.COMPLETED
    task.completed_at = now
    task.completed_by = actor_id
    task.reminder_generation = task.reminder_generation + 1
    task.updated_by = actor_id
    db.flush()
    record_audit(
        db,
        table_name="operational_tasks",
        record_id=task.id,
        action=f"task_completed_via_{reason}",
        actor_id=actor_id,
        changed_fields={"status": [old.get("status"), "COMPLETED"]},
        old_value=old,
        new_value=serialize_row(task),
    )
    suppress_pending_redeliveries(
        db, task.id, actor_id=actor_id, reason=f"expense_{reason}", now=now
    )


def _complete_linked_payment_tasks(
    db: Session,
    expense: Expense,
    *,
    actor_id: int,
) -> None:
    """P0-EXPENSE-PAID-CLOSEOUT-001: marking an approved expense PAID also
    closes every still-active expense-linked operational task (APPROVAL_PENDING
    / PAYMENT_PENDING) atomically in the same transaction, so the to-do list
    never keeps showing "waiting for payment" for a paid expense."""
    now = datetime.now(timezone.utc)
    tasks = (
        db.query(OperationalTask)
        .filter(
            OperationalTask.source_type == "expense",
            OperationalTask.source_id == expense.id,
            OperationalTask.task_type.in_(
                [OperationalTaskType.APPROVAL_PENDING, OperationalTaskType.PAYMENT_PENDING]
            ),
            OperationalTask.status.in_(
                [OperationalTaskStatus.PENDING, OperationalTaskStatus.IN_PROGRESS]
            ),
        )
        .all()
    )
    for task in tasks:
        old = serialize_row(task)
        task.status = OperationalTaskStatus.COMPLETED
        task.completed_at = now
        task.completed_by = actor_id
        task.reminder_generation = task.reminder_generation + 1
        task.updated_by = actor_id
        db.flush()
        record_audit(
            db,
            table_name="operational_tasks",
            record_id=task.id,
            action="task_completed_via_payment",
            actor_id=actor_id,
            changed_fields={"status": [old.get("status"), "COMPLETED"]},
            old_value=old,
            new_value=serialize_row(task),
        )
        suppress_pending_redeliveries(
            db, task.id, actor_id=actor_id, reason="expense_payment", now=now
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
        _complete_linked_approval_task(
            db, obj, actor_id=user.id, reason="approval"
        )
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
        _complete_linked_approval_task(
            db, obj, actor_id=user.id, reason="rejection"
        )
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
        _complete_linked_payment_tasks(db, obj, actor_id=user.id)
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
