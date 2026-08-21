"""PASAY-TASK-006 Onboarding P0 — service layer coordinator.

**Scope (CONFIRMED BY ISSUE #24 CONTRACT)**:
1. New entry role path: Owner / Secretary split.
2. Owner path:
   - 0 active memberships -> prompt to create organization.
   - 1+ active memberships -> direct to existing org, NEVER re-bootstrap.
3. Secretary path:
   - NEVER expose "Create company" semantics.
   - NEVER call bootstrap_first_owner.
   - ONLY join via valid SecretaryInvite code.
   - No invite -> English hint "Ask your Owner to invite you to the workspace."
   - Valid invite -> accept via existing accept_secretary_invite helper.
4. Guard:
   - Bootstrap endpoint raises BootstrapForbidden when user already holds
     ANY active membership in ANY organization.
   - REMOVED Secretary re-using an old ACCEPTED invite is rejected via
     the membership service's InviteConsumed rule (one-time key).
5. Language: Owner zh, Secretary en (literal strings only, no i18n).

**Explicit NON-goals (Issue "严格禁止扩大范围")**:
- NO Property / Channel / Rent / Expense / Repair / Task logic.
- NO Telegram 3x2 menu changes.
- NO Mini App IA restructure.
- NO new invite model (fully reuses SecretaryInvite).
- NO Redis / queue / extra services.
- NO OpenDesign edits.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models.membership import (
    InviteState,
    Membership,
    MembershipState,
    Organization,
    OrganizationRole,
    SecretaryInvite,
)
from app.models.user import User, UserRole
from app.schemas.onboarding import (
    MembershipRef,
    OnboardingStateResponse,
    OrganizationRef,
    SecretaryAcceptInviteResponse,
    OwnerBootstrapResponse,
)
from app.services.membership import (
    AlreadyMember,
    BootstrapBlocked,
    InviteConsumed,
    accept_secretary_invite,
    bootstrap_first_owner,
    get_invite_by_code,
    list_active_orgs_for_user,
)


HINT_OWNER_CHOOSE_ORG_NAME_ZH = "请输入要创建的公司/组织名称"
HINT_SECRETARY_NO_INVITE_EN = "Ask your Owner to invite you to the workspace."
HINT_SECRETARY_ACCEPT_EN = "You have a pending invite. Review the workspace and accept to join."


def _is_owner_role(user: User) -> bool:
    role = user.role.value if isinstance(user.role, UserRole) else str(user.role)
    return role == UserRole.admin.value


class BootstrapForbidden(PermissionError):
    """Raised when the caller already has an active membership and is not
    allowed to bootstrap another Organization. Translates to HTTP 403."""


class InviteNotAccepted(ValueError):
    """Raised when a Secretary presents a code that cannot be consumed.
    The nested ``reason`` field captures the semantic outcome so the
    HTTP layer can surface it without leaking implementation detail.

    Reasons: ``invalid`` / ``expired`` / ``consumed`` / ``cancelled`` /
    ``already_member``.
    """

    def __init__(self, reason: str, detail: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or reason


@dataclass(frozen=True)
class _StageOutcome:
    stage: str
    extras: dict[str, Any]


def _org_ref(org: Organization) -> OrganizationRef:
    return OrganizationRef(id=org.id, name=org.name, display_name=org.display_name)


def _membership_ref(m: Membership) -> MembershipRef:
    return MembershipRef(
        id=m.id,
        organization_id=m.organization_id,
        role=(m.role.value if isinstance(m.role, OrganizationRole) else str(m.role)),
        state=(m.state.value if isinstance(m.state, MembershipState) else str(m.state)),
    )


# ---------------------------------------------------------------------------
# 1. Role-agnostic onboarding state resolver
# ---------------------------------------------------------------------------

def get_onboarding_state(
    db: Session,
    user: User,
    *,
    pending_invite_code: str | None = None,
    now: datetime | None = None,
) -> OnboardingStateResponse:
    """Return the authoritative onboarding routing outcome for ``user``.

    This function is side-effect free. It never writes, never transitions
    invite state, never commits. It is pure read.

    Order of precedence (first match wins):
      1. User has >= 1 ACTIVE membership -> EXISTING_MEMBER.
      2. A pending invite code is supplied and resolves to a PENDING invite
         that is NOT expired (expires_at > now) for a real Organization
         -> SECRETARY_VALID_INVITE_PENDING_ACCEPT.
         Expired PENDING invites are intentionally treated as invalid
         (P0-2: must not leak organization name or valid-invite stage).
      3. Fallback decision is bound to server-side ``users.role``:
         - OWNER role (``UserRole.admin``): ``OWNER_BOOTSTRAP_REQUIRED``
           with Owner Chinese hint only.
         - SECRETARY role (any non-admin human): ``SECRETARY_NO_INVITE``
           with Secretary English hint only.  **No Owner guidance is
           leaked to an uninvited Secretary** (P0-1).
    """
    now = now or datetime.now(timezone.utc)
    memberships = list_active_orgs_for_user(db, user.id)

    if memberships:
        org, m = memberships[0]
        return OnboardingStateResponse(
            stage="EXISTING_MEMBER",
            user_id=user.id,
            existing_organization=_org_ref(org),
            existing_membership=_membership_ref(m),
        )

    if pending_invite_code:
        invite = get_invite_by_code(db, pending_invite_code)
        if (
            invite is not None
            and invite.state == InviteState.PENDING
            and invite.expires_at is not None
            and invite.expires_at > now
        ):
            org = db.get(Organization, invite.organization_id)
            return OnboardingStateResponse(
                stage="SECRETARY_VALID_INVITE_PENDING_ACCEPT",
                user_id=user.id,
                secretary_hint_en=HINT_SECRETARY_ACCEPT_EN,
                invite_organization_name=org.name if org else None,
            )

    if _is_owner_role(user):
        return OnboardingStateResponse(
            stage="OWNER_BOOTSTRAP_REQUIRED",
            user_id=user.id,
            owner_hint_zh=HINT_OWNER_CHOOSE_ORG_NAME_ZH,
        )

    return OnboardingStateResponse(
        stage="SECRETARY_NO_INVITE",
        user_id=user.id,
        secretary_hint_en=HINT_SECRETARY_NO_INVITE_EN,
    )


# ---------------------------------------------------------------------------
# 2. Owner action: create Organization
# ---------------------------------------------------------------------------

def owner_create_organization(
    db: Session,
    user: User,
    org_name: str,
    *,
    now: datetime | None = None,
) -> OwnerBootstrapResponse:
    """Create an Organization for the calling user ONLY IF the caller has
    ZERO active memberships anywhere AND carries the OWNER server-side role.

    Guard (P0-1 + Issue #24 Scope §2 and §4):
      - **Role gate first**: If the caller does NOT carry
        ``UserRole.admin``, the call is rejected with ``BootstrapForbidden``
        BEFORE any membership lookups or writes.  A "fresh Secretary"
        (``UserRole.agent`` / ``UserRole.manager``) with zero memberships
        and no invite MUST be denied — they can never become an OWNER
        via this endpoint.
      - If the user already holds ANY active membership, the call is
        rejected with ``BootstrapForbidden`` **before** we touch the
        membership service. This guarantees that a Secretary (or a past
        Owner of a different org) can never use the Owner bootstrap
        endpoint as a back-door to fabricate a second Organization.
      - If the underlying ``bootstrap_first_owner`` rejects with
        ``BootstrapBlocked`` (different org name vs existing one-member org,
        etc.) we translate the semantic exception into a ``BootstrapForbidden``
        so the HTTP layer returns a stable 403.
    """
    if not _is_owner_role(user):
        raise BootstrapForbidden(
            f"user_id={user.id!r} role={getattr(user.role, 'value', user.role)!r} "
            "is not an OWNER-role user; Owner bootstrap endpoint only serves "
            "UserRole.admin callers."
        )
    existing = list_active_orgs_for_user(db, user.id)
    if existing:
        raise BootstrapForbidden(
            f"user_id={user.id!r} already holds {len(existing)} active memberships; "
            "Owner bootstrap endpoint only serves fresh users with zero memberships."
        )
    try:
        org, m = bootstrap_first_owner(db, user.id, org_name, now=now)
    except BootstrapBlocked as exc:
        raise BootstrapForbidden(str(exc)) from exc
    return OwnerBootstrapResponse(
        organization=_org_ref(org),
        membership=_membership_ref(m),
    )


# ---------------------------------------------------------------------------
# 3. Secretary action: join via invite ONLY
# ---------------------------------------------------------------------------

def secretary_join_via_invite(
    db: Session,
    user: User,
    invite_code: str,
    *,
    now: datetime | None = None,
) -> SecretaryAcceptInviteResponse:
    """Accept a SecretaryInvite on behalf of ``user``.

    This is the **only** backend action a Secretary has for joining an
    Organization. The endpoint NEVER reads org_name from request body
    and NEVER calls bootstrap_first_owner.

    Invite-state semantics are inherited 1:1 from accept_secretary_invite:
      * PENDING and not expired -> produce SECRETARY ACTIVE Membership.
      * PENDING but expired -> EXPIRED transition committed, then InviteConsumed.
      * ACCEPTED (re-enter by same user) -> return existing Membership IF
        still ACTIVE; otherwise InviteConsumed (REMOVED Secretary cannot
        re-activate off an old invite — they need a new one).
      * CANCELLED / ACCEPTED by someone else -> InviteConsumed.

    AlreadyMember (user is already an active member of the target org)
    is surfaced separately so the UI can show "You are already a member".
    """
    try:
        m = accept_secretary_invite(db, user.id, invite_code, now=now)
    except InviteConsumed as exc:
        invite = get_invite_by_code(db, invite_code)
        if invite is None:
            raise InviteNotAccepted("invalid", "Invite code does not exist.") from exc
        state_val = invite.state.value if isinstance(invite.state, InviteState) else str(invite.state)
        if state_val == "EXPIRED":
            raise InviteNotAccepted("expired", "Invite has expired; ask your Owner for a new invite.") from exc
        if state_val == "CANCELLED":
            raise InviteNotAccepted("cancelled", "Invite was cancelled by the Owner.") from exc
        raise InviteNotAccepted("consumed", f"Invite already consumed ({state_val}).") from exc
    except AlreadyMember as exc:
        raise InviteNotAccepted(
            "already_member",
            "You are already an active member of this organization.",
        ) from exc

    org = db.get(Organization, m.organization_id)
    if org is None:
        raise RuntimeError(
            f"invite produced membership org_id={m.organization_id!r} not found"
        )
    return SecretaryAcceptInviteResponse(
        organization=_org_ref(org),
        membership=_membership_ref(m),
    )
