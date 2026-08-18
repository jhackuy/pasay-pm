"""REPAIR-AI-EMPLOYEE-WORKFLOW-008A — Repair Operation state machine.

The Repair Operation is the REAL-world problem. Its business status expresses
where the problem sits in the real world, and the derived AI-employee fields
(``next_action`` / ``waiting_on`` / ``blocked_reason``) tell a human exactly
what to do next.

Crucial 008A invariants (all enforced here + tested):
- **Rejecting a proposal never closes and never cancels the repair.**
- **Paying an expense never closes the repair.** Payment is only one
  prerequisite; the repair may advance to VERIFYING but never straight to
  CLOSED.
- **CLOSED is reachable ONLY through the verification gate** (a human
  confirmation / a credible structured completion event). It is never
  reached by "proposal approved" / "payment paid" / "reminder sent" /
  "vendor contacted".
- **Approve / Pay / Reject do not REJECT the repair** either — a rejected
  quote just moves the repair into a requote/get-quote continuation.

Transitions (terminal statuses CLOSED/CANCELLED are absorbing):

- OPEN
- IN_PROGRESS               (work started / vendor engaged)
- WAITING_HUMAN             (a real person must act next, e.g. requote)
- WAITING_VENDOR            (waiting on vendor quote / vendor work)
- WAITING_APPROVAL          (a proposal is pending owner approval)
- WAITING_PAYMENT           (a quote was approved; expense pending payment)
- VERIFYING                 (work/payment done; awaiting real verification)
- CLOSED                    (verification recorded)
- CANCELLED                 (repair explicitly dropped, not just a rejection)
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum


class TransitionError(Exception):
    """Illegal repair operation transition (business rule violation)."""


class ClosureSignal(str, Enum):
    # Human / evidence confirmations that ARE allowed to close a repair.
    HUMAN_CONFIRMED = "HUMAN_CONFIRMED"
    COMPLETION_EVENT = "COMPLETION_EVENT"


# Statuses where the operation is still "alive" (able to move).
ACTIVE_STATUSES = {
    "OPEN",
    "IN_PROGRESS",
    "WAITING_HUMAN",
    "WAITING_VENDOR",
    "WAITING_APPROVAL",
    "WAITING_PAYMENT",
    "VERIFYING",
}
TERMINAL_STATUSES = {"CLOSED", "CANCELLED"}

# Explicit transition table. ``None`` = that transition is forbidden.
# Keys: (current, next) -> event label.
_ALLOWED_TRANSITIONS: dict[tuple[str, str], str] = {
    ("OPEN", "IN_PROGRESS"): "work_started",
    ("OPEN", "WAITING_APPROVAL"): "proposal_submitted",
    ("OPEN", "WAITING_VENDOR"): "vendor_engaged",
    ("OPEN", "WAITING_HUMAN"): "awaiting_human",
    ("OPEN", "VERIFYING"): "awaiting_verification",
    ("OPEN", "CANCELLED"): "cancelled",

    ("IN_PROGRESS", "OPEN"): "reopened",
    ("IN_PROGRESS", "WAITING_VENDOR"): "vendor_engaged",
    ("IN_PROGRESS", "WAITING_APPROVAL"): "proposal_submitted",
    ("IN_PROGRESS", "WAITING_HUMAN"): "awaiting_human",
    ("IN_PROGRESS", "WAITING_PAYMENT"): "awaiting_payment",
    ("IN_PROGRESS", "VERIFYING"): "awaiting_verification",
    ("IN_PROGRESS", "CANCELLED"): "cancelled",

    ("WAITING_HUMAN", "IN_PROGRESS"): "work_started",
    ("WAITING_HUMAN", "WAITING_APPROVAL"): "proposal_submitted",
    ("WAITING_HUMAN", "WAITING_VENDOR"): "vendor_engaged",
    ("WAITING_HUMAN", "VERIFYING"): "awaiting_verification",
    ("WAITING_HUMAN", "CANCELLED"): "cancelled",

    ("WAITING_VENDOR", "IN_PROGRESS"): "work_started",
    ("WAITING_VENDOR", "WAITING_APPROVAL"): "quote_received",
    ("WAITING_VENDOR", "WAITING_HUMAN"): "awaiting_human",
    ("WAITING_VENDOR", "VERIFYING"): "awaiting_verification",
    ("WAITING_VENDOR", "CANCELLED"): "cancelled",

    ("WAITING_APPROVAL", "IN_PROGRESS"): "work_started",
    ("WAITING_APPROVAL", "WAITING_HUMAN"): "proposal_rejected",
    ("WAITING_APPROVAL", "WAITING_PAYMENT"): "proposal_approved",
    ("WAITING_APPROVAL", "VERIFYING"): "awaiting_verification",
    ("WAITING_APPROVAL", "CANCELLED"): "cancelled",

    ("WAITING_PAYMENT", "VERIFYING"): "payment_paid",
    ("WAITING_PAYMENT", "WAITING_HUMAN"): "rework_needed",
    ("WAITING_PAYMENT", "IN_PROGRESS"): "budget_refused_but_repair_continues",
    ("WAITING_PAYMENT", "CANCELLED"): "cancelled",

    ("VERIFYING", "WAITING_HUMAN"): "rework_needed",
    ("VERIFYING", "CLOSED"): "verified",
    ("VERIFYING", "IN_PROGRESS"): "rework_needed",
    ("VERIFYING", "CANCELLED"): "cancelled",

    # CANCELLED -> nothing; CLOSED -> nothing (absorbing).
}

# Default human-facing "waiting on" value for each state (overridable).
WAITING_ON_BY_STATUS = {
    "OPEN": None,
    "IN_PROGRESS": "vendor",
    "WAITING_HUMAN": "secretary",
    "WAITING_VENDOR": "vendor",
    "WAITING_APPROVAL": "owner",
    "WAITING_PAYMENT": "payer",
    "VERIFYING": "secretary",
    "CLOSED": None,
    "CANCELLED": None,
}


def can_transition(current: str, next_status: str) -> bool:
    return (current, next_status) in _ALLOWED_TRANSITIONS


def is_active(status: str) -> bool:
    return status in ACTIVE_STATUSES


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES


def transition_label(current: str, next_status: str) -> str | None:
    return _ALLOWED_TRANSITIONS.get((current, next_status))


def default_waiting_on(status: str) -> str | None:
    return WAITING_ON_BY_STATUS.get(status)


def _require_alive(current: str):
    if current in TERMINAL_STATUSES:
        raise TransitionError(
            f"Repair operation is {current} (terminal) and cannot move"
        )


def transition_to(
    current: str,
    next_status: str,
    *,
    reason: str | None = None,
    now: datetime | None = None,
) -> tuple[str, str]:
    """Validate + describe a status transition. Returns (event, next_status).

    Raises ``TransitionError`` on any illegal move. ``reason`` is carried for
    audit/timeline but the transition table is the source of truth.
    """
    now = now or datetime.now(timezone.utc)
    if next_status not in _ALLOWED_TRANSITIONS and next_status not in TERMINAL_STATUSES:
        raise TransitionError(f"Unknown repair status {next_status!r}")
    if current == next_status:
        # Idempotent same-state requests are allowed as no-ops.
        return "noop", current
    _require_alive(current)
    label = _ALLOWED_TRANSITIONS.get((current, next_status))
    if label is None:
        raise TransitionError(
            f"Illegal repair transition {current} -> {next_status} "
            f"(reason={reason or 'none'})"
        )
    return label, next_status


def ensure_closable_via_verification(signal: str) -> None:
    """Guard: only a real verification signal may close a repair.

    Any caller that tries to CLOSE a repair must present one of the allowed
    closure signals (human confirmation or a credible completion event).
    """
    if signal not in {s.value for s in ClosureSignal}:
        raise TransitionError(
            "Repair may only be CLOSED through verification "
            f"(human confirmation or completion event); got {signal!r}"
        )
