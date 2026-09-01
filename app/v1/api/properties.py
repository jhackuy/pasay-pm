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
    UnitDetailRead,
    UnitEventCreate,
    UnitLifecycleEventRead,
    UnitRead,
    UnitStatusUpdate,
)
from app.v1.services.errors import ConflictError, NotFoundError, ValidationError
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


@router.get(
    "/{property_id}",
    response_model=PropertyRead,
)
def get_property(
    property_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> PropertyRead:
    svc = PropertyService(db)
    try:
        prop, _ = svc.get_property_detail(
            principal, org_id=org_id, property_id=property_id,
        )
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return PropertyRead.model_validate(prop)


@router.post(
    "/{property_id}/archive",
    response_model=PropertyRead,
)
def archive_property(
    property_id: int,
    org_id: int,
    principal: Principal = Depends(require_role(Role.OWNER)),
    db: Session = Depends(get_db_dep),
) -> PropertyRead:
    """Archive (sets archived_at). Never destroys history."""
    svc = PropertyService(db)
    try:
        prop = svc.archive_property(
            principal, org_id=org_id, property_id=property_id,
        )
    except PermissionDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return PropertyRead.model_validate(prop)


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


@router.get(
    "/units/{unit_id}",
    response_model=UnitDetailRead,
)
def get_unit_detail(
    unit_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> UnitDetailRead:
    """Unit + its lifecycle event history (newest first)."""
    svc = PropertyService(db)
    try:
        unit, events = svc.get_unit_detail(
            principal, org_id=org_id, unit_id=unit_id,
        )
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return UnitDetailRead(
        unit=UnitRead.model_validate(unit),
        lifecycle_events=[
            UnitLifecycleEventRead.model_validate(e) for e in events
        ],
    )


@router.patch(
    "/units/{unit_id}/status",
    response_model=UnitRead,
)
def set_unit_status(
    unit_id: int,
    body: UnitStatusUpdate,
    org_id: int,
    principal: Principal = Depends(require_role(Role.OWNER, Role.SECRETARY)),
    db: Session = Depends(get_db_dep),
) -> UnitRead:
    """Flip the Unit's status. Records a UnitLifecycleEvent."""
    svc = PropertyService(db)
    try:
        unit = svc.set_unit_status_v1(
            principal,
            org_id=org_id,
            unit_id=unit_id,
            status=body.status,
            note=body.note,
        )
    except PermissionDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return UnitRead.model_validate(unit)


@router.post(
    "/units/{unit_id}/events",
    response_model=UnitLifecycleEventRead,
    status_code=status.HTTP_201_CREATED,
)
def record_unit_event(
    unit_id: int,
    body: UnitEventCreate,
    org_id: int,
    principal: Principal = Depends(require_role(Role.OWNER, Role.SECRETARY)),
    db: Session = Depends(get_db_dep),
) -> UnitLifecycleEventRead:
    """Append-only UnitLifecycleEvent."""
    svc = PropertyService(db)
    try:
        event = svc.record_unit_event(
            principal,
            org_id=org_id,
            unit_id=unit_id,
            kind=body.kind,
            note=body.note,
        )
    except PermissionDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return UnitLifecycleEventRead.model_validate(event)
