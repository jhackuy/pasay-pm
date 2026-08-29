"""Workspace (org) and membership API — thin router."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.permissions import PermissionDenied, Principal, Role
from app.v1.deps import get_current_principal, get_db_dep, require_role
from app.v1.schemas.workspace import (
    MembershipCreate,
    MembershipRead,
    WorkspaceCreate,
    WorkspaceRead,
)
from app.v1.services.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.v1.services.workspace import WorkspaceService

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


@router.post(
    "", response_model=WorkspaceRead, status_code=status.HTTP_201_CREATED,
)
def create_workspace(
    body: WorkspaceCreate,
    principal: Principal = Depends(require_role(Role.OWNER)),
    db: Session = Depends(get_db_dep),
) -> WorkspaceRead:
    svc = WorkspaceService(db)
    try:
        org = svc.create_workspace(principal, name=body.name)
    except PermissionDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return WorkspaceRead.model_validate(org)


@router.get("", response_model=list[WorkspaceRead])
def list_workspaces(
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[WorkspaceRead]:
    svc = WorkspaceService(db)
    return [
        WorkspaceRead.model_validate(o)
        for o in svc.list_workspaces(principal)
    ]


@router.post(
    "/{org_id}/members",
    response_model=MembershipRead,
    status_code=status.HTTP_201_CREATED,
)
def add_member(
    org_id: int,
    body: MembershipCreate,
    principal: Principal = Depends(require_role(Role.OWNER, Role.SECRETARY)),
    db: Session = Depends(get_db_dep),
) -> MembershipRead:
    svc = WorkspaceService(db)
    try:
        m = svc.add_member(
            principal,
            org_id=org_id,
            user_id=body.user_id,
            role=body.role,
        )
    except PermissionDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return MembershipRead.model_validate(m)


@router.get(
    "/{org_id}/members", response_model=list[MembershipRead],
)
def list_members(
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[MembershipRead]:
    svc = WorkspaceService(db)
    return [
        MembershipRead.model_validate(m)
        for m in svc.list_members(principal, org_id=org_id)
    ]
