"""Permission and role guards.

AGENTS.md §4: Permission boundary = Organization + Membership, fail-closed.

API:
- Role enum: OWNER, SECRETARY only (DATA_CONTRACT §2.5). No TENANT, no
  ADMIN. ADMIN was a reserved placeholder and is removed entirely.
- Principal: identity record (user_id, org_id, role, membership_state).
- SystemPrincipal: NON-HUMAN scheduled-job identity record (credential +
  purpose name). Issue #119 P0 v1_api_credential_bootstrap.
- require_org_scope(principal, org_id): enforce same-org access.
- assert_not_bootstrap_for_secretary: deny bootstrap endpoints to secretary.
- UnknownRoleError: distinct from PermissionDenied for parse failures.

Reviewer finding (PR #100): Role.parse must raise UnknownRoleError (a
ValueError subclass), NOT PermissionDenied, so callers can correctly
classify malformed input (400 Bad Request) vs auth failure (401/403).

DATA_CONTRACT §2.5: Role enum values are UPPERCASE ("OWNER", "SECRETARY")
to match the DB CHECK constraints in app.v1.models.foundation. The parse
method still accepts lowercase aliases ("owner", "secretary") for caller
convenience.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, TYPE_CHECKING


if TYPE_CHECKING:
    # Avoid a runtime import cycle: app.v1.models.foundation imports
    # from app.v1.models.base, which does NOT import permissions, so a
    # direct import here is safe at runtime — TYPE_CHECKING is just to
    # keep mypy happy without forcing it.
    from app.v1.models.foundation import ApiCredential


class UnknownRoleError(ValueError):
    """Raised when a role string cannot be parsed into a Role enum member.

    Distinct from PermissionDenied — represents BAD REQUEST (400), not
    unauthorized (401) or forbidden (403).
    """


class Role(str, Enum):
    """Permission roles. OWNER + SECRETARY only (DATA_CONTRACT §2.5)."""
    OWNER = "OWNER"
    SECRETARY = "SECRETARY"

    @classmethod
    def parse(cls, value: Any) -> "Role":
        """Parse a role from str or Role. Raises UnknownRoleError on failure.

        Accepts:
        - Role instance (returned as-is).
        - Exact uppercase member value: "OWNER", "SECRETARY".
        - Lowercase alias: "owner", "secretary".
        - Whitespace-padded variants of either.

        Raises UnknownRoleError (a ValueError subclass, NOT a
        PermissionDenied) for any string not matching the enum, including
        the legacy reserved values "ADMIN", "TENANT", the empty string,
        and None. This keeps 400 (parse failure) cleanly distinguishable
        from 401/403 (authorization failure).
        """
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            normalized = value.strip()
            for member in cls:
                # Match exact member value (uppercase, e.g. "OWNER") or
                # lowercase alias (e.g. "owner"). Reject everything else.
                if (
                    normalized == member.value
                    or normalized.lower() == member.value.lower()
                ):
                    return member
        raise UnknownRoleError(
            f"unknown role: {value!r} (expected one of: "
            f"{[m.value for m in cls]})"
        )


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


@dataclass(frozen=True)
class SystemPrincipal:
    """Identity record for a NON-HUMAN SYSTEM scheduled-job credential.

    Issue #119 P0 v1_api_credential_bootstrap: the JOB-SERVICE-AUTH-002
    contract requires that scheduled jobs authenticate as a SYSTEM
    principal that does NOT bind a Telegram id, does NOT resolve to a
    user, and does NOT carry a Role. Carryover to V1: this dataclass
    holds the canonical credential row + the purpose name (e.g.
    ``internal:scheduler``); ``user_id`` and ``org_id`` are deliberately
    not part of the surface because no HUMAN identity is behind a
    SYSTEM credential.

    Use this class to:
    - Distinguish a SYSTEM caller from a HUMAN caller at the dep level
      (``isinstance(principal, Principal)`` is False for SystemPrincipal
      — the canonical test for "this caller is not a human").
    - Audit / log the credential id without exposing the raw key (the
      credential row has the SHA-256 hash, not the raw bearer).
    - Pass through ``require_org_scope`` if/when a SYSTEM read needs
      org scope; the caller is expected to know the org_id explicitly
      because SystemPrincipal does not carry one.
    """
    credential: "ApiCredential"
    name: str

    @property
    def credential_id(self) -> int:
        return int(self.credential.id)


def require_org_scope(principal: Any, org_id: int) -> None:
    """Enforce principal.org_id == org_id. Raises PermissionDenied otherwise.

    For SystemPrincipal: there is no org_id; this guard is a NO-OP for
    SYSTEM callers (the caller is expected to pass an explicit
    ``target_org_id`` to the underlying service). The guard never
    raises for SYSTEM callers.
    """
    if isinstance(principal, SystemPrincipal):
        # SYSTEM callers carry no org_id; the caller's service layer is
        # responsible for choosing the right target org.
        return
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
