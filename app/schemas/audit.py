from datetime import datetime

from pydantic import BaseModel

from app.models import AuditAction
from app.schemas.common import ORMModel


class AuditLogRead(ORMModel):
    id: int
    table_name: str
    record_id: int
    action: AuditAction
    actor_id: int | None = None
    changed_fields: dict | None = None
    old_value: dict | None = None
    new_value: dict | None = None
    created_at: datetime
