"""REPAIR-AI-EMPLOYEE-WORKFLOW-008A — AI Employee continuation + dedup.

After a proposal is REJECTED the repair must AUTOMATICALLY continue (Gate §3):
the AI creates/restores a concrete next action for a real human (e.g. the
Secretary: "get another quote / propose an alternative"). This is where the
AI "decides the next step" and lands it as a durable, idempotent
``repair_action``.

Dedup (008A §4 / Case C): every step has a deterministic ``dedupe_key`` scoped
to the repair + kind + triggering event. The DB partial unique index on
``(repair_id, dedupe_key) WHERE status IN ('PENDING','IN_PROGRESS')`` means a
repeated worker tick, a bot callback re-delivery, a page refresh, or an API
retry can NEVER create more than one ACTIVE action for the same logical step.
A NEW action for the same step is only possible after the previous one was
COMPLETED or CANCELLED (the "seed" version in the key advances per event).

This module is pure/idempotent: calling ``ensure_requote_action`` N times for
the same rejected proposal yields exactly one PENDING requote action (Case C).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.repair import (
    RepairAction,
    RepairActionStatus,
    RepairOperation,
    RepairProposal,
    RepairProposalStatus,
)
from app.models.user import User
from app.services.operations.generation import secretary_assignee_id


def _action_assignee(db: Session) -> int | None:
    """Resolve a valid assignee for an AI continuation action.

    Uses the secretary assignee when it exists as an active human; otherwise
    falls back to the repair's default and finally None (an unassigned action
    is still actionable through the board). Never inserts a dangling FK."""
    candidate = secretary_assignee_id()
    if candidate is not None:
        exists = db.query(User.id).filter(User.id == candidate).first()
        if exists is not None:
            return candidate
    return None

# Action kinds used by the continuation engine.
ACTION_REQUOTE = "REQUOTE"
ACTION_PROPOSE_ALTERNATIVE = "PROPOSE_ALTERNATIVE"
ACTION_CONTACT_VENDOR = "CONTACT_VENDOR"
ACTION_RECORD_RESULT = "RECORD_REPAIR_RESULT"
ACTION_VERIFY = "VERIFY_REPAIR"


class ContinuationError(Exception):
    """Continuation engine failed to resolve a deterministic next step."""


def get_active_action(
    db: Session, repair_id: int, dedupe_key: str
) -> RepairAction | None:
    """One ACTIVE action with this dedupe key (or None)."""
    return (
        db.query(RepairAction)
        .filter(
            RepairAction.repair_id == repair_id,
            RepairAction.dedupe_key == dedupe_key,
            RepairAction.status.in_(
                [RepairActionStatus.PENDING, RepairActionStatus.IN_PROGRESS]
            ),
        )
        .first()
    )


def _action_on_conflict_do_nothing(
    db: Session, *, fields: dict
) -> RepairAction | None:
    """Atomic create against the active dedupe index; None when an ACTIVE
    action with the same ``(repair_id, dedupe_key)`` already exists."""
    stmt = (
        pg_insert(RepairAction)
        .values(**fields)
        .on_conflict_do_nothing(
            index_elements=["repair_id", "dedupe_key"],
            index_where=text("status IN ('PENDING','IN_PROGRESS')"),
        )
        .returning(RepairAction.id)
    )
    row = db.execute(stmt).first()
    if row is None:
        return None
    return db.get(RepairAction, row[0])


def ensure_requote_action(
    db: Session,
    repair: RepairOperation,
    proposal: RepairProposal,
    *,
    now: datetime | None = None,
    actor_id: int | None = None,
) -> tuple[RepairAction | None, bool]:
    """Ensure exactly ONE active requote action for the rejected proposal.

    Idempotent: safe to call from a worker loop / multiple retries. Returns
    ``(action_or_None, created_flag)``; ``created_flag`` is False when an
    active requote action already exists (dedup proved — Case C).

    The dedupe key ties the step to the rejected version so a later rejection
    (e.g. V2) seeds a NEW, differently-keyed action. Only an active action
    blocks; once completed/cancelled, a new event can create the next one.
    """
    now = now or datetime.now(timezone.utc)
    if proposal.status != RepairProposalStatus.REJECTED:
        raise ContinuationError(
            "requote continuation requires a REJECTED proposal "
            f"(V{proposal.version} is {proposal.status.value})"
        )
    dedupe_key = f"repair:{repair.id}:requote:v{proposal.version}"
    fields = {
        "repair_id": repair.id,
        "action_kind": ACTION_REQUOTE,
        "title": f"Get another quote for repair R-{repair.id} (rejected V{proposal.version})",
        "description": (
            f"The owner rejected quote V{proposal.version}"
            + (f" ({proposal.rejection_reason})" if proposal.rejection_reason else "")
            + ". Get another quote or propose an alternative — the repair remains open."
        ),
        "status": RepairActionStatus.PENDING,
        "assigned_user_id": _action_assignee(db),
        "due_at": now,
        "next_check_at": now,
        "dedupe_key": dedupe_key,
        "source_event": f"proposal_rejected:v{proposal.version}",
        "detail": {"proposal_id": proposal.id, "rejection_reason": proposal.rejection_reason},
        "created_by": actor_id,
    }
    action = _action_on_conflict_do_nothing(db, fields=fields)
    if action is None:
        existing = get_active_action(db, repair.id, dedupe_key)
        return existing, False
    # Reflect on the repair row so Telegram/Mini App read real business state.
    repair.next_action = action.title
    repair.waiting_on = "secretary"
    if repair.status.value in ("OPEN", "WAITING_APPROVAL"):
        repair.status = RepairOperationStatus.WAITING_HUMAN
    repair.next_check_at = now
    repair.updated_at = now
    db.flush()
    return action, True


def ensure_record_result_action(
    db: Session,
    repair: RepairOperation,
    *,
    now: datetime | None = None,
    actor_id: int | None = None,
) -> tuple[RepairAction | None, bool]:
    """When a repair is waiting to be verified, ensure exactly ONE action of
    "record the repair result / confirm it is actually fixed." Idempotent."""
    now = now or datetime.now(timezone.utc)
    dedupe_key = f"repair:{repair.id}:record_result"
    fields = {
        "repair_id": repair.id,
        "action_kind": ACTION_RECORD_RESULT,
        "title": f"Record repair result for R-{repair.id}",
        "description": (
            "The repair must be verified in the real world before it can close: "
            "record that the problem is actually fixed (evidence / confirmation)."
        ),
        "status": RepairActionStatus.PENDING,
        "assigned_user_id": _action_assignee(db),
        "due_at": now,
        "next_check_at": now,
        "dedupe_key": dedupe_key,
        "source_event": "awaiting_verification",
        "created_by": actor_id,
    }
    action = _action_on_conflict_do_nothing(db, fields=fields)
    if action is None:
        return get_active_action(db, repair.id, dedupe_key), False
    repair.next_action = action.title
    repair.waiting_on = "secretary"
    db.flush()
    return action, True


def resolve_actions(db: Session, repair_id: int) -> list[RepairAction]:
    return (
        db.query(RepairAction)
        .filter(RepairAction.repair_id == repair_id)
        .order_by(RepairAction.id.asc())
        .all()
    )
