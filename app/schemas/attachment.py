from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import AuditFields


class AttachmentRead(AuditFields):
    id: int
    filedata: str
    original_filename: str
    mime_type: str | None = None
    related_type: str | None = None
    related_id: int | None = None
    uploaded_by: int | None = None
    uploaded_at: datetime | None = None
