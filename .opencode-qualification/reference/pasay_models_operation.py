"""PASAY reference implementation — Operation / Task / TaskTransition ORM models.

Issue #99 / PR #100 /oc continuation. Staged under
``.opencode-qualification/reference/`` pending Owner-side promotion to
``app/models/operation.py`` and ``app/models/task.py``.

Entities in this file:
    * Operation       — Truth-bearing record. Moves to COMPLETED only when
                        the underlying real-world problem is resolved.
    * Task            — Projection of human action on an Operation.
    * TaskTransition  — append-only audit log of every Task state change.

Truth hierarchy (PRODUCT_RULES.md §1):
    * Reminder sent, Owner replied, Notification generated != Operation CLOSED.
    * Operation CLOSED only when the real-world truth flips.
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pasay_db_layer import AuditMixin, Base, OrgScopedMixin


class OperationKindEnum(str, enum.Enum):
    RENT_DUE = "RENT_DUE"
    EXPENSE_DUE = "EXPENSE_DUE"
    REPAIR = "REPAIR"
    INSPECTION = "INSPECTION"
    RENEWAL = "RENEWAL"
    MOVE_OUT = "MOVE_OUT"
    OTHER = "OTHER"


class OperationStateEnum(str, enum.Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class TaskStateEnum(str, enum.Enum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class Operation(Base, AuditMixin, OrgScopedMixin):
    """Truth-bearing record. COMPLETED ⇔ real-world resolution."""

    __tablename__ = "operations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[OperationKindEnum] = mapped_column(
        SAEnum(
            OperationKindEnum,
            name="operation_kind_enum",
            native_enum=False,
            length=32,
        ),
        nullable=False,
        index=True,
    )
    state: Mapped[OperationStateEnum] = mapped_column(
        SAEnum(
            OperationStateEnum,
            name="operation_state_enum",
            native_enum=False,
            length=16,
        ),
        nullable=False,
        server_default=text("'PENDING'"),
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    related_lease_id: Mapped[Optional[str]] = mapped_column(
        String(64),
        ForeignKey("leases.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    related_unit_id: Mapped[Optional[str]] = mapped_column(
        String(64),
        ForeignKey("units.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    related_repair_id: Mapped[Optional[str]] = mapped_column(
        String(64),
        ForeignKey("repairs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    related_tenant_id: Mapped[Optional[str]] = mapped_column(
        String(64),
        ForeignKey("tenants.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    due_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_by_user_id: Mapped[Optional[str]] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )

    tasks: Mapped[list["Task"]] = relationship(
        back_populates="operation", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "(state = 'COMPLETED' AND completed_at IS NOT NULL) OR "
            "(state <> 'COMPLETED')",
            name="ck_operations_completed_at",
        ),
        Index(
            "ix_operations_org_state",
            "org_id",
            "state",
        ),
    )


class Task(Base, AuditMixin, OrgScopedMixin):
    """Projection of human action on an Operation.

    Task state changes are NEVER the source of truth. Operation state is.
    A Task can be DONE while the Operation is still PENDING waiting on
    external evidence (e.g. rent bank transfer).
    """

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    operation_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("operations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    state: Mapped[TaskStateEnum] = mapped_column(
        SAEnum(
            TaskStateEnum,
            name="task_state_enum",
            native_enum=False,
            length=16,
        ),
        nullable=False,
        server_default=text("'OPEN'"),
    )
    assignee_user_id: Mapped[Optional[str]] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    due_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    done_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    operation: Mapped["Operation"] = relationship(back_populates="tasks")
    transitions: Mapped[list["TaskTransition"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "(state = 'DONE' AND done_at IS NOT NULL) OR (state <> 'DONE')",
            name="ck_tasks_done_at",
        ),
    )


class TaskTransition(Base, AuditMixin, OrgScopedMixin):
    """Append-only audit log of every Task state change.

    Insert-only. UPDATEs and DELETEs are forbidden at the service layer
    so the audit trail is immutable.
    """

    __tablename__ = "task_transitions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    from_state: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    to_state: Mapped[str] = mapped_column(String(16), nullable=False)
    transitioned_by_user_id: Mapped[Optional[str]] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    task: Mapped["Task"] = relationship(back_populates="transitions")

    __table_args__ = (
        CheckConstraint("length(to_state) > 0", name="ck_task_trans_to_nonempty"),
        Index(
            "ix_task_transitions_task_time",
            "task_id",
            "created_at",
        ),
    )


__all__ = [
    "OperationKindEnum",
    "OperationStateEnum",
    "TaskStateEnum",
    "Operation",
    "Task",
    "TaskTransition",
]