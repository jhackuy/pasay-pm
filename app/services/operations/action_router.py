"""PASAY-AI-EMPLOYEE-FOUNDATION-007 §19 — Action Router foundation.

A lightweight responsibility router that answers "who executes this action?".
Only two routes are foundational (no full module migration):
  RENT_FOLLOWUP        -> SECRETARY (the human who actually contacts the tenant)
  EXPENSE_OWNER_PAYMENT-> OWNER    (only the Owner pays out)

The interface is extensible so future routes (REPAIR -> Secretary/Assignee,
LEASE_RENEWAL -> Secretary, APPROVAL -> Owner) can be added without a rewrite.
"""
from __future__ import annotations

import enum


class Responsibility(str, enum.Enum):
    SECRETARY = "SECRETARY"
    OWNER = "OWNER"
    ASSIGNEE = "ASSIGNEE"


class ActionType(str, enum.Enum):
    RENT_FOLLOWUP = "RENT_FOLLOWUP"
    EXPENSE_OWNER_PAYMENT = "EXPENSE_OWNER_PAYMENT"
    REPAIR = "REPAIR"
    LEASE_RENEWAL = "LEASE_RENEWAL"
    APPROVAL = "APPROVAL"


# Canonical routing table (foundation routes only; the rest are stubs that
# raise ``RouteNotRouted`` until a future task wires them).
_ROUTES: dict[ActionType, Responsibility] = {
    ActionType.RENT_FOLLOWUP: Responsibility.SECRETARY,
    ActionType.EXPENSE_OWNER_PAYMENT: Responsibility.OWNER,
}


class RouteNotRouted(Exception):
    """The action type has no canonical route yet (foundation scope)."""


def route_action(action_type: ActionType) -> Responsibility:
    """Return the canonical responsibility for an action type.

    Deterministic; a not-yet-routed type raises ``RouteNotRouted`` so the
    caller fails closed instead of guessing.
    """
    try:
        key = ActionType(action_type)
    except (ValueError, TypeError):
        raise RouteNotRouted(f"unknown action type {action_type!r}")
    r = _ROUTES.get(key)
    if r is None:
        raise RouteNotRouted(f"action type {key.value} is not routed yet")
    return r


def is_routed(action_type: ActionType) -> bool:
    try:
        route_action(action_type)
        return True
    except RouteNotRouted:
        return False


def route_code(action_type: ActionType) -> str:
    """Stable route code for audit/provenance: ``RENT_FOLLOWUP->SECRETARY``."""
    return f"{ActionType(action_type).value}->{route_action(action_type).value}"
