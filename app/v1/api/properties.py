"""Property + Unit API — thin router."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.permissions import PermissionDenied, Principal, Role
from app.v1.deps import get_current_principal, get_db_dep, require_role
from app.v1.schemas.property import (
    PropertyCreate,
    PropertyRead,
    UnitCreate,
    UnitRead,
)
from app.v1.services.errors import ConflictError, NotFoundError
from app.v1.services.property import PropertyService

router = APIRouter(prefix="/properties", tags=["properties"])


@router.post(
    "", response_model=PropertyRead, status_code=status.HTTP_201_CREATED,
)
def create_property(
    body: PropertyCreate,
    org_id: int,
    principal: Principal = Depends(require_role(Role.OWNER, Role.SECRETARY)),
    db: Session = Depends(get_db_dep),
) -> PropertyRead:
    svc = PropertyService(db)
    try:
        p = svc.create_property(
            principal,
            org_id=org_id,
            name=body.name,
            address_line1=body.address_line1,
            address_line2=body.address_line2,
            city=body.city,
            region=body.region,
            postal_code=body.postal_code,
        )
    except PermissionDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    return PropertyRead.model_validate(p)


@router.get("", response_model=list[PropertyRead])
def list_properties(
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[PropertyRead]:
    svc = PropertyService(db)
    return [
        PropertyRead.model_validate(p)
        for p in svc.list_properties(principal, org_id=org_id)
    ]


@router.post(
    "/{property_id}/units",
    response_model=UnitRead,
    status_code=status.HTTP_201_CREATED,
)
def create_unit(
    property_id: int,
    body: UnitCreate,
    org_id: int,
    principal: Principal = Depends(require_role(Role.OWNER, Role.SECRETARY)),
    db: Session = Depends(get_db_dep),
) -> UnitRead:
    svc = PropertyService(db)
    try:
        u = svc.create_unit(
            principal,
            org_id=org_id,
            property_id=property_id,
            label=body.label,
            bedrooms=body.bedrooms,
            bathrooms=body.bathrooms,
            monthly_rent=body.monthly_rent,
        )
    except PermissionDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return UnitRead.model_validate(u)


@router.get(
    "/{property_id}/units", response_model=list[UnitRead],
)
def list_units(
    property_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[UnitRead]:
    svc = PropertyService(db)
    return [
        UnitRead.model_validate(u)
        for u in svc.list_units(
            principal, org_id=org_id, property_id=property_id,
        )
    ]
