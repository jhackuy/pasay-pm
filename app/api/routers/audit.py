from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import admin_only
from app.database import get_db
from app.models.audit_log import AuditLog
from app.models.user import User
from app.schemas.audit import AuditLogRead
from app.schemas.common import Paginated

router = APIRouter(prefix="/audit-logs", tags=["audit"])


@router.get("", response_model=Paginated[AuditLogRead])
def list_audit_logs(
    table_name: str | None = Query(default=None, max_length=100),
    record_id: int | None = Query(default=None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: User = Depends(admin_only),
):
    query = db.query(AuditLog)
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
