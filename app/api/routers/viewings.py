"""AI-OPS-FOUNDATION-001 §17: viewings / vacancy workflow.

A message like "Someone will view 1608 tomorrow at 2pm" is persisted as a
business event (a ``viewings`` row), not chat-only context:
- the unit is bound and the scheduled time stored;
- the Secretary gets a reminder task just before the viewing
  (``dedupe_key = viewing:{id}`` — one active reminder per viewing);
- after the viewing the minimal outcome is recorded
  (interested / not_interested / follow_up + rejection reason), so future
  vacancy/pricing analysis has real data.

Deterministic; no LLM.
"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, manager_or_admin
from app.database import get_db
from app.models.evidence import Viewing, ViewingOutcome, ViewingStatus
from app.models.operations import OperationalTaskPriority, OperationalTaskStatus, OperationalTaskType
from app.models.property import Property, Unit
from app.models.user import User
from app.services.audit import record_audit, serialize_row
from app.services.operations.config import SECRETARY_ASSIGNEE_ID
from app.services.operations.generation import create_operational_task
from app.schemas.common import Paginated

router = APIRouter(prefix="/viewings", tags=["viewings"])


class ViewingCreate(BaseModel):
    unit_id: int
    scheduled_at: datetime
    notes: Optional[str] = Field(default=None, max_length=1000)


class ViewingOutcomeIn(BaseModel):
    outcome: ViewingOutcome
    reason: Optional[str] = Field(default=None, max_length=500)


class ViewingRead(BaseModel):
    id: int
    unit_id: int
    property_id: Optional[int] = None
    scheduled_at: datetime
    status: str
    outcome: Optional[str] = None
    reason: Optional[str] = None
    notes: Optional[str] = None
    created_by: Optional[int] = None


def _serialize(v: Viewing) -> dict:
    return {
        "id": v.id,
        "unit_id": v.unit_id,
        "property_id": v.property_id,
        "scheduled_at": v.scheduled_at,
        "status": v.status.value,
        "outcome": v.outcome.value if v.outcome else None,
        "reason": v.reason,
        "notes": v.notes,
        "created_by": v.created_by,
    }


def _secretary_fallback():
    """Secretary assignee with a safe fallback to the default owner (resolves
    through generation's monkeypatchable seam so tests can pin the real
    secretary user)."""
    from app.services.operations.generation import secretary_assignee_id

    return secretary_assignee_id()


def _schedule_viewing_reminder(db: Session, viewing: Viewing) -> None:
    """One active Secretary reminder per viewing (dedupe_key = viewing:{id}),
    due 1h before the scheduled time; never a duplicate."""
    from datetime import timedelta

    remind_at = viewing.scheduled_at - timedelta(hours=1)
    fields = {
        "task_type": OperationalTaskType.FOLLOWUP,
        "title": f"看房提醒 · Unit {viewing.unit_id}",
        "description": "A viewing is scheduled for this unit; confirm it happened "
                       "and record the outcome (interested / not interested / follow-up).",
        "property_id": viewing.property_id,
        "assigned_user_id": _secretary_fallback(),
        "priority": OperationalTaskPriority.medium,
        "status": OperationalTaskStatus.PENDING,
        "due_at": remind_at,
        "next_action": "Record the viewing outcome",
        "next_check_at": viewing.scheduled_at,
        "dedupe_key": f"viewing:{viewing.id}",
        "source_type": "viewing",
        "source_id": viewing.id,
        "details": {"viewing_id": viewing.id, "unit_id": viewing.unit_id},
    }
    create_operational_task(db, fields=fields, now=datetime.now(timezone.utc))


@router.post("", response_model=ViewingRead, status_code=status.HTTP_201_CREATED)
def create_viewing(
    payload: ViewingCreate,
    db: Session = Depends(get_db),
    user: User = Depends(manager_or_admin),
):
    unit = db.query(Unit).filter(Unit.id == payload.unit_id, Unit.deleted_at.is_(None)).first()
    if unit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unit not found")
    obj = Viewing(
        unit_id=payload.unit_id,
        property_id=unit.property_id,
        scheduled_at=payload.scheduled_at,
        notes=payload.notes,
        status=ViewingStatus.scheduled,
        created_by=user.id,
        updated_by=user.id,
    )
    db.add(obj)
    db.flush()
    record_audit(
        db,
        table_name="viewings",
        record_id=obj.id,
        action="create",
        actor_id=user.id,
        new_value=serialize_row(obj),
    )
    _schedule_viewing_reminder(db, obj)
    db.commit()
    db.refresh(obj)
    return _serialize(obj)


@router.get("", response_model=Paginated[ViewingRead])
def list_viewings(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    unit_id: Optional[int] = Query(default=None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    query = db.query(Viewing)
    if status_filter:
        try:
            query = query.filter(Viewing.status == ViewingStatus(status_filter))
        except ValueError:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown viewing status")
    if unit_id is not None:
        query = query.filter(Viewing.unit_id == unit_id)
    ordered = query.order_by(Viewing.scheduled_at, Viewing.id)
    total = ordered.count()
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    rows = ordered.offset(offset).limit(limit).all()
    items = [_serialize(v) for v in rows]
    return Paginated(items=items, total=total, limit=limit, offset=offset)


@router.post("/{viewing_id}/outcome", response_model=ViewingRead)
def record_outcome(
    viewing_id: int,
    payload: ViewingOutcomeIn,
    db: Session = Depends(get_db),
    user: User = Depends(manager_or_admin),
):
    """AI-OPS-FOUNDATION-001 §17: after the viewing, record the minimal
    outcome (+ rejection reason where applicable) — real data for future
    vacancy/pricing analysis. Completing the outcome closes the viewing and
    its Secretary reminder."""
    obj = db.get(Viewing, viewing_id)
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Viewing not found")
    if obj.status == ViewingStatus.cancelled:
        raise HTTPException(status.HTTP_409_CONFLICT, "Viewing was cancelled")
    obj.status = ViewingStatus.done
    obj.outcome = payload.outcome
    obj.reason = payload.reason
    obj.updated_by = user.id
    db.flush()
    record_audit(
        db,
        table_name="viewings",
        record_id=obj.id,
        action="update",
        actor_id=user.id,
        changed_fields={"status": ["scheduled", "done"], "outcome": [None, payload.outcome.value]},
        old_value=serialize_row(obj),
        new_value=serialize_row(obj),
    )
    _close_viewing_reminders(db, obj, actor_id=user.id)
    db.commit()
    db.refresh(obj)
    return _serialize(obj)


@router.post("/{viewing_id}/cancel", response_model=ViewingRead)
def cancel_viewing(
    viewing_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(manager_or_admin),
):
    obj = db.get(Viewing, viewing_id)
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Viewing not found")
    if obj.status == ViewingStatus.done:
        raise HTTPException(status.HTTP_409_CONFLICT, "Viewing already done")
    obj.status = ViewingStatus.cancelled
    obj.updated_by = user.id
    db.flush()
    _close_viewing_reminders(db, obj, actor_id=user.id)
    db.commit()
    db.refresh(obj)
    return _serialize(obj)


def _close_viewing_reminders(db: Session, viewing: Viewing, *, actor_id: int | None) -> None:
    """Close the Secretary reminder when the viewing is done/cancelled (the
    business state is the source of truth; stale tasks must never stay)."""
    from app.models.operations import OperationalTask
    from app.services.operations.redelivery import suppress_pending_redeliveries

    now = datetime.now(timezone.utc)
    tasks = (
        db.query(OperationalTask)
        .filter(
            OperationalTask.dedupe_key == f"viewing:{viewing.id}",
            OperationalTask.status.in_(
                [OperationalTaskStatus.PENDING, OperationalTaskStatus.IN_PROGRESS]
            ),
        )
        .all()
    )
    for task in tasks:
        task.status = OperationalTaskStatus.COMPLETED
        task.completed_at = now
        task.completed_by = actor_id
        task.reminder_generation = (task.reminder_generation or 0) + 1
        task.updated_at = now
        db.flush()
        suppress_pending_redeliveries(
            db, task.id, actor_id=actor_id, reason="viewing_closed", now=now,
        )
