"""Snooze redelivery: the reliable delayed-reminder loop (V1.2.2 Phase A).

Gap this closes: snooze only stored ``snoozed_until`` and nothing proactively
redelivered a notification when the window elapsed. This module adds the
missing worker step:

    task snoozed (snoozed_until in DB)
    -> scheduler pass: PENDING task with snoozed_until <= now
    -> atomic claim (conditional UPDATE) + outbox enqueue in ONE transaction
    -> notifier pass claims + sends via the EXISTING outbox/retry path
    -> re-run worker = no duplicate (unique dedupe_key per snooze window)

Safety properties:
- DB-level dedupe: ``uq_notification_outbox_dedupe`` guards the enqueue, so
  overlapping scheduler passes / multiple instances can only land once.
- Worker restart-safe / multi-instance safe: nothing lives in Python memory;
  the claim is a conditional UPDATE (``status=PENDING AND snoozed_until=<seen>``)
  so only one worker wins a window; losers see rowcount 0 and skip.
- Repeated snoozes: the dedupe key embeds the exact ``snoozed_until`` window,
  so each window is independent. Any already-enqueued-but-unsent reminder for
  an older window is suppressed by ``suppress_pending_redeliveries`` (called on
  re-snooze / complete / cancel) and by the notifier's send-time guard.
- Suppression on complete/cancel/reconcile: the scan only ever selects
  PENDING tasks, and the scheduler runs reconcile BEFORE the scan, so a task
  settled in the same pass is never redelivered.
- Every state change is audited via ``record_audit`` (actor_id=None, system).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.operations import (
    NotificationOutbox,
    NotificationStatus,
    OperationalTask,
    OperationalTaskStatus,
)
from app.services.audit import record_audit, serialize_row
from app.services.operations.config import (
    NOTIFY_CHANNEL_TELEGRAM,
    SNOOZE_REDELIVERY_BATCH,
    SNOOZE_REDELIVERY_KEY_PREFIX,
)
from app.services.operations.outbox import enqueue_notification, resolve_recipient


def _utc_iso(value: datetime) -> str:
    """Stable ISO-8601 key component: always UTC, regardless of session tz."""
    return value.astimezone(timezone.utc).isoformat()


def snooze_redelivery_dedupe_key(
    task_id: int, snoozed_until: datetime, generation: int = 0
) -> str:
    """One dedupe key per task per (reminder generation, snooze window).

    The window is ISO-8601 normalized to UTC so the key is identical no matter
    which session timezone writes it. The generation (bumped on every snooze /
    complete / cancel) makes the logical identity ``(task, generation, window)``
    — so a DROPPED old-generation row for a window can never block a NEW
    generation enqueued for the same window.
    """
    return (
        f"{SNOOZE_REDELIVERY_KEY_PREFIX}{task_id}:{generation}:"
        f"{snoozed_until.astimezone(timezone.utc).isoformat()}"
    )


def is_snooze_redelivery_key(dedupe_key: str | None) -> bool:
    return bool(dedupe_key) and dedupe_key.startswith(SNOOZE_REDELIVERY_KEY_PREFIX)


def redeliver_due_snoozes(
    db: Session, *, now: datetime | None = None, batch: int = SNOOZE_REDELIVERY_BATCH,
    org_id: int | None = None,
) -> int:
    """Enqueue one reminder per due snooze window; returns how many fired.

    Runs inside the caller's transaction (the scheduler commits). Only PENDING
    tasks are considered. For each candidate the claim is a conditional UPDATE
    matching the exact ``snoozed_until`` the scan observed, so concurrent
    passes / instances cannot double-fire a window; the outbox unique index is
    a second, independent guard.

    ``org_id`` fail-closes the candidate scan via the canonical 3-channel OR
    (property / lease / tenant → organization); None preserves the global
    standalone-worker behavior.
    """
    now = now or datetime.now(timezone.utc)
    filters = [
        OperationalTask.status == OperationalTaskStatus.PENDING,
        OperationalTask.snoozed_until.is_not(None),
        OperationalTask.snoozed_until <= now,
    ]
    if org_id is not None:
        from app.services.operations.summary import _scoped_task_query
        filters.append(_scoped_task_query(db, org_id))
    query = (
        db.query(OperationalTask)
        .filter(*filters)
        .order_by(OperationalTask.snoozed_until, OperationalTask.id)
        .limit(batch)
    )
    candidates = query.all()
    redelivered = 0
    for task in candidates:
        window = task.snoozed_until
        if window is None:
            continue
        before = serialize_row(task)
        # Atomic claim: only wins while the task is still PENDING with the same
        # snooze window (a concurrent re-snooze / complete / cancel no-ops).
        result = db.execute(
            update(OperationalTask)
            .where(
                OperationalTask.id == task.id,
                OperationalTask.status == OperationalTaskStatus.PENDING,
                OperationalTask.snoozed_until == window,
            )
            .values(snoozed_until=None, updated_at=now),
            execution_options={"synchronize_session": False},
        )
        if result.rowcount != 1:
            continue
        task.snoozed_until = None
        task.updated_at = now

        recipient = resolve_recipient(db, task.assigned_user_id)
        if recipient is None:
            # No recipient resolvable -> nothing to deliver; the window is
            # still consumed so the task re-enters the normal board.
            _audit_redelivery(db, task, window, now, enqueued=False, before=before)
            continue
        generation = task.reminder_generation
        dedupe_key = snooze_redelivery_dedupe_key(task.id, window, generation=generation)
        enqueued = enqueue_notification(
            db,
            task_id=task.id,
            channel=NOTIFY_CHANNEL_TELEGRAM,
            recipient=recipient,
            payload={
                "task_id": task.id,
                "task_type": task.task_type.value,
                "title": task.title,
                "due_at": task.due_at.isoformat(),
                "snooze_window": _utc_iso(window),
                "reminder_generation": generation,
                "message": f"🔔 待办提醒（继续）\n{task.title}",
            },
            dedupe_key=dedupe_key,
        )
        _audit_redelivery(db, task, window, now, enqueued=enqueued, before=before)
        if enqueued:
            redelivered += 1
    return redelivered


def _audit_redelivery(
    db: Session, task: OperationalTask, window: datetime, now: datetime, *,
    enqueued: bool, before: dict,
) -> None:
    record_audit(
        db,
        table_name="operational_tasks",
        record_id=task.id,
        action="task_reminder_redelivered",
        actor_id=None,  # system / scheduler
        changed_fields={
            "snoozed_until": [_utc_iso(window), None],
            "notification_enqueued": enqueued,
        },
        old_value=before,
        new_value=serialize_row(task),
    )


def suppress_pending_redeliveries(
    db: Session, task_id: int, *, actor_id: int | None, reason: str, now: datetime | None = None
) -> int:
    """DROPPED any PENDING snooze-redelivery outbox rows for a task.

    Called when a task is re-snoozed (an old window must never fire), completed
    or cancelled (no further reminders), so an already-enqueued-but-unsent
    reminder can never reach Telegram. Audited (actor = the user or None for
    system transitions). Returns how many rows were dropped.
    """
    now = now or datetime.now(timezone.utc)
    prefix = f"{SNOOZE_REDELIVERY_KEY_PREFIX}{task_id}:"
    rows = (
        db.query(NotificationOutbox)
        .filter(
            NotificationOutbox.task_id == task_id,
            NotificationOutbox.status == NotificationStatus.PENDING,
            NotificationOutbox.dedupe_key.like(prefix + "%"),
        )
        .all()
    )
    dropped = 0
    for row in rows:
        old = serialize_row(row)
        row.status = NotificationStatus.DROPPED
        row.updated_at = now
        record_audit(
            db,
            table_name="notification_outbox",
            record_id=row.id,
            action="outbox_dropped",
            actor_id=actor_id,
            changed_fields={"status": ["PENDING", "DROPPED"], "reason": reason},
            old_value=old,
            new_value=serialize_row(row),
        )
        dropped += 1
    return dropped
