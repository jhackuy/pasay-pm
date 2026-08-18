"""Shared rent follow-up truth helpers.

One computed snapshot drives both the Owner text state and the actionable
buttons. This keeps the UI aligned to the same authority:
OperationalTask truth + the persisted same-day executed mark.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from pasay_bot.state.store import PH_TZ, ph_local_date


@dataclass(frozen=True)
class FollowupSnapshot:
    status: str
    actionable: bool
    reason: str
    task_id: int | None
    last_followup_at: str

    @property
    def assigned(self) -> bool:
        return self.status == "assigned"

    @property
    def followed_up_today(self) -> bool:
        return self.status == "followed_up_today"


def _unit_key(value: str | None) -> str:
    return str(value or "").split("-")[-1].strip()


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=PH_TZ)
    return stamp.astimezone(PH_TZ)


def _same_ph_day(value: str | None, today: str) -> bool:
    stamp = _parse_dt(value)
    if stamp is None:
        return False
    return stamp.astimezone(PH_TZ).date().isoformat() == today


def _display_followup(value: str | None) -> str:
    stamp = _parse_dt(value)
    if stamp is None:
        return ""
    return stamp.strftime("%Y-%m-%d %H:%M")


def iter_followup_tasks(tasks: Iterable, leases: Iterable, unit_id: int, unit_code: str):
    """Yield all candidate follow-up tasks matching one unit."""
    lease_id = None
    for lease in leases:
        if getattr(lease, "unit_id", None) == unit_id and getattr(lease, "status", "") == "active":
            lease_id = getattr(lease, "id", None)
            break
    key = _unit_key(unit_code)
    for task in tasks:
        task_type = str(getattr(task, "task_type", "") or "").upper()
        if task_type not in ("RENT_OVERDUE", "FOLLOWUP"):
            continue
        details = getattr(task, "details", None) or {}
        task_unit_key = _unit_key(details.get("unit_number"))
        if lease_id is not None and getattr(task, "lease_id", None) != lease_id:
            if task_unit_key != key:
                continue
        elif lease_id is None and task_unit_key != key:
            continue
        yield task


def match_followup_task(tasks: Iterable, leases: Iterable, unit_id: int, unit_code: str):
    """Return the most relevant follow-up task for one unit, if any."""
    snapshot = compute_followup_snapshot(tasks, leases, None, unit_id, unit_code)
    if snapshot.task_id is None:
        return None
    for task in iter_followup_tasks(tasks, leases, unit_id, unit_code):
        if getattr(task, "id", None) == snapshot.task_id:
            return task
    return None


def followup_assigned(task) -> bool:
    if task is None:
        return False
    if str(getattr(task, "status", "") or "").upper() == "COMPLETED":
        return False
    details = getattr(task, "details", None) or {}
    return bool(details.get("assigned_to"))


def followup_completed_today(task, store, unit_id: int) -> bool:
    today = ph_local_date()
    if today and store is not None:
        try:
            if store.is_marked_daily(f"followup:{unit_id}:{today}"):
                return True
        except Exception:  # noqa: BLE001 - best effort
            pass
    if task is None or not today:
        return False
    if str(getattr(task, "status", "") or "").upper() != "COMPLETED":
        return False
    completed_at = getattr(task, "completed_at", None) or (getattr(task, "details", None) or {}).get("executed_at")
    return _same_ph_day(completed_at, today)


def compute_followup_snapshot(
    tasks: Iterable,
    leases: Iterable,
    store,
    unit_id: int,
    unit_code: str,
    *,
    last_followup_at: str | None = None,
) -> FollowupSnapshot:
    """Compute the one authoritative follow-up snapshot for a unit."""
    today = ph_local_date()
    matched = list(iter_followup_tasks(tasks, leases, unit_id, unit_code))
    assigned_candidates = [
        task for task in matched
        if followup_assigned(task)
    ]
    completed_candidates = [
        task for task in matched
        if str(getattr(task, "status", "") or "").upper() == "COMPLETED"
    ]
    assigned_task = max(
        assigned_candidates,
        key=lambda task: _parse_dt((getattr(task, "details", None) or {}).get("assigned_at")) or datetime.min.replace(tzinfo=PH_TZ),
        default=None,
    )
    completed_task = max(
        completed_candidates,
        key=lambda task: _parse_dt(
            getattr(task, "completed_at", None) or (getattr(task, "details", None) or {}).get("executed_at")
        ) or datetime.min.replace(tzinfo=PH_TZ),
        default=None,
    )
    persisted_today = False
    if store is not None:
        try:
            persisted_today = store.is_marked_daily(f"followup:{unit_id}:{today}")
        except Exception:  # noqa: BLE001 - best effort only
            persisted_today = False
    completed_today = False
    if completed_task is not None:
        completed_today = followup_completed_today(completed_task, store, unit_id)
    display_last_followup = (last_followup_at or "").strip()
    if not display_last_followup and completed_task is not None:
        display_last_followup = _display_followup(
            getattr(completed_task, "completed_at", None)
            or (getattr(completed_task, "details", None) or {}).get("executed_at")
        )
    if persisted_today or completed_today:
        reason = "persisted_same_day_mark" if persisted_today else "completed_today"
        task_id = getattr(completed_task, "id", None) or getattr(assigned_task, "id", None)
        return FollowupSnapshot(
            status="followed_up_today",
            actionable=False,
            reason=reason,
            task_id=task_id,
            last_followup_at=display_last_followup,
        )
    if assigned_task is not None:
        return FollowupSnapshot(
            status="assigned",
            actionable=False,
            reason="assigned_to_secretary",
            task_id=getattr(assigned_task, "id", None),
            last_followup_at=display_last_followup,
        )
    reason = "completed_before_today" if completed_task is not None else "actionable_now"
    return FollowupSnapshot(
        status="actionable",
        actionable=True,
        reason=reason,
        task_id=getattr(completed_task, "id", None),
        last_followup_at=display_last_followup,
    )
