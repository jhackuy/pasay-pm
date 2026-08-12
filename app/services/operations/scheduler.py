"""Scheduler: one DB transaction per run.

- Claims due recurring rules with SELECT ... FOR UPDATE SKIP LOCKED (safe
  for multiple worker instances / restarts — the DB is the only source of
  truth, never Python memory).
- Generates rule-driven tasks and advances next_run_at.
- Generates business-source tasks (dedupe via partial unique index +
  ON CONFLICT DO NOTHING).
- Reconciles stale tasks.
- Writes notification_outbox rows in the same transaction.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.operations import RecurringRule
from app.schemas.operations import SchedulerRunResult
from app.services.operations.config import SCHEDULER_RULE_BATCH
from app.services.operations.generation import generate_business_tasks, generate_rule_task
from app.services.operations.reconcile import reconcile_tasks
from app.services.operations.redelivery import redeliver_due_snoozes
from app.services.identity import bind_internal_audit


def claim_due_rules(db: Session, *, now: datetime, batch: int = SCHEDULER_RULE_BATCH) -> list[RecurringRule]:
    """Claim enabled rules that are due. SKIP LOCKED: concurrent workers each
    claim a disjoint set; a crashed worker's uncommitted claims are released
    automatically and picked up again on the next pass."""
    stmt = (
        select(RecurringRule)
        .where(
            RecurringRule.enabled.is_(True),
            RecurringRule.deleted_at.is_(None),
            RecurringRule.next_run_at <= now,
        )
        .order_by(RecurringRule.next_run_at)
        .limit(batch)
        .with_for_update(skip_locked=True)
    )
    return list(db.execute(stmt).scalars())


def run_scheduler_once(db: Session, *, now: datetime | None = None) -> SchedulerRunResult:
    """Run one full scheduler pass and commit. Returns a result summary.

    Order matters for snooze safety: reconcile settles stale PENDING tasks
    BEFORE the snooze redelivery scan, so a task that reconcile completed or
    cancelled in the same pass is never redelivered (the scan only selects
    tasks still PENDING after reconciliation).
    """
    now = now or datetime.now(timezone.utc)
    bind_internal_audit(db, "scheduler")
    rules = claim_due_rules(db, now=now)
    tasks_created = 0
    notifications_enqueued = 0
    for rule in rules:
        _, enqueued = generate_rule_task(db, rule, now=now)
        tasks_created += 1
        notifications_enqueued += 1 if enqueued else 0

    biz_created, biz_notif = generate_business_tasks(db, now=now)
    tasks_created += biz_created
    notifications_enqueued += biz_notif

    bind_internal_audit(db, "reconcile")
    auto_completed, auto_cancelled = reconcile_tasks(db, now=now)

    bind_internal_audit(db, "scheduler")
    snooze_redelivered = redeliver_due_snoozes(db, now=now)

    db.commit()
    return SchedulerRunResult(
        tasks_created=tasks_created,
        notifications_enqueued=notifications_enqueued,
        rules_claimed=len(rules),
        rules_advanced=len(rules),
        reconciled_completed=auto_completed,
        reconciled_cancelled=auto_cancelled,
        snooze_redelivered=snooze_redelivered,
    )
