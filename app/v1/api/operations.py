"""Operations + Tasks + Notifications API — thin router.

Exposes the centralized ``OperationService`` / ``TaskService`` /
``NotificationService`` from ``app/v1/services/operations.py``.

Coverage Matrix rows 8.1 (Operation is truth), 8.2 (Task projection),
8.3 (next_actor / next_action consistency), 8.4 (no duplicate status
truth), 8.5 (Reminder != Completion).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.permissions import PermissionDenied, Principal, Role
from app.v1.deps import get_current_principal, get_db_dep, require_role
from app.v1.models.base import OperationState, TaskState
from app.v1.services.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.v1.services.operations import (
    NotificationService,
    OperationService,
    TaskService,
)


router = APIRouter(prefix="/operations", tags=["operations"])


# ---------- pydantic schemas ----------


class OperationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    kind: str
    subject_type: str
    subject_id: int
    state: str
    due_at: datetime | None = None
    resolved_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class TaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    operation_id: int
    kind: str
    title: str
    state: str
    due_at: datetime | None = None
    done_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class OperationAdvance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_state: str = Field(
        description="One of: open, in_progress, resolved, cancelled",
    )


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=200)
    due_at: datetime | None = None


class NotificationSend(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=1000)


class NotificationAck(BaseModel):
    operation_id: int
    delivered: bool
    message: str
    operation_state_at_send: str


# ---------- routes ----------


@router.get("", response_model=list[OperationRead])
def list_operations(
    org_id: int,
    state: str | None = None,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[OperationRead]:
    svc = OperationService(db)
    return [
        OperationRead.model_validate(o)
        for o in svc.list_for_org(
            principal, org_id=org_id, state=state,
        )
    ]


@router.get("/{operation_id}", response_model=OperationRead)
def get_operation(
    operation_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> OperationRead:
    svc = OperationService(db)
    try:
        op = svc.get(
            principal, org_id=org_id, operation_id=operation_id,
        )
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return OperationRead.model_validate(op)


@router.patch(
    "/{operation_id}/state",
    response_model=OperationRead,
)
def advance_operation(
    operation_id: int,
    body: OperationAdvance,
    org_id: int,
    principal: Principal = Depends(require_role(Role.OWNER, Role.SECRETARY)),
    db: Session = Depends(get_db_dep),
) -> OperationRead:
    """Coverage Matrix 8.3: ``next_actor`` / ``next_action`` consistency."""
    if body.to_state not in {s.value for s in OperationState}:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"to_state must be one of {[s.value for s in OperationState]}",
        )
    svc = OperationService(db)
    try:
        op = svc.advance(
            principal,
            org_id=org_id,
            operation_id=operation_id,
            to_state=body.to_state,
        )
    except PermissionDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return OperationRead.model_validate(op)


@router.post(
    "/{operation_id}/sync-status",
    response_model=OperationRead,
)
def sync_operation_status(
    operation_id: int,
    org_id: int,
    principal: Principal = Depends(require_role(Role.OWNER, Role.SECRETARY)),
    db: Session = Depends(get_db_dep),
) -> OperationRead:
    """Coverage Matrix 8.4: reconcile Operation.state with Task.state."""
    svc = OperationService(db)
    try:
        op = svc.sync_status(
            principal, org_id=org_id, operation_id=operation_id,
        )
    except PermissionDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return OperationRead.model_validate(op)


@router.post(
    "/{operation_id}/tasks",
    response_model=TaskRead,
    status_code=status.HTTP_201_CREATED,
)
def create_task_projection(
    operation_id: int,
    body: TaskCreate,
    org_id: int,
    principal: Principal = Depends(require_role(Role.OWNER, Role.SECRETARY)),
    db: Session = Depends(get_db_dep),
) -> TaskRead:
    """Coverage Matrix 8.2: Task ≤ 1 current projection per Operation."""
    svc = TaskService(db)
    try:
        task = svc.create_projection(
            principal,
            org_id=org_id,
            operation_id=operation_id,
            kind=body.kind,
            title=body.title,
            due_at=body.due_at,
        )
    except PermissionDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return TaskRead.model_validate(task)


@router.post(
    "/tasks/{task_id}/complete",
    response_model=TaskRead,
)
def complete_task_projection(
    task_id: int,
    org_id: int,
    principal: Principal = Depends(require_role(Role.OWNER, Role.SECRETARY)),
    db: Session = Depends(get_db_dep),
) -> TaskRead:
    """Coverage Matrix 8.5: complete a Task. NEVER resolves the parent
    Operation (Reminder != Completion)."""
    svc = TaskService(db)
    try:
        task = svc.complete(
            principal, org_id=org_id, task_id=task_id,
        )
    except PermissionDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return TaskRead.model_validate(task)


@router.post(
    "/{operation_id}/notify",
    response_model=NotificationAck,
)
def notify_operation(
    operation_id: int,
    body: NotificationSend,
    org_id: int,
    principal: Principal = Depends(require_role(Role.OWNER, Role.SECRETARY)),
    db: Session = Depends(get_db_dep),
) -> NotificationAck:
    """Coverage Matrix 8.5: NotificationService.send is READ-ONLY w.r.t.
    Operation.status. Sending NEVER marks an Operation completed.
    """
    svc = NotificationService(db)
    try:
        result = svc.send(
            principal,
            org_id=org_id,
            operation_id=operation_id,
            message=body.message,
        )
    except PermissionDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return NotificationAck(**result)
