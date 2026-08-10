from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, manager_or_admin
from app.database import get_db
from app.models.task import Task, TaskStatus
from app.models.user import User
from app.schemas.common import MessageResponse
from app.schemas.task import TaskCompleteResult, TaskCreate, TaskRead, TaskUpdate
from app.services.audit import field_changes, record_audit, serialize_row
from app.services.dates import add_months

router = APIRouter(prefix="/tasks", tags=["tasks"])


def _get_or_404(db: Session, task_id: int) -> Task:
    obj = db.query(Task).filter(Task.id == task_id, Task.deleted_at.is_(None)).first()
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    return obj


def _check_assignee(db: Session, assigned_to: int | None) -> None:
    if assigned_to is None:
        return
    user = (
        db.query(User)
        .filter(User.id == assigned_to, User.is_active.is_(True))
        .first()
    )
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Assigned user not found")


def _sync_next_due_date(obj: Task) -> None:
    """Derive next_due_date for recurring tasks (or clear it otherwise)."""
    if obj.recurring and obj.interval_months and obj.due_date:
        obj.next_due_date = add_months(obj.due_date, obj.interval_months)
    else:
        obj.next_due_date = None


@router.get("", response_model=list[TaskRead])
def list_tasks(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return db.query(Task).filter(Task.deleted_at.is_(None)).order_by(Task.id).all()


@router.post("", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
def create_task(
    payload: TaskCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _check_assignee(db, payload.assigned_to)
    obj = Task(**payload.model_dump())
    _sync_next_due_date(obj)
    obj.created_by = user.id
    obj.updated_by = user.id
    db.add(obj)
    db.flush()
    record_audit(
        db,
        table_name="tasks",
        record_id=obj.id,
        action="create",
        actor_id=user.id,
        new_value=serialize_row(obj),
    )
    db.commit()
    db.refresh(obj)
    return obj


@router.get("/{task_id}", response_model=TaskRead)
def get_task(
    task_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)
):
    return _get_or_404(db, task_id)


@router.patch("/{task_id}", response_model=TaskRead)
def update_task(
    task_id: int,
    payload: TaskUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(manager_or_admin),
):
    obj = _get_or_404(db, task_id)
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return obj
    _check_assignee(db, updates.get("assigned_to"))
    old = serialize_row(obj)
    changed = field_changes(obj, updates)
    for field, value in updates.items():
        setattr(obj, field, value)
    _sync_next_due_date(obj)
    obj.updated_by = user.id
    record_audit(
        db,
        table_name="tasks",
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


@router.delete("/{task_id}", response_model=MessageResponse)
def delete_task(
    task_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(manager_or_admin),
):
    obj = _get_or_404(db, task_id)
    from datetime import datetime, timezone

    obj.deleted_at = datetime.now(timezone.utc)
    obj.updated_by = user.id
    record_audit(
        db,
        table_name="tasks",
        record_id=obj.id,
        action="soft_delete",
        actor_id=user.id,
        old_value=serialize_row(obj),
    )
    db.commit()
    return MessageResponse(detail="Task deleted")


@router.post("/{task_id}/complete", response_model=TaskCompleteResult)
def complete_task(
    task_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(manager_or_admin),
):
    obj = _get_or_404(db, task_id)
    if obj.status == TaskStatus.completed:
        raise HTTPException(status.HTTP_409_CONFLICT, "Task already completed")

    now = datetime.now(timezone.utc)
    obj.status = TaskStatus.completed
    obj.completed_at = now
    obj.last_completed_at = now
    obj.updated_by = user.id

    next_task = None
    if obj.recurring and obj.interval_months and obj.due_date:
        next_due = add_months(obj.due_date, obj.interval_months)
        next_task = Task(
            title=obj.title,
            description=obj.description,
            unit_id=obj.unit_id,
            status=TaskStatus.scheduled,
            priority=obj.priority,
            due_date=next_due,
            recurring=True,
            interval_months=obj.interval_months,
            assigned_to=obj.assigned_to,
        )
        _sync_next_due_date(next_task)
        next_task.created_by = user.id
        next_task.updated_by = user.id
        db.add(next_task)
        db.flush()
        record_audit(
            db,
            table_name="tasks",
            record_id=next_task.id,
            action="create",
            actor_id=user.id,
            new_value=serialize_row(next_task),
        )

    old = serialize_row(obj)
    record_audit(
        db,
        table_name="tasks",
        record_id=obj.id,
        action="update",
        actor_id=user.id,
        old_value=old,
        new_value=serialize_row(obj),
    )
    db.commit()
    db.refresh(obj)
    if next_task is not None:
        db.refresh(next_task)
    return TaskCompleteResult(completed=obj, next=next_task)
