from datetime import datetime
from enum import Enum

from sqlalchemy import BigInteger, DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import AuditMixin, Base, pg_enum


class AuditAction(str, Enum):
    # V1.1 values (never reorder / rename — old audit rows reference them).
    create = "create"
    update = "update"
    soft_delete = "soft_delete"
    confirm = "confirm"
    approve = "approve"
    reject = "reject"
    pay = "pay"
    reverse = "reverse"
    # V1.2 PROACTIVE OPERATIONS values (appended only).
    task_created = "task_created"
    task_completed = "task_completed"
    task_cancelled = "task_cancelled"
    task_snoozed = "task_snoozed"
    rule_created = "rule_created"
    rule_updated = "rule_updated"
    rule_disabled = "rule_disabled"
    task_auto_completed = "task_auto_completed"
    task_auto_cancelled = "task_auto_cancelled"
    task_backfilled = "task_backfilled"
    # V1.2.2 OPS COPILOT values (appended only).
    task_reminder_redelivered = "task_reminder_redelivered"
    outbox_dropped = "outbox_dropped"
    copilot_context_built = "copilot_context_built"
    copilot_proposal_created = "copilot_proposal_created"
    copilot_proposal_confirmed = "copilot_proposal_confirmed"
    copilot_proposal_cancelled = "copilot_proposal_cancelled"
    copilot_proposal_expired = "copilot_proposal_expired"
    copilot_proposal_confirm_rejected = "copilot_proposal_confirm_rejected"


class AuditLog(AuditMixin, Base):
    __tablename__ = "audit_logs"

    table_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    record_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    action: Mapped[AuditAction] = mapped_column(
        pg_enum(AuditAction, "audit_action"), nullable=False
    )
    actor_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    changed_fields: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    old_value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
