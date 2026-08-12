"""Notification outbox worker: atomic claim -> send -> finalize.

Concurrency model (V1.2.2 A+B.1 hardening):
- Candidates are selected with SELECT ... FOR UPDATE SKIP LOCKED, then the
  batch lock is released. The REAL claim is a per-row conditional UPDATE
  (``status=PENDING AND claimed_at`` lease) — only one worker ever wins a row.
- For snooze-redelivery rows the claim transaction also takes a
  SELECT ... FOR UPDATE on the TASK row and re-validates the reminder inside
  the SAME transaction (generation + PENDING + not deferred into the future),
  so a concurrently completed/cancelled/re-snoozed task is observed BEFORE any
  send. Lock order is always task -> outbox (same as the API transition path),
  so no lock cycle is possible.
- The claim is COMMITTED before the outbound HTTP call: no DB transaction is
  held across the Telegram send.
- Finalization is atomic and idempotent: success sets SENT + message id;
  failure reverts to PENDING with exponential backoff (or FAILED at
  ``max_attempts``). If the row was concurrently DROPPED (task no longer
  warrants the reminder), the conditional finalize matches zero rows and the
  row is never retried. A worker that crashes after the claim leaves
  ``claimed_at`` set; the row is reclaimed only after the lease
  (``NOTIFY_CLAIM_LEASE_SECONDS``) expires — at-least-once, no double claim of
  a live send.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Protocol

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.models.operations import (
    NotificationOutbox,
    NotificationStatus,
    OperationalTask,
    OperationalTaskStatus,
)
from app.services.audit import record_audit, serialize_row
from app.services.operations.config import (
    NOTIFY_BACKOFF_BASE_SECONDS,
    NOTIFY_CLAIM_LEASE_SECONDS,
    NOTIFY_MAX_ATTEMPTS,
    OUTBOX_CLAIM_BATCH,
    SNOOZE_REDELIVERY_KEY_PREFIX,
)
from app.services.operations.redelivery import is_snooze_redelivery_key
from app.services.identity import bind_internal_audit

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


class NotificationSender(Protocol):
    """Send one notification; returns the provider message id (or None)."""

    def send(
        self, recipient: str, text: str, reply_markup: dict | None = None,
    ) -> str | None: ...


class TelegramSender:
    """Synchronous Telegram sendMessage via httpx.

    ``recipient`` may be a chat id or a ``user:{id}`` placeholder resolved
    through ``resolve_user`` (a callable returning a chat id or None).
    """

    def __init__(self, bot_token: str, resolve_user=None, timeout: float = 10.0):
        self._token = bot_token
        self._resolve_user = resolve_user
        self._timeout = timeout

    def send(
        self, recipient: str, text: str, reply_markup: dict | None = None,
    ) -> str | None:
        chat_id = recipient
        if recipient.startswith("user:"):
            if self._resolve_user is None:
                return None
            chat_id = self._resolve_user(recipient[5:])
            if not chat_id:
                raise ValueError(f"no telegram chat id for user {recipient[5:]}")
        body = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if reply_markup is not None:
            body["reply_markup"] = reply_markup
        resp = httpx.post(
            f"{TELEGRAM_API}/bot{self._token}/sendMessage",
            json=body,
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
    """Select PENDING rows that are due (or never scheduled).

    Returns rows locked with FOR UPDATE SKIP LOCKED as *candidates*. The
    durable claim happens later via ``_claim_row``'s conditional UPDATE, so
    the caller must COMMIT (release) these batch locks before sending.
    """
    stmt = (
        select(NotificationOutbox)
        .where(
            NotificationOutbox.status == NotificationStatus.PENDING,
            or_(
                NotificationOutbox.next_attempt_at.is_(None),
                NotificationOutbox.next_attempt_at <= now,
            ),
            or_(
                NotificationOutbox.claimed_at.is_(None),
                NotificationOutbox.claimed_at <= now - timedelta(seconds=NOTIFY_CLAIM_LEASE_SECONDS),
            ),
        )
        .order_by(NotificationOutbox.id)
        .limit(batch)
        .with_for_update(skip_locked=True)
    )
    return list(db.execute(stmt).scalars())


def _message_text(item: NotificationOutbox) -> str:
    """Humanized notification text (V1.3): no task_type codes, no #id prefixes.
    An explicit payload ``message`` (the human card from generation or the
    English copilot override) wins; otherwise compose from title/amount/due."""
    payload = item.payload or {}
    explicit = payload.get("message")
    if explicit:
        return explicit
    lines = ["🔔 待办提醒"]
    title = payload.get("title")
    if title:
        lines.append(title)
    amount = payload.get("amount")
    if amount is not None and str(amount) not in ("", "None"):
        lines.append(f"金额：{amount}")
    due = payload.get("due_at")
    if due:
        lines.append(f"到期：{str(due)[:16].replace('T', ' ')}")
    return "\n".join(lines)


def process_notifications_once(
    db: Session,
    sender: NotificationSender,
    *,
    now: datetime | None = None,
    max_attempts: int = NOTIFY_MAX_ATTEMPTS,
    backoff_base: int = NOTIFY_BACKOFF_BASE_SECONDS,
    batch: int = OUTBOX_CLAIM_BATCH,
) -> dict:
    """One notifier pass. Per row: atomic claim+validate tx -> COMMIT -> send
    (no DB tx held) -> atomic finalize tx.

    Returns ``{"claimed": n, "sent": n, "retried": n, "failed": n}``.
    """
    now = now or datetime.now(timezone.utc)
    bind_internal_audit(db, "notifier")
    candidates = claim_pending_notifications(db, now=now, batch=batch)
    db.commit()  # release the SKIP LOCKED batch locks; the per-row claim is conditional
    result = {"claimed": 0, "sent": 0, "retried": 0, "failed": 0}
    for item in candidates:
        row = _claim_row(db, item.id, now=now)
        if row is None:
            continue  # claimed elsewhere, dropped, or stale (drop committed inside)
        result["claimed"] += 1
        text = _message_text(row)
        try:
            message_id = sender.send(
                row.recipient, text,
                reply_markup=row.payload.get("reply_markup") if row.payload else None,
            )
            if not _finalize_sent(db, row, message_id=message_id, now=now):
                # The row was concurrently DROPPED (task no longer warrants the
                # reminder) or re-claimed: never retry a possibly-stale send.
                logger.warning("outbox %s finalized by another path; send not retried", row.id)
            result["sent"] += 1
        except Exception as exc:  # noqa: BLE001 - persist any delivery error
            outcome = _finalize_failed(
                db, row, exc, now=now, max_attempts=max_attempts, backoff_base=backoff_base
            )
            if outcome is True:
                result["retried"] += 1
            elif outcome is False:
                result["failed"] += 1
    return result


def _claim_row(db: Session, outbox_id: int, *, now: datetime) -> NotificationOutbox | None:
    """Atomic claim + validation in ONE transaction (COMMITs before returning).

    Snooze-redelivery rows: locks the TASK row first (lock order task -> outbox,
    matching the API transition path) and re-validates the reminder against the
    CURRENT task state; stale rows are DROPPED here, before any send. The claim
    itself is a conditional UPDATE (status=PENDING + claim lease), so concurrent
    workers cannot double-claim a live row.
    """
    row = db.get(NotificationOutbox, outbox_id)
    if row is None or row.status != NotificationStatus.PENDING:
        db.rollback()
        return None
    if is_snooze_redelivery_key(row.dedupe_key):
        task = _lock_task(db, row.task_id)
        if not _redelivery_still_valid(db, row, task, now):
            _drop_stale_row(db, row, now=now)
            db.commit()
            return None
    res = db.execute(
        update(NotificationOutbox)
        .where(
            NotificationOutbox.id == outbox_id,
            NotificationOutbox.status == NotificationStatus.PENDING,
            or_(
                NotificationOutbox.claimed_at.is_(None),
                NotificationOutbox.claimed_at <= now - timedelta(seconds=NOTIFY_CLAIM_LEASE_SECONDS),
            ),
        )
        .values(claimed_at=now, updated_at=now),
        execution_options={"synchronize_session": False},
    )
    if res.rowcount != 1:
        db.rollback()
        return None
    db.commit()
    db.expire_all()
    return db.get(NotificationOutbox, outbox_id)


def _lock_task(db: Session, task_id: int | None) -> OperationalTask | None:
    if task_id is None:
        return None
    return (
        db.execute(
            select(OperationalTask)
            .where(OperationalTask.id == task_id)
            .with_for_update()
        )
        .scalar_one_or_none()
    )


def _redelivery_still_valid(
    db: Session, item: NotificationOutbox, task: OperationalTask | None, now: datetime
) -> bool:
    """Send-time guard, evaluated while the task row is locked (claim tx):
    the task must still be PENDING, the reminder generation must match the
    row's, and the task must not be deferred into the future again."""
    _ = db
    if task is None or task.status != OperationalTaskStatus.PENDING:
        return False
    generation = _dedupe_generation(item.dedupe_key)
    if generation is not None and generation != task.reminder_generation:
        return False
    if task.snoozed_until is not None and task.snoozed_until > now:
        return False
    return True


def _dedupe_generation(dedupe_key: str | None) -> int | None:
    """Parse the reminder generation embedded in a snooze-redelivery dedupe
    key (``{prefix}{task_id}:{generation}:{window}``). Returns None for rows
    without a parseable generation (legacy format / non-snooze rows), in which
    case the generation equality check is skipped."""
    if not is_snooze_redelivery_key(dedupe_key):
        return None
    rest = dedupe_key[len(SNOOZE_REDELIVERY_KEY_PREFIX):]
    parts = rest.split(":", 2)
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except (TypeError, ValueError):
        return None


def _drop_stale_row(db: Session, row: NotificationOutbox, *, now: datetime) -> None:
    old = serialize_row(row)
    res = db.execute(
        update(NotificationOutbox)
        .where(
            NotificationOutbox.id == row.id,
            NotificationOutbox.status == NotificationStatus.PENDING,
        )
        .values(status=NotificationStatus.DROPPED, updated_at=now),
        execution_options={"synchronize_session": False},
    )
    if res.rowcount == 1:
        record_audit(
            db,
            table_name="notification_outbox",
            record_id=row.id,
            action="outbox_dropped",
            actor_id=None,  # system / notifier guard
            changed_fields={
                "status": ["PENDING", "DROPPED"],
                "reason": "task no longer warrants the snoozed reminder",
            },
            old_value=old,
            new_value=serialize_row(row),
        )


def _finalize_sent(
    db: Session, row: NotificationOutbox, *, message_id: str | None, now: datetime
) -> bool:
    """Atomic finalization: PENDING(claimed) -> SENT. Idempotent: matches zero
    rows if the row was concurrently DROPPED / re-finalized — no retry."""
    try:
        telegram_message_id = int(message_id)
    except (TypeError, ValueError):
        telegram_message_id = None
    res = db.execute(
        update(NotificationOutbox)
        .where(
            NotificationOutbox.id == row.id,
            NotificationOutbox.status == NotificationStatus.PENDING,
            NotificationOutbox.claimed_at == row.claimed_at,
        )
        .values(
            status=NotificationStatus.SENT,
            sent_at=now,
            attempts=NotificationOutbox.attempts + 1,
            next_attempt_at=None,
            last_error=None,
            claimed_at=None,
            telegram_message_id=telegram_message_id,
            updated_at=now,
        ),
        execution_options={"synchronize_session": False},
    )
    db.commit()
    return res.rowcount == 1


def _finalize_failed(
    db: Session,
    row: NotificationOutbox,
    exc: Exception,
    *,
    now: datetime,
    max_attempts: int,
    backoff_base: int,
) -> bool | None:
    """Atomic failure finalization: PENDING(claimed) -> retry (PENDING with
    backoff) or FAILED. Returns True (retry scheduled), False (FAILED), or
    None when the row was concurrently dropped/finalized (no-op)."""
    new_attempts = (row.attempts or 0) + 1
    if new_attempts >= max_attempts:
        status = NotificationStatus.FAILED
        next_attempt_at = None
    else:
        status = NotificationStatus.PENDING
        next_attempt_at = now + timedelta(seconds=backoff_base * (2 ** (new_attempts - 1)))
    res = db.execute(
        update(NotificationOutbox)
        .where(
            NotificationOutbox.id == row.id,
            NotificationOutbox.status == NotificationStatus.PENDING,
            NotificationOutbox.claimed_at == row.claimed_at,
        )
        .values(
            status=status,
            attempts=new_attempts,
            next_attempt_at=next_attempt_at,
            last_error=str(exc)[:1000],
            claimed_at=None,
            updated_at=now,
        ),
        execution_options={"synchronize_session": False},
    )
    db.commit()
    if res.rowcount != 1:
        return None
    if status == NotificationStatus.PENDING:
        return True
    return False
