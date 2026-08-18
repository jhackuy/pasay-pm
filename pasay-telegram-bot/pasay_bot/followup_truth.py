"""Shared rent follow-up truth helpers.

These helpers keep the Owner/Secretary follow-up UI aligned to the same
authoritative state: operational task truth plus the persisted same-day
execution mark. They do not invent a second workflow state.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable


def _unit_key(value: str | None) -> str:
    return str(value or "").split("-")[-1].strip()


def match_followup_task(tasks: Iterable, leases: Iterable, unit_id: int, unit_code: str):
    """Return the authoritative follow-up task for one unit, if any."""
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
    try:
        from pasay_bot.state.store import ph_local_date

        today = ph_local_date()
    except Exception:  # noqa: BLE001 - callers fall back to task truth only
        today = ""
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
    completed_at = getattr(task, "completed_at", None)
    if not completed_at:
        return False
    try:
        stamp = datetime.fromisoformat(str(completed_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    return stamp.date().isoformat() == today

