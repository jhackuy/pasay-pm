"""Audit log helpers: row serialization + record_audit."""
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy.orm import Session

from app.models.audit_log import AuditAction, AuditLog


def jsonable(value):
    """Convert a model attribute into a JSONB-safe value."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def serialize_row(obj) -> dict:
    """Snapshot a SQLAlchemy row as a plain dict."""
    return {c.name: jsonable(getattr(obj, c.name)) for c in obj.__table__.columns}


def field_changes(obj, updates: dict) -> dict:
    """Build {field: [old, new]} for changed fields from an update payload."""
    changes = {}
    for field, new_value in updates.items():
        old_value = getattr(obj, field, None)
        if old_value != new_value:
            changes[field] = [jsonable(old_value), jsonable(new_value)]
    return changes


def record_audit(
    db: Session,
    *,
    table_name: str,
    record_id: int,
    action: AuditAction | str,
    actor_id: int | None = None,
    changed_fields: dict | None = None,
    old_value: dict | None = None,
    new_value: dict | None = None,
) -> AuditLog:
    """Persist one audit log row. The caller is responsible for committing."""
    entry = AuditLog(
        table_name=table_name,
        record_id=record_id,
        action=AuditAction(action),
        actor_id=actor_id,
        changed_fields=changed_fields,
        old_value=old_value,
        new_value=new_value,
    )
    db.add(entry)
    return entry
