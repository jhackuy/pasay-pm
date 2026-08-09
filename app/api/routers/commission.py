from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import admin_only, get_current_user, manager_or_admin
from app.database import get_db
from app.models.commission import (
    CommissionRule,
    CommissionSettlement,
    CommissionSettlementStatus,
)
from app.models.lease import Lease
from app.models.user import User
from app.schemas.common import MessageResponse
from app.schemas.commission import (
    CommissionRuleCreate,
    CommissionRuleRead,
    CommissionRuleUpdate,
    CommissionSettlementCreate,
    CommissionSettlementRead,
)
from app.services.audit import field_changes, record_audit, serialize_row
from app.services.commission_engine import compute_settlement

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


@router.get("/rules", response_model=list[CommissionRuleRead])
def list_rules(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return (
        db.query(CommissionRule)
        .filter(CommissionRule.deleted_at.is_(None))
        .order_by(CommissionRule.id)
        .all()
    )


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
@router.get("/settlements", response_model=list[CommissionSettlementRead])
def list_settlements(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    query = db.query(CommissionSettlement)
    if user.role == "agent":
        query = query.filter(CommissionSettlement.agent_id == user.id)
    return query.order_by(CommissionSettlement.id).all()


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
    agent = db.query(User).filter(User.id == payload.agent_id, User.is_active.is_(True)).first()
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    lease = db.query(Lease).filter(Lease.id == payload.lease_id, Lease.deleted_at.is_(None)).first()
    if lease is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lease not found")
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
    obj = (
        db.query(CommissionSettlement)
        .filter(CommissionSettlement.id == settlement_id)
        .first()
    )
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Commission settlement not found")
    if user.role == "agent" and obj.agent_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions")
    return obj


@router.post("/settlements/{settlement_id}/confirm", response_model=CommissionSettlementRead)
def confirm_settlement(
    settlement_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
):
    obj = (
        db.query(CommissionSettlement)
        .filter(CommissionSettlement.id == settlement_id)
        .first()
    )
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Commission settlement not found")
    if obj.status != CommissionSettlementStatus.pending:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only pending settlements can be confirmed")
    old = serialize_row(obj)
    obj.status = CommissionSettlementStatus.confirmed
    obj.updated_by = user.id
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
