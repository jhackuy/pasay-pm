"""Repair API — thin router over RepairService.

No business rule, state transition or money arithmetic lives here. Every
endpoint delegates to ``app.v1.services.repair.RepairService`` so the
REST API, the Telegram adapter and the Mini App share exactly one
implementation of the repair truth.

The closure invariant — Repair closes only after the OWNER verifies a
real completion claim — lives in ``RepairService._close_operation``.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.core.idempotency import IdempotencyConflictError, IdempotencyKeyError
from app.core.money import MoneyError
from app.core.permissions import PermissionDenied, Principal, Role
from app.core.time import NaiveDatetimeError
from app.v1.deps import (
    get_current_principal,
    get_db_dep,
    parse_idempotency_key_header,
    require_role,
)
from app.v1.schemas.repair import (
    OperationRead,
    RepairActivityRead,
    RepairCompletionClaimCreate,
    RepairCompletionClaimRead,
    RepairFollowUpCreate,
    RepairQuoteDecision,
    RepairQuoteRead,
    RepairQuoteSubmit,
    RepairReportCreate,
    RepairReportRead,
    RepairReversalCreate,
    RepairTechnicianAssign,
    RepairVerificationCreate,
    RepairVerificationRead,
    RepairWorkCreate,
    RepairWorkRead,
    TaskRead,
)
from app.v1.services.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.v1.services.repair import RepairService

router = APIRouter(prefix="/repairs", tags=["repairs"])


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


# ---------- report lifecycle ----------


@router.post(
    "/reports",
    response_model=RepairReportRead,
    status_code=status.HTTP_201_CREATED,
)
def open_report(
    body: RepairReportCreate,
    org_id: int,
    response: Response,
    principal: Principal = Depends(get_current_principal),
    idempotency_key: Optional[str] = Depends(parse_idempotency_key_header),
    db: Session = Depends(get_db_dep),
) -> RepairReportRead:
    """Open a new repair report. ``Idempotency-Key`` is mandatory.

    An identical replay answers 200 with the original report; a
    conflicting reuse of the same key answers 409.
    """
    if idempotency_key is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Idempotency-Key header is required for repair reports",
        )
    service = RepairService(db)
    with _mapped_errors():
        result = service.open_report(
            principal,
            org_id=org_id,
            unit_id=body.unit_id,
            title=body.title,
            description=body.description,
            category=body.category,
            severity=body.severity,
            linked_expense_payment_id=body.linked_expense_payment_id,
            idempotency_key=idempotency_key,
        )
    if result.replayed:
        response.status_code = status.HTTP_200_OK
    return RepairReportRead.model_validate(result.report)


@router.get("/reports", response_model=list[RepairReportRead])
def list_reports(
    org_id: int,
    state: Optional[str] = None,
    category: Optional[str] = None,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[RepairReportRead]:
    service = RepairService(db)
    with _mapped_errors():
        items = service.list_reports(
            principal, org_id=org_id, state=state, category=category,
        )
    return [RepairReportRead.model_validate(r) for r in items]


@router.get("/reports/{report_id}", response_model=RepairReportRead)
def get_report(
    report_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> RepairReportRead:
    service = RepairService(db)
    with _mapped_errors():
        report = service.get_report(
            principal, org_id=org_id, report_id=report_id,
        )
    return RepairReportRead.model_validate(report)


@router.get("/reports/{report_id}/operation", response_model=OperationRead)
def get_operation(
    report_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> OperationRead:
    service = RepairService(db)
    with _mapped_errors():
        op = service.get_operation(
            principal, org_id=org_id, report_id=report_id,
        )
    return OperationRead.model_validate(op)


@router.get(
    "/reports/{report_id}/activity", response_model=list[RepairActivityRead],
)
def list_activity(
    report_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[RepairActivityRead]:
    service = RepairService(db)
    with _mapped_errors():
        items = service.list_activity(
            principal, org_id=org_id, report_id=report_id,
        )
    return [RepairActivityRead.model_validate(a) for a in items]


@router.post(
    "/reports/{report_id}/confirm", response_model=RepairReportRead,
)
def confirm_report(
    report_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> RepairReportRead:
    service = RepairService(db)
    with _mapped_errors():
        report = service.confirm_report(
            principal, org_id=org_id, report_id=report_id,
        )
    return RepairReportRead.model_validate(report)


@router.post(
    "/reports/{report_id}/assign-technician",
    response_model=RepairReportRead,
)
def assign_technician(
    report_id: int,
    body: RepairTechnicianAssign,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> RepairReportRead:
    service = RepairService(db)
    with _mapped_errors():
        report = service.assign_technician(
            principal,
            org_id=org_id,
            report_id=report_id,
            technician_name=body.technician_name,
            technician_source=body.technician_source,
            technician_eta_at=body.technician_eta_at,
        )
    return RepairReportRead.model_validate(report)


@router.post(
    "/reports/{report_id}/request-quote", response_model=RepairReportRead,
)
def request_quote(
    report_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> RepairReportRead:
    service = RepairService(db)
    with _mapped_errors():
        report = service.request_quote(
            principal, org_id=org_id, report_id=report_id,
        )
    return RepairReportRead.model_validate(report)


# ---------- quotes ----------


@router.post(
    "/reports/{report_id}/quotes",
    response_model=RepairQuoteRead,
    status_code=status.HTTP_201_CREATED,
)
def submit_quote(
    report_id: int,
    body: RepairQuoteSubmit,
    org_id: int,
    response: Response,
    principal: Principal = Depends(get_current_principal),
    idempotency_key: Optional[str] = Depends(parse_idempotency_key_header),
    db: Session = Depends(get_db_dep),
) -> RepairQuoteRead:
    """Submit a quote. ``Idempotency-Key`` is mandatory."""
    if idempotency_key is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Idempotency-Key header is required for repair quotes",
        )
    service = RepairService(db)
    with _mapped_errors():
        result = service.submit_quote(
            principal,
            org_id=org_id,
            report_id=report_id,
            amount=body.amount,
            description=body.description,
            technician_name=body.technician_name,
            idempotency_key=idempotency_key,
        )
    if result.replayed:
        response.status_code = status.HTTP_200_OK
    return RepairQuoteRead.model_validate(result.quote)


@router.get(
    "/reports/{report_id}/quotes", response_model=list[RepairQuoteRead],
)
def list_quotes(
    report_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[RepairQuoteRead]:
    service = RepairService(db)
    with _mapped_errors():
        items = service.list_quotes(
            principal, org_id=org_id, report_id=report_id,
        )
    return [RepairQuoteRead.model_validate(q) for q in items]


@router.post(
    "/reports/{report_id}/quotes/{quote_id}/approve",
    response_model=RepairReportRead,
)
def approve_quote(
    report_id: int,
    quote_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> RepairReportRead:
    service = RepairService(db)
    with _mapped_errors():
        report = service.approve_quote(
            principal,
            org_id=org_id,
            report_id=report_id,
            quote_id=quote_id,
        )
    return RepairReportRead.model_validate(report)


@router.post(
    "/reports/{report_id}/quotes/{quote_id}/reject",
    response_model=RepairReportRead,
)
def reject_quote(
    report_id: int,
    quote_id: int,
    body: RepairQuoteDecision,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> RepairReportRead:
    service = RepairService(db)
    with _mapped_errors():
        report = service.reject_quote(
            principal,
            org_id=org_id,
            report_id=report_id,
            quote_id=quote_id,
            reason=body.reason,
        )
    return RepairReportRead.model_validate(report)


# ---------- work progress ----------


@router.post(
    "/reports/{report_id}/work",
    response_model=RepairWorkRead,
    status_code=status.HTTP_201_CREATED,
)
def record_work(
    report_id: int,
    body: RepairWorkCreate,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> RepairWorkRead:
    service = RepairService(db)
    with _mapped_errors():
        item = service.record_work(
            principal,
            org_id=org_id,
            report_id=report_id,
            state=body.state,
            note=body.note,
        )
    return RepairWorkRead.model_validate(item)


@router.get(
    "/reports/{report_id}/work", response_model=list[RepairWorkRead],
)
def list_work(
    report_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[RepairWorkRead]:
    service = RepairService(db)
    with _mapped_errors():
        items = service.list_work(
            principal, org_id=org_id, report_id=report_id,
        )
    return [RepairWorkRead.model_validate(w) for w in items]


# ---------- completion / verification ----------


@router.post(
    "/reports/{report_id}/completion-claim",
    response_model=RepairCompletionClaimRead,
    status_code=status.HTTP_201_CREATED,
)
def claim_completion(
    report_id: int,
    body: RepairCompletionClaimCreate,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> RepairCompletionClaimRead:
    service = RepairService(db)
    with _mapped_errors():
        item = service.claim_completion(
            principal,
            org_id=org_id,
            report_id=report_id,
            summary=body.summary,
        )
    return RepairCompletionClaimRead.model_validate(item)


@router.get(
    "/reports/{report_id}/completion-claim",
    response_model=list[RepairCompletionClaimRead],
)
def list_completion_claims(
    report_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[RepairCompletionClaimRead]:
    service = RepairService(db)
    with _mapped_errors():
        items = service.list_completion_claims(
            principal, org_id=org_id, report_id=report_id,
        )
    return [RepairCompletionClaimRead.model_validate(c) for c in items]


@router.post(
    "/reports/{report_id}/verify-completion",
    response_model=RepairReportRead,
)
def verify_completion(
    report_id: int,
    body: RepairVerificationCreate,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> RepairReportRead:
    """OWNER verifies a real completion. Closure gate.

    The report and the linked Operation close ONLY through this path.
    Linked expense/payment verifications do NOT close the repair.
    """
    service = RepairService(db)
    with _mapped_errors():
        report = service.verify_completion(
            principal,
            org_id=org_id,
            report_id=report_id,
            reason=body.reason,
        )
    return RepairReportRead.model_validate(report)


@router.post(
    "/reports/{report_id}/reject-completion",
    response_model=RepairReportRead,
)
def reject_completion(
    report_id: int,
    body: RepairVerificationCreate,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> RepairReportRead:
    service = RepairService(db)
    with _mapped_errors():
        report = service.reject_completion(
            principal,
            org_id=org_id,
            report_id=report_id,
            reason=body.reason,
        )
    return RepairReportRead.model_validate(report)


@router.post(
    "/reports/{report_id}/reverse-verification",
    response_model=RepairReportRead,
)
def reverse_verification(
    report_id: int,
    body: RepairReversalCreate,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> RepairReportRead:
    service = RepairService(db)
    with _mapped_errors():
        report = service.reverse_verification(
            principal,
            org_id=org_id,
            report_id=report_id,
            reason=body.reason,
        )
    return RepairReportRead.model_validate(report)


@router.post(
    "/reports/{report_id}/cancel", response_model=RepairReportRead,
)
def cancel_report(
    report_id: int,
    body: RepairReversalCreate,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> RepairReportRead:
    service = RepairService(db)
    with _mapped_errors():
        report = service.cancel_report(
            principal,
            org_id=org_id,
            report_id=report_id,
            reason=body.reason,
        )
    return RepairReportRead.model_validate(report)


@router.post(
    "/reports/{report_id}/close",
    response_model=RepairReportRead,
)
def close_report(
    report_id: int,
    org_id: int,
    principal: Principal = Depends(require_role(Role.OWNER)),
    db: Session = Depends(get_db_dep),
) -> RepairReportRead:
    """Coverage Matrix 5.8: explicit close after verified completion.

    Idempotent: if the report is already COMPLETED, returns the report
    unchanged. Raises 409 if the report is CANCELLED or has no active
    VERIFIED decision. The closure gate runs through
    ``assert_not_closed_by_payment`` (Coverage Matrix 5.9) before any
    state mutation.
    """
    service = RepairService(db)
    with _mapped_errors():
        report = service.close(
            principal,
            org_id=org_id,
            report_id=report_id,
        )
    return RepairReportRead.model_validate(report)


@router.get(
    "/reports/{report_id}/verifications",
    response_model=list[RepairVerificationRead],
)
def list_verifications(
    report_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[RepairVerificationRead]:
    service = RepairService(db)
    with _mapped_errors():
        items = service.list_verifications(
            principal, org_id=org_id, report_id=report_id,
        )
    return [RepairVerificationRead.model_validate(v) for v in items]


# ---------- follow-up (Task projection) ----------


@router.post(
    "/reports/{report_id}/follow-ups",
    response_model=TaskRead,
    status_code=status.HTTP_201_CREATED,
)
def create_follow_up(
    report_id: int,
    body: RepairFollowUpCreate,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> TaskRead:
    service = RepairService(db)
    with _mapped_errors():
        task = service.create_follow_up(
            principal,
            org_id=org_id,
            report_id=report_id,
            title=body.title,
            due_at=body.due_at,
        )
    return TaskRead.model_validate(task)


@router.get(
    "/reports/{report_id}/follow-ups", response_model=list[TaskRead],
)
def list_follow_ups(
    report_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[TaskRead]:
    service = RepairService(db)
    with _mapped_errors():
        tasks = service.list_follow_ups(
            principal, org_id=org_id, report_id=report_id,
        )
    return [TaskRead.model_validate(t) for t in tasks]


@router.post("/follow-ups/{task_id}/complete", response_model=TaskRead)
def complete_follow_up(
    task_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> TaskRead:
    """Mark a follow-up Task as done.

    This endpoint NEVER closes the linked Operation / repair report.
    Operation closure is gated exclusively by ``verify_completion``.
    """
    service = RepairService(db)
    with _mapped_errors():
        task = service.complete_follow_up(
            principal, org_id=org_id, task_id=task_id,
        )
    return TaskRead.model_validate(task)
