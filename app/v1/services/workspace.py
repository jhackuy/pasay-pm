"""WorkspaceService: create orgs, invite/list members."""
from __future__ import annotations

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
from app.v1.models.foundation import (
    ApiCredential,
    Membership,
    MembershipState,
    Organization,
    User,
)
from app.v1.services.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)


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