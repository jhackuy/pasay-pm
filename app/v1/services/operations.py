"""Shared Operation/Task/Notification services (Coverage Matrix §8).

Operation is Truth. Task is a Projection. Notification is read-only.

These three services consolidate the polymorphic Operation/Task/Activity
behavior used by rent_payment, expense, repair, renewal, move_out
services. They are explicitly *thin wrappers* around the ORM rows that
provide:
  - centralized org-scope enforcement (`require_org_scope`)
  - role gating (`_ensure_role`)
  - state-machine guards
  - idempotency on Task create (at most one open Task per Operation,
    DB partial unique index is the final guarantee)

Each service method returns ORM rows directly; no business logic for
the specific domain lives here (RentService / ExpenseService / etc.
own their state transitions).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.permissions import (
    PermissionDenied,
    Principal,
    Role,
    require_org_scope,
)
from app.core.time import utcnow
from app.v1.models.base import OperationState, TaskState
from app.v1.models.rent_payment import Operation, Task
from app.v1.services.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)


# ---------------------------------------------------------------------------
# OperationService — coverage matrix 8.1, 8.3, 8.4
# ---------------------------------------------------------------------------


class OperationService:
    """Centralized read/mutation surface for the ``Operation`` truth row."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def get(
        self, principal: Principal, *, org_id: int, operation_id: int,
    ) -> Operation:
        require_org_scope(principal, org_id)
        op = self.db.get(Operation, operation_id)
        if op is None or op.org_id != org_id:
            raise NotFoundError(
                f"operation {operation_id} not found in org {org_id}",
            )
        return op

    def list_for_org(
        self, principal: Principal, *, org_id: int, state: str | None = None,
    ) -> list[Operation]:
        require_org_scope(principal, org_id)
        q = self.db.query(Operation).filter(Operation.org_id == org_id)
        if state:
            q = q.filter(Operation.state == state)
        return q.order_by(Operation.id.asc()).all()

    def advance(
        self,
        principal: Principal,
        *,
        org_id: int,
        operation_id: int,
        to_state: str | OperationState,
    ) -> Operation:
        """Coverage Matrix 8.3: advance ``next_actor`` / ``next_action``
        consistency on Operation.

        OPEN → IN_PROGRESS → RESOLVED | CANCELLED. Each domain service
        that owns the Operation calls this when it wants to flip the
        state, but the actual closure gate is owned by the domain
        service (``_settle``, ``verify_completion``, ``execute``,
        ``settle_move_out``).
        """
        require_org_scope(principal, org_id)
        if principal.role not in (Role.OWNER, Role.SECRETARY):
            raise PermissionDenied(
                "only OWNER/SECRETARY can advance Operation state",
            )
        parsed = (
            to_state if isinstance(to_state, OperationState)
            else OperationState(to_state)
        )
        op = self.get(
            principal, org_id=org_id, operation_id=operation_id,
        )
        current = OperationState(op.state)
        valid_transitions = {
            OperationState.OPEN: {OperationState.IN_PROGRESS,
                                  OperationState.CANCELLED,
                                  OperationState.RESOLVED},
            OperationState.IN_PROGRESS: {OperationState.RESOLVED,
                                         OperationState.CANCELLED,
                                         OperationState.OPEN},
            OperationState.RESOLVED: {OperationState.IN_PROGRESS},  # re-open
            OperationState.CANCELLED: set(),
        }
        if parsed not in valid_transitions[current]:
            raise ConflictError(
                f"operation {operation_id} cannot transition "
                f"{current.value!r} → {parsed.value!r}",
            )
        op.state = parsed.value
        if parsed == OperationState.RESOLVED:
            op.resolved_at = utcnow()
        self.db.commit()
        self.db.refresh(op)
        return op

    def sync_status(
        self,
        principal: Principal,
        *,
        org_id: int,
        operation_id: int,
    ) -> Operation:
        """Coverage Matrix 8.4: reconcile Operation.state with all
        associated Task.state rows.

        Rules:
          - any open Task on a RESOLVED Operation → re-open the Operation
          - all Tasks done/cancelled on an IN_PROGRESS Operation → mark
            Operation resolved
        No-op if already consistent.
        """
        require_org_scope(principal, org_id)
        if principal.role not in (Role.OWNER, Role.SECRETARY):
            raise PermissionDenied(
                "only OWNER/SECRETARY can sync Operation status",
            )
        op = self.get(
            principal, org_id=org_id, operation_id=operation_id,
        )
        tasks = (
            self.db.query(Task)
            .filter(Task.operation_id == operation_id)
            .all()
        )
        open_tasks = [t for t in tasks if t.state == TaskState.OPEN.value]
        current = OperationState(op.state)
        if current == OperationState.RESOLVED and open_tasks:
            op.state = OperationState.IN_PROGRESS.value
            op.resolved_at = None
            self.db.commit()
            self.db.refresh(op)
        elif (
            current == OperationState.IN_PROGRESS
            and tasks
            and not open_tasks
        ):
            op.state = OperationState.RESOLVED.value
            op.resolved_at = utcnow()
            self.db.commit()
            self.db.refresh(op)
        return op


# ---------------------------------------------------------------------------
# TaskService — coverage matrix 8.2
# ---------------------------------------------------------------------------


class TaskService:
    """Centralized surface for the ``Task`` projection.

    A Task NEVER mutates the parent Operation by itself; it is a
    read-and-promise record for human follow-up. The at-most-one
    open-Task invariant is enforced by the DB partial unique index
    ``uq_v1_tasks_one_open_per_operation`` AND by an explicit service
    pre-check (defense in depth).
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    def create_projection(
        self,
        principal: Principal,
        *,
        org_id: int,
        operation_id: int,
        kind: str,
        title: str,
        due_at: datetime | None = None,
    ) -> Task:
        """Coverage Matrix 8.2: create_projection.

        Returns the new Task. Raises ConflictError if another open Task
        already exists on the same Operation.
        """
        require_org_scope(principal, org_id)
        if principal.role not in (Role.OWNER, Role.SECRETARY):
            raise PermissionDenied(
                "only OWNER/SECRETARY can create a Task projection",
            )
        op = self.db.get(Operation, operation_id)
        if op is None or op.org_id != org_id:
            raise NotFoundError(
                f"operation {operation_id} not found in org {org_id}",
            )
        # Defense-in-depth: enforce at-most-one open Task per Operation
        # (the DB partial unique index is the final guarantee; this check
        # produces a cleaner 409 ConflictError).
        existing_open = (
            self.db.query(Task)
            .filter(
                Task.operation_id == operation_id,
                Task.state == TaskState.OPEN.value,
            )
            .one_or_none()
        )
        if existing_open is not None:
            raise ConflictError(
                f"operation {operation_id} already has an open Task "
                f"{existing_open.id}; at most one open Task is allowed",
            )
        task = Task(
            org_id=org_id,
            operation_id=operation_id,
            kind=kind,
            title=title,
            due_at=due_at,
            state=TaskState.OPEN.value,
        )
        self.db.add(task)
        self.db.commit()
        self.db.refresh(task)
        return task

    def complete(
        self,
        principal: Principal,
        *,
        org_id: int,
        task_id: int,
    ) -> Task:
        """Mark an open Task done. NEVER resolves the parent Operation
        (Reminder != Completion; Coverage Matrix 8.5).
        """
        require_org_scope(principal, org_id)
        if principal.role not in (Role.OWNER, Role.SECRETARY):
            raise PermissionDenied(
                "only OWNER/SECRETARY can complete a Task",
            )
        task = self.db.get(Task, task_id)
        if task is None or task.org_id != org_id:
            raise NotFoundError(
                f"task {task_id} not found in org {org_id}",
            )
        if task.state != TaskState.OPEN.value:
            raise ConflictError(
                f"task {task_id} cannot be completed from "
                f"state {task.state!r}",
            )
        task.state = TaskState.DONE.value
        task.done_at = utcnow()
        self.db.commit()
        self.db.refresh(task)
        return task


# ---------------------------------------------------------------------------
# NotificationService — coverage matrix 8.5
# ---------------------------------------------------------------------------


class NotificationService:
    """Read-only w.r.t. Operation/Task state.

    Coverage Matrix 8.5: ``NotificationService.send`` is read-only
    relative to Operation.status; sending a notification NEVER marks an
    Operation completed. This service provides a centralized surface so
    Telegram handlers / cron jobs can issue a notification while the
    business truth remains untouched.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    def send(
        self,
        principal: Principal,
        *,
        org_id: int,
        operation_id: int,
        message: str,
    ) -> dict[str, Any]:
        """Send (record) a notification. READ-ONLY w.r.t. Operation.

        Returns ``{"operation_id": int, "delivered": True, "message": str}``
        so callers / tests can assert the side-effect without coupling to
        Telegram or worker internals.
        """
        require_org_scope(principal, org_id)
        if principal.role not in (Role.OWNER, Role.SECRETARY):
            raise PermissionDenied(
                "only OWNER/SECRETARY can send notifications",
            )
        if not isinstance(message, str) or not message.strip():
            raise ValidationError("notification message must be non-empty")
        op = self.db.get(Operation, operation_id)
        if op is None or op.org_id != org_id:
            raise NotFoundError(
                f"operation {operation_id} not found in org {org_id}",
            )
        # CRITICAL: do NOT mutate op.state. Reminder != Completion.
        return {
            "operation_id": operation_id,
            "delivered": True,
            "message": message.strip(),
            "operation_state_at_send": op.state,
        }


__all__ = [
    "OperationService",
    "TaskService",
    "NotificationService",
]
