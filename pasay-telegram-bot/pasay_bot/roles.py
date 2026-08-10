"""Telegram-user role resolution (deterministic, unit-testable).

Phase source of truth: a hard-coded copy of the pre-existing ``roles.json``
mapping (OWNER / SECRETARY by Telegram user id) so the logic can be unit
tested. The backend API key is the *real* enforcement point — the bot only
uses this map to hide/show UI and to refuse writes before they reach the API.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional


class Role(str, Enum):
    OWNER = "owner"
    SECRETARY = "secretary"


# Telegram user id -> role (from the pre-existing roles.json).
TELEGRAM_USER_ID_TO_ROLE: dict[int, Role] = {
    5177241442: Role.OWNER,     # 全权
    1083657401: Role.SECRETARY,  # 录收入，不能 confirm/finalize
}

PERMISSION_READ = frozenset({"properties", "finance", "overdue"})
PERMISSION_RENT_ENTRY = "rent_entry"
PERMISSION_RENT_CONFIRM = "rent_confirm"
PERMISSION_REVERSE = "reverse"

# Mirrors the backend user_role enum (agent/manager/admin). The backend
# enforces these against the API key; the bot mirrors them for UI + fast
# refusal of hand-crafted callbacks.
API_ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "agent": frozenset(PERMISSION_READ),
    "manager": frozenset(
        {*PERMISSION_READ, PERMISSION_RENT_ENTRY, PERMISSION_RENT_CONFIRM}
    ),
    "admin": frozenset(
        {
            *PERMISSION_READ,
            PERMISSION_RENT_ENTRY,
            PERMISSION_RENT_CONFIRM,
            PERMISSION_REVERSE,
        }
    ),
}

ROLE_PERMISSIONS: dict[Role, frozenset[str]] = {
    # OWNER: full access.
    Role.OWNER: API_ROLE_PERMISSIONS["admin"],
    # SECRETARY: can record income (create pending) but not confirm/finalize.
    Role.SECRETARY: frozenset({*PERMISSION_READ, PERMISSION_RENT_ENTRY}),
}

ROLE_LOCALES: dict[Role, str] = {
    Role.OWNER: "zh",
    Role.SECRETARY: "en",
}


def role_for_telegram_id(telegram_id: Optional[int]) -> Optional[Role]:
    if telegram_id is None:
        return None
    return TELEGRAM_USER_ID_TO_ROLE.get(telegram_id)


def has_permission(role: Optional[Role], permission: str) -> bool:
    if role is None:
        return False
    return permission in ROLE_PERMISSIONS.get(role, frozenset())


def has_read_permission(role: Optional[Role]) -> bool:
    """True when the role may access the read-only pages (F4): OWNER and
    SECRETARY both hold the full PERMISSION_READ set; unknown users do not."""
    if role is None:
        return False
    return PERMISSION_READ.issubset(ROLE_PERMISSIONS.get(role, frozenset()))


def locale_for(role: Optional[Role]) -> str:
    return ROLE_LOCALES.get(role, "zh")


def permissions_for_api_role(api_role: Optional[str]) -> frozenset[str]:
    """Permission set that the backend would grant for a given API-key role."""
    if api_role is None:
        return frozenset()
    return API_ROLE_PERMISSIONS.get(api_role, frozenset())
