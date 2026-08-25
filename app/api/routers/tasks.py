from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.database import get_db
from app.models.membership import OrganizationRole
from app.models.task import Task, TaskStatus
from app.models.user import User
from app.schemas.common import MessageResponse
from app.schemas.task import TaskCompleteResult, TaskCreate, TaskRead, TaskUpdate
from app.services.organization_scope import (
    resolve_org_membership,
    scope_exception_to_http,
    list_active_org_ids_for_user,
)

router = APIRouter(prefix="/tasks", tags=["tasks"])

_DEPRECATED_HEADER = "legacy-tasks-router-v1; use /operations/tasks"
_DEPRECATION_DETAIL = {
    "error": "METHOD_NOT_ALLOWED",
    "message": "Legacy /tasks write endpoints are deprecated in Pasay V1. Use /operations/tasks instead.",
    "deprecation": "See PASAY-M003 Scope Unification",
}


def _deprecation_headers(response: Response) -> None:
    response.headers["X-Deprecated-Endpoint"] = _DEPRECATED_HEADER


def _write_405(response: Response):
    _deprecation_headers(response)
    raise HTTPException(
        status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
        detail=_DEPRECATION_DETAIL,
        headers={"X-Deprecated-Endpoint": _DEPRECATED_HEADER},
    )


@router.get("", response_model=list[TaskRead])
def list_tasks(
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _deprecation_headers(response)
    try:
        org_ids = list_active_org_ids_for_user(db, user.id)
        if not org_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No active organization membership",
            )
        resolve_org_membership(
            db, user.id, org_ids[0],
            role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY],
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc
    return db.query(Task).filter(False).all()


@router.post("", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
def create_task(
    response: Response,
    payload: TaskCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _write_405(response)


@router.get("/{task_id}", response_model=TaskRead)
def get_task(
    task_id: int,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _deprecation_headers(response)
    try:
        org_ids = list_active_org_ids_for_user(db, user.id)
        if not org_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No active organization membership",
            )
        resolve_org_membership(
            db, user.id, org_ids[0],
            role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY],
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")


@router.patch("/{task_id}", response_model=TaskRead)
def update_task(
    task_id: int,
    response: Response,
    payload: TaskUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _write_405(response)


@router.delete("/{task_id}", response_model=MessageResponse)
def delete_task(
    task_id: int,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _write_405(response)


@router.post("/{task_id}/complete", response_model=TaskCompleteResult)
def complete_task(
    task_id: int,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _write_405(response)
