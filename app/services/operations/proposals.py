"""Canonical Copilot proposal builder (V1.2.2 Phase C2).

The bot / LLM only supplies an intent plus resolved refs — ALL critical
fields (assignee, due time, target, property scope, idempotency key) are
resolved HERE deterministically against the DB. Free text (``note``) is
carried as DATA only: a follow-up task can never carry a financial mutation.

Intent -> action mapping (deterministic, no LLM on this path):
    "安排秘书跟进" / "follow up"        -> create_followup_task
    "指派/交给/转给" / "assign"        -> assign_task
    "明天再提醒" / "snooze/提醒"        -> snooze_task
    anything else                      -> rejected (fail closed)

Resolution rules:
- Assignee: a backend-validated ``assignee_user_id`` wins; otherwise the
  unique active secretary (agent) candidate; ambiguity -> ``NEEDS_CLARIFICATION``
  (the UX must present buttons, never guess).
- Due/snooze time: Manila-aware default (tomorrow 09:00 Asia/Manila for
  followups; the shared snooze presets for snoozes). The resolved value is
  always shown on the confirmation card — never a hidden guess.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.models.operations import OperationalTask, OperationalTaskStatus
from app.models.property import Unit
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.operations import copilot as copilot_svc
from app.services.operations.config import SECRETARY_ASSIGNEE_ID

MANILA_TZ = ZoneInfo("Asia/Manila")
PROPOSAL_DEFAULT_TTL = timedelta(hours=24)
FOLLOWUP_SOURCE_TYPES = frozenset({"property", "lease", "task"})
# Secretary = agent role in this system's role model; managers/admins are also
# eligible assignees (all backend-resolved, never a raw LLM user id).
ASSIGNEE_ROLES = frozenset(
    {UserRole.agent.value, UserRole.manager.value, UserRole.admin.value}
)
SNOOZE_PRESETS = frozenset({"1h", "today_afternoon", "tomorrow_morning", "3d"})
DEFAULT_FOLLOWUP_HOUR = 9  # Manila wall-clock for the default followup due time


class ProposalNeedsClarification(copilot_svc.ProposalValidationError):
    """Ambiguous resolution — the confirmation UX must present buttons."""


# ---------------------------------------------------------------------------
# intent parsing (deterministic)
# ---------------------------------------------------------------------------

def parse_action_intent(intent: str) -> str:
    """Map a natural-language intent string to one of the 3 EXECUTABLE codes.

    NFC-normalized + invisible-character-stripped before matching, so
    confusable variants either canonicalize to a known intent or get rejected.
    """
    text = copilot_svc.canonicalize(intent or "").strip().lower()
    if not text:
        raise copilot_svc.ProposalValidationError("intent is required")
    if any(k in text for k in ("snooze", "再提醒", "提醒", "明天再")):
        return "snooze_task"
    if any(k in text for k in ("assign", "指派", "转给", "交给", "分配", "安排给")):
        return "assign_task"
    if any(k in text for k in ("follow", "跟进", "安排", "follow-up", "followup")):
        return "create_followup_task"
    raise copilot_svc.ProposalValidationError(f"unrecognized copilot intent '{intent}'")


# ---------------------------------------------------------------------------
# deterministic resolution helpers
# ---------------------------------------------------------------------------

def resolve_assignee(db: Session, *, preferred_user_id: int | None = None) -> User:
    """Deterministic assignee resolution.

    ``preferred_user_id`` (already backend-resolved by the caller) is
    validated against active + eligible roles; otherwise the unique active
    secretary (agent) wins; 0 or 2+ candidates -> ``ProposalNeedsClarification``.
    """
    if preferred_user_id is not None:
        user = db.get(User, preferred_user_id)
        if user is None or not user.is_active or user.role.value not in ASSIGNEE_ROLES:
            raise copilot_svc.ProposalValidationError(
                f"assignee user {preferred_user_id} is not an active, eligible assignee"
            )
        return user
    candidates = (
        db.query(User)
        .filter(User.is_active.is_(True), User.role == UserRole.agent)
        .order_by(User.id)
        .all()
    )
    if len(candidates) == 1:
        return candidates[0]
    # No unique active agent -> deterministic fallback to the designated
    # Secretary/Operator (human) identity, if it is active and eligible. This
    # is what makes "安排秘书跟进" resolve deterministically to the real secretary
    # channel instead of surfacing MARIA/DEV-candidate ambiguity. It is NOT a
    # routing engine (requirement #4); general proactive-ops routing stays on
    # the existing DEFAULT_ASSIGNED_USER_ID path.
    if not candidates and SECRETARY_ASSIGNEE_ID is not None and SECRETARY_ASSIGNEE_ID != 14:
        sec = db.get(User, SECRETARY_ASSIGNEE_ID)
        if (
            sec is not None
            and sec.is_active
            and sec.role.value in ASSIGNEE_ROLES
            and sec.username.casefold() != "maria"
        ):
            return sec
    raise ProposalNeedsClarification("no eligible assignee candidate available")


def resolve_snooze_until(
    *, until: datetime | None = None, preset: str | None = None, now: datetime
) -> datetime:
    """Shared snooze-target resolution (mirrors the existing task-snooze
    presets: 1h / today_afternoon / tomorrow_morning / 3d)."""
    if until is not None:
        if until <= now:
            raise copilot_svc.ProposalValidationError(
                "snooze until must be in the future"
            )
        return until
    if preset is None or preset not in SNOOZE_PRESETS:
        raise copilot_svc.ProposalValidationError(
            f"preset must be one of {sorted(SNOOZE_PRESETS)} or provide until"
        )
    if preset == "1h":
        return now + timedelta(hours=1)
    if preset == "today_afternoon":
        target = now.replace(hour=17, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target
    if preset == "tomorrow_morning":
        return (now + timedelta(days=1)).replace(
            hour=9, minute=0, second=0, microsecond=0
        )
    return now + timedelta(days=3)  # "3d"


def _resolve_followup_due(due_at: datetime | None, now: datetime) -> datetime:
    if due_at is not None:
        if due_at <= now:
            raise copilot_svc.ProposalValidationError("due_at must be in the future")
        return due_at
    # Product default: tomorrow 09:00 Asia/Manila (shown on the confirm card).
    manila = now.astimezone(MANILA_TZ)
    target = (manila + timedelta(days=1)).replace(
        hour=DEFAULT_FOLLOWUP_HOUR, minute=0, second=0, microsecond=0
    )
    return target.astimezone(timezone.utc)


def _resolve_followup_target(db: Session, source_type: str, source_id: int):
    source_type = copilot_svc.canonicalize(source_type)
    if source_type not in FOLLOWUP_SOURCE_TYPES:
        raise copilot_svc.ProposalValidationError(
            f"followup source_type must be one of {sorted(FOLLOWUP_SOURCE_TYPES)}"
        )
    target = copilot_svc._resolve_target(db, source_type, source_id)
    if target is None:
        raise copilot_svc.ProposalValidationError(
            f"followup target {source_type}:{source_id} does not exist"
        )
    if source_type == "task" and target.status != OperationalTaskStatus.PENDING:
        raise copilot_svc.ProposalValidationError(
            "cannot follow up on a non-pending task"
        )
    return target


def _followup_display_context(db: Session, source_type: str, target) -> dict:
    if source_type == "lease":
        unit = db.get(Unit, target.unit_id)
        tenant = db.get(Tenant, target.tenant_id)
        return {
            "unit": unit.unit_number if unit else None,
            "tenant": tenant.full_name if tenant else None,
            "lease_id": target.id,
        }
    if source_type == "property":
        return {"property": target.name}
    return {"task_id": target.id, "title": target.title}


# ---------------------------------------------------------------------------
# builders (one per executable action) — all critical fields backend-resolved
# ---------------------------------------------------------------------------

def build_followup_proposal(
    db: Session,
    actor: User,
    *,
    source_type: str,
    source_id: int,
    reason_code: str,
    assignee_user_id: int | None = None,
    due_at: datetime | None = None,
    note: str | None = None,
    expires_at: datetime | None = None,
    now: datetime | None = None,
):
    """Canonical create_followup_task proposal (target + assignee + due +
    property scope all resolved here; the LLM/bot never types them)."""
    now = now or datetime.now(timezone.utc)
    source_type = copilot_svc.canonicalize(source_type)
    target = _resolve_followup_target(db, source_type, source_id)
    assignee = resolve_assignee(db, preferred_user_id=assignee_user_id)
    reason_code = copilot_svc.canonicalize(reason_code or "").strip()
    if not reason_code:
        raise copilot_svc.ProposalValidationError("reason_code is required")
    if len(reason_code) > 50:
        raise copilot_svc.ProposalValidationError("reason_code exceeds 50 chars")
    resolved_due = _resolve_followup_due(due_at, now)
    payload = {
        "action": "create_followup_task",
        "reason_code": reason_code,
        "assignee_user_id": assignee.id,
        "due_at": resolved_due.isoformat(),
        "note": note,
        "display_context": _followup_display_context(db, source_type, target),
    }
    idempotency_key = f"followup:{source_type}:{source_id}:{reason_code}:{actor.id}"
    proposal, created = copilot_svc.create_proposal(
        db,
        actor=actor,
        action_type="create_followup_task",
        target_type=source_type,
        target_id=source_id,
        payload=payload,
        idempotency_key=idempotency_key,
        expires_at=expires_at or (now + PROPOSAL_DEFAULT_TTL),
        now=now,
    )
    return proposal, created, payload


def build_assign_proposal(
    db: Session,
    actor: User,
    *,
    task_ref: int,
    assignee_user_id: int,
    note: str | None = None,
    expires_at: datetime | None = None,
    now: datetime | None = None,
):
    """Canonical assign_task proposal; assignee fully resolved + validated."""
    now = now or datetime.now(timezone.utc)
    task = copilot_svc._resolve_target(db, "task", task_ref)
    if task is None:
        raise copilot_svc.ProposalValidationError(f"task {task_ref} does not exist")
    if task.status != OperationalTaskStatus.PENDING:
        raise copilot_svc.ProposalValidationError("only pending tasks can be reassigned")
    assignee = resolve_assignee(db, preferred_user_id=assignee_user_id)
    payload = {
        "action": "assign_task",
        "assignee_user_id": assignee.id,
        "note": note,
        "display_context": {"task_id": task.id, "title": task.title},
    }
    idempotency_key = f"assign:{task.id}:{assignee.id}:{actor.id}"
    proposal, created = copilot_svc.create_proposal(
        db,
        actor=actor,
        action_type="assign_task",
        target_type="task",
        target_id=task.id,
        payload=payload,
        idempotency_key=idempotency_key,
        expires_at=expires_at or (now + PROPOSAL_DEFAULT_TTL),
        now=now,
    )
    return proposal, created, payload


def build_snooze_proposal(
    db: Session,
    actor: User,
    *,
    task_ref: int,
    until: datetime | None = None,
    preset: str | None = None,
    note: str | None = None,
    expires_at: datetime | None = None,
    now: datetime | None = None,
):
    """Canonical snooze_task proposal; ``until`` is a Manila-aware target time
    (product default when no time is given — the resolved value is shown on
    the confirmation card, never hidden)."""
    now = now or datetime.now(timezone.utc)
    task = copilot_svc._resolve_target(db, "task", task_ref)
    if task is None:
        raise copilot_svc.ProposalValidationError(f"task {task_ref} does not exist")
    if task.status != OperationalTaskStatus.PENDING:
        raise copilot_svc.ProposalValidationError("only pending tasks can be snoozed")
    resolved_until = resolve_snooze_until(until=until, preset=preset, now=now)
    payload = {
        "action": "snooze_task",
        "until": resolved_until.isoformat(),
        "preset": preset,
        "note": note,
        "display_context": {"task_id": task.id, "title": task.title},
    }
    idempotency_key = f"snooze:{task.id}:{resolved_until.isoformat()}:{actor.id}"
    proposal, created = copilot_svc.create_proposal(
        db,
        actor=actor,
        action_type="snooze_task",
        target_type="task",
        target_id=task.id,
        payload=payload,
        idempotency_key=idempotency_key,
        expires_at=expires_at or (now + PROPOSAL_DEFAULT_TTL),
        now=now,
    )
    return proposal, created, payload


def build_proposal_from_intent(
    db: Session,
    actor: User,
    *,
    intent: str,
    source_type: str | None = None,
    source_id: int | None = None,
    task_ref: int | None = None,
    reason_code: str | None = None,
    assignee_user_id: int | None = None,
    due_at: datetime | None = None,
    preset: str | None = None,
    note: str | None = None,
    expires_at: datetime | None = None,
    now: datetime | None = None,
):
    """POST /copilot/recommend dispatcher: intent -> canonical PENDING proposal.

    Returns ``(proposal, created, payload)``.
    """
    action = parse_action_intent(intent)
    if action == "create_followup_task":
        if source_type is None or source_id is None:
            raise copilot_svc.ProposalValidationError(
                "followup requires source_type and source_id"
            )
        return build_followup_proposal(
            db,
            actor,
            source_type=source_type,
            source_id=source_id,
            reason_code=reason_code or "FOLLOWUP",
            assignee_user_id=assignee_user_id,
            due_at=due_at,
            note=note,
            expires_at=expires_at,
            now=now,
        )
    if action == "assign_task":
        if task_ref is None or assignee_user_id is None:
            raise copilot_svc.ProposalValidationError(
                "assign requires task_ref and assignee_user_id"
            )
        return build_assign_proposal(
            db,
            actor,
            task_ref=task_ref,
            assignee_user_id=assignee_user_id,
            note=note,
            expires_at=expires_at,
            now=now,
        )
    if action == "snooze_task":
        if task_ref is None:
            raise copilot_svc.ProposalValidationError("snooze requires task_ref")
        return build_snooze_proposal(
            db,
            actor,
            task_ref=task_ref,
            until=due_at,
            preset=preset,
            note=note,
            expires_at=expires_at,
            now=now,
        )
    raise copilot_svc.ProposalValidationError(f"unrecognized copilot intent '{intent}'")
