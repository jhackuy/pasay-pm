"""Standalone worker loop: scheduler pass + notifier pass.

Run directly:  .venv/bin/python -m app.services.operations.worker
or via bin/run-operations-worker.py. Multiple instances are safe — every
claim uses PostgreSQL SKIP LOCKED and dedupe is enforced by DB indexes.
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timezone

from app.config import settings
from app.database import SessionLocal
from app.models.user import User
from app.services.operations.config import (
    NOTIFY_BACKOFF_BASE_SECONDS,
    NOTIFY_MAX_ATTEMPTS,
)
from app.services.operations.notifier import TelegramSender, process_notifications_once
from app.services.operations.scheduler import run_scheduler_once

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("pasay.operations.worker")


def _build_sender() -> TelegramSender:
    def resolve_user(user_id_str: str) -> str | None:
        db = SessionLocal()
        try:
            user = db.get(User, int(user_id_str))
            return user.telegram_chat_id if user else None
        finally:
            db.close()

    return TelegramSender(settings.telegram_bot_token, resolve_user=resolve_user)


def run_worker_once(
    *,
    max_attempts: int = NOTIFY_MAX_ATTEMPTS,
    backoff_base: int = NOTIFY_BACKOFF_BASE_SECONDS,
    sender=None,
    now: datetime | None = None,
) -> dict:
    """One pass: scheduler + notifier in separate transactions."""
    db = SessionLocal()
    try:
        sched = run_scheduler_once(db, now=now)
        sender = sender or _build_sender()
        notif = process_notifications_once(
            db, sender, now=now, max_attempts=max_attempts, backoff_base=backoff_base
        )
        return {"scheduler": sched, "notifier": notif}
    finally:
        db.close()


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="pasay-operations-worker")
    parser.add_argument("--once", action="store_true", help="run a single pass and exit")
    parser.add_argument("--interval", type=float, default=60.0, help="loop interval seconds")
    parser.add_argument("--max-attempts", type=int, default=NOTIFY_MAX_ATTEMPTS)
    parser.add_argument("--backoff-base", type=int, default=NOTIFY_BACKOFF_BASE_SECONDS)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.once:
        result = run_worker_once(max_attempts=args.max_attempts, backoff_base=args.backoff_base)
        logger.info("worker pass: %s", result)
        return 0

    stop = False

    def _stop(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    logger.info("operations worker starting (interval=%ss)", args.interval)
    while not stop:
        try:
            result = run_worker_once(max_attempts=args.max_attempts, backoff_base=args.backoff_base)
            logger.info("worker pass: %s", result)
        except Exception:  # noqa: BLE001 - keep the loop alive
            logger.exception("worker pass failed; continuing")
        if stop:
            break
        time.sleep(args.interval)
    logger.info("operations worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
