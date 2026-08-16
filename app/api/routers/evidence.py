"""AI-OPS-FOUNDATION-001 §11/§12: universal evidence index endpoints.

One shared evidence model for every business media/document (property photos,
before/after repair evidence, quotes, receipts, payment proof, lease,
move-in/out, other). Media bytes live in a storage layer (initially the free
Telegram private archive channel); PostgreSQL here stays the authoritative
index and relationship store. ``storage_provider`` / ``external_file_id``
keep the layer portable (future R2/NAS/S3 without touching domain models).

POST also:
- closes any open repair-evidence FOLLOWUP for the linked repair task when
  completion evidence is uploaded (AI-OPS-FOUNDATION-001 §13);
- marks the repair task's ``completion_evidence`` so the completeness check
  passes on re-completion.
"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.database import get_db
from app.models.evidence import Evidence, EvidenceCategory
from app.models.property import Property, Unit
from app.models.user import User
from app.services.audit import record_audit, serialize_row

router = APIRouter(prefix="/evidence", tags=["evidence"])

_EVIDENCE_KEYS = (
    "storage_provider", "external_file_id", "external_message_id", "media_type",
    "mime_type", "filename", "size_bytes", "checksum", "category",
    "property_id", "unit_id", "entity_type", "entity_id",
)


class EvidenceCreate(BaseModel):
    storage_provider: str = Field(default="telegram_channel", max_length=30)
    external_file_id: str = Field(min_length=1, max_length=300)
    external_message_id: Optional[int] = None
    media_type: Optional[str] = Field(default=None, max_length=30)
    mime_type: Optional[str] = Field(default=None, max_length=100)
    filename: Optional[str] = Field(default=None, max_length=255)
    size_bytes: Optional[int] = None
    checksum: Optional[str] = Field(default=None, max_length=128)
    category: Optional[str] = Field(default=None, max_length=50)
    property_id: Optional[int] = None
    unit_id: Optional[int] = None
    entity_type: Optional[str] = Field(default=None, max_length=50)
    entity_id: Optional[int] = None


class EvidenceRead(BaseModel):
    id: int
    storage_provider: str
    external_file_id: str
    external_message_id: Optional[int] = None
    media_type: Optional[str] = None
    mime_type: Optional[str] = None
    filename: Optional[str] = None
    size_bytes: Optional[int] = None
    checksum: Optional[str] = None
    category: Optional[str] = None
    property_id: Optional[int] = None
    unit_id: Optional[int] = None
    entity_type: Optional[str] = None
    entity_id: Optional[int] = None
    uploaded_by: Optional[int] = None
    created_at: Optional[datetime] = None


def _serialize(ev: Evidence) -> dict:
    return {
        "id": ev.id,
        "storage_provider": ev.storage_provider,
        "external_file_id": ev.external_file_id,
        "external_message_id": ev.external_message_id,
        "media_type": ev.media_type,
        "mime_type": ev.mime_type,
        "filename": ev.filename,
        "size_bytes": ev.size_bytes,
        "checksum": ev.checksum,
        "category": ev.category.value if ev.category else None,
        "property_id": ev.property_id,
        "unit_id": ev.unit_id,
        "entity_type": ev.entity_type,
        "entity_id": ev.entity_id,
        "uploaded_by": ev.uploaded_by,
        "created_at": ev.created_at,
    }


def _check_links(db: Session, payload: EvidenceCreate) -> None:
    if payload.property_id is not None:
        prop = db.query(Property).filter(Property.id == payload.property_id).first()
        if prop is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Property not found")
    if payload.unit_id is not None:
        unit = db.query(Unit).filter(Unit.id == payload.unit_id, Unit.deleted_at.is_(None)).first()
        if unit is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Unit not found")
    if payload.category and payload.category not in {c.value for c in EvidenceCategory}:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown evidence category"
        )


def _repair_evidence_key(category: str | None) -> str | None:
    """Map an evidence category to the repair-task completion-evidence key."""
    return {
        "after_repair": "after_photo",
        "before_repair": "before_photo",
        "receipt": "receipt",
        "payment_proof": "receipt",
        "quote": "quote",
        "diagnosis": "diagnosis",
    }.get(category or "", "") or None


@router.post("", response_model=EvidenceRead, status_code=status.HTTP_201_CREATED)
def create_evidence(
    payload: EvidenceCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _check_links(db, payload)
    obj = Evidence(
        storage_provider=payload.storage_provider,
        external_file_id=payload.external_file_id,
        external_message_id=payload.external_message_id,
        media_type=payload.media_type,
        mime_type=payload.mime_type,
        filename=payload.filename,
        size_bytes=payload.size_bytes,
        checksum=payload.checksum,
        category=payload.category,
        property_id=payload.property_id,
        unit_id=payload.unit_id,
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        uploaded_by=user.id,
    )
    db.add(obj)
    db.flush()
    record_audit(
        db,
        table_name="evidence",
        record_id=obj.id,
        action="create",
        actor_id=user.id,
        new_value=serialize_row(obj),
    )
    # Patch the real evidence id into the repair task's completion_evidence.
    if payload.entity_type == "task" and payload.entity_id is not None:
        from app.models.operations import OperationalTask

        task = db.get(OperationalTask, payload.entity_id)
        if task is not None:
            details = dict(task.details or {})
            evidence = dict(details.get("completion_evidence") or {})
            key = _repair_evidence_key(payload.category)
            if key:
                evidence[key] = {"evidence_id": obj.id}
            details["completion_evidence"] = evidence
            task.details = details
            task.updated_at = datetime.now(timezone.utc)
            db.flush()
            from app.services.operations.repair_flow import close_evidence_followups

            close_evidence_followups(db, task.id, actor_id=user.id)
    db.commit()
    db.refresh(obj)
    return _serialize(obj)


@router.get("", response_model=list[EvidenceRead])
def list_evidence(
    property_id: Optional[int] = Query(default=None),
    unit_id: Optional[int] = Query(default=None),
    entity_type: Optional[str] = Query(default=None),
    entity_id: Optional[int] = Query(default=None),
    category: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    query = db.query(Evidence)
    if property_id is not None:
        query = query.filter(Evidence.property_id == property_id)
    if unit_id is not None:
        query = query.filter(Evidence.unit_id == unit_id)
    if entity_type is not None:
        query = query.filter(Evidence.entity_type == entity_type)
    if entity_id is not None:
        query = query.filter(Evidence.entity_id == entity_id)
    if category is not None:
        query = query.filter(Evidence.category == category)
    rows = query.order_by(Evidence.created_at.desc(), Evidence.id.desc()).all()
    return [_serialize(ev) for ev in rows]


@router.get("/{evidence_id}", response_model=EvidenceRead)
def get_evidence(
    evidence_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    obj = db.get(Evidence, evidence_id)
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Evidence not found")
    return _serialize(obj)
