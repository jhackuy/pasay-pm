"""Rent / Payment API — thin router over RentPaymentService.

No business rule, state transition or money arithmetic lives here. Every
endpoint delegates to ``app.v1.services.rent_payment.RentPaymentService``
so the REST API, the Telegram adapter and the Mini App share exactly one
implementation of the rent truth.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from typing import Iterator, Optional

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
from app.v1.schemas.rent_payment import (
    OperationRead,
    RentActivityRead,
    RentBalanceRead,
    RentDecisionRequest,
    RentDueScheduleCreate,
    RentDueScheduleRead,
    RentEvidenceIn,
    RentEvidenceRead,
    RentFollowUpCreate,
    RentPaymentClaimCreate,
    RentPaymentRead,
    RentVerificationRead,
    RentVerifyRequest,
    TaskRead,
)
from app.v1.services.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.v1.services.rent_payment import RentPaymentService

router = APIRouter(prefix="/rent", tags=["rent"])


@contextmanager
def _mapped_errors() -> Iterator[None]:
    """Translate service errors into HTTP status codes in exactly one place.

    Role and org-scope enforcement stays in the service, so the router never
    duplicates an authorization rule.
    """
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


# ---------- due / overdue ----------


@router.post(
    "/due-schedules",
    response_model=RentDueScheduleRead,
    status_code=status.HTTP_201_CREATED,
)
def create_due_schedule(
    body: RentDueScheduleCreate,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> RentDueScheduleRead:
    service = RentPaymentService(db)
    with _mapped_errors():
        schedule = service.create_due_schedule(
            principal,
            org_id=org_id,
            lease_id=body.lease_id,
            period_start=body.period_start,
            due_date=body.due_date,
            amount_due=body.amount_due,
        )
    return RentDueScheduleRead.model_validate(schedule)


@router.get("/due-schedules", response_model=list[RentDueScheduleRead])
def list_due_schedules(
    org_id: int,
    lease_id: Optional[int] = None,
    state: Optional[str] = None,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[RentDueScheduleRead]:
    service = RentPaymentService(db)
    with _mapped_errors():
        schedules = service.list_due_schedules(
            principal, org_id=org_id, lease_id=lease_id, state=state,
        )
    return [RentDueScheduleRead.model_validate(s) for s in schedules]


@router.get("/overdue", response_model=list[RentDueScheduleRead])
def list_overdue(
    org_id: int,
    as_of: Optional[date] = None,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[RentDueScheduleRead]:
    service = RentPaymentService(db)
    with _mapped_errors():
        schedules = service.list_overdue(
            principal, org_id=org_id, as_of=as_of,
        )
    return [RentDueScheduleRead.model_validate(s) for s in schedules]


@router.get("/claims", response_model=list[RentPaymentRead])
def list_all_claims(
    org_id: int,
    status: Optional[str] = None,
    due_schedule_id: Optional[int] = None,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[RentPaymentRead]:
    """Org-scoped list of every RentPayment claim.

    Used by the Mini App dashboard / Finance view, which renders all claims
    at once rather than per-due-schedule.
    """
    service = RentPaymentService(db)
    with _mapped_errors():
        payments = service.list_all_payments(
            principal,
            org_id=org_id,
            status=status,
            due_schedule_id=due_schedule_id,
        )
    return [RentPaymentRead.model_validate(p) for p in payments]


@router.post("/mark-overdue", response_model=list[RentDueScheduleRead])
def mark_overdue(
    org_id: int,
    as_of: Optional[date] = None,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[RentDueScheduleRead]:
    service = RentPaymentService(db)
    with _mapped_errors():
        schedules = service.mark_overdue(
            principal, org_id=org_id, as_of=as_of,
        )
    return [RentDueScheduleRead.model_validate(s) for s in schedules]


@router.get(
    "/due-schedules/{due_schedule_id}", response_model=RentDueScheduleRead,
)
def get_due_schedule(
    due_schedule_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> RentDueScheduleRead:
    service = RentPaymentService(db)
    with _mapped_errors():
        schedule = service.get_due_schedule(
            principal, org_id=org_id, due_schedule_id=due_schedule_id,
        )
    return RentDueScheduleRead.model_validate(schedule)


@router.get(
    "/due-schedules/{due_schedule_id}/balance",
    response_model=RentBalanceRead,
)
def get_remaining_balance(
    due_schedule_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> RentBalanceRead:
    service = RentPaymentService(db)
    with _mapped_errors():
        snapshot = service.remaining_balance(
            principal, org_id=org_id, due_schedule_id=due_schedule_id,
        )
    return RentBalanceRead.model_validate(snapshot)


@router.get(
    "/due-schedules/{due_schedule_id}/operation",
    response_model=OperationRead,
)
def get_operation(
    due_schedule_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> OperationRead:
    service = RentPaymentService(db)
    with _mapped_errors():
        operation = service.get_operation(
            principal, org_id=org_id, due_schedule_id=due_schedule_id,
        )
    return OperationRead.model_validate(operation)


@router.get(
    "/due-schedules/{due_schedule_id}/activity",
    response_model=list[RentActivityRead],
)
def list_activity(
    due_schedule_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[RentActivityRead]:
    service = RentPaymentService(db)
    with _mapped_errors():
        activity = service.list_activity(
            principal, org_id=org_id, due_schedule_id=due_schedule_id,
        )
    return [RentActivityRead.model_validate(a) for a in activity]


# ---------- follow-up (Task projection) ----------


@router.post(
    "/due-schedules/{due_schedule_id}/follow-ups",
    response_model=TaskRead,
    status_code=status.HTTP_201_CREATED,
)
def create_follow_up(
    due_schedule_id: int,
    body: RentFollowUpCreate,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> TaskRead:
    service = RentPaymentService(db)
    with _mapped_errors():
        task = service.create_follow_up(
            principal,
            org_id=org_id,
            due_schedule_id=due_schedule_id,
            title=body.title,
            due_at=body.due_at,
        )
    return TaskRead.model_validate(task)


@router.get(
    "/due-schedules/{due_schedule_id}/follow-ups",
    response_model=list[TaskRead],
)
def list_follow_ups(
    due_schedule_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[TaskRead]:
    service = RentPaymentService(db)
    with _mapped_errors():
        tasks = service.list_follow_ups(
            principal, org_id=org_id, due_schedule_id=due_schedule_id,
        )
    return [TaskRead.model_validate(t) for t in tasks]


@router.post("/follow-ups/{task_id}/complete", response_model=TaskRead)
def complete_follow_up(
    task_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> TaskRead:
    service = RentPaymentService(db)
    with _mapped_errors():
        task = service.complete_follow_up(
            principal, org_id=org_id, task_id=task_id,
        )
    return TaskRead.model_validate(task)


# ---------- claim / evidence ----------


@router.post(
    "/due-schedules/{due_schedule_id}/claims",
    response_model=RentPaymentRead,
    status_code=status.HTTP_201_CREATED,
)
def claim_payment(
    due_schedule_id: int,
    body: RentPaymentClaimCreate,
    org_id: int,
    response: Response,
    principal: Principal = Depends(get_current_principal),
    idempotency_key: Optional[str] = Depends(parse_idempotency_key_header),
    db: Session = Depends(get_db_dep),
) -> RentPaymentRead:
    """Record a payment claim. ``Idempotency-Key`` is mandatory.

    An identical replay answers 200 with the original claim; a conflicting
    reuse of the same key answers 409.
    """
    if idempotency_key is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Idempotency-Key header is required for rent payment claims",
        )
    service = RentPaymentService(db)
    with _mapped_errors():
        result = service.claim_payment(
            principal,
            org_id=org_id,
            due_schedule_id=due_schedule_id,
            claimed_amount=body.claimed_amount,
            idempotency_key=idempotency_key,
            evidence=[item.model_dump() for item in body.evidence],
        )
    if result.replayed:
        response.status_code = status.HTTP_200_OK
    return RentPaymentRead.model_validate(result.payment)


@router.get(
    "/due-schedules/{due_schedule_id}/claims",
    response_model=list[RentPaymentRead],
)
def list_claims(
    due_schedule_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[RentPaymentRead]:
    service = RentPaymentService(db)
    with _mapped_errors():
        payments = service.list_payments(
            principal, org_id=org_id, due_schedule_id=due_schedule_id,
        )
    return [RentPaymentRead.model_validate(p) for p in payments]


@router.post(
    "/claims/{rent_payment_id}/evidence",
    response_model=RentEvidenceRead,
    status_code=status.HTTP_201_CREATED,
)
def add_evidence(
    rent_payment_id: int,
    body: RentEvidenceIn,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> RentEvidenceRead:
    service = RentPaymentService(db)
    with _mapped_errors():
        item = service.add_evidence(
            principal,
            org_id=org_id,
            rent_payment_id=rent_payment_id,
            kind=body.kind,
            reference=body.reference,
        )
    return RentEvidenceRead.model_validate(item)


@router.get(
    "/claims/{rent_payment_id}/evidence",
    response_model=list[RentEvidenceRead],
)
def list_evidence(
    rent_payment_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[RentEvidenceRead]:
    service = RentPaymentService(db)
    with _mapped_errors():
        items = service.list_evidence(
            principal, org_id=org_id, rent_payment_id=rent_payment_id,
        )
    return [RentEvidenceRead.model_validate(i) for i in items]


# ---------- verification ----------


@router.post("/claims/{rent_payment_id}/verify", response_model=RentPaymentRead)
def verify_payment(
    rent_payment_id: int,
    body: RentVerifyRequest,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> RentPaymentRead:
    service = RentPaymentService(db)
    with _mapped_errors():
        payment = service.verify_payment(
            principal,
            org_id=org_id,
            rent_payment_id=rent_payment_id,
            verified_amount=body.verified_amount,
        )
    return RentPaymentRead.model_validate(payment)


@router.post("/claims/{rent_payment_id}/reject", response_model=RentPaymentRead)
def reject_payment(
    rent_payment_id: int,
    body: RentDecisionRequest,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> RentPaymentRead:
    service = RentPaymentService(db)
    with _mapped_errors():
        payment = service.reject_payment(
            principal,
            org_id=org_id,
            rent_payment_id=rent_payment_id,
            reason=body.reason,
        )
    return RentPaymentRead.model_validate(payment)


@router.post(
    "/claims/{rent_payment_id}/reverse", response_model=RentPaymentRead,
)
def reverse_payment(
    rent_payment_id: int,
    body: RentDecisionRequest,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> RentPaymentRead:
    service = RentPaymentService(db)
    with _mapped_errors():
        payment = service.reverse_payment(
            principal,
            org_id=org_id,
            rent_payment_id=rent_payment_id,
            reason=body.reason,
        )
    return RentPaymentRead.model_validate(payment)


@router.get(
    "/claims/{rent_payment_id}/verifications",
    response_model=list[RentVerificationRead],
)
def list_verifications(
    rent_payment_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[RentVerificationRead]:
    service = RentPaymentService(db)
    with _mapped_errors():
        items = service.list_verifications(
            principal, org_id=org_id, rent_payment_id=rent_payment_id,
        )
    return [RentVerificationRead.model_validate(i) for i in items]
