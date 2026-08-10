#!/usr/bin/env python3
"""Standalone idempotent backfill for V1.2 production data-hardening.

Assigns the safe default assignee (``OPERATIONS_DEFAULT_ASSIGNEE``) to PENDING
business-source operational tasks that have no owner, then re-enqueues missing proactive
notifications — all in ONE transaction. Safe to re-run: a second run finds nothing to do
and produces no duplicate outbox rows.

Usage:
    .venv/bin/python bin/run-backfill.py [--assignee <user_id>]

Exit code 0 on success, non-zero when the default assignee is misconfigured (fails fast
before touching any data).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal  # noqa: E402
from app.services.operations.backfill import (  # noqa: E402
    backfill_unassigned_business_tasks,
)
from app.services.operations.config import DEFAULT_ASSIGNED_USER_ID  # noqa: E402


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="pasay-run-backfill")
    parser.add_argument(
        "--assignee",
        type=int,
        default=DEFAULT_ASSIGNED_USER_ID,
        help="default assignee user id (default: OPERATIONS_DEFAULT_ASSIGNEE)",
    )
    args = parser.parse_args(argv)

    db = SessionLocal()
    try:
        # backfill_unassigned_business_tasks validates the default assignee (fail-fast)
        # and runs backfill + notification re-enqueue in one transaction, committing.
        report = backfill_unassigned_business_tasks(
            db, default_assignee_id=args.assignee
        )
    except RuntimeError as exc:
        # Misconfigured default -> fail loudly without touching any data.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()

    print(
        "backfill report:\n"
        f"  tasks_backfilled={report.tasks_backfilled}\n"
        f"  tasks_skipped_already_assigned={report.tasks_skipped_already_assigned}\n"
        f"  tasks_missing_notification={report.tasks_missing_notification}\n"
        f"  notifications_enqueued={report.notifications_enqueued}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
