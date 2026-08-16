"""AI-OPS-FOUNDATION-001 §13: repair lifecycle completion evidence.

Repair progress is tracked on the AC_MAINTENANCE task's JSONB details
(``details.repair_stage``), following the lifecycle:

ISSUE_REPORTED -> DIAGNOSIS/SCHEDULED -> QUOTE -> APPROVAL (when required)
-> IN_PROGRESS -> WORK_COMPLETED -> PAYMENT -> VERIFICATION -> CLOSED

Not every stage requires evidence. On completion we run a LIGHTWEIGHT
completeness check (before photo / after photo / quote / receipt) and, when
completion evidence is missing, assign a FOLLOWUP task to the SECRETARY
(never the Owner) — the repair itself still completes (completeness checks
never block normal work).

Deterministic; no LLM.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models.operations import (
    OperationalTask,
    OperationalTaskPriority,
    OperationalTaskStatus,
    OperationalTaskType,
)
from app.services.operations.config import SECRETARY_ASSIGNEE_ID
from app.services.operations.generation import create_operational_task

logger = logging.getLogger(__name__)

# Recommended (not mandatory) completion evidence.
REQUIRED_EVIDENCE_KEYS = ("after_photo", "before_photo", "receipt")


def repair_stage(task: OperationalTask) -> str:
    details = task.details or {}
    return str(details.get("repair_stage") or "ISSUE_REPORTED")


def set_repair_stage(task: OperationalTask, stage: str) -> None:
    details = dict(task.details or {})
    details["repair_stage"] = stage
    task.details = details


def completion_evidence(task: OperationalTask) -> dict:
    details = task.details or {}
    evidence = details.get("completion_evidence")
    return evidence if isinstance(evidence, dict) else {}


def has_minimal_completion_evidence(task: OperationalTask) -> bool:
    """Lightweight check: at least one piece of completion evidence (after
    photo recommended; receipt/quote acceptable) OR an explicit
    ``evidence_ok`` marker set by the human."""
    evidence = completion_evidence(task)
    if evidence.get("evidence_ok"):
        return True
    return any(bool(evidence.get(key)) for key in REQUIRED_EVIDENCE_KEYS)


def _secretary_fallback(db: Session) -> int | None:
    """Secretary assignee with a safe fallback to the default owner so the
    follow-up always has a recipient. Resolves through generation's
    monkeypatchable seam so tests can pin the real secretary user; a
    configured-but-missing secretary falls back to the default owner."""
    from app.models.user import User
    from app.services.operations.config import DEFAULT_ASSIGNED_USER_ID
    from app.services.operations.generation import secretary_assignee_id

    candidate = secretary_assignee_id()
    if candidate is not None:
        exists = db.query(User.id).filter(User.id == candidate).first()
        if exists is not None:
            return candidate
    return DEFAULT_ASSIGNED_USER_ID


def ensure_evidence_followup(
    db: Session, repair_task: OperationalTask, *, now: datetime | None = None,
    actor_id: int | None = None,
) -> tuple[OperationalTask | None, bool]:
    """AI-OPS-FOUNDATION-001 §13: when a repair completes WITHOUT minimal
    completion evidence, create ONE secretary follow-up task
    (``dedupe_key = repair-evidence:{task_id}``) so the evidence gap is
    chased by the Secretary — never the Owner. Returns (task_or_None,
    created_flag)."""
    now = now or datetime.now(timezone.utc)
    if repair_task.task_type != OperationalTaskType.AC_MAINTENANCE:
        return None, False
    if has_minimal_completion_evidence(repair_task):
        return None, False
    fields = {
        "task_type": OperationalTaskType.FOLLOWUP,
        "title": f"上传维修完成凭证 · {repair_task.title or 'Repair'}",
        "description": "Repair completed without completion evidence; upload "
                       "before/after photos or the receipt to close the loop.",
        "property_id": repair_task.property_id,
        "lease_id": repair_task.lease_id,
        "source_type": "task",
        "source_id": repair_task.id,
        "assigned_user_id": _secretary_fallback(db),
        "priority": OperationalTaskPriority.medium,
        "status": OperationalTaskStatus.PENDING,
        "due_at": now + timedelta(days=2),
        "next_action": "Upload repair completion evidence (after photo / receipt)",
        "next_check_at": now + timedelta(days=2),
        "dedupe_key": f"repair-evidence:{repair_task.id}",
        "details": {
            "repair_task_id": repair_task.id,
            "unit_number": (repair_task.details or {}).get("unit_number"),
            "missing": [
                key for key in REQUIRED_EVIDENCE_KEYS
                if not completion_evidence(repair_task).get(key)
            ],
        },
    }
    task, enqueued = create_operational_task(
        db, fields=fields, now=now, actor_id=actor_id,
    )
    return task, task is not None


def close_evidence_followups(
    db: Session, repair_task_id: int, *, actor_id: int | None = None,
) -> int:
    """AI-OPS-FOUNDATION-001 §13: once completion evidence is uploaded for a
    repair, close any open evidence follow-up for it (deterministic)."""
    from app.services.operations.redelivery import suppress_pending_redeliveries

    now = datetime.now(timezone.utc)
    tasks = (
        db.query(OperationalTask)
        .filter(
            OperationalTask.task_type == OperationalTaskType.FOLLOWUP,
            OperationalTask.source_type == "task",
            OperationalTask.source_id == repair_task_id,
            OperationalTask.dedupe_key == f"repair-evidence:{repair_task_id}",
            OperationalTask.status.in_(
                [OperationalTaskStatus.PENDING, OperationalTaskStatus.IN_PROGRESS]
            ),
        )
        .all()
    )
    for task in tasks:
        task.status = OperationalTaskStatus.COMPLETED
        task.completed_at = now
        task.completed_by = actor_id
        task.reminder_generation = (task.reminder_generation or 0) + 1
        task.updated_at = now
        db.flush()
        suppress_pending_redeliveries(
            db, task.id, actor_id=actor_id, reason="evidence_uploaded", now=now,
        )
    return len(tasks)
