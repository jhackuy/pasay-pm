import uuid
from pathlib import Path

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

router = APIRouter(prefix="/attachments", tags=["attachments"])


def _get_or_404(db: Session, attachment_id: int) -> Attachment:
    obj = db.query(Attachment).filter(Attachment.id == attachment_id).first()
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    return obj


def _resolve_path(filedata: str) -> Path:
    path = Path(filedata)
    if not path.is_absolute():
        path = Path(settings.upload_dir) / path
    return path


@router.get("", response_model=Paginated[AttachmentRead])
def list_attachments(
    related_type: str | None = Query(default=None, max_length=50),
    related_id: int | None = Query(default=None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    query = db.query(Attachment)
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
    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}_{Path(file.filename or 'file').name}"
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
    attachment_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)
):
    return _get_or_404(db, attachment_id)


@router.get("/{attachment_id}/download")
def download_attachment(
    attachment_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)
):
    obj = _get_or_404(db, attachment_id)
    path = _resolve_path(obj.filedata)
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment file missing on disk")
    return FileResponse(path, media_type=obj.mime_type, filename=obj.original_filename)
