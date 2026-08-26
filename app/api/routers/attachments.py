import uuid
from pathlib import Path as FSPath

from fastapi import APIRouter, Depends, File, Form, HTTPException, Path, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.config import settings
from app.database import get_db
from app.models.attachment import Attachment
from app.models.user import User
from app.schemas.attachment import AttachmentRead
from app.schemas.common import Paginated
from app.services.organization_scope import (
    list_active_org_ids_for_user,
    org_property_ids as _org_property_ids,
    org_lease_ids as _org_lease_ids,
    org_tenant_ids as _org_tenant_ids,
)

router = APIRouter(prefix="/attachments", tags=["attachments"])


def _resolve_attachment_org_scope_filter(db: Session, for_user_id: int):
    orgs = list_active_org_ids_for_user(db, for_user_id)
    if not orgs:
        return Attachment.id == -1
    prop_ids = set()
    lease_ids = set()
    tenant_ids = set()
    for oid in orgs:
        prop_ids |= _org_property_ids(db, oid)
        lease_ids |= _org_lease_ids(db, oid)
        tenant_ids |= _org_tenant_ids(db, oid)
    from sqlalchemy import or_ as _sa_or
    from app.models.lease import Lease
    from app.models.financial import Expense, Income
    from app.models.operations import OperationalTask
    from app.models.property import Property, Unit
    from app.models.tenant import Tenant
    from app.models.repair import RepairOperation
    from app.models.move_out import MoveOutInspection
    from app.models.deposit_settlement import DepositSettlement

    clauses = []
    if prop_ids:
        clauses.append(
            (Attachment.related_type == "property") & Attachment.related_id.in_(list(prop_ids))
        )
        prop_list = list(prop_ids)
        unit_q = db.query(Unit.id).filter(Unit.property_id.in_(prop_list)).subquery()
        clauses.append(
            (Attachment.related_type == "unit") & Attachment.related_id.in_(unit_q)
        )
    if lease_ids:
        clauses.append(
            (Attachment.related_type == "lease") & Attachment.related_id.in_(list(lease_ids))
        )
    if tenant_ids:
        clauses.append(
            (Attachment.related_type == "tenant") & Attachment.related_id.in_(list(tenant_ids))
        )
    if prop_ids:
        prop_list = list(prop_ids)
        clauses.append(
            (Attachment.related_type == "expense")
            & Attachment.related_id.in_(
                db.query(Expense.id).filter(Expense.property_id.in_(prop_list)).subquery()
            )
        )
    if lease_ids:
        lease_list = list(lease_ids)
        clauses.append(
            (Attachment.related_type == "income")
            & Attachment.related_id.in_(
                db.query(Income.id).filter(Income.lease_id.in_(lease_list)).subquery()
            )
        )
    if prop_ids or lease_ids or tenant_ids:
        or_terms = []
        from sqlalchemy import Integer as _SAInteger
        if prop_ids:
            or_terms.append(OperationalTask.property_id.in_(list(prop_ids)))
        if lease_ids:
            or_terms.append(OperationalTask.lease_id.in_(list(lease_ids)))
        if tenant_ids:
            or_terms.append(OperationalTask.tenant_id.in_(list(tenant_ids)))
        if or_terms:
            task_q = db.query(OperationalTask.id).filter(_sa_or(*or_terms)).subquery()
            clauses.append(
                (Attachment.related_type == "task") & Attachment.related_id.in_(task_q)
            )
    if prop_ids:
        prop_list = list(prop_ids)
        prop_list_arg = list(prop_ids)
        repair_or_terms = []
        from sqlalchemy import or_ as _sa_or2
        repair_or_terms.append(RepairOperation.property_id.in_(prop_list_arg))
        unit_q = db.query(Unit.id).filter(Unit.property_id.in_(prop_list_arg)).subquery()
        repair_or_terms.append(RepairOperation.unit_id.in_(unit_q))
        repair_q = db.query(RepairOperation.id).filter(_sa_or2(*repair_or_terms)).subquery()
        clauses.append(
            (Attachment.related_type == "repair") & Attachment.related_id.in_(repair_q)
        )
    if lease_ids:
        lease_list = list(lease_ids)
        moveout_q = db.query(MoveOutInspection.id).filter(
            MoveOutInspection.lease_id.in_(lease_list)
        ).subquery()
        clauses.append(
            (Attachment.related_type == "move_out_inspection")
            & Attachment.related_id.in_(moveout_q)
        )
        settlement_q = db.query(DepositSettlement.id).filter(
            DepositSettlement.lease_id.in_(lease_list)
        ).subquery()
        clauses.append(
            (Attachment.related_type == "deposit_settlement")
            & Attachment.related_id.in_(settlement_q)
        )
    clauses.append(Attachment.uploaded_by == for_user_id)
    return _sa_or(*clauses)


def _scoped_get_attachment(db: Session, attachment_id: int, for_user_id: int) -> Attachment:
    scope_filter = _resolve_attachment_org_scope_filter(db, for_user_id)
    obj = (
        db.query(Attachment)
        .filter(Attachment.id == attachment_id, scope_filter)
        .first()
    )
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    return obj


def _resolve_path(filedata: str) -> FSPath:
    path = FSPath(filedata)
    if not path.is_absolute():
        path = FSPath(settings.upload_dir) / path
    return path


@router.get("", response_model=Paginated[AttachmentRead])
def list_attachments(
    related_type: str | None = Query(default=None, max_length=50),
    related_id: int | None = Query(default=None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    scope_filter = _resolve_attachment_org_scope_filter(db, user.id)
    query = db.query(Attachment).filter(scope_filter)
    if related_type is not None:
        query = query.filter(Attachment.related_type == related_type)
    if related_id is not None:
        query = query.filter(Attachment.related_id == related_id)
    ordered = query.order_by(Attachment.created_at.desc(), Attachment.id.desc())
    total = ordered.count()
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    rows = ordered.offset(offset).limit(limit).all()
    return Paginated(items=rows, total=total, limit=limit, offset=offset)


@router.post("", response_model=AttachmentRead, status_code=status.HTTP_201_CREATED)
async def upload_attachment(
    file: UploadFile = File(...),
    related_type: str | None = Form(default=None, max_length=50),
    related_id: int | None = Form(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    upload_dir = FSPath(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}_{FSPath(file.filename or 'file').name}"
    target = upload_dir / stored_name
    content = await file.read()
    target.write_bytes(content)

    obj = Attachment(
        filedata=stored_name,
        original_filename=file.filename or stored_name,
        mime_type=file.content_type,
        related_type=related_type,
        related_id=related_id,
        uploaded_by=user.id,
    )
    obj.created_by = user.id
    obj.updated_by = user.id
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.get("/{attachment_id}", response_model=AttachmentRead)
def get_attachment(
    attachment_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    return _scoped_get_attachment(db, attachment_id, for_user_id=user.id)


@router.get("/{attachment_id}/download")
def download_attachment(
    attachment_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    obj = _scoped_get_attachment(db, attachment_id, for_user_id=user.id)
    path = _resolve_path(obj.filedata)
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment file missing on disk")
    return FileResponse(path, media_type=obj.mime_type, filename=obj.original_filename)
