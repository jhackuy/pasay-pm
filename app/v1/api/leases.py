"""Lease API — thin router."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.permissions import PermissionDenied, Principal, Role
from app.v1.deps import get_current_principal, get_db_dep, require_role
from app.v1.models.tenant_lease import LEASE_CONTACT_STATUSES
from app.v1.schemas.lease import LeaseContactUpdate, LeaseCreate, LeaseRead
from app.v1.services.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.v1.services.lease import LeaseService

router = APIRouter(prefix="/leases", tags=["leases"])


@router.post(
    "", response_model=LeaseRead, status_code=status.HTTP_201_CREATED,
)
def create_lease(
    body: LeaseCreate,
    org_id: int,
    principal: Principal = Depends(require_role(Role.OWNER, Role.SECRETARY)),
    db: Session = Depends(get_db_dep),
) -> LeaseRead:
    svc = LeaseService(db)
    try:
        lease = svc.create_lease(
            principal,
            org_id=org_id,
            unit_id=body.unit_id,
            tenant_id=body.tenant_id,
            start_date=body.start_date,
            end_date=body.end_date,
            monthly_rent=body.monthly_rent,
            deposit=body.deposit,
        )
    except PermissionDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return LeaseRead.model_validate(lease)


@router.get("", response_model=list[LeaseRead])
def list_leases(
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[LeaseRead]:
    svc = LeaseService(db)
    return [
        LeaseRead.model_validate(l)
        for l in svc.list_leases(principal, org_id=org_id)
    ]


@router.post("/{lease_id}/activate", response_model=LeaseRead)
def activate_lease(
    lease_id: int,
    org_id: int,
    principal: Principal = Depends(require_role(Role.OWNER, Role.SECRETARY)),
    db: Session = Depends(get_db_dep),
) -> LeaseRead:
    svc = LeaseService(db)
    try:
        lease = svc.activate_lease(
            principal, org_id=org_id, lease_id=lease_id,
        )
    except PermissionDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return LeaseRead.model_validate(lease)


@router.post("/{lease_id}/terminate", response_model=LeaseRead)
def terminate_lease(
    lease_id: int,
    org_id: int,
    principal: Principal = Depends(require_role(Role.OWNER, Role.SECRETARY)),
    db: Session = Depends(get_db_dep),
) -> LeaseRead:
    svc = LeaseService(db)
    try:
        lease = svc.terminate_lease(
            principal, org_id=org_id, lease_id=lease_id,
        )
    except PermissionDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return LeaseRead.model_validate(lease)


@router.patch("/{lease_id}/contact", response_model=LeaseRead)
def update_lease_contact_status(
    lease_id: int,
    body: LeaseContactUpdate,
    org_id: int,
    principal: Principal = Depends(require_role(Role.OWNER, Role.SECRETARY)),
    db: Session = Depends(get_db_dep),
) -> LeaseRead:
    """Update per-lease contact/follow-up state used by the Telegram NL
    bridge and the Owner/Secretary contact flow.

    Body: ``{"contact_status": "REPLIED" | "WRONG_NUMBER" | ...}``
    """
    if body.contact_status not in LEASE_CONTACT_STATUSES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"contact_status must be one of "
            f"{sorted(LEASE_CONTACT_STATUSES)}",
        )
    svc = LeaseService(db)
    try:
        lease = svc.set_contact_status(
            principal,
            org_id=org_id,
            lease_id=lease_id,
            contact_status=body.contact_status,
        )
    except PermissionDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return LeaseRead.model_validate(lease)
