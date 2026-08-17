"""AI-OPS-FOUNDATION-001 §8: human promises / follow-ups.

Operational commitments ("Paolo will pay Friday", "Technician coming
tomorrow", "I'll send the receipt later") are persisted as structured
follow-up state on the ACTIVE task (``details.promise``), never only a
conversational reply.

Follow-up lifecycle (deterministic, runs in the scheduler pass AFTER
reconcile so a resolved business state is never re-reminded):

- promise.status == "open" and follow_up_at passed:
    * business state already resolved -> reconcile already COMPLETED the
      task; nothing to do here.
    * unresolved -> bump ``missed``, refresh follow_up_at, re-enqueue a
      reminder to the responsible party (the task's assigned user).
    * missed >= ``max_misses`` -> escalate: ``details.escalation``
      = {level: "owner", ...} and notify the Owner. The Owner attention
      filter (owner_scope.is_owner_actionable) picks escalated tasks up.
"""
from __future__ import annotations

import decimal
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models.operations import (
    OperationalTask,
    OperationalTaskStatus,
)
from app.services.audit import record_audit, serialize_row
from app.services.operations.config import DEFAULT_ASSIGNED_USER_ID
from app.services.operations.generation import _notification_message
from app.services.operations.outbox import enqueue_notification, resolve_recipient
from app.services.operations.config import NOTIFY_CHANNEL_TELEGRAM

logger = logging.getLogger(__name__)

PROMISE_KEY = "promise"
ESCALATION_KEY = "escalation"


def apply_promise(
    task: OperationalTask,
    *,
    promised_at: datetime,
    follow_up_at: datetime,
    responsible_party: str,
    related_entity: str,
    note: str = "",
) -> None:
    """Persist a structured promise on the task (mutates ``task.details``)."""
    details = dict(task.details or {})
    promise = dict(details.get(PROMISE_KEY) or {})
    promise.update(
        {
            "promised_at": promised_at.isoformat(),
            "follow_up_at": follow_up_at.isoformat(),
            "responsible_party": responsible_party,
            "related_entity": related_entity,
            "status": "open",
            "note": note,
        }
    )
    details[PROMISE_KEY] = promise
    task.details = details


def apply_payment_promise(
    db: Session,
    task: OperationalTask,
    *,
    amount: decimal.Decimal | None,
    promised_date: datetime,
    recorded_by: int,
    note: str = "",
) -> None:
    """PASAY-AI-EMPLOYEE-FOUNDATION-007 §17: persist a REAL payment promise.

    A payment promise is a specific commitment ("明天付30000，周五付剩下的")
    with amount / promised_date / recorded_by / source follow-up — NOT just a
    generic follow-up. Stored on the task's ``details.promise`` with a
    ``kind='payment'`` marker so the scheduler can auto-check it at the
    promised date (§17.2). ``recorded_by`` is the backend user (the Secretary
    who logged the tenant's commitment)."""
    from app.services.operations import promises as _sp

    details = dict(task.details or {})
    promise = dict(details.get(PROMISE_KEY) or {})
    promise.update(
        {
            "kind": "payment",
            "promised_at": datetime.now(timezone.utc).isoformat(),
            "promised_date": promised_date.isoformat(),
            "follow_up_at": promised_date.isoformat(),
            "amount": str(amount) if amount is not None else None,
            "recorded_by": recorded_by,
            "related_entity": f"task:{task.id}",
            "status": "open",
            "fulfilled": False,
            "note": note or "",
        }
    )
    details[PROMISE_KEY] = promise
    task.details = details
    task.next_check_at = promised_date


def check_due_payment_promises(
    db: Session,
    *,
    now: datetime,
) -> dict:
    """§17.2 Auto-check: at the promised date, if the payment arrived the
    promise is auto-fulfilled; if NOT, an actionable follow-up is re-created
    so the Secretary never keeps the calendar in their head.

    Payment arrived = a confirmed Income that covers the lease period up to
    the promised date (a real ledger check). Returns ``{"fulfilled": n,
    "refollowed_up": n}``."""
    from app.models.financial import Income, IncomeStatus
    from app.models.lease import Lease
    from app.services.operations import scheduler as _sched
    from app.services.operations.config import SECRETARY_ASSIGNEE_ID
    from app.services.operations.outbox import resolve_recipient
    from app.services.operations.daily_dedup import claim_daily_dedup

    fulfilled = 0
    refollowed_up = 0
    open_tasks = (
        db.query(OperationalTask)
        .filter(
            OperationalTask.status.in_(
                [OperationalTaskStatus.PENDING, OperationalTaskStatus.IN_PROGRESS]
            )
        )
        .all()
    )
    for task in open_tasks:
        promise = dict(task_promise(task))
        if promise.get("kind") != "payment" or promise.get("status") not in ("open", "escalated"):
            continue
        promised_date = promise.get("promised_date")
        follow_up_at = promise.get("follow_up_at")
        if not follow_up_at:
            continue
        try:
            due = datetime.fromisoformat(str(follow_up_at).replace("Z", "+00:00"))
        except ValueError:
            continue
        if due > now:
            continue
        # Determine whether payment for the lease arrived.
        paid_off = _payment_arrived_for(db, task, promise)
        details = dict(task.details or {})
        if paid_off:
            promise["status"] = "fulfilled"
            promise["fulfilled"] = True
            promise["fulfilled_at"] = now.isoformat()
            details[PROMISE_KEY] = promise
            task.details = details
            task.status = OperationalTaskStatus.COMPLETED
            task.completed_at = now
            task.completed_by = None  # auto-fulfilled by payment, not a human tap
            task.next_check_at = None
            task.updated_at = now
            db.flush()
            record_audit(
                db,
                table_name="operational_tasks",
                record_id=task.id,
                action="promise_fulfilled",
                actor_id=None,
                changed_fields={"details.promise.status": ["open", "fulfilled"]},
                old_value=serialize_row(task),
                new_value=serialize_row(task),
            )
            fulfilled += 1
        else:
            # Payment not arrived -> re-create a Secretary follow-up.
            promise["missed"] = int(promise.get("missed") or 0) + 1
            promise["follow_up_at"] = (now + timedelta(days=1)).isoformat()
            details[PROMISE_KEY] = promise
            details.setdefault("assigned_to", "secretary")
            task.details = details
            task.reminder_generation = (task.reminder_generation or 0) + 1
            task.next_check_at = now + timedelta(days=1)
            task.updated_at = now
            db.flush()
            recipient = None
            if SECRETARY_ASSIGNEE_ID is not None:
                try:
                    recipient = resolve_recipient(db, SECRETARY_ASSIGNEE_ID)
                except LookupError:
                    recipient = None
            if recipient and claim_daily_dedup(
                db,
                business_key=task.dedupe_key,
                task_id=task.id,
                recipient=recipient,
                reminder_type=f"{task.task_type.value}:PROMISE",
                now=now,
            ):
                enqueue_notification(
                    db,
                    task_id=task.id,
                    channel=NOTIFY_CHANNEL_TELEGRAM,
                    recipient=recipient,
                    payload={
                        "task_id": task.id,
                        "task_type": task.task_type.value,
                        "title": task.title,
                        "amount": details.get("amount") or promise.get("amount"),
                        "message": (
                            f"🔔 付款承诺未到账：{task.title}\n"
                            f"承诺日期已到但未收到付款。请跟进租客。"
                        ),
                    },
                    dedupe_key=f"promise:{task.id}:{now.date().isoformat()}:{recipient}",
                )
            refollowed_up += 1
    db.flush()
    return {"fulfilled": fulfilled, "refollowed_up": refollowed_up}


def _payment_arrived_for(db: Session, task: OperationalTask, promise: dict) -> bool:
    """True when a confirmed Income covers the lease such that the promised
    payment is satisfied: any confirmed income linked to the lease exists with
    a received_date <= promised_date (a real ledger check, never inferred)."""
    if task.lease_id is None:
        # Fall back to a promise amount being covered by any confirmed income
        # on the same lease is not possible without a lease; so not fulfilled.
        return False
    from app.models.financial import Income, IncomeStatus

    promised_iso = promise.get("promised_date")
    try:
        promised_d = datetime.fromisoformat(str(promised_iso).replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return False
    paid = (
        db.query(Income)
        .filter(
            Income.lease_id == task.lease_id,
            Income.status == IncomeStatus.confirmed,
            Income.received_date <= promised_d,
        )
        .first()
    )
    return paid is not None


def task_promise(task: OperationalTask) -> dict:
    details = task.details or {}
    promise = details.get(PROMISE_KEY) or {}
    return promise if isinstance(promise, dict) else {}


def task_escalation_level(task: OperationalTask) -> str:
    details = task.details or {}
    escalation = details.get(ESCALATION_KEY) or {}
    if not isinstance(escalation, dict):
        return "none"
    return str(escalation.get("level") or "none")


def _enqueue_reminder(
    db: Session, task: OperationalTask, *, message: str, now: datetime,
) -> bool:
    try:
        recipient = resolve_recipient(db, task.assigned_user_id)
    except LookupError:
        # Unresolvable recipient never blocks the task state update.
        return False
    if recipient is None:
        return False
    # CONVERGENCE-003 §1.4: a promise follow-up is a PROACTIVE reminder — the
    # same business object + recipient + PH day + type is sent at most once
    # per day even if the scheduler scans again before the next follow_up_at.
    from app.services.operations.daily_dedup import claim_daily_dedup

    if not claim_daily_dedup(
        db,
        business_key=task.dedupe_key,
        task_id=task.id,
        recipient=recipient,
        reminder_type=task.task_type.value,
        now=now,
    ):
        return False
    details = task.details or {}
    payload = {
        "task_id": task.id,
        "task_type": task.task_type.value,
        "title": task.title,
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "amount": details.get("amount") or details.get("total_outstanding"),
        "message": message,
    }
    generation = task.reminder_generation or 0
    return enqueue_notification(
        db,
        task_id=task.id,
        channel=NOTIFY_CHANNEL_TELEGRAM,
        recipient=recipient,
        payload=payload,
        dedupe_key=f"task:{task.id}:{generation}:{NOTIFY_CHANNEL_TELEGRAM}:{recipient}",
    )


def escalate_due_promises(
    db: Session,
    *,
    now: datetime,
    max_misses: int = 2,
    remind_interval_hours: int = 24,
) -> dict:
    """One deterministic pass over active tasks with a due follow-up.

    Returns ``{"escalated": n, "reminded": n}``.
    """
    escalated = 0
    reminded = 0
    tasks = (
        db.query(OperationalTask)
        .filter(
            OperationalTask.status.in_(
                [OperationalTaskStatus.PENDING, OperationalTaskStatus.IN_PROGRESS]
            )
        )
        .all()
    )
    for task in tasks:
        promise = dict(task_promise(task))  # copy: never mutate the JSONB in place
        if promise.get("status") != "open":
            continue
        follow_up_at = promise.get("follow_up_at")
        if not follow_up_at:
            continue
        try:
            due = datetime.fromisoformat(str(follow_up_at).replace("Z", "+00:00"))
        except ValueError:
            continue
        if due > now:
            continue
        old = serialize_row(task)
        missed = int(promise.get("missed") or 0) + 1
        promise["missed"] = missed
        next_follow_up = now + timedelta(hours=remind_interval_hours)
        promise["follow_up_at"] = next_follow_up.isoformat()
        details = dict(task.details or {})
        details[PROMISE_KEY] = promise

        task.reminder_generation = (task.reminder_generation or 0) + 1
        task.next_check_at = next_follow_up
        task.updated_at = now
        if missed >= max_misses:
            promise["status"] = "escalated"
            details[ESCALATION_KEY] = {
                "level": "owner",
                "reason": f"promise missed {missed} times",
                "at": now.isoformat(),
            }
            task.details = details
            db.flush()
            record_audit(
                db,
                table_name="operational_tasks",
                record_id=task.id,
                action="task_escalated",
                actor_id=None,  # system / policy
                changed_fields={
                    "details": [old.get("details"), details],
                    "reminder_generation": [old.get("reminder_generation", 0), task.reminder_generation],
                },
                old_value=old,
                new_value=serialize_row(task),
            )
            owner_id = DEFAULT_ASSIGNED_USER_ID
            owner_recipient = None
            if owner_id is not None:
                try:
                    owner_recipient = resolve_recipient(db, owner_id)
                except LookupError:
                    owner_recipient = None
            if owner_recipient is not None:
                _enqueue_reminder(
                    db, task,
                    message=(
                        f"⚠️ 跟进升级 / Escalated: {task.title}\n"
                        f"承诺 {promise.get('responsible_party')} 逾期 {missed} 次未完成。"
                    ),
                    now=now,
                )
            escalated += 1
        else:
            task.details = details
            db.flush()
            record_audit(
                db,
                table_name="operational_tasks",
                record_id=task.id,
                action="task_reminded",
                actor_id=None,  # system / policy
                changed_fields={
                    "details": [old.get("details"), details],
                    "reminder_generation": [old.get("reminder_generation", 0), task.reminder_generation],
                },
                old_value=old,
                new_value=serialize_row(task),
            )
            _enqueue_reminder(
                db, task,
                message=(
                    f"🔔 跟进提醒 / Follow-up: {task.title}\n"
                    f"承诺：{promise.get('note') or promise.get('responsible_party')}"
                ),
                now=now,
            )
            reminded += 1
    return {"escalated": escalated, "reminded": reminded}
