"""PASAY-AI-EMPLOYEE-FOUNDATION-007 §8 — Operational Resolver + Self-Healing.

A resolver turns "a business action is blocked because real execution data is
missing/invalid" (a dead end) into ONE actionable prompt and, once the data is
supplied, automatically resumes the blocked action — the user never has to
re-click. No dead-end warnings, ever.

Design (reuses existing storage — ``OperationalTask.details`` JSONB, never a
new large schema; §8 "可复用现有 task/action/metadata"):

- A "blocked issue" is recorded on a task's ``details.blocked`` sub-dict:
  ``issue_type / entity / field / blocked_action / risk_level /
  suggested_fix / resume_ref / created_at / resolved_at``.
- ``resolve_issue`` clears ``details.blocked``, stamps ``resolved_at``, and
  returns the original ``blocked_action`` (``resume_ref``) so the caller can
  run the blocked action automatically (self-healing).
- ``suggested_fix_command`` returns the SHORTEST user input example for an
  issue type (e.g. ``1680 租客电话 09171234567``) — messages that carry it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models.operations import OperationalTask

logger = logging.getLogger(__name__)

BLOCKED_KEY = "blocked"
# risk tiers (PASAY-AI-EMPLOYEE-FOUNDATION-007 §9)
RISK_LOW = "low"
RISK_HIGH = "high"

# issue_type -> (shortest_phrase, display_hint, risk)
_ISSUE_HINTS: dict[str, dict] = {
    "TENANT_PHONE_MISSING": {
        "example": "{unit} 租客电话 09XXXXXXXXX",
        "hint": "缺少租客联系电话",
        "risk": RISK_LOW,
    },
    "TENANT_PHONE_INVALID": {
        "example": "{unit} 租客电话 09XXXXXXXXX",
        "hint": "租客电话无效",
        "risk": RISK_LOW,
    },
    "LEASE_END_MISSING": {
        "example": "{unit} 合同到期 2027-05-31",
        "hint": "合同到期时间缺失",
        "risk": RISK_HIGH,
    },
    "PROPERTY_MGMT_PHONE_MISSING": {
        "example": "{unit} 物业电话 02XXXXXXXX",
        "hint": "物业联系电话缺失",
        "risk": RISK_LOW,
    },
    "EXPENSE_PAYEE_MISSING": {
        "example": "E{expense} 收款方 ABC Aircon",
        "hint": "支出收款方缺失",
        "risk": RISK_LOW,
    },
    "ARCHIVE_CAPTION_MISSING": {
        "example": "{unit} 水表",
        "hint": "档案缺少说明",
        "risk": RISK_LOW,
    },
}


def suggested_fix_command(
    issue_type: str, *, unit: str = "", expense: Optional[int] = None
) -> str:
    """The SHORTEST user reply that resolves an issue (§8 / NO-DEAD-END)."""
    hint = _ISSUE_HINTS.get(issue_type)
    if not hint:
        return ""
    example = hint["example"]
    if "{unit}" in example and unit:
        example = example.replace("{unit}", unit)
    if "{expense}" in example and expense is not None:
        example = example.replace("{expense}", str(expense))
    # Leave any still-unsubstituted placeholder empty so the human only sees a
    # partial example they can complete from context.
    example = example.replace("{unit}", "").replace("{expense}", "")
    return example


def issue_risk(issue_type: str) -> str:
    return _ISSUE_HINTS.get(issue_type, {}).get("risk", RISK_LOW)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def task_blocked(task: OperationalTask) -> dict:
    """The current ``blocked`` metadata on a task (dict or {})."""
    details = task.details or {}
    blocked = details.get(BLOCKED_KEY)
    return blocked if isinstance(blocked, dict) else {}


def create_blocked_issue(
    task: OperationalTask,
    *,
    issue_type: str,
    entity: str,
    field: str,
    blocked_action: str,
    suggested_fix: str = "",
    now: Optional[datetime] = None,
) -> dict:
    """Record a blocked issue on a task (self-healing block step).

    Idempotent per ``(issue_type, entity, field, blocked_action)`` — a repeated
    scan never stacks duplicate blocks. Mutates ``task.details`` in place.
    """
    now = now or _now()
    details = dict(task.details or {})
    current = dict(task_blocked(task))
    current.update(
        {
            "issue_type": issue_type,
            "entity": entity,
            "field": field,
            "blocked_action": blocked_action,
            "risk_level": issue_risk(issue_type),
            "suggested_fix": suggested_fix,
            "resume_ref": blocked_action,
            "created_at": current.get("created_at") or now.isoformat(),
            "resolved_at": None,
        }
    )
    details[BLOCKED_KEY] = current
    task.details = details
    return current


def resolve_issue(task: OperationalTask, *, now: Optional[datetime] = None) -> str | None:
    """Resolve a blocked issue and RETURN the blocked action to auto-resume.

    Clears ``details.blocked``, stamps ``resolved_at`` on the (historically
    kept) block and returns the original ``blocked_action``. Returns None when
    the task was not blocked. The caller executes the returned action so the
    workflow resumes automatically (§2).
    """
    now = now or _now()
    details = dict(task.details or {})
    current = dict(task_blocked(task))
    if not current:
        return None
    resume_ref = current.get("resume_ref") or current.get("blocked_action")
    current["resolved_at"] = now.isoformat()
    blocked_history = list(details.get("blocked_history") or [])
    blocked_history.append(current)
    details["blocked_history"] = blocked_history
    details.pop(BLOCKED_KEY, None)
    task.details = details
    return resume_ref


def scan_missing_and_block(
    db: Session,
    task: OperationalTask,
    *,
    blocked_action: str,
    now: Optional[datetime] = None,
) -> dict | None:
    """Determine the FIRST blocking data gap for a task and block it.

    Returns the blocked metadata dict (or None when not blocked). This is the
    deterministic "AI checks execution materials before handing a task to the
    Secretary" step (§12): phone missing/invalid and lease-end missing are the
    primary gates for rent collection; others extend analogously.
    """
    details = task.details or {}
    lease = None
    tenant = None
    unit = None
    unit_code = details.get("unit_number") or ""
    if task.lease_id is not None:
        from app.models.lease import Lease

        lease = db.get(Lease, task.lease_id)
    if lease is not None:
        from app.models.property import Unit
        from app.models.tenant import Tenant

        unit = db.get(Unit, lease.unit_id)
        tenant = db.get(Tenant, lease.tenant_id)
        unit_code = unit_code or (unit.unit_number if unit else "")
    if not unit_code:
        unit_code = unit.unit_number if unit else ""
    phone = (tenant.phone or "").strip() if tenant else ""
    phone_alt = (tenant.secondary_phone or "").strip() if tenant else ""
    has_phone = bool(phone or phone_alt)
    if not has_phone:
        return create_blocked_issue(
            task,
            issue_type="TENANT_PHONE_MISSING",
            entity=f"tenant:{tenant.id if tenant else '?'}",
            field="phone",
            blocked_action=blocked_action,
            suggested_fix=suggested_fix_command("TENANT_PHONE_MISSING", unit=unit_code),
            now=now,
        )
    # Lease end missing always blocks a rent/lease action.
    if lease is not None and not lease.end_date:
        return create_blocked_issue(
            task,
            issue_type="LEASE_END_MISSING",
            entity=f"lease:{lease.id}",
            field="end_date",
            blocked_action=blocked_action,
            suggested_fix=suggested_fix_command("LEASE_END_MISSING", unit=unit_code),
            now=now,
        )
    return None
