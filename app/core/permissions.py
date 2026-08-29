"""Permission and role guards.

AGENTS.md §4: Permission boundary = Organization + Membership, fail-closed.

API:
- Role enum: OWNER, SECRETARY, TENANT, ADMIN (ADMIN reserved).
- Principal: identity record (user_id, org_id, role, membership_state).
- require_org_scope(principal, org_id): enforce same-org access.
- assert_not_bootstrap_for_secretary: deny bootstrap endpoints to secretary.
- UnknownRoleError: distinct from PermissionDenied for parse failures.

Reviewer finding (PR #100): Role.parse must raise UnknownRoleError (a
ValueError subclass), NOT PermissionDenied, so callers can correctly
classify malformed input (400 Bad Request) vs auth failure (401/403).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class Role(str, Enum):
    """Permission roles."""
    OWNER = "owner"
    SECRETARY = "secretary"
    TENANT = "tenant"
    ADMIN = "admin"

    @classmethod
    def parse(cls, value: Any) -> "Role":
        """Parse a role from str or Role. Raises UnknownRoleError on failure.

        UnknownRoleError is distinct from PermissionDenied so 400 (parse
        failure) is not confused with 401/403 (authorization failure).
        """
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            for member in cls:
                if member.value == normalized:
                    return member
        raise UnknownRoleError(
            f"unknown role: {value!r} (expected one of: "
            f"{[m.value for m in cls]})"
        )


class UnknownRoleError(ValueError):
    """Raised when a role string cannot be parsed into a Role enum member.

    Distinct from PermissionDenied — represents BAD REQUEST (400), not
    unauthorized (401) or forbidden (403).
    """


class PermissionDenied(Exception):
    """Raised when an authorization check fails for a valid identity."""


@dataclass(frozen=True)
class Principal:
    """Identity record used by services to enforce scope.

    AGENTS.md §4: fail-closed. REMOVED memberships are not valid Principals.
    """
    user_id: int
    org_id: int
    role: Role
    membership_state: str = "ACTIVE"

    def __post_init__(self) -> None:
        if isinstance(self.role, str) and not isinstance(self.role, Role):
            object.__setattr__(self, "role", Role.parse(self.role))
        if self.membership_state != "ACTIVE":
            raise PermissionDenied(
                f"membership state {self.membership_state!r} is not ACTIVE"
            )
        if self.role == Role.ADMIN:
            raise PermissionDenied(
                "ADMIN role is reserved and not assignable via API"
            )


def require_org_scope(principal: Principal, org_id: int) -> None:
    """Enforce principal.org_id == org_id. Raises PermissionDenied otherwise.

    This is the canonical org-scope guard used by every service.
    """
    if not isinstance(principal, Principal):
        raise PermissionDenied("invalid principal")
    if principal.org_id != org_id:
        raise PermissionDenied(
            f"cross-org access denied: principal org_id={principal.org_id} "
            f"target org_id={org_id}"
        )


def assert_not_bootstrap_for_secretary(
    principal: Principal, *, is_bootstrap: bool
) -> None:
    """Deny bootstrap endpoints to SECRETARY role."""
    if is_bootstrap and principal.role == Role.SECRETARY:
        raise PermissionDenied("secretary cannot perform bootstrap operations")