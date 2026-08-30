"""Expense API — thin router over ExpenseClaimService.

No business rule, state transition or money arithmetic lives here. Every
endpoint delegates to ``app.v1.services.expense.ExpenseClaimService`` so
the REST API, the Telegram adapter and the Mini App share exactly one
implementation of the expense truth.
"""
from __future__ import annotations

from contextlib import contextmanager
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
from app.v1.schemas.expense import (
    ExpenseActivityRead,
    ExpenseBalanceRead,
    ExpenseClaimOpen,
    ExpenseClaimRead,
    ExpenseDecisionRequest,
    ExpenseFollowUpCreate,
    ExpenseReceiptIn,
    ExpenseReceiptRead,
    ExpenseVerificationRead,
    ExpenseVerifyRequest,
    OperationRead,
    TaskRead,
)
from app.v1.services.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.v1.services.expense import ExpenseClaimService

router = APIRouter(prefix="/expenses", tags=["expenses"])


@contextmanager
def _mapped_errors() -> Iterator[None]:
    """Translate service errors into HTTP status codes in exactly one place.

    Role and org-scope enforcement stays in the service, so the router
    never duplicates an authorization rule.
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


# ---------- claim / receipts ----------


@router.post(
    "/claims",
    response_model=ExpenseClaimRead,
    status_code=status.HTTP_201_CREATED,
)
def open_claim(
    body: ExpenseClaimOpen,
    org_id: int,
    response: Response,
    principal: Principal = Depends(get_current_principal),
    idempotency_key: Optional[str] = Depends(parse_idempotency_key_header),
    db: Session = Depends(get_db_dep),
) -> ExpenseClaimRead:
    """Open a new expense claim. ``Idempotency-Key`` is mandatory.

    An identical replay answers 200 with the original claim; a conflicting
    reuse of the same key answers 409.
    """
    if idempotency_key is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Idempotency-Key header is required for expense claims",
        )
    service = ExpenseClaimService(db)
    with _mapped_errors():
        result = service.open_claim(
            principal,
            org_id=org_id,
            title=body.title,
            category=body.category,
            claimed_amount=body.claimed_amount,
            idempotency_key=idempotency_key,
            receipts=[r.model_dump() for r in body.receipts],
        )
    if result.replayed:
        response.status_code = status.HTTP_200_OK
    return ExpenseClaimRead.model_validate(result.claim)


@router.get("/claims", response_model=list[ExpenseClaimRead])
def list_claims(
    org_id: int,
    status_filter: Optional[str] = None,
    category: Optional[str] = None,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[ExpenseClaimRead]:
    service = ExpenseClaimService(db)
    with _mapped_errors():
        claims = service.list_claims(
            principal,
            org_id=org_id,
            status=status_filter,
            category=category,
        )
    return [ExpenseClaimRead.model_validate(c) for c in claims]


@router.get("/claims/{claim_id}", response_model=ExpenseClaimRead)
def get_claim(
    claim_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> ExpenseClaimRead:
    service = ExpenseClaimService(db)
    with _mapped_errors():
        claim = service.get_claim(principal, org_id=org_id, claim_id=claim_id)
    return ExpenseClaimRead.model_validate(claim)


@router.get(
    "/claims/{claim_id}/operation", response_model=OperationRead,
)
def get_operation(
    claim_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> OperationRead:
    service = ExpenseClaimService(db)
    with _mapped_errors():
        op = service.get_operation(
            principal, org_id=org_id, claim_id=claim_id,
        )
    return OperationRead.model_validate(op)


@router.get(
    "/claims/{claim_id}/balance", response_model=ExpenseBalanceRead,
)
def get_remaining_balance(
    claim_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> ExpenseBalanceRead:
    service = ExpenseClaimService(db)
    with _mapped_errors():
        snapshot = service.remaining_balance(
            principal, org_id=org_id, claim_id=claim_id,
        )
    return ExpenseBalanceRead.model_validate(snapshot)


@router.get(
    "/claims/{claim_id}/activity", response_model=list[ExpenseActivityRead],
)
def list_activity(
    claim_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[ExpenseActivityRead]:
    service = ExpenseClaimService(db)
    with _mapped_errors():
        activity = service.list_activity(
            principal, org_id=org_id, claim_id=claim_id,
        )
    return [ExpenseActivityRead.model_validate(a) for a in activity]


# ---------- receipts ----------


@router.post(
    "/claims/{claim_id}/receipts",
    response_model=ExpenseReceiptRead,
    status_code=status.HTTP_201_CREATED,
)
def add_receipt(
    claim_id: int,
    body: ExpenseReceiptIn,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> ExpenseReceiptRead:
    service = ExpenseClaimService(db)
    with _mapped_errors():
        receipt = service.add_receipt(
            principal,
            org_id=org_id,
            claim_id=claim_id,
            kind=body.kind,
            reference=body.reference,
        )
    return ExpenseReceiptRead.model_validate(receipt)


@router.get(
    "/claims/{claim_id}/receipts", response_model=list[ExpenseReceiptRead],
)
def list_receipts(
    claim_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[ExpenseReceiptRead]:
    service = ExpenseClaimService(db)
    with _mapped_errors():
        receipts = service.list_receipts(
            principal, org_id=org_id, claim_id=claim_id,
        )
    return [ExpenseReceiptRead.model_validate(r) for r in receipts]


# ---------- follow-up (Task projection) ----------


@router.post(
    "/claims/{claim_id}/follow-ups",
    response_model=TaskRead,
    status_code=status.HTTP_201_CREATED,
)
def create_follow_up(
    claim_id: int,
    body: ExpenseFollowUpCreate,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> TaskRead:
    service = ExpenseClaimService(db)
    with _mapped_errors():
        task = service.create_follow_up(
            principal,
            org_id=org_id,
            claim_id=claim_id,
            title=body.title,
            due_at=body.due_at,
        )
    return TaskRead.model_validate(task)


@router.get(
    "/claims/{claim_id}/follow-ups", response_model=list[TaskRead],
)
def list_follow_ups(
    claim_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[TaskRead]:
    service = ExpenseClaimService(db)
    with _mapped_errors():
        tasks = service.list_follow_ups(
            principal, org_id=org_id, claim_id=claim_id,
        )
    return [TaskRead.model_validate(t) for t in tasks]


# ---------- verification ----------


@router.post(
    "/claims/{claim_id}/verify", response_model=ExpenseClaimRead,
)
def verify_claim(
    claim_id: int,
    body: ExpenseVerifyRequest,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> ExpenseClaimRead:
    service = ExpenseClaimService(db)
    with _mapped_errors():
        claim = service.verify_claim(
            principal,
            org_id=org_id,
            claim_id=claim_id,
            verified_amount=body.verified_amount,
        )
    return ExpenseClaimRead.model_validate(claim)


@router.post(
    "/claims/{claim_id}/reject", response_model=ExpenseClaimRead,
)
def reject_claim(
    claim_id: int,
    body: ExpenseDecisionRequest,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> ExpenseClaimRead:
    service = ExpenseClaimService(db)
    with _mapped_errors():
        claim = service.reject_claim(
            principal,
            org_id=org_id,
            claim_id=claim_id,
            reason=body.reason,
        )
    return ExpenseClaimRead.model_validate(claim)


@router.post(
    "/claims/{claim_id}/reverse", response_model=ExpenseClaimRead,
)
def reverse_claim(
    claim_id: int,
    body: ExpenseDecisionRequest,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> ExpenseClaimRead:
    service = ExpenseClaimService(db)
    with _mapped_errors():
        claim = service.reverse_claim(
            principal,
            org_id=org_id,
            claim_id=claim_id,
            reason=body.reason,
        )
    return ExpenseClaimRead.model_validate(claim)


@router.get(
    "/claims/{claim_id}/verifications",
    response_model=list[ExpenseVerificationRead],
)
def list_verifications(
    claim_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[ExpenseVerificationRead]:
    service = ExpenseClaimService(db)
    with _mapped_errors():
        items = service.list_verifications(
            principal, org_id=org_id, claim_id=claim_id,
        )
    return [ExpenseVerificationRead.model_validate(i) for i in items]