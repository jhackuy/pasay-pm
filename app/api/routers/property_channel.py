from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.database import get_db
from app.models.property_channel import BindingStatus
from app.models.user import User
from app.schemas.common import Paginated
from app.schemas.property import UnitChannelBindingCreate, UnitChannelBindingRead
from app.services.organization_scope import scope_exception_to_http
from app.services.property_channel import (
    BindingConflict,
    OwnerRequired,
    ScopeBlocked,
    _validate_purpose,
    bind_unit_channel,
    get_active_binding,
    list_bindings_for_unit,
    revoke_unit_channel,
    scoped_get_unit,
    scoped_lookup_unit,
)

router = APIRouter(prefix="/property-channel", tags=["property_channel"])
DEFAULT_PAGINATION_LIMIT = 50
MAX_PAGINATION_LIMIT = 500


def _clamp_limit_offset(limit: int, offset: int) -> tuple[int, int]:
    if limit < 1:
        limit = 1
    if limit > MAX_PAGINATION_LIMIT:
        limit = MAX_PAGINATION_LIMIT
    if offset < 0:
        offset = 0
    return limit, offset


def _paginate_list(items: list, *, limit: int, offset: int) -> Paginated:
    limit2, offset2 = _clamp_limit_offset(limit, offset)
    total = len(items)
    page = items[offset2:offset2 + limit2]
    return Paginated(
        items=page,
        total=total,
        limit=limit2,
        offset=offset2,
    )


def _route_exception_to_http(exc: Exception) -> HTTPException:
    """Router-specific mapping (ValueError→400, BindingConflict→409);
    all org-scope and unknown exceptions fall through to canonical shared helper."""
    if isinstance(exc, ValueError):
        return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    if isinstance(exc, BindingConflict):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return scope_exception_to_http(exc)


@router.get("/units/lookup", response_model=UnitChannelBindingRead | None)
def lookup_unit(
    organization_id: int = Query(gt=0),
    property_id: int = Query(gt=0),
    unit_number: str = Query(min_length=1),
    purpose: str = Query(min_length=1),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        _validate_purpose(purpose)
    except Exception as exc:
        raise _route_exception_to_http(exc) from exc
    try:
        _unit, _membership = scoped_lookup_unit(
            db,
            organization_id=organization_id,
            property_id=property_id,
            unit_number=unit_number,
            for_user_id=user.id,
        )
    except Exception as exc:
        raise _route_exception_to_http(exc) from exc
    active = get_active_binding(db, _unit.id, purpose)
    return active


@router.get("/units/{unit_id}/bindings", response_model=Paginated[UnitChannelBindingRead])
def list_unit_bindings(
    unit_id: int,
    status_filter: BindingStatus | None = None,
    limit: int = Query(DEFAULT_PAGINATION_LIMIT, ge=1, le=MAX_PAGINATION_LIMIT),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        _unit, _membership = scoped_get_unit(db, unit_id, for_user_id=user.id)
    except Exception as exc:
        raise _route_exception_to_http(exc) from exc
    all_items = list_bindings_for_unit(db, unit_id, status=status_filter)
    return _paginate_list(all_items, limit=limit, offset=offset)


@router.get("/units/{unit_id}/bindings/active", response_model=UnitChannelBindingRead | None)
def get_unit_active_binding(
    unit_id: int,
    purpose: str = Query(min_length=1),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        _validate_purpose(purpose)
    except Exception as exc:
        raise _route_exception_to_http(exc) from exc
    try:
        _unit, _membership = scoped_get_unit(db, unit_id, for_user_id=user.id)
    except Exception as exc:
        raise _route_exception_to_http(exc) from exc
    return get_active_binding(db, unit_id, purpose)


@router.post("/bindings", response_model=UnitChannelBindingRead, status_code=status.HTTP_201_CREATED)
def create_binding(
    payload: UnitChannelBindingCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        binding = bind_unit_channel(
            db,
            unit_id=payload.unit_id,
            purpose=payload.purpose,
            channel_chat_id=payload.channel_chat_id,
            thread_topic_id=payload.thread_topic_id,
            actor_user_id=user.id,
            notes=payload.notes,
        )
    except Exception as exc:
        raise _route_exception_to_http(exc) from exc
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"concurrent bind conflict on unit_id={payload.unit_id} "
            f"purpose={payload.purpose!r}",
        ) from exc
    db.refresh(binding)
    return binding


@router.post("/bindings/{binding_id}/revoke", response_model=UnitChannelBindingRead)
def revoke_binding(
    binding_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        binding = revoke_unit_channel(
            db,
            binding_id=binding_id,
            actor_user_id=user.id,
        )
    except Exception as exc:
        raise _route_exception_to_http(exc) from exc
    db.commit()
    db.refresh(binding)
    return binding
