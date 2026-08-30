"""Move-out / Settlement API — thin router over MoveOutService.

No business rule, state transition or money arithmetic lives here.
Every endpoint delegates to ``app.v1.services.move_out.MoveOutService``
so the REST API, the Telegram adapter and the Mini App share exactly
one implementation of the move-out truth.

The closure invariant — MoveOut SETTLES only via
``POST /move-outs/{id}/settlement``, which is the single path that
records the terminal DepositSettlement, transitions the move-out to
SETTLED, and resolves the linked Operation — lives in the service.
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
from app.v1.schemas.move_out import (
    DepositSettlementCreate,
    DepositSettlementRead,
    MoveOutActivityRead,
    MoveOutBalanceRead,
    MoveOutCancelRequest,
    MoveOutDamageAcceptRequest,
    MoveOutDamageCreate,
    MoveOutDamageRead,
    MoveOutFollowUpCreate,
    MoveOutInspectionCreate,
    MoveOutInspectionRead,
    MoveOutRead,
    MoveOutRequestCreate,
    OperationRead,
    TaskRead,
)
from app.v1.services.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.v1.services.move_out import MoveOutService

router = APIRouter(prefix="/move-outs", tags=["move-outs"])


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


# ---------- request ----------


@router.post(
    "", response_model=MoveOutRead, status_code=status.HTTP_201_CREATED,
)
def request_move_out(
    body: MoveOutRequestCreate,
    response: Response,
    org_id: int,
    idempotency_key: str = Depends(parse_idempotency_key_header),
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> MoveOutRead:
    service = MoveOutService(db)
    with _mapped_errors():
        result = service.request(
            principal,
            org_id=org_id,
            lease_id=body.lease_id,
            planned_move_out_date=body.planned_move_out_date,
            notes=body.notes,
            idempotency_key=idempotency_key,
        )
    if result.replayed:
        response.status_code = status.HTTP_200_OK
    return MoveOutRead.model_validate(result.move_out)


# ---------- read ----------


@router.get("", response_model=list[MoveOutRead])
def list_move_outs(
    org_id: int,
    state: str | None = None,
    lease_id: int | None = None,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[MoveOutRead]:
    service = MoveOutService(db)
    with _mapped_errors():
        items = service.list_move_outs(
            principal,
            org_id=org_id,
            state=state,
            lease_id=lease_id,
        )
    return [MoveOutRead.model_validate(m) for m in items]


@router.get("/{move_out_id}", response_model=MoveOutRead)
def get_move_out(
    move_out_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> MoveOutRead:
    service = MoveOutService(db)
    with _mapped_errors():
        m = service.get_move_out(
            principal, org_id=org_id, move_out_id=move_out_id,
        )
    return MoveOutRead.model_validate(m)


@router.get("/{move_out_id}/operation", response_model=OperationRead)
def get_operation(
    move_out_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> OperationRead:
    service = MoveOutService(db)
    with _mapped_errors():
        op = service.get_operation(
            principal, org_id=org_id, move_out_id=move_out_id,
        )
    return OperationRead.model_validate(op)


@router.get("/{move_out_id}/activity", response_model=list[MoveOutActivityRead])
def list_activity(
    move_out_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[MoveOutActivityRead]:
    service = MoveOutService(db)
    with _mapped_errors():
        items = service.list_activity(
            principal, org_id=org_id, move_out_id=move_out_id,
        )
    return [MoveOutActivityRead.model_validate(a) for a in items]


@router.get("/{move_out_id}/balance", response_model=MoveOutBalanceRead)
def get_balance(
    move_out_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> MoveOutBalanceRead:
    service = MoveOutService(db)
    with _mapped_errors():
        data = service.get_balance(
            principal, org_id=org_id, move_out_id=move_out_id,
        )
    return MoveOutBalanceRead(**data)


# ---------- inspection ----------


@router.post(
    "/{move_out_id}/inspections",
    response_model=MoveOutInspectionRead,
    status_code=status.HTTP_201_CREATED,
)
def record_inspection(
    move_out_id: int,
    body: MoveOutInspectionCreate,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> MoveOutInspectionRead:
    service = MoveOutService(db)
    with _mapped_errors():
        _, ins = service.record_inspection(
            principal,
            org_id=org_id,
            move_out_id=move_out_id,
            summary=body.summary,
        )
    return MoveOutInspectionRead.model_validate(ins)


@router.get(
    "/{move_out_id}/inspections", response_model=list[MoveOutInspectionRead],
)
def list_inspections(
    move_out_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[MoveOutInspectionRead]:
    service = MoveOutService(db)
    with _mapped_errors():
        items = service.list_inspections(
            principal, org_id=org_id, move_out_id=move_out_id,
        )
    return [MoveOutInspectionRead.model_validate(i) for i in items]


# ---------- damages ----------


@router.post(
    "/{move_out_id}/damages",
    response_model=MoveOutDamageRead,
    status_code=status.HTTP_201_CREATED,
)
def record_damage(
    move_out_id: int,
    body: MoveOutDamageCreate,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> MoveOutDamageRead:
    service = MoveOutService(db)
    with _mapped_errors():
        d = service.record_damage(
            principal,
            org_id=org_id,
            move_out_id=move_out_id,
            kind=body.kind,
            description=body.description,
            amount=body.amount,
            accepted_amount=body.accepted_amount,
        )
    return MoveOutDamageRead.model_validate(d)


@router.get(
    "/{move_out_id}/damages", response_model=list[MoveOutDamageRead],
)
def list_damages(
    move_out_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[MoveOutDamageRead]:
    service = MoveOutService(db)
    with _mapped_errors():
        items = service.list_damages(
            principal, org_id=org_id, move_out_id=move_out_id,
        )
    return [MoveOutDamageRead.model_validate(d) for d in items]


@router.post(
    "/damages/{damage_id}/accept",
    response_model=MoveOutDamageRead,
)
def accept_damage(
    damage_id: int,
    body: MoveOutDamageAcceptRequest,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> MoveOutDamageRead:
    service = MoveOutService(db)
    with _mapped_errors():
        d = service.accept_damage(
            principal,
            org_id=org_id,
            damage_id=damage_id,
            accepted_amount=body.accepted_amount,
        )
    return MoveOutDamageRead.model_validate(d)


# ---------- settlement (closure gate) ----------


@router.post(
    "/{move_out_id}/settlement",
    response_model=DepositSettlementRead,
)
def settle_move_out(
    move_out_id: int,
    body: DepositSettlementCreate,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> DepositSettlementRead:
    """Closure gate. The single path that records the terminal
    DepositSettlement, transitions the move-out to SETTLED, and
    resolves the linked Operation.
    """
    service = MoveOutService(db)
    with _mapped_errors():
        result = service.settle(
            principal,
            org_id=org_id,
            move_out_id=move_out_id,
            disposition=body.disposition,
            deposit_held=body.deposit_held,
            refund_amount=body.refund_amount,
            additional_owed=body.additional_owed,
            notes=body.notes,
        )
    return DepositSettlementRead.model_validate(result.settlement)


# ---------- cancel ----------


@router.post("/{move_out_id}/cancel", response_model=MoveOutRead)
def cancel_move_out(
    move_out_id: int,
    body: MoveOutCancelRequest,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> MoveOutRead:
    service = MoveOutService(db)
    with _mapped_errors():
        m = service.cancel(
            principal,
            org_id=org_id,
            move_out_id=move_out_id,
            reason=body.reason,
        )
    return MoveOutRead.model_validate(m)


# ---------- follow-up (Task projection) ----------


@router.post(
    "/follow-ups", response_model=TaskRead, status_code=status.HTTP_201_CREATED,
)
def create_follow_up(
    body: MoveOutFollowUpCreate,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> TaskRead:
    service = MoveOutService(db)
    with _mapped_errors():
        task = service.create_follow_up(
            principal,
            org_id=org_id,
            move_out_id=body.move_out_id,
            title=body.title,
            due_at=body.due_at,
        )
    return TaskRead.model_validate(task)


@router.get(
    "/{move_out_id}/follow-ups", response_model=list[TaskRead],
)
def list_follow_ups(
    move_out_id: int,
    org_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[TaskRead]:
    service = MoveOutService(db)
    with _mapped_errors():
        tasks = service.list_follow_ups(
            principal, org_id=org_id, move_out_id=move_out_id,
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
    service = MoveOutService(db)
    with _mapped_errors():
        task = service.complete_follow_up(
            principal, org_id=org_id, task_id=task_id,
        )
    return TaskRead.model_validate(task)
