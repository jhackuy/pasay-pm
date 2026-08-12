"""Audit log helpers: row serialization + record_audit."""
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from contextvars import ContextVar

from sqlalchemy.orm import Session

from app.models.audit_log import AuditAction, AuditLog
from app.models.identity import ApiCredential, Principal

audit_context: ContextVar[tuple[int | None, int | None, int | None, str | None]] = ContextVar(
    "audit_context", default=(None, None, None, None))
AUDIT_SESSION_KEY = "pasay.audit_context"


def set_audit_context(
    db: Session,
    value: tuple[int | None, int | None, int | None, str | None],
) -> None:
    """Bind provenance to both execution context and the request DB session.

    FastAPI may execute a synchronous dependency and endpoint in separate
    copied contexts, so ContextVar writes alone are not a reliable hand-off.
    The request-scoped SQLAlchemy session is shared across both layers and is
    therefore the canonical transport; the ContextVar remains compatible with
    direct service calls and async code.
    """
    audit_context.set(value)
    db.info[AUDIT_SESSION_KEY] = value


def current_audit_context(
    db: Session,
) -> tuple[int | None, int | None, int | None, str | None]:
    return db.info.get(AUDIT_SESSION_KEY, audit_context.get())


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
    """Snapshot a SQLAlchemy row as a plain dict.

    Column names are used as dict keys; the mapped attribute name resolves
    the value (handles columns whose attribute differs, e.g. the reserved
    ``metadata`` column mapped to a ``details`` attribute).
    """
    mapper = obj.__mapper__
    out = {}
    for column in obj.__table__.columns:
        key = column.name
        try:
            key = mapper.get_property_by_column(column).key
        except Exception:  # noqa: BLE001 - fall back to the column name
            pass
        out[column.name] = jsonable(getattr(obj, key))
    return out


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
    subject_principal_id, caller_principal_id, credential_id, channel = (
        current_audit_context(db)
    )
    # ContextVars intentionally cross helper layers, but a recycled worker/test
    # context must never attach stale foreign keys from a prior transaction.
    if subject_principal_id is not None and db.get(Principal, subject_principal_id) is None:
        subject_principal_id = None
    if caller_principal_id is not None and db.get(Principal, caller_principal_id) is None:
        caller_principal_id = None
    if credential_id is not None and db.get(ApiCredential, credential_id) is None:
        credential_id = None
    entry = AuditLog(
        table_name=table_name,
        record_id=record_id,
        action=AuditAction(action),
        actor_id=actor_id,
        changed_fields=changed_fields,
        old_value=old_value,
        new_value=new_value,
        subject_principal_id=subject_principal_id,
        caller_principal_id=caller_principal_id,
        credential_id=credential_id,
        channel=channel,
    )
    db.add(entry)
    return entry
