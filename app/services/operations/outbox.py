"""Notification outbox helpers: same-transaction enqueue + recipient resolve.

The outbox row is written in the SAME transaction that creates the task
(create task + insert outbox + commit), giving at-least-once delivery: a
crash after commit leaves a durable PENDING row that the notifier claims
with SELECT ... FOR UPDATE SKIP LOCKED.
"""
from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.operations import NotificationOutbox
from app.models.user import User


def resolve_recipient(db: Session, user_id: int | None) -> str | None:
    """Map a backend user to a notifier recipient.

    Prefers the user's registered Telegram chat id; falls back to a
    ``user:{id}`` placeholder that the notifier can resolve or drop.
    Returns None when there is no user at all (no recipient -> no outbox).
    """
    if user_id is None:
        return None
    user = db.get(User, user_id)
    if user is None:
        return None
    if user.telegram_chat_id:
        return user.telegram_chat_id
    return f"user:{user.id}"


def enqueue_notification(
    db: Session,
    *,
    task_id: int,
    channel: str,
    recipient: str,
    payload: dict,
    dedupe_key: str,
) -> bool:
    """Insert one outbox row; returns False when the dedupe key already exists.

    ``uq_notification_outbox_dedupe`` makes this atomic — concurrent
    enqueues of the same notification can only land once.
    """
    stmt = (
        pg_insert(NotificationOutbox)
        .values(
            task_id=task_id,
            channel=channel,
            recipient=recipient,
            payload=payload,
            dedupe_key=dedupe_key,
        )
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
    )
    result = db.execute(stmt)
    return result.rowcount == 1
