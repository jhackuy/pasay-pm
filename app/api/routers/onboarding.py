"""PASAY-TASK-006 Onboarding P0 — FastAPI router.

Endpoints (all require authenticated HUMAN user via ``get_current_user``):

  GET  /onboarding/state
       → Authoritative routing outcome. Accepts optional ``?invite_code=``
         for Secretary deep-link / start-parameter flows.

  POST /onboarding/owner/bootstrap
       → Owner creates Organization. GUARDED (user has 0 active memberships).
         Body: { "org_name": str }.  Owner Chinese-priority hints in errors.

  POST /onboarding/secretary/accept-invite
       → Secretary joins ONLY via invite code. Body: { "invite_code": str }.
         NO "Create company" semantics on this endpoint. English-priority hints.

Strictly within Issue #24 scope; never touches Property/Channel/Rent/Menu.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.onboarding import (
    OnboardingStateResponse,
    OwnerBootstrapRequest,
    OwnerBootstrapResponse,
    SecretaryAcceptInviteRequest,
    SecretaryAcceptInviteResponse,
)
from app.services.onboarding import (
    BootstrapForbidden,
    InviteNotAccepted,
    get_onboarding_state,
    owner_create_organization,
    secretary_join_via_invite,
)


router = APIRouter(prefix="/onboarding", tags=["onboarding"])


MSG_OWNER_403_ZH = "你已经是某个组织的成员，不能再创建新的公司/组织。请进入现有组织工作区。"
MSG_SEC_403_NO_BOOTSTRAP_EN = (
    "Secretaries cannot create organizations. Ask your Owner for an invite code."
)


@router.get("/state", response_model=OnboardingStateResponse, summary="Get onboarding state")
def get_state(
    invite_code: str | None = Query(default=None, max_length=128),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> OnboardingStateResponse:
    return get_onboarding_state(db, user, pending_invite_code=invite_code)


@router.post(
    "/owner/bootstrap",
    response_model=OwnerBootstrapResponse,
    status_code=status.HTTP_201_CREATED,
    summary="[Owner only] Create organization and become first OWNER",
)
def owner_bootstrap(
    payload: OwnerBootstrapRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> OwnerBootstrapResponse:
    try:
        return owner_create_organization(db, user, payload.org_name)
    except BootstrapForbidden as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=MSG_OWNER_403_ZH,
        ) from exc
    except ValueError as exc:  # empty org_name etc.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except LookupError as exc:  # user not found / inactive (should not reach after auth)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Identity inactive; cannot bootstrap.",
        ) from exc


@router.post(
    "/secretary/accept-invite",
    response_model=SecretaryAcceptInviteResponse,
    status_code=status.HTTP_201_CREATED,
    summary="[Secretary only] Join organization via invite code",
)
def secretary_accept_invite(
    payload: SecretaryAcceptInviteRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SecretaryAcceptInviteResponse:
    try:
        return secretary_join_via_invite(db, user, payload.invite_code)
    except InviteNotAccepted as exc:
        detail_map = {
            "invalid": "Invalid invite code.",
            "expired": "Invite has expired. Ask your Owner for a new invite.",
            "consumed": "Invite has already been used or revoked.",
            "cancelled": "Invite was cancelled by the Owner.",
            "already_member": "You are already a member of this organization.",
        }
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=exc.detail or detail_map.get(exc.reason, "Invite not accepted."),
        ) from exc
    except LookupError as exc:  # acceptor user inactive / not found
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Identity inactive; cannot accept invite.",
        ) from exc


@router.post(
    "/secretary/bootstrap",
    status_code=status.HTTP_403_FORBIDDEN,
    summary="[GUARD] Explicit forbidden: Secretaries cannot bootstrap orgs.",
)
def secretary_bootstrap_forbidden(
    _payload: dict | None = None,
    user: User | None = Depends(get_current_user),
) -> dict:
    """Explicit endpoint that ALWAYS refuses bootstrap for Secretaries.

    This endpoint exists so the UI/bot can issue a visible call and receive
    a stable, auditable 403 instead of silently routing through a generic
    permission error. Issue #24 Scope §4: "Secretary 均不得绕过前端调用 Owner bootstrap".
    We therefore provide a stable forbidden endpoint rather than only relying
    on the owner endpoint's guard (which also forbids them).
    """
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=MSG_SEC_403_NO_BOOTSTRAP_EN,
    )
