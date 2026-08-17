"""REPAIR-AI-EMPLOYEE-WORKFLOW-008A — deterministic Telegram Repair fast-path.

Telegram shows ONLY the single most important next action (008A §8). The text
here is DERIVED from real business state (the RepairOperation row + its active
RepairAction), never hand-typed chat copy — so the bot and the Mini App agree
on the same truth.

The Owner never needs to understand the internal Proposal/Operation model; the
Secretary gets the minimal actionable card (rejected quote -> get another
quote -> repair stays open).

Additive and isolated: nothing else imports this module yet, so it cannot
affect existing handlers. Existing callers wire it in when a repair fast path
is surfaced.
"""
from __future__ import annotations

from typing import Optional

from pasay_bot.api_client import RepairAction, RepairOperation

# Human-facing status labels (never leak raw enum values to the Owner).
_STATUS_LABEL = {
    "OPEN": "Open",
    "IN_PROGRESS": "In progress",
    "WAITING_HUMAN": "Waiting on action",
    "WAITING_VENDOR": "Waiting on vendor",
    "WAITING_APPROVAL": "Waiting on your approval",
    "WAITING_PAYMENT": "Waiting on payment",
    "VERIFYING": "Awaiting verification",
    "CLOSED": "Closed",
    "CANCELLED": "Cancelled",
}


def _amount(value) -> str:
    try:
        d = round(float(value or 0), 2)
        return f"₱{d:,.2f}"
    except (TypeError, ValueError):
        return ""


def status_label(status: str) -> str:
    return _STATUS_LABEL.get(status, status)


def _active_action(repair: RepairOperation) -> Optional[RepairAction]:
    """The single ACTIVE human action (dedup guarantees at most one per step)."""
    for action in repair.actions:
        if action.status in ("PENDING", "IN_PROGRESS"):
            return action
    return None


def render_repair_card(repair: RepairOperation) -> str:
    """One short card for a repair — the most important next thing to know.

    English Secretary-facing, mirroring the existing bilingual product copy.
    For a rejected-quote repair this is the §8 example verbatim in meaning:
        🔧 Aircon repair · 1608
        Owner rejected the ₱8,000 quote.
        Reason: Too expensive.
        Please get another quote.
        Repair remains open.
    """
    action = _active_action(repair)
    lines = [f"🔧 {repair.issue}"]
    unit = ""
    if repair.unit_id:
        unit = f" #{repair.unit_id}"
    # Latest rejected approval reason (for a requote card).
    rejection = None
    for proposal in repair.proposals:
        if proposal.status == "REJECTED" and proposal.rejection_reason:
            rejection = proposal
    if rejection is not None:
        lines.append(f"Owner rejected the {_amount(rejection.amount)} quote" + unit + ".")
        if rejection.rejection_reason:
            lines.append(f"Reason: {rejection.rejection_reason}.")
    elif action is not None:
        lines.append(action.title)
    # Only the real next action, straight from the AI-employee derived state.
    if repair.next_action:
        lines.append("")
        lines.append(repair.next_action)
    if action is not None:
        lines.append("")
        lines.append("Please get another quote." if action.action_kind == "REQUOTE"
                     else action.title)
    if repair.status in ("OPEN", "IN_PROGRESS", "WAITING_HUMAN", "WAITING_VENDOR",
                         "WAITING_APPROVAL", "WAITING_PAYMENT", "VERIFYING"):
        lines.append("")
        lines.append("Repair remains open.")
    else:
        lines.append("")
        lines.append(f"Status: {status_label(repair.status)}.")
    return "\n".join(lines)


def render_repair_status_line(repair: RepairOperation) -> str:
    """A one-line wait/next-step summary (for list views). Real state only."""
    where = ""
    waiting = repair.waiting_on
    if repair.status == "CLOSED":
        where = "closed"
    elif repair.status == "CANCELLED":
        where = "cancelled"
    elif waiting:
        where = f"waiting on {waiting}"
    else:
        where = repair.next_action or "open"
    return f"{repair.issue} · {status_label(repair.status)} ({where})"
