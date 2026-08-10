"""Notification outbox worker: claim -> send -> mark SENT / retry / FAILED.

- Claim uses SELECT ... FOR UPDATE SKIP LOCKED: at-least-once delivery,
  crash-safe (uncommitted claims are released on disconnect and re-claimed).
- Exponential backoff: next_attempt_at = now + base * 2^(attempts-1).
- After ``max_attempts`` the row is FAILED with the last error persisted.
- Telegram being down never loses a task: the row stays PENDING and is
  retried later.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Protocol

import httpx
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.operations import NotificationOutbox, NotificationStatus
from app.services.operations.config import (
    NOTIFY_BACKOFF_BASE_SECONDS,
    NOTIFY_MAX_ATTEMPTS,
    OUTBOX_CLAIM_BATCH,
)

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


class NotificationSender(Protocol):
    """Send one notification; returns the provider message id (or None)."""

    def send(self, recipient: str, text: str) -> str | None: ...


class TelegramSender:
    """Synchronous Telegram sendMessage via httpx.

    ``recipient`` may be a chat id or a ``user:{id}`` placeholder resolved
    through ``resolve_user`` (a callable returning a chat id or None).
    """

    def __init__(self, bot_token: str, resolve_user=None, timeout: float = 10.0):
        self._token = bot_token
        self._resolve_user = resolve_user
        self._timeout = timeout

    def send(self, recipient: str, text: str) -> str | None:
        chat_id = recipient
        if recipient.startswith("user:"):
            if self._resolve_user is None:
                return None
            chat_id = self._resolve_user(recipient[5:])
            if not chat_id:
                raise ValueError(f"no telegram chat id for user {recipient[5:]}")
        resp = httpx.post(
            f"{TELEGRAM_API}/bot{self._token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=self._timeout,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"telegram sendMessage {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        message = (data.get("result") or {}).get("message_id")
        return str(message) if message is not None else None


def claim_pending_notifications(
    db: Session, *, now: datetime, batch: int = OUTBOX_CLAIM_BATCH
) -> list[NotificationOutbox]:
    """Claim PENDING rows that are due (or never scheduled)."""
    stmt = (
        select(NotificationOutbox)
        .where(
            NotificationOutbox.status == NotificationStatus.PENDING,
            or_(
                NotificationOutbox.next_attempt_at.is_(None),
                NotificationOutbox.next_attempt_at <= now,
            ),
        )
        .order_by(NotificationOutbox.id)
        .limit(batch)
        .with_for_update(skip_locked=True)
    )
    return list(db.execute(stmt).scalars())


def _message_text(item: NotificationOutbox) -> str:
    payload = item.payload or {}
    return (
        payload.get("message")
        or payload.get("title")
        or f"🔔 待办提醒 #{item.task_id}"
    )


def process_notifications_once(
    db: Session,
    sender: NotificationSender,
    *,
    now: datetime | None = None,
    max_attempts: int = NOTIFY_MAX_ATTEMPTS,
    backoff_base: int = NOTIFY_BACKOFF_BASE_SECONDS,
    batch: int = OUTBOX_CLAIM_BATCH,
) -> dict:
    """One notifier pass over a claimed batch. Commits the outcome."""
    now = now or datetime.now(timezone.utc)
    items = claim_pending_notifications(db, now=now, batch=batch)
    result = {"claimed": len(items), "sent": 0, "retried": 0, "failed": 0}
    for item in items:
        text = _message_text(item)
        try:
            message_id = sender.send(item.recipient, text)
            item.status = NotificationStatus.SENT
            item.sent_at = now
            item.attempts += 1
            item.next_attempt_at = None
            item.last_error = None
            if message_id:
                try:
                    item.telegram_message_id = int(message_id)
                except (TypeError, ValueError):
                    pass
            result["sent"] += 1
        except Exception as exc:  # noqa: BLE001 - persist any delivery error
            item.attempts += 1
            item.last_error = str(exc)[:1000]
            if item.attempts >= max_attempts:
                item.status = NotificationStatus.FAILED
                item.next_attempt_at = None
                result["failed"] += 1
            else:
                delay = backoff_base * (2 ** (item.attempts - 1))
                item.next_attempt_at = now + timedelta(seconds=delay)
                result["retried"] += 1
        db.add(item)
    db.commit()
    return result
