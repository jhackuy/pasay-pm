"""PASAY reference implementation — permissions / org scope guards.

Issue #99 / PR #100 /oc continuation. Staged under
``.opencode-qualification/reference/`` pending Owner-side promotion to
``app/core/permissions.py``.

Hard invariants enforced by this module:
    * Organization + Membership is the SOLE business permission boundary.
      Telegram IDs are user identifiers only; they are NOT business roles.
    * Every guard is fail-closed: missing or malformed input raises
      ``PermissionDenied`` — it NEVER falls through to "allow".
    * Bootstrap endpoints are OWNER-only. Secretary role MUST NOT trigger
      bootstrap even if explicitly requested.
    * Cross-organization access is denied even if the user holds an active
      membership in the target org — only the org in the principal's
      principal-binding is the scope.

Reference promotion to ``app/core/permissions.py`` requires no behavioural
change.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional

# Role semantics — frozen per project_rules.md §5.4 and PRODUCT_RULES.md.
class Role(str, Enum):
    OWNER = "OWNER"
    SECRETARY = "SECRETARY"
    TENANT = "TENANT"

    @classmethod
    def parse(cls, value: object) -> "Role":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value.upper())
            except ValueError:
                pass
        raise PermissionDenied(f"unknown role: {value!r}")


class MembershipState(str, Enum):
    ACTIVE = "ACTIVE"
    REMOVED = "REMOVED"


class PermissionDenied(PermissionError):
    """Raised when a permission check fails. Fail-closed."""


@dataclass(frozen=True)
class Principal:
    """The authenticated caller bound to exactly one Organization.

    Constructed ONLY by the auth layer (JWT verification + DB lookup).
    Other modules must NOT instantiate Principal directly except in tests.
    """

    user_id: str
    org_id: str
    role: Role
    membership_state: MembershipState = MembershipState.ACTIVE
    telegram_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.user_id, str) or not self.user_id.strip():
            raise PermissionDenied("Principal.user_id is required")
        if not isinstance(self.org_id, str) or not self.org_id.strip():
            raise PermissionDenied("Principal.org_id is required")
        # Role is normalised by the enum coercion above; trust the type.
        if not isinstance(self.role, Role):
            raise PermissionDenied(f"Principal.role invalid: {self.role!r}")
        if not isinstance(self.membership_state, MembershipState):
            raise PermissionDenied(
                f"Principal.membership_state invalid: {self.membership_state!r}"
            )
        if self.membership_state is not MembershipState.ACTIVE:
            raise PermissionDenied(
                "Principal.membership_state must be ACTIVE to act; "
                f"got {self.membership_state.value}"
            )


def require_role(principal: Principal, allowed: Iterable[Role]) -> None:
    """Fail-closed role check. Raises ``PermissionDenied`` on mismatch."""
    if principal is None:
        raise PermissionDenied("no principal")
    allowed_set = set(allowed)
    if principal.role not in allowed_set:
        raise PermissionDenied(
            f"role {principal.role.value!r} not in {{"
            + ", ".join(r.value for r in allowed_set)
            + "}"
        )


def require_owner(principal: Principal) -> None:
    require_role(principal, {Role.OWNER})


def require_owner_or_secretary(principal: Principal) -> None:
    require_role(principal, {Role.OWNER, Role.SECRETARY})


def require_org_scope(principal: Principal, target_org_id: str) -> None:
    """Deny cross-org access even if principal is active in target.

    Raises ``PermissionDenied`` unless ``target_org_id == principal.org_id``.
    """
    if principal is None:
        raise PermissionDenied("no principal")
    if not isinstance(target_org_id, str) or not target_org_id.strip():
        raise PermissionDenied("target_org_id is required")
    if principal.org_id != target_org_id:
        raise PermissionDenied("cross-org access denied")


def require_membership_active(principal: Principal) -> None:
    """Defence in depth — Principal already enforces ACTIVE in __post_init__."""
    if principal.membership_state is not MembershipState.ACTIVE:
        raise PermissionDenied(
            f"membership is {principal.membership_state.value}; ACTIVE required"
        )


def is_owner(principal: Principal) -> bool:
    try:
        require_owner(principal)
        return True
    except PermissionDenied:
        return False


def is_bootstrap_endpoint(endpoint_name: str) -> bool:
    """Return True iff the endpoint is an OWNER-only bootstrap path.

    Secretary MUST be blocked even with explicit request.
    """
    if not isinstance(endpoint_name, str):
        return False
    name = endpoint_name.strip().lower()
    return name in {
        "bootstrap.create_organization",
        "bootstrap.invite_secretary",
        "bootstrap.remove_membership",
        "bootstrap.transfer_ownership",
    }


def assert_not_bootstrap_for_secretary(
    principal: Principal, endpoint_name: str
) -> None:
    if is_bootstrap_endpoint(endpoint_name) and principal.role is not Role.OWNER:
        raise PermissionDenied(
            f"bootstrap endpoint {endpoint_name!r} requires OWNER role"
        )


__all__ = [
    "Role",
    "MembershipState",
    "PermissionDenied",
    "Principal",
    "require_role",
    "require_owner",
    "require_owner_or_secretary",
    "require_org_scope",
    "require_membership_active",
    "is_owner",
    "is_bootstrap_endpoint",
    "assert_not_bootstrap_for_secretary",
]
