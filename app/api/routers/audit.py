from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import admin_only
from app.database import get_db
from app.models.audit_log import AuditLog
from app.models.identity import Principal, PrincipalType
from app.models.membership import Membership, MembershipState
from app.models.user import User
from app.schemas.audit import AuditLogRead
from app.schemas.common import Paginated

router = APIRouter(prefix="/audit-logs", tags=["audit"])


def _scoped_audit_log_ids(db: Session, for_user_id: int) -> set[int] | None:
    """Restrict audit logs to rows whose subject_principal maps back to
    a Membership.active row in organizations where `for_user_id` holds an
    ACTIVE membership, OR whose actor_id == for_user_id (self-audit visibility
    always passes — fail-closed minimum)."""
    org_ids = [
        m.organization_id
        for m in db.query(Membership).filter(
            Membership.user_id == for_user_id,
            Membership.state == MembershipState.ACTIVE,
        ).all()
    ]
    from sqlalchemy import or_ as _sa_or, and_ as _sa_and
    clauses: list = []
    clauses.append(AuditLog.actor_id == for_user_id)
    if org_ids:
        clauses.append(
            AuditLog.id.in_(
                db.query(AuditLog.id)
                .join(
                    Principal,
                    Principal.id == AuditLog.subject_principal_id,
                )
                .join(
                    Membership,
                    _sa_and(
                        Membership.user_id == Principal.user_id,
                        Membership.organization_id.in_(org_ids),
                        Membership.state == MembershipState.ACTIVE,
                        Principal.principal_type == PrincipalType.HUMAN,
                    ),
                )
            )
        )
    visible = (
        db.query(AuditLog.id)
        .filter(_sa_or(*clauses))
        .all()
    )
    return {v.id for v in visible} if visible else set()


@router.get("", response_model=Paginated[AuditLogRead])
def list_audit_logs(
    table_name: str | None = Query(default=None, max_length=100),
    record_id: int | None = Query(default=None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
):
    visible_ids = _scoped_audit_log_ids(db, user.id)
    if not visible_ids:
        return Paginated(
            items=[], total=0,
            limit=max(1, min(limit, 500)),
            offset=max(0, offset),
        )
    query = db.query(AuditLog).filter(AuditLog.id.in_(visible_ids))
    if table_name is not None:
        query = query.filter(AuditLog.table_name == table_name)
    if record_id is not None:
        query = query.filter(AuditLog.record_id == record_id)
    ordered = query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    total = ordered.count()
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    rows = ordered.offset(offset).limit(limit).all()
    return Paginated(items=rows, total=total, limit=limit, offset=offset)
