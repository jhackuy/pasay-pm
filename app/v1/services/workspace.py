"""WorkspaceService: create orgs, invite/list members."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.core.permissions import (
    PermissionDenied,
    Principal,
    Role,
    UnknownRoleError,
    require_org_scope,
)
from app.core.security import hash_api_key
from app.core.time import utcnow
from app.v1.models.foundation import (
    ApiCredential,
    Membership,
    MembershipState,
    Organization,
    SecretaryInvite,
    User,
)
from app.v1.services.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)


def default_language_for_role(role: str) -> str:
    """Canonical Owner=zh-CN / Secretary=en-US default language mapping.

    Used by the Telegram adapter to pick the right bundle.
    Returns ``en-US`` for any unknown role.
    """
    if role == "OWNER":
        return "zh-CN"
    if role == "SECRETARY":
        return "en-US"
    return "en-US"


class WorkspaceService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create_workspace(
        self, principal: Principal, *, name: str,
    ) -> Organization:
        if principal.role != Role.OWNER:
            raise PermissionDenied("only OWNER can create workspaces")
        existing = (
            self.db.query(Organization)
            .filter(Organization.name == name)
            .one_or_none()
        )
        if existing is not None:
            raise ConflictError(
                f"workspace name {name!r} already exists",
            )
        org = Organization(name=name)
        self.db.add(org)
        self.db.flush()
        # The creator gets an OWNER membership in the new org.
        membership = Membership(
            org_id=org.id,
            user_id=principal.user_id,
            role=Role.OWNER.value,
            state=MembershipState.ACTIVE.value,
            is_bootstrap=True,
        )
        self.db.add(membership)
        self.db.commit()
        self.db.refresh(org)
        return org

    def add_member(
        self,
        principal: Principal,
        *,
        org_id: int,
        user_id: int,
        role: str,
    ) -> Membership:
        require_org_scope(principal, org_id)
        if principal.role not in (Role.OWNER, Role.SECRETARY):
            raise PermissionDenied(
                "only OWNER/SECRETARY can add members",
            )
        try:
            parsed_role = Role.parse(role)
        except UnknownRoleError as exc:
            raise ValidationError(str(exc)) from exc
        if parsed_role == Role.ADMIN:
            raise PermissionDenied("ADMIN role is reserved")
        user = self.db.get(User, user_id)
        if user is None:
            raise NotFoundError(f"user {user_id} not found")
        existing = (
            self.db.query(Membership)
            .filter(
                Membership.org_id == org_id,
                Membership.user_id == user_id,
            )
            .one_or_none()
        )
        if existing is not None and existing.state != MembershipState.REMOVED.value:
            raise ConflictError(
                f"user {user_id} already has membership "
                f"{existing.state} in org {org_id}",
            )
        if existing is not None:
            existing.state = MembershipState.ACTIVE.value
            existing.role = parsed_role.value
            self.db.commit()
            self.db.refresh(existing)
            return existing
        m = Membership(
            org_id=org_id,
            user_id=user_id,
            role=parsed_role.value,
            state=MembershipState.ACTIVE.value,
        )
        self.db.add(m)
        self.db.commit()
        self.db.refresh(m)
        return m

    def remove_member(
        self,
        principal: Principal,
        *,
        org_id: int,
        member_id: int,
    ) -> Membership:
        """Remove a member (sets state=REMOVED). Last-Owner protection enforced.

        The caller must be an OWNER or SECRETARY of the org. Cross-org callers
        raise NotFoundError. The last ACTIVE OWNER of the org cannot be removed.
        """
        require_org_scope(principal, org_id)
        if principal.role not in (Role.OWNER, Role.SECRETARY):
            raise PermissionDenied("only OWNER/SECRETARY can remove members")
        membership = self.db.get(Membership, member_id)
        if membership is None or membership.org_id != org_id:
            raise NotFoundError(f"member {member_id} not found in org {org_id}")
        if membership.user_id == principal.user_id:
            raise PermissionDenied("cannot remove yourself")
        if membership.role == Role.OWNER.value:
            # Last-Owner guard: count ACTIVE OWNERs, must be > 1 to remove.
            active_owners = (
                self.db.query(Membership)
                .filter(
                    Membership.org_id == org_id,
                    Membership.role == Role.OWNER.value,
                    Membership.state == MembershipState.ACTIVE.value,
                )
                .count()
            )
            if active_owners <= 1:
                raise ConflictError(
                    "cannot remove the last active OWNER",
                )
        membership.state = MembershipState.REMOVED.value
        self.db.commit()
        self.db.refresh(membership)
        return membership

    def create_invite(
        self,
        principal: Principal,
        *,
        org_id: int,
        invitee_username: str | None = None,
        invitee_telegram_id: int | None = None,
        expires_in: timedelta = timedelta(days=7),
    ) -> SecretaryInvite:
        """Create a PENDING Secretary invite for the org.

        Owner-only. Token is opaque; expires after ``expires_in`` (default 7 days).
        """
        require_org_scope(principal, org_id)
        if principal.role != Role.OWNER:
            raise PermissionDenied("only OWNER can create invites")
        token = secrets.token_urlsafe(32)[:64]
        expires_at = utcnow() + expires_in
        invite = SecretaryInvite(
            org_id=org_id,
            invited_by_user_id=principal.user_id,
            invite_token=token,
            invitee_username=invitee_username,
            invitee_telegram_id=invitee_telegram_id,
            role=Role.SECRETARY.value,
            state="PENDING",
            expires_at=expires_at,
        )
        self.db.add(invite)
        self.db.commit()
        self.db.refresh(invite)
        return invite

    def accept_invite(
        self,
        *,
        invite_token: str,
        accepting_user_id: int,
    ) -> SecretaryInvite:
        """Accept a PENDING invite (creates/activates a SECRETARY membership).

        Org-scope: implicit via invite → org mapping.
        """
        if not invite_token or not invite_token.strip():
            raise ValidationError("invite_token required")
        invite = (
            self.db.query(SecretaryInvite)
            .filter(SecretaryInvite.invite_token == invite_token)
            .one_or_none()
        )
        if invite is None:
            raise NotFoundError("invite not found")
        if invite.state == "ACCEPTED":
            raise ConflictError("invite already accepted")
        if invite.state == "CANCELLED":
            raise ConflictError("invite cancelled")
        if invite.state == "EXPIRED":
            raise ConflictError("invite expired")
        if invite.expires_at <= utcnow():
            invite.state = "EXPIRED"
            self.db.commit()
            raise ConflictError("invite expired")
        user = self.db.get(User, accepting_user_id)
        if user is None:
            raise NotFoundError(f"user {accepting_user_id} not found")
        # Create or re-activate the membership.
        existing = (
            self.db.query(Membership)
            .filter(
                Membership.org_id == invite.org_id,
                Membership.user_id == accepting_user_id,
            )
            .one_or_none()
        )
        if existing is not None:
            existing.state = MembershipState.ACTIVE.value
            existing.role = Role.SECRETARY.value
            membership = existing
        else:
            membership = Membership(
                org_id=invite.org_id,
                user_id=accepting_user_id,
                role=Role.SECRETARY.value,
                state=MembershipState.ACTIVE.value,
            )
            self.db.add(membership)
        invite.state = "ACCEPTED"
        invite.accepted_at = utcnow()
        invite.accepted_by_user_id = accepting_user_id
        self.db.commit()
        self.db.refresh(invite)
        return invite

    def cancel_invite(
        self, principal: Principal, *, org_id: int, invite_id: int,
    ) -> SecretaryInvite:
        """Owner-only cancel of a PENDING invite."""
        require_org_scope(principal, org_id)
        if principal.role != Role.OWNER:
            raise PermissionDenied("only OWNER can cancel invites")
        invite = self.db.get(SecretaryInvite, invite_id)
        if invite is None or invite.org_id != org_id:
            raise NotFoundError(f"invite {invite_id} not found in org {org_id}")
        if invite.state != "PENDING":
            raise ConflictError(f"invite is {invite.state}, cannot cancel")
        invite.state = "CANCELLED"
        self.db.commit()
        self.db.refresh(invite)
        return invite

    def list_invites(
        self, principal: Principal, *, org_id: int,
    ) -> list[SecretaryInvite]:
        require_org_scope(principal, org_id)
        return (
            self.db.query(SecretaryInvite)
            .filter(SecretaryInvite.org_id == org_id)
            .order_by(SecretaryInvite.id.desc())
            .all()
        )

    def list_members(
        self, principal: Principal, *, org_id: int,
    ) -> list[Membership]:
        require_org_scope(principal, org_id)
        return (
            self.db.query(Membership)
            .filter(Membership.org_id == org_id)
            .order_by(Membership.id.asc())
            .all()
        )

    def list_workspaces(
        self, principal: Principal,
    ) -> list[Organization]:
        # Org-scope: principal sees only orgs where they have an ACTIVE membership.
        return (
            self.db.query(Organization)
            .join(
                Membership,
                Membership.org_id == Organization.id,
            )
            .filter(Membership.user_id == principal.user_id)
            .filter(Membership.state == MembershipState.ACTIVE.value)
            .order_by(Organization.id.asc())
            .all()
        )