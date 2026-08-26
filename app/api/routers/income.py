from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, manager_or_admin, owner_subject_only
from app.database import get_db
from app.models.financial import Income, IncomeStatus
from app.models.lease import Lease
from app.models.membership import OrganizationRole
from app.models.user import User
from app.schemas.financial import (
    IncomeCreate,
    IncomeRead,
    IncomeUpdate,
    RentClaimCreate,
    RentClaimFail,
    RentClaimOut,
    RentClaimReverse,
    RentClaimVerify,
    RentDetailOut,
    RentPeriodPaymentInfo,
)
from app.services.audit import field_changes, record_audit, serialize_row
from app.services.organization_scope import (
    CrossOrgReference,
    ScopeBlocked,
    income_org_id,
    lease_org_id,
    list_active_org_ids_for_user,
    resolve_org_membership,
    scope_exception_to_http,
    scoped_get_income,
    scoped_get_lease,
    scoped_get_rent_payment_claim,
    scoped_list_incomes,
    scoped_list_rent_payment_claims,
)
from app.services.rent_claims import (
    RentClaimError,
    create_claim,
    fail_claim,
    list_claims,
    reverse_claim,
    verify_claim,
)
from app.services.rent_payment_truth import snapshot
from app.schemas.common import Paginated

router = APIRouter(prefix="/incomes", tags=["incomes"])


def _check_lease(db: Session, lease_id: int | None) -> None:
    if lease_id is None:
        raise CrossOrgReference("Income requires canonical lease ownership (lease_id is required)")
    lease = db.query(Lease).filter(Lease.id == lease_id, Lease.deleted_at.is_(None)).first()
    if lease is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lease not found")


def _assert_lease_co_org(db: Session, user: User, lease_id: int | None) -> None:
    if lease_id is None:
        raise CrossOrgReference("Income requires canonical lease ownership (lease_id is required)")
    object_org_id = lease_org_id(db, lease_id)
    if object_org_id is None:
        raise CrossOrgReference(
            f"Lease id={lease_id} not found or has no organization"
        )
    try:
        resolve_org_membership(db, user.id, object_org_id)
    except ScopeBlocked:
        raise CrossOrgReference(
            f"Lease id={lease_id} does not belong to the caller's organization"
        ) from None


def _assert_income_co_org(db: Session, user: User, income_id: int) -> None:
    object_org_id = income_org_id(db, income_id)
    if object_org_id is None:
        raise LookupError(f"income {income_id} not found or has no organization")
    if object_org_id not in list_active_org_ids_for_user(db, user.id):
        raise LookupError(f"income {income_id} not found")


@router.get("", response_model=Paginated[IncomeRead])
def list_incomes(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    try:
        rows = scoped_list_incomes(db, for_user_id=user.id)
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    total = len(rows)
    paged = rows[offset:offset + limit]
    return Paginated(items=paged, total=total, limit=limit, offset=offset)


@router.post("", response_model=IncomeRead, status_code=status.HTTP_201_CREATED)
def create_income(
    payload: IncomeCreate,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(manager_or_admin),
):
    if payload.status not in (IncomeStatus.pending, IncomeStatus.confirmed):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Income can only be created as pending or confirmed",
        )
    try:
        _check_lease(db, payload.lease_id)
        _assert_lease_co_org(db, user, payload.lease_id)
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc
    if payload.idempotency_key:
        existing = (
            db.query(Income)
            .filter(Income.idempotency_key == payload.idempotency_key)
            .first()
        )
        if existing is not None:
            try:
                _assert_income_co_org(db, user, existing.id)
            except Exception as exc:
                raise scope_exception_to_http(exc) from exc
            response.status_code = status.HTTP_200_OK
            return existing
    obj = Income(**payload.model_dump())
    obj.created_by = user.id
    obj.updated_by = user.id
    if obj.status == IncomeStatus.confirmed:
        obj.confirmed_by = user.id
        obj.confirmed_at = datetime.now(timezone.utc)
    db.add(obj)
    try:
        db.flush()
        record_audit(
            db,
            table_name="incomes",
            record_id=obj.id,
            action="create",
            actor_id=user.id,
            new_value=serialize_row(obj),
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        if payload.idempotency_key:
            existing = (
                db.query(Income)
                .filter(Income.idempotency_key == payload.idempotency_key)
                .first()
            )
            if existing is not None:
                try:
                    _assert_income_co_org(db, user, existing.id)
                except Exception as exc:
                    raise scope_exception_to_http(exc) from exc
                response.status_code = status.HTTP_200_OK
                return existing
        raise
    db.refresh(obj)
    return obj


# ---------------------------------------------------------------------------
# Static segments MUST be registered BEFORE the dynamic /{income_id} capture
# so that FastAPI's route resolution does not greedily match e.g. "claims"
# against int:income_id (which returns 422 instead of the claims list).
# ---------------------------------------------------------------------------


def _claim_out(c) -> dict:
    return {
        "id": c.id,
        "lease_id": c.lease_id,
        "period": c.period,
        "income_id": c.income_id,
        "claimed_amount": str(c.claimed_amount) if c.claimed_amount is not None else "0.00",
        "claimed_by": c.claimed_by,
        "claimed_at": c.claimed_at,
        "received_date": (
            c.received_date.date()
            if c.received_date is not None and hasattr(c.received_date, "date")
            else c.received_date
        ),
        "status": c.status.value if hasattr(c.status, "value") else str(c.status),
        "evidence_ids": list(c.evidence_ids or []),
        "verification_note": c.verification_note,
        "verified_amount": (
            str(c.verified_amount) if c.verified_amount is not None else None
        ),
        "verified_by": c.verified_by,
        "verified_at": c.verified_at,
        "mismatch": bool(c.mismatch),
        "mismatch_reason": c.mismatch_reason,
        "failure_reason": c.failure_reason,
    }


@router.get("/claims", response_model=Paginated[RentClaimOut])
def list_all_rent_claims(
    lease_id: int | None = Query(default=None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        claims = scoped_list_rent_payment_claims(
            db, for_user_id=user.id, lease_id=lease_id
        )
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc
    rows = [_claim_out(c) for c in claims]
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    total = len(rows)
    paged = rows[offset:offset + limit]
    return Paginated(items=paged, total=total, limit=limit, offset=offset)


@router.get("/claims/{claim_id}", response_model=RentClaimOut)
def get_rent_claim(
    claim_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        claim, _mem = scoped_get_rent_payment_claim(
            db, claim_id, for_user_id=user.id
        )
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc
    return _claim_out(claim)


@router.patch("/claims/{claim_id}/verify", response_model=RentClaimOut)
def verify_rent_claim(
    claim_id: int,
    payload: RentClaimVerify,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Verify a PENDING claim and reconcile the period's verified-paid aggregate.

    * Scope: active OWNER membership in the claim's organization only.
      Global ``UserRole.admin`` without a membership does NOT grant access —
      secretary / non-owner active members get 403.
    * Invariant: claim ≠ verified — this endpoint is what marks a claim as
      actually VERIFIED (and hence enters the aggregate).
    * Invariant: over-claim mismatch (claim.verified_amount would make
      total > required) is FAILED with mismatch=True, never auto-paid."""
    try:
        claim, _mem = scoped_get_rent_payment_claim(
            db, claim_id, for_user_id=user.id, role=OrganizationRole.OWNER
        )
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc
    lease = db.get(Lease, claim.lease_id)
    if lease is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lease for claim not found")
    try:
        claim = verify_claim(
            db,
            lease,
            claim_id,
            verified_by=user.id,
            verified_amount=payload.verified_amount,
            result=payload.result,
        )
    except RentClaimError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    db.commit()
    db.refresh(claim)
    return _claim_out(claim)


@router.patch("/claims/{claim_id}/fail", response_model=RentClaimOut)
def fail_rent_claim(
    claim_id: int,
    payload: RentClaimFail,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Fail a PENDING claim (owner-only action).

    Global ``UserRole.admin`` without an OWNER membership is refused;
    secretary / non-owner active members get 403."""
    try:
        claim, _mem = scoped_get_rent_payment_claim(
            db, claim_id, for_user_id=user.id, role=OrganizationRole.OWNER
        )
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc
    lease = db.get(Lease, claim.lease_id)
    if lease is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lease for claim not found")
    try:
        claim = fail_claim(
            db, lease, claim_id, failed_by=user.id, reason=payload.reason
        )
    except RentClaimError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    db.commit()
    db.refresh(claim)
    return _claim_out(claim)


@router.patch("/claims/{claim_id}/reverse", response_model=RentClaimOut)
def reverse_rent_claim(
    claim_id: int,
    payload: RentClaimReverse,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Reverse a VERIFIED claim (owner-only: bounced check / clawback).

    Global ``UserRole.admin`` without an OWNER membership is refused;
    secretary / non-owner active members get 403."""
    try:
        claim, _mem = scoped_get_rent_payment_claim(
            db, claim_id, for_user_id=user.id, role=OrganizationRole.OWNER
        )
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc
    lease = db.get(Lease, claim.lease_id)
    if lease is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lease for claim not found")
    try:
        claim = reverse_claim(
            db, lease, claim_id, reversed_by=user.id, reason=payload.reason
        )
    except RentClaimError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    db.commit()
    db.refresh(claim)
    return _claim_out(claim)


@router.post("/leases/{lease_id}/claims", response_model=RentClaimOut, status_code=status.HTTP_201_CREATED)
def create_rent_claim(
    lease_id: int,
    payload: RentClaimCreate,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(manager_or_admin),
):
    """Report a claimed payment for a given lease + period (PENDING only).

    This endpoint NEVER marks the period as paid. Verification must be done
    separately via ``PATCH /incomes/claims/{id}/verify``."""
    try:
        lease, _mem = scoped_get_lease(db, lease_id, for_user_id=user.id)
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc
    try:
        claim, created = create_claim(
            db,
            lease,
            payload.period,
            claimed_amount=payload.claimed_amount,
            claimed_by=user.id,
            received_date=payload.received_date,
            verification_note=payload.verification_note,
            evidence_ids=payload.evidence_ids,
            idempotency_key=payload.idempotency_key,
        )
    except RentClaimError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    db.commit()
    db.refresh(claim)
    if not created:
        response.status_code = status.HTTP_200_OK
    return _claim_out(claim)


@router.get("/leases/{lease_id}/claims", response_model=list[RentClaimOut])
def list_rent_claims(
    lease_id: int,
    period: str | None = Query(default=None, min_length=7, max_length=7),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        _lease, _mem = scoped_get_lease(db, lease_id, for_user_id=user.id)
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc
    claims = list_claims(db, lease_id, period=period)
    return [_claim_out(c) for c in claims]


@router.get("/leases/{lease_id}/periods/{period}", response_model=RentDetailOut)
def get_rent_period_detail(
    lease_id: int,
    period: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if len(period) != 7 or period[4] != "-":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "period must be YYYY-MM")
    try:
        lease, _mem = scoped_get_lease(db, lease_id, for_user_id=user.id)
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc
    truth = snapshot(db, lease_id, period)
    claims = list_claims(db, lease_id, period=period)
    return RentDetailOut(
        lease_id=lease_id,
        period=period,
        truth=RentPeriodPaymentInfo(
            required_amount=str(truth.required_amount),
            verified_paid=str(truth.verified_paid_total),
            remaining=str(truth.remaining),
            overpaid=str(truth.overpaid),
            fully_paid=truth.is_fully_paid,
            partially_paid=truth.is_partially_paid,
            pending_claim_count=truth.pending_claim_count,
            verified_claim_count=truth.verified_claim_count,
            failed_claim_count=truth.failed_claim_count,
            reversed_claim_count=truth.reversed_claim_count,
            pending_claimed_total=str(truth.pending_claimed_total),
            has_mismatch=truth.has_mismatch,
            overclaimed_total=str(truth.overclaimed_total),
        ),
        claims=[_claim_out(c) for c in claims],
        evidence=None,
        timeline=[],
    )


@router.get("/{income_id}", response_model=IncomeRead)
def get_income(
    income_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    try:
        obj, _membership = scoped_get_income(db, income_id, for_user_id=user.id)
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc
    return obj


@router.patch("/{income_id}", response_model=IncomeRead)
def update_income(
    income_id: int,
    payload: IncomeUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(manager_or_admin),
):
    try:
        obj, _membership = scoped_get_income(db, income_id, for_user_id=user.id)
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return obj
    if obj.status == IncomeStatus.reversed:
        raise HTTPException(status.HTTP_409_CONFLICT, "Cannot edit a reversed income")
    if obj.status == IncomeStatus.confirmed and "amount" in updates and updates["amount"] != obj.amount:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Cannot change the amount of a confirmed income"
        )
    if "lease_id" in updates:
        try:
            _check_lease(db, updates["lease_id"])
            _assert_lease_co_org(db, user, updates["lease_id"])
        except Exception as exc:
            raise scope_exception_to_http(exc) from exc
    old = serialize_row(obj)
    changed = field_changes(obj, updates)
    for field, value in updates.items():
        setattr(obj, field, value)
    obj.updated_by = user.id
    record_audit(
        db,
        table_name="incomes",
        record_id=obj.id,
        action="update",
        actor_id=user.id,
        changed_fields=changed,
        old_value=old,
        new_value=serialize_row(obj),
    )
    db.commit()
    db.refresh(obj)
    return obj


@router.post("/{income_id}/confirm", response_model=IncomeRead)
def confirm_income(
    income_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(owner_subject_only),
):
    try:
        obj, _membership = scoped_get_income(db, income_id, for_user_id=user.id)
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc
    old = serialize_row(obj)
    result = db.execute(
        update(Income)
        .where(Income.id == income_id, Income.status == IncomeStatus.pending)
        .values(
            status=IncomeStatus.confirmed,
            confirmed_by=user.id,
            confirmed_at=datetime.now(timezone.utc),
            updated_by=user.id,
            updated_at=func.now(),
        ),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount == 1:
        db.refresh(obj)
        record_audit(
            db,
            table_name="incomes",
            record_id=obj.id,
            action="confirm",
            actor_id=user.id,
            old_value=old,
            new_value=serialize_row(obj),
        )
        db.commit()
        db.refresh(obj)
        return obj
    db.rollback()
    try:
        current, _m = scoped_get_income(db, income_id, for_user_id=user.id)
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc
    if current.status == IncomeStatus.confirmed:
        return current
    raise HTTPException(status.HTTP_409_CONFLICT, "Only pending income can be confirmed")


@router.post("/{income_id}/reverse", response_model=IncomeRead)
def reverse_income(
    income_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(owner_subject_only),
):
    try:
        obj, _membership = scoped_get_income(db, income_id, for_user_id=user.id)
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc
    old = serialize_row(obj)
    result = db.execute(
        update(Income)
        .where(Income.id == income_id, Income.status == IncomeStatus.confirmed)
        .values(
            status=IncomeStatus.reversed,
            updated_by=user.id,
            updated_at=func.now(),
        ),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount == 1:
        db.refresh(obj)
        record_audit(
            db,
            table_name="incomes",
            record_id=obj.id,
            action="reverse",
            actor_id=user.id,
            old_value=old,
            new_value=serialize_row(obj),
        )
        db.commit()
        db.refresh(obj)
        return obj
    db.rollback()
    try:
        current, _m = scoped_get_income(db, income_id, for_user_id=user.id)
    except Exception as exc:
        raise scope_exception_to_http(exc) from exc
    if current.status == IncomeStatus.reversed:
        return current
    raise HTTPException(status.HTTP_409_CONFLICT, "Only confirmed income can be reversed")
