"""Renewal API — thin router over LeaseRenewalService.

No business rule, state transition or money arithmetic lives here. Every
endpoint delegates to ``app.v1.services.renewal.LeaseRenewalService`` so
the REST API, the Telegram adapter and the Mini App share exactly one
implementation of the renewal truth.

The closure invariant — Renewal EXECUTES only via the
``POST /proposals/{id}/execute`` route, which is the single path that
terminates the source lease, creates the new lease, activates it, flips
the unit status, and resolves the linked Operation — lives in the
service.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.core.idempotency import IdempotencyConflictError, IdempotencyKeyError
from app.core.money import MoneyError
from app.core.permissions import PermissionDenied, Principal
from app.core.time import NaiveDatetimeError
from app.v1.deps import (
    get_current_principal,
    get_db_dep,
    parse_idempotency_key_header,
)
from app.v1.schemas.renewal import (
    LeaseRead,
    OperationRead,
    RenewalActivityRead,
    RenewalCancelRequest,
    RenewalDecisionRequest,
    RenewalExecuteResponse,
    RenewalFollowUpCreate,
    RenewalProposeRequest,
    RenewalRead,
    TaskRead,
)
from app.v1.services.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.v1.services.renewal import LeaseRenewalService

router = APIRouter(prefix="/renewals", tags=["renewals"])


@contextmanager
def _mapped_errors() -> Iterator[None]:
    try:
        yield
    except PermissionDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except (ConflictError, IdempotencyConflictError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except (
        ValidationError,
        IdempotencyKeyError,
        MoneyError,
        NaiveDatetimeError,
    ) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


# ---------- propose ----------


@router.post(
    "/proposals",
    response_model=RenewalRead,
    status_code=status.HTTP_201_CREATED,
)
def propose_renewal(
    body: RenewalProposeRequest,
    response: Response,
    org_id: int,
    idempotency_key: str = Depends(parse_idempotency_key_header),
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> RenewalRead:
    """Propose a renewal. Replay → 200, first → 201."""
    service = LeaseRenewalService(db)
    with _mapped_errors():
        result = service.propose(
            principal,
            org_id=org_id,
            source_lease_id=body.source_lease_id,
            proposed_start_date=body.proposed_start_date,
            proposed_end_date=body.proposed_end_date,
            proposed_monthly_rent=body.proposed_monthly_rent,
            proposed_deposit=body.proposed_deposit,
            idempotency_key=idempotency_key,
        )
    if result.replayed:
        response.status_code = status.HTTP_200_OK
    return RenewalRead.model_validate(result.renewal)


# ---------- read ----------


@router.get("", response_model=list[RenewalRead])
def list_renewals(
    org_id: int,
    state: str | None = None,
    source_lease_id: int | None = None,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[RenewalRead]:
    service = LeaseRenewalService(db)
    with _mapped_errors():
        items = service.list_renewals(
            principal,
            org_id=org_id,
            state=state,
            source_lease_id=source_lease_id,
        )
    return [RenewalRead.model_validate(r) for r in items]


@router.get("/proposals/{renewal_id}", response_model=RenewalRead)
def get_renewal(
    renewal_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> RenewalRead:
    service = LeaseRenewalService(db)
    with _mapped_errors():
        renewal = service.get_renewal(
            principal, org_id=org_id, renewal_id=renewal_id,
        )
    return RenewalRead.model_validate(renewal)


@router.get(
    "/proposals/{renewal_id}/operation", response_model=OperationRead,
)
def get_operation(
    renewal_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> OperationRead:
    service = LeaseRenewalService(db)
    with _mapped_errors():
        op = service.get_operation(
            principal, org_id=org_id, renewal_id=renewal_id,
        )
    return OperationRead.model_validate(op)


@router.get(
    "/proposals/{renewal_id}/activity", response_model=list[RenewalActivityRead],
)
def list_activity(
    renewal_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[RenewalActivityRead]:
    service = LeaseRenewalService(db)
    with _mapped_errors():
        items = service.list_activity(
            principal, org_id=org_id, renewal_id=renewal_id,
        )
    return [RenewalActivityRead.model_validate(a) for a in items]


# ---------- approve / reject ----------


@router.post(
    "/proposals/{renewal_id}/approve", response_model=RenewalRead,
)
def approve_renewal(
    renewal_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> RenewalRead:
    service = LeaseRenewalService(db)
    with _mapped_errors():
        renewal = service.approve(
            principal, org_id=org_id, renewal_id=renewal_id,
        )
    return RenewalRead.model_validate(renewal)


@router.post(
    "/proposals/{renewal_id}/reject", response_model=RenewalRead,
)
def reject_renewal(
    renewal_id: int,
    body: RenewalDecisionRequest,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> RenewalRead:
    service = LeaseRenewalService(db)
    with _mapped_errors():
        renewal = service.reject(
            principal,
            org_id=org_id,
            renewal_id=renewal_id,
            reason=body.reason,
        )
    return RenewalRead.model_validate(renewal)


# ---------- execute (closure gate) ----------


@router.post(
    "/proposals/{renewal_id}/execute",
    response_model=RenewalExecuteResponse,
)
def execute_renewal(
    renewal_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> RenewalExecuteResponse:
    """Closure gate. APPROVED → EXECUTED.

    The single path that terminates the source lease, creates the new
    lease, activates it, flips the unit status to OCCUPIED, and
    resolves the linked Operation.
    """
    service = LeaseRenewalService(db)
    with _mapped_errors():
        result = service.execute(
            principal, org_id=org_id, renewal_id=renewal_id,
        )
    return RenewalExecuteResponse(
        renewal=RenewalRead.model_validate(result.renewal),
        new_lease=LeaseRead.model_validate(result.new_lease),
    )


# ---------- cancel ----------


@router.post(
    "/proposals/{renewal_id}/cancel", response_model=RenewalRead,
)
def cancel_renewal(
    renewal_id: int,
    body: RenewalCancelRequest,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> RenewalRead:
    service = LeaseRenewalService(db)
    with _mapped_errors():
        renewal = service.cancel(
            principal,
            org_id=org_id,
            renewal_id=renewal_id,
            reason=body.reason,
        )
    return RenewalRead.model_validate(renewal)


# ---------- follow-up (Task projection) ----------


@router.post(
    "/follow-ups", response_model=TaskRead, status_code=status.HTTP_201_CREATED,
)
def create_follow_up(
    body: RenewalFollowUpCreate,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> TaskRead:
    service = LeaseRenewalService(db)
    with _mapped_errors():
        task = service.create_follow_up(
            principal,
            org_id=org_id,
            renewal_id=body.renewal_id,
            title=body.title,
            due_at=body.due_at,
        )
    return TaskRead.model_validate(task)


@router.get(
    "/proposals/{renewal_id}/follow-ups", response_model=list[TaskRead],
)
def list_follow_ups(
    renewal_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[TaskRead]:
    service = LeaseRenewalService(db)
    with _mapped_errors():
        tasks = service.list_follow_ups(
            principal, org_id=org_id, renewal_id=renewal_id,
        )
    return [TaskRead.model_validate(t) for t in tasks]


@router.post("/follow-ups/{task_id}/complete", response_model=TaskRead)
def complete_follow_up(
    task_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> TaskRead:
    """Mark a Task as DONE. NEVER resolves the linked Operation."""
    service = LeaseRenewalService(db)
    with _mapped_errors():
        task = service.complete_follow_up(
            principal, org_id=org_id, task_id=task_id,
        )
    return TaskRead.model_validate(task)
