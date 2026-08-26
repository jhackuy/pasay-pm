from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.api.deps import admin_only, get_current_user, manager_or_admin
from app.database import get_db
from app.models.commission import (
    CommissionRule,
    CommissionSettlement,
    CommissionSettlementStatus,
)
from app.models.lease import Lease
from app.models.membership import Membership, MembershipState
from app.models.property import Property, Unit
from app.models.user import User
from app.schemas.common import MessageResponse, Paginated
from app.schemas.commission import (
    CommissionRuleCreate,
    CommissionRuleRead,
    CommissionRuleUpdate,
    CommissionSettlementCreate,
    CommissionSettlementRead,
)
from app.services.audit import field_changes, record_audit, serialize_row
from app.services.commission_engine import compute_settlement
from app.services.organization_scope import list_active_org_ids_for_user

router = APIRouter(prefix="/commission", tags=["commission"])


# --- rules ---
def _get_rule_or_404(db: Session, rule_id: int) -> CommissionRule:
    obj = (
        db.query(CommissionRule)
        .filter(CommissionRule.id == rule_id, CommissionRule.deleted_at.is_(None))
        .first()
    )
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Commission rule not found")
    return obj


def _scoped_settlement_ids(
    db: Session, for_user_id: int, for_user_role: str
) -> set[int]:
    """Return CommissionSettlement.ids visible to caller.

    Scoping rules match pre-existing test_commission.py contract:
      * Agent role: sees ONLY settlements with agent_id == for_user_id (their
        own generated settlements — this is the historical "my commissions"
        view preserved so test_agent_sees_only_own_settlements still passes).
      * Manager / Admin role: sees settlements scoped via org membership
        (settlement -> lease -> unit -> property.organization_id IN caller
        active orgs) PLUS the historical agent self-visibility OR.

    The result set is fail-closed: empty = caller sees nothing (downstream
    returns [] / 404 / 403 accordingly)."""
    role_norm = (for_user_role.value if hasattr(for_user_role, "value") else str(for_user_role)).lower()
    if role_norm == "agent":
        rows = (
            db.query(CommissionSettlement.id)
            .filter(CommissionSettlement.agent_id == for_user_id)
            .all()
        )
        return {r.id for r in rows} if rows else set()
    # Manager / Admin: org scoping via JOIN chain, OR with agent self-visibility
    from sqlalchemy import or_ as _sa_or
    org_ids = list_active_org_ids_for_user(db, for_user_id)
    clauses: list = []
    if org_ids:
        clauses.append(
            CommissionSettlement.id.in_(
                db.query(CommissionSettlement.id)
                .join(Lease, Lease.id == CommissionSettlement.lease_id)
                .join(Unit, Unit.id == Lease.unit_id)
                .join(Property, Property.id == Unit.property_id)
                .filter(Property.organization_id.in_(org_ids))
            )
        )
    clauses.append(CommissionSettlement.agent_id == for_user_id)
    rows = (
        db.query(CommissionSettlement.id)
        .filter(_sa_or(*clauses))
        .all()
    )
    return {r.id for r in rows} if rows else set()


def _ensure_settlement_in_scope(
    db: Session, settlement_id: int, visible: set[int]
) -> CommissionSettlement:
    if settlement_id not in visible:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Commission settlement not found"
        )
    obj = (
        db.query(CommissionSettlement)
        .filter(CommissionSettlement.id == settlement_id)
        .first()
    )
    if obj is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Commission settlement not found"
        )
    return obj


def _ensure_lease_in_caller_orgs(
    db: Session, lease: Lease, caller_org_ids: list[int]
) -> None:
    """Fail-closed guard for write endpoints (create/confirm): the settlement
    is bound to a lease, so callers MUST be members of the organization that
    owns the lease's unit's property.  Otherwise 403 so cross-org writes are
    impossible."""
    if not caller_org_ids:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Commission settlements require active organization membership",
        )
    unit = db.query(Unit).filter(Unit.id == lease.unit_id).first()
    if unit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lease unit missing")
    if unit.property_id is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Lease is not bound to a scoped organization property",
        )
    prop = db.query(Property).filter(Property.id == unit.property_id).first()
    if prop is None or prop.organization_id not in set(caller_org_ids):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Cannot create commission settlement for another organization's lease",
        )


@router.get("/rules", response_model=Paginated[CommissionRuleRead])
def list_rules(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db), _: User = Depends(get_current_user)
):
    query = (
        db.query(CommissionRule)
        .filter(CommissionRule.deleted_at.is_(None))
        .order_by(CommissionRule.id)
    )
    total = query.count()
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    rows = query.offset(offset).limit(limit).all()
    return Paginated(items=rows, total=total, limit=limit, offset=offset)


@router.post("/rules", response_model=CommissionRuleRead, status_code=status.HTTP_201_CREATED)
def create_rule(
    payload: CommissionRuleCreate,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
):
    obj = CommissionRule(**payload.model_dump())
    obj.created_by = user.id
    obj.updated_by = user.id
    db.add(obj)
    db.flush()
    record_audit(
        db,
        table_name="commission_rules",
        record_id=obj.id,
        action="create",
        actor_id=user.id,
        new_value=serialize_row(obj),
    )
    db.commit()
    db.refresh(obj)
    return obj


@router.get("/rules/{rule_id}", response_model=CommissionRuleRead)
def get_rule(
    rule_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)
):
    return _get_rule_or_404(db, rule_id)


@router.patch("/rules/{rule_id}", response_model=CommissionRuleRead)
def update_rule(
    rule_id: int,
    payload: CommissionRuleUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
):
    obj = _get_rule_or_404(db, rule_id)
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return obj
    old = serialize_row(obj)
    changed = field_changes(obj, updates)
    for field, value in updates.items():
        setattr(obj, field, value)
    obj.updated_by = user.id
    record_audit(
        db,
        table_name="commission_rules",
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


@router.delete("/rules/{rule_id}", response_model=MessageResponse)
def delete_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
):
    obj = _get_rule_or_404(db, rule_id)
    from datetime import datetime as _dt, timezone as _tz

    obj.deleted_at = _dt.now(_tz.utc)
    obj.updated_by = user.id
    record_audit(
        db,
        table_name="commission_rules",
        record_id=obj.id,
        action="soft_delete",
        actor_id=user.id,
        old_value=serialize_row(obj),
    )
    db.commit()
    return MessageResponse(detail="Commission rule deleted")


# --- settlements ---
@router.get("/settlements")
def list_settlements(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    visible = _scoped_settlement_ids(db, user.id, user.role.value if hasattr(user.role, "value") else str(user.role))
    if not visible:
        return []
    query = db.query(CommissionSettlement).filter(CommissionSettlement.id.in_(visible))
    ordered = query.order_by(CommissionSettlement.id)
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    rows = ordered.offset(offset).limit(limit).all()
    return rows


@router.post(
    "/settlements",
    response_model=CommissionSettlementRead,
    status_code=status.HTTP_201_CREATED,
)
def create_settlement(
    payload: CommissionSettlementCreate,
    db: Session = Depends(get_db),
    user: User = Depends(manager_or_admin),
):
    caller_org_ids = list_active_org_ids_for_user(db, user.id)
    agent = db.query(User).filter(User.id == payload.agent_id, User.is_active.is_(True)).first()
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    lease = db.query(Lease).filter(Lease.id == payload.lease_id, Lease.deleted_at.is_(None)).first()
    if lease is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lease not found")
    _ensure_lease_in_caller_orgs(db, lease, caller_org_ids)
    rule = _get_rule_or_404(db, payload.rule_id)
    if not rule.is_active:
        raise HTTPException(status.HTTP_409_CONFLICT, "Commission rule is not active")

    computed = compute_settlement(None, rule, lease.monthly_rent)
    obj = CommissionSettlement(
        agent_id=payload.agent_id,
        lease_id=payload.lease_id,
        rule_id=payload.rule_id,
        computed_amount=computed,
        status=CommissionSettlementStatus.pending,
        notes=payload.notes,
    )
    obj.created_by = user.id
    obj.updated_by = user.id
    db.add(obj)
    db.flush()
    record_audit(
        db,
        table_name="commission_settlements",
        record_id=obj.id,
        action="create",
        actor_id=user.id,
        new_value=serialize_row(obj),
    )
    db.commit()
    db.refresh(obj)
    return obj


@router.get("/settlements/{settlement_id}", response_model=CommissionSettlementRead)
def get_settlement(
    settlement_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    visible = _scoped_settlement_ids(db, user.id, user.role.value if hasattr(user.role, "value") else str(user.role))
    obj = _ensure_settlement_in_scope(db, settlement_id, visible)
    # Pre-existing agent self-visibility is already handled in _scoped_settlement_ids,
    # but we keep the original explicit "agent != owner 403" check so a cross-org
    # agent settlement fetch still fails.
    role_str = user.role.value if hasattr(user.role, "value") else str(user.role)
    is_agent_role = role_str.lower() == "agent"
    if is_agent_role and obj.agent_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions")
    return obj


@router.post("/settlements/{settlement_id}/confirm", response_model=CommissionSettlementRead)
def confirm_settlement(
    settlement_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
):
    caller_org_ids = list_active_org_ids_for_user(db, user.id)
    visible = _scoped_settlement_ids(db, user.id, user.role.value if hasattr(user.role, "value") else str(user.role))
    obj = _ensure_settlement_in_scope(db, settlement_id, visible)
    # Confirm is admin_only; additionally ensure the admin is writing within
    # the organization that owns the settlement's lease (otherwise 403).
    lease = db.query(Lease).filter(Lease.id == obj.lease_id, Lease.deleted_at.is_(None)).first()
    if lease is not None:
        _ensure_lease_in_caller_orgs(db, lease, caller_org_ids)
    old = serialize_row(obj)
    result = db.execute(
        update(CommissionSettlement)
        .where(
            CommissionSettlement.id == settlement_id,
            CommissionSettlement.status == CommissionSettlementStatus.pending,
        )
        .values(
            status=CommissionSettlementStatus.confirmed,
            updated_by=user.id,
            updated_at=func.now(),
        ),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount == 1:
        db.refresh(obj)
        record_audit(
            db,
            table_name="commission_settlements",
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
    current = (
        db.query(CommissionSettlement)
        .filter(CommissionSettlement.id == settlement_id)
        .first()
    )
    if current is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Commission settlement not found")
    if current.status == CommissionSettlementStatus.confirmed:
        return current
    raise HTTPException(status.HTTP_409_CONFLICT, "Only pending settlements can be confirmed")
