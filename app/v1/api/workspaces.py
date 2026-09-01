"""Workspace (org) and membership API — thin router."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.permissions import PermissionDenied, Principal, Role
from app.v1.deps import get_current_principal, get_db_dep, require_role
from app.v1.schemas.workspace import (
    MembershipCreate,
    MembershipRead,
    SecretaryInviteAccept,
    SecretaryInviteCreate,
    SecretaryInviteRead,
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


@router.delete(
    "/{org_id}/members/{member_id}",
    response_model=MembershipRead,
)
def remove_member(
    org_id: int,
    member_id: int,
    principal: Principal = Depends(require_role(Role.OWNER, Role.SECRETARY)),
    db: Session = Depends(get_db_dep),
) -> MembershipRead:
    """Remove a member (state=REMOVED). Last-Owner protected."""
    svc = WorkspaceService(db)
    try:
        m = svc.remove_member(
            principal, org_id=org_id, member_id=member_id,
        )
    except PermissionDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
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


@router.post(
    "/{org_id}/invites",
    response_model=SecretaryInviteRead,
    status_code=status.HTTP_201_CREATED,
)
def create_invite(
    org_id: int,
    body: SecretaryInviteCreate,
    principal: Principal = Depends(require_role(Role.OWNER)),
    db: Session = Depends(get_db_dep),
) -> SecretaryInviteRead:
    """Create a PENDING Secretary invite. Owner-only."""
    svc = WorkspaceService(db)
    try:
        invite = svc.create_invite(
            principal,
            org_id=org_id,
            invitee_username=body.invitee_username,
            invitee_telegram_id=body.invitee_telegram_id,
        )
    except PermissionDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    return SecretaryInviteRead.model_validate(invite)


@router.get(
    "/{org_id}/invites", response_model=list[SecretaryInviteRead],
)
def list_invites(
    org_id: int,
    principal: Principal = Depends(require_role(Role.OWNER, Role.SECRETARY)),
    db: Session = Depends(get_db_dep),
) -> list[SecretaryInviteRead]:
    svc = WorkspaceService(db)
    return [
        SecretaryInviteRead.model_validate(i)
        for i in svc.list_invites(principal, org_id=org_id)
    ]


@router.post(
    "/{org_id}/invites/{invite_id}/cancel",
    response_model=SecretaryInviteRead,
)
def cancel_invite(
    org_id: int,
    invite_id: int,
    principal: Principal = Depends(require_role(Role.OWNER)),
    db: Session = Depends(get_db_dep),
) -> SecretaryInviteRead:
    svc = WorkspaceService(db)
    try:
        invite = svc.cancel_invite(
            principal, org_id=org_id, invite_id=invite_id,
        )
    except PermissionDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return SecretaryInviteRead.model_validate(invite)


@router.post(
    "/invites/accept",
    response_model=SecretaryInviteRead,
)
def accept_invite(
    body: SecretaryInviteAccept,
    db: Session = Depends(get_db_dep),
) -> SecretaryInviteRead:
    """Accept a Secretary invite by token. Not org-scoped (token is the scope)."""
    svc = WorkspaceService(db)
    try:
        invite = svc.accept_invite(
            invite_token=body.invite_token,
            accepting_user_id=body.accepting_user_id,
        )
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return SecretaryInviteRead.model_validate(invite)
