"""PASAY-VNEXT-FOUNDATION-LEGACY-001 — minimal one-way canonical→legacy projection.

This module owns the **only** place canonical Repair / Expense domain code is
allowed to create / complete legacy ``operational_tasks`` rows. Two small,
explicit seams:

- ``project_active_repair_action``: create (or reuse an active sibling by
  dedupe) one task for an ACTIVE ``RepairAction`` bound to a still-ALIVE
  ``RepairOperation``. Returns ``(None, False)`` without a write when the
  source is inactive / wrong-repair / closed / or when an ACTIVE sibling
  already exists.

- ``close_active_projections``: transition ACTIVE task(s) to COMPLETED, bump
  ``reminder_generation``, suppress pending redeliveries, audit under an
  EXISTING audit action (default ``task_completed``; callers may pass
  legacy labels like ``task_completed_via_approval``).

Provenance travels in ``task.details["canonical_projection"]`` and in
``changed_fields["source_domain"] / "reason"`` — durable markers on the row
itself, no new ``AuditAction`` enum slot, no production migration.

Reads (``query(OperationalTask)``) are not a violation; only writes must
flow through these seams.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy.orm import Session

from app.models.operations import (
    OperationalTask,
    OperationalTaskPriority,
    OperationalTaskStatus,
    OperationalTaskType,
)
from app.models.repair import (
    RepairAction,
    RepairActionStatus,
    RepairOperation,
    RepairOperationStatus,
)
from app.services.audit import record_audit, serialize_row
from app.services.operations.generation import create_operational_task
from app.services.operations.redelivery import suppress_pending_redeliveries


# Canonical-rejection keywords (these cannot appear as column names on
# ``OperationalTask``; ``create_operational_task`` rejects them if passed
# inside ``fields`` as a SQLAlchemy ``**kwargs``).
_NON_COLUMN_FIELDS = frozenset({"notification_message", "reply_markup"})


# ---------------------------------------------------------------------------
# Repair-side projection (validated create)
# ---------------------------------------------------------------------------


def _action_may_project(action: RepairAction, *, repair_id: int) -> bool:
    """Action must be ACTIVE and bound to this repair."""
    if action.status not in (
        RepairActionStatus.PENDING,
        RepairActionStatus.IN_PROGRESS,
    ):
        return False
    return action.repair_id == repair_id


def _repair_is_alive(repair: RepairOperation) -> bool:
    """A projection is one-way. A CLOSED/CANCELLED repair must never spawn
    new legacy tasks — even if a stale Action row still looks ACTIVE."""
    return repair.status not in (
        RepairOperationStatus.CLOSED,
        RepairOperationStatus.CANCELLED,
    )


def _dedupe_key_for(repair: RepairOperation) -> str:
    return f"repair-requote:{repair.id}"


def _strip_non_columns(fields: dict) -> tuple[dict, dict]:
    """Pop non-column kwargs (notification_message, reply_markup) so the
    remaining dict is safe to pass as ``OperationalTask(**fields)``."""
    extras = {k: fields.pop(k) for k in _NON_COLUMN_FIELDS if k in fields}
    return fields, extras


def _existing_active(db: Session, dedupe_key: str) -> OperationalTask | None:
    """Pre-read the DB partial index widener: PENDING OR IN_PROGRESS. The
    DB index only protects PENDING, so an IN_PROGRESS sibling would slip
    past ON CONFLICT and produce a duplicate PENDING row."""
    return (
        db.query(OperationalTask)
        .filter(
            OperationalTask.dedupe_key == dedupe_key,
            OperationalTask.status.in_(
                [OperationalTaskStatus.PENDING, OperationalTaskStatus.IN_PROGRESS]
            ),
        )
        .first()
    )


def project_active_repair_action(
    db: Session,
    repair: RepairOperation,
    action: RepairAction,
    *,
    fields: dict,
    actor_id: int | None = None,
    now: datetime | None = None,
) -> tuple[OperationalTask | None, bool]:
    """Validated one-way create. Returns ``(task_or_existing, created_flag)``.

    Rejects (returns ``(None, False)`` without a write) when:
    - the ``RepairAction`` is not ACTIVE,
    - the ``RepairAction.repair_id`` does not match ``repair.id``,
    - the ``RepairOperation`` is CLOSED or CANCELLED,
    - an ACTIVE projected task with the same dedupe key already exists.

    ``fields`` carries the caller-supplied canonical payload (title /
    description / dedupe_key / notification_message). The adapter enforces
    the canonical columns (task_type, source_*, status, priority,
    dates) and stores a durable provenance marker in
    ``task.details["canonical_projection"]``.
    """
    now = now or datetime.now(timezone.utc)
    if not _action_may_project(action, repair_id=repair.id):
        return None, False
    if not _repair_is_alive(repair):
        return None, False
    base: dict = dict(fields)
    dedupe_key = base.get("dedupe_key") or _dedupe_key_for(repair)
    # Pre-check the DB partial-index widener so an IN_PROGRESS sibling does
    # not get a fresh PENDING duplicate even though the DB index only protects
    # PENDING rows.
    existing = _existing_active(db, dedupe_key)
    if existing is not None:
        return existing, False
    provenance = {
        "direction": "canonical_to_legacy",
        "canonical_entity": "repair_action",
        "canonical_id": action.id,
    }
    details = dict(base.get("details") or {})
    details["canonical_projection"] = provenance
    base["details"] = details
    base.update(
        task_type=OperationalTaskType.FOLLOWUP,
        source_type="repair",
        source_id=repair.id,
        status=OperationalTaskStatus.PENDING,
        priority=OperationalTaskPriority.high,
        due_at=base.get("due_at") or (now + timedelta(days=2)),
        next_action=base.get("next_action") or "Get another quote / propose an alternative",
        next_check_at=base.get("next_check_at") or (now + timedelta(days=2)),
        dedupe_key=dedupe_key,
    )
    base, extras = _strip_non_columns(base)
    # ``updated_by`` is one of the audited columns on OperationalTask; the
    # canonical create path sets it through ``create_operational_task``.
    base.setdefault("updated_by", actor_id)
    task, enqueued = create_operational_task(
        db,
        fields=base,
        now=now,
        actor_id=actor_id,
        notification_message=extras.get("notification_message"),
    )
    if task is None:
        # ON CONFLICT DO NOTHING fired on PENDING; re-read in case the
        # pre-check missed a concurrently-inserted sibling.
        existing = _existing_active(db, dedupe_key)
        return existing, False
    return task, enqueued and True


# ---------------------------------------------------------------------------
# Active projection completion (existing audit label)
# ---------------------------------------------------------------------------


def close_active_projections(
    db: Session,
    *,
    tasks: Iterable[OperationalTask],
    actor_id: int | None,
    reason: str,
    source_domain: str,
    audit_action: str = "task_completed",
    now: datetime | None = None,
) -> int:
    """Transition ACTIVE projected tasks to COMPLETED with the EXISTING
    ``task_completed`` audit action by default. Callers may pass the historic
    expense labels (``task_completed_via_approval``,
    ``task_completed_via_rejection``, ``task_completed_via_payment``) so
    production contracts and existing tests stay green.

    Preserves the canonical ``task.updated_by`` behavior on close, in
    addition to ``task.completed_by`` and ``task.updated_at``.
    """
    now = now or datetime.now(timezone.utc)
    closed = 0
    for task in tasks:
        if task.status not in (
            OperationalTaskStatus.PENDING,
            OperationalTaskStatus.IN_PROGRESS,
        ):
            continue
        old = serialize_row(task)
        task.status = OperationalTaskStatus.COMPLETED
        task.completed_at = now
        task.completed_by = actor_id
        task.updated_by = actor_id
        task.reminder_generation = (task.reminder_generation or 0) + 1
        task.updated_at = now
        db.flush()
        record_audit(
            db,
            table_name="operational_tasks",
            record_id=task.id,
            action=audit_action,
            actor_id=actor_id,
            changed_fields={
                "status": [old.get("status"), OperationalTaskStatus.COMPLETED.value],
                "reminder_generation": [
                    old.get("reminder_generation", 0),
                    task.reminder_generation,
                ],
                "source_domain": [None, source_domain],
                "reason": None if not reason else reason,
            },
            old_value=old,
            new_value=serialize_row(task),
        )
        suppress_pending_redeliveries(
            db,
            task.id,
            actor_id=actor_id,
            reason=f"projected_close:{source_domain}",
            now=now,
        )
        closed += 1
    return closed


# ---------------------------------------------------------------------------
# Closed projection reopening (reverse-path seam)
# ---------------------------------------------------------------------------


def reopen_closed_projections(
    db: Session,
    *,
    task_ids: list[int],
    actor_id: int | None,
    source_domain: str,
    reason: str,
    now: datetime | None = None,
) -> int:
    """Reopen COMPLETED projected tasks back to PENDING.

    Used by the Expense reverse path: when a VERIFIED payment claim is reversed,
    the PAYMENT_PENDING projection tasks that were closed at pay() time must
    come back to life so the human workflow re-engages the payer.

    Wake-up semantics:
    - ``wake_at`` (remind_at / next_check_at) = ``now`` so the worker picks it
      up on the next tick.
    - If ``due_at`` has already passed, bump priority to high (silent
      "escalation" — the task was due yesterday but was reversed today, so it
      deserves prompt re-attention).

    Tasks that are NOT in COMPLETED status are silently skipped (idempotent
    replays / concurrent writes).
    """
    now = now or datetime.now(timezone.utc)
    reopened = 0
    for tid in task_ids:
        task = db.get(OperationalTask, tid)
        if task is None:
            continue
        if task.status != OperationalTaskStatus.COMPLETED:
            continue
        old = serialize_row(task)
        task.status = OperationalTaskStatus.PENDING
        task.completed_at = None
        task.completed_by = None
        task.reminder_generation = (task.reminder_generation or 0) + 1
        task.updated_by = actor_id
        task.updated_at = now
        task.remind_at = now
        task.next_check_at = now
        if task.due_at and task.due_at < now:
            task.priority = OperationalTaskPriority.high
        db.flush()
        record_audit(
            db,
            table_name="operational_tasks",
            record_id=task.id,
            action="task_reopened",
            actor_id=actor_id,
            changed_fields={
                "status": [old.get("status"), OperationalTaskStatus.PENDING.value],
                "reminder_generation": [
                    old.get("reminder_generation", 0),
                    task.reminder_generation,
                ],
                "source_domain": [None, source_domain],
                "reason": None if not reason else reason,
            },
            old_value=old,
            new_value=serialize_row(task),
        )
        reopened += 1
    return reopened
