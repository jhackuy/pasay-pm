"""Dashboard + cross-domain audit read API.

Single endpoint that aggregates:
- Urgent Operations (open/in_progress) needing next-actor input
- Recent activity events across all domains (audit timeline)

Issue #99 OWNER ADDENDUM: Mini App #/dashboard + #/archive surface.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.permissions import PermissionDenied, Principal, require_org_scope
from app.v1.deps import get_current_principal, get_db_dep
from app.v1.models.expense import ExpenseActivity
from app.v1.models.move_out import MoveOutActivity
from app.v1.models.renewal import RenewalActivity
from app.v1.models.rent_payment import RentActivity
from app.v1.models.repair import RepairActivity
from app.v1.schemas.rent_payment import RentActivityRead


router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("", response_model=list[dict[str, Any]])
def list_audit_events(
    org_id: int,
    limit: int = Query(default=50, ge=1, le=500),
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db_dep),
) -> list[dict[str, Any]]:
    """Append-only audit timeline across all V1 activity tables."""
    try:
        require_org_scope(principal, org_id)
    except PermissionDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    out: list[dict[str, Any]] = []
    for model in (
        RentActivity, ExpenseActivity, RepairActivity,
        RenewalActivity, MoveOutActivity,
    ):
        rows = (
            db.query(model)
            .filter(model.org_id == org_id)
            .order_by(model.id.desc())
            .limit(limit)
            .all()
        )
        for r in rows:
            out.append({
                "id": r.id,
                "kind": r.kind,
                "subject_type": _subject_for(model),
                "subject_id": _subject_id(r),
                "org_id": r.org_id,
                "created_at": r.created_at.isoformat(),
            })
    out.sort(key=lambda e: e["created_at"], reverse=True)
    return out[:limit]


def _subject_for(model: type) -> str:
    name = model.__name__
    if name == "RentActivity":
        return "rent"
    if name == "ExpenseActivity":
        return "expense"
    if name == "RepairActivity":
        return "repair"
    if name == "RenewalActivity":
        return "renewal"
    if name == "MoveOutActivity":
        return "move_out"
    return name.lower()


def _subject_id(row: Any) -> int:
    """Best-effort subject id from a polymorphic activity row."""
    for attr in ("rent_payment_id", "claim_id", "report_id",
                 "renewal_id", "move_out_id"):
        if hasattr(row, attr):
            return int(getattr(row, attr) or 0)
    return 0


__all__ = ["router", "list_audit_events"]
