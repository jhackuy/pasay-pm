"""Idempotent production-data backfill (V1.2 data-hardening).

Two cooperating, re-runnable operations:

- ``backfill_unassigned_business_tasks``: assigns the safe default assignee to every
  PENDING business-source task that currently has no assignee, then re-enqueues missing
  proactive notifications — all in ONE transaction. Never overwrites an already-assigned
  task and is SAFE TO RE-RUN: a second call finds zero unassigned business tasks and does
  nothing.
- ``enqueue_missing_notifications``: re-enqueues the proactive notification for every
  PENDING business task that is assigned but has no SENT outbox row. The
  ``uq_notification_outbox_dedupe`` unique index makes it idempotent AND concurrency-safe
  (concurrent processes can only ever insert one row per dedupe_key).

These never touch the financial routers / sources of truth — they only assign ownership
and re-queue notifications through the outbox, always via ``enqueue_notification`` (never
a direct Telegram sendMessage).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.operations import (
    NotificationOutbox,
    NotificationStatus,
    OperationalTask,
    OperationalTaskStatus,
)
from app.models.user import User
from app.services.audit import record_audit, serialize_row
from app.services.operations.assignee import validate_default_assignee
from app.services.operations.config import NOTIFY_CHANNEL_TELEGRAM
from app.services.operations.generation import BUSINESS_SOURCE_TYPES, _notification_message
from app.services.operations.outbox import enqueue_notification, resolve_recipient
from app.services.identity import bind_internal_audit

# System actor for backfill audit rows. Falls back to the validated default assignee id
# when ``actor_id`` is not provided (so the row always carries who owns it afterwards).
_DEFAULT_BACKFILL_REASON = "backfill:default_assignee:v1.2.0"


@dataclass
class BackfillReport:
    """Human-readable summary of one backfill run (safe to serialize to JSON)."""

    tasks_backfilled: list[int] = field(default_factory=list)
    tasks_skipped_already_assigned: int = 0
    tasks_missing_notification: list[int] = field(default_factory=list)
    notifications_enqueued: int = 0


def backfill_unassigned_business_tasks(
    db: Session,
    *,
    default_assignee_id: int,
    now: datetime | None = None,
    actor_id: int | None = None,
    reason: str = _DEFAULT_BACKFILL_REASON,
) -> BackfillReport:
    """Assign the default assignee + re-enqueue notifications, in one transaction.

    Idempotent: re-running finds zero unassigned business PENDING tasks and does not
    touch already-assigned tasks. Commits before returning (the caller treats this as
    one unit).
    """
    now = now or datetime.now(timezone.utc)
    validate_default_assignee(db, default_assignee_id)
    bind_internal_audit(db, "backfill")

    report = BackfillReport()

    # --- 1) Assign the default owner to unassigned business PENDING tasks ---------
    # Scan ALL PENDING business tasks so the report can count already-assigned ones.
    candidates = (
        db.query(OperationalTask)
        .filter(
            OperationalTask.status == OperationalTaskStatus.PENDING,
            OperationalTask.source_type.in_(BUSINESS_SOURCE_TYPES),
        )
        .all()
    )
    for task in candidates:
        if task.assigned_user_id is not None:
            report.tasks_skipped_already_assigned += 1
            continue
        before = serialize_row(task)
        # Conditional UPDATE: only wins if still unassigned (safe under concurrency).
        result = db.execute(
            update(OperationalTask)
            .where(
                OperationalTask.id == task.id,
                OperationalTask.status == OperationalTaskStatus.PENDING,
                OperationalTask.assigned_user_id.is_(None),
            )
            .values(assigned_user_id=default_assignee_id, updated_at=now),
            execution_options={"synchronize_session": False},
        )
        if result.rowcount != 1:
            report.tasks_skipped_already_assigned += 1  # a concurrent run claimed it
            continue
        task.assigned_user_id = default_assignee_id
        task.updated_at = now
        record_audit(
            db,
            table_name="operational_tasks",
            record_id=task.id,
            action="task_backfilled",
            actor_id=actor_id,
            changed_fields={"assigned_user_id": [None, default_assignee_id], "reason": reason},
            old_value=before,
            new_value=serialize_row(task),
        )
        report.tasks_backfilled.append(task.id)

    # --- 2) Re-enqueue missing notifications for assigned business PENDING tasks ---
    missing, enqueued = _select_and_enqueue_missing(db, report_notifications=True)
    report.tasks_missing_notification = missing
    report.notifications_enqueued = enqueued

    db.commit()
    return report


def enqueue_missing_notifications(
    db: Session,
    *,
    channel: str = NOTIFY_CHANNEL_TELEGRAM,
    now: datetime | None = None,
) -> tuple[list[int], int]:
    """Re-enqueue proactive notifications for assigned business tasks lacking a SENT one.

    Idempotent + concurrency-safe via ``uq_notification_outbox_dedupe``: an existing row
    (PENDING/SENT/FAILED/DROPPED) always makes ``enqueue_notification`` return False, so
    no duplicate is ever created — even when two processes run concurrently. Commits.

    Returns (task_ids_missing_a_sent_notification, notifications_enqueued_this_run).
    """
    _ = now  # kept for a stable signature / potential future timestamped payloads
    missing, enqueued = _select_and_enqueue_missing(db, channel=channel, report_notifications=False)
    db.commit()
    return missing, enqueued


def _select_and_enqueue_missing(
    db: Session, *, channel: str = NOTIFY_CHANNEL_TELEGRAM, report_notifications: bool
) -> tuple[list[int], int]:
    """Find assigned business PENDING tasks without a SENT outbox and enqueue them.

    ``report_notifications`` controls whether the returned list is the full set of
    candidates (for the backfill report) or only the ones actually enqueued this run
    (backfill semantics keep them distinct).
    """
    # tasks that already have a SENT notification outbox for telegram.
    sent_task_ids = select(NotificationOutbox.task_id).where(
        NotificationOutbox.status == NotificationStatus.SENT,
        NotificationOutbox.task_id.is_not(None),
    )
    candidates = (
        db.query(OperationalTask)
        .filter(
            OperationalTask.status == OperationalTaskStatus.PENDING,
            OperationalTask.source_type.in_(BUSINESS_SOURCE_TYPES),
            OperationalTask.assigned_user_id.is_not(None),
            OperationalTask.id.not_in(sent_task_ids),
        )
        .all()
    )
    missing_ids: list[int] = [t.id for t in candidates]
    enqueued = 0
    for task in candidates:
        recipient = resolve_recipient(db, task.assigned_user_id)
        if recipient is None:
            continue  # no recipient resolvable -> nothing to enqueue
        payload = {
            "task_id": task.id,
            "task_type": task.task_type.value,
            "title": task.title,
            "due_at": task.due_at.isoformat(),
            "message": _notification_message(task),
        }
        if enqueue_notification(
            db,
            task_id=task.id,
            channel=channel,
            recipient=recipient,
            payload=payload,
            dedupe_key=f"task:{task.id}:{channel}:{recipient}",
        ):
            enqueued += 1
    return missing_ids, enqueued
