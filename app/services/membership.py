"""PASAY-TASK-002 — Membership service: bootstrap, invite, accept, remove, auth helpers.

Authoritative identity chain (CONFIRMED BY CODE):
    Telegram external_user_id
      -> TelegramIdentityBinding (active + verified)
      -> HUMAN Principal (active)
      -> User (active)
      -> Membership (role, state)

This module deliberately does NOT touch the deprecated `users.role` column.
Legacy callers may still read `users.role`; this slice only guarantees that
**new** personnel management (Org creation, invite, accept, remove) operates
against the Membership truth table.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models.audit_log import AuditAction
from app.models.identity import Principal, PrincipalType
from app.services.identity import resolve_telegram_human
from app.models.membership import (
    InviteState,
    Membership,
    MembershipState,
    Organization,
    OrganizationRole,
    SecretaryInvite,
)
from app.models.user import User
from app.services.audit import record_audit


DEFAULT_INVITE_TTL = timedelta(days=7)


# ---------------------------------------------------------------------------
# Membership authorization helpers
# ---------------------------------------------------------------------------

def has_active_membership(
    db: Session,
    user_id: int,
    organization_id: int,
    *,
    role: OrganizationRole | Iterable[OrganizationRole] | None = None,
) -> Membership | None:
    """Return the ACTIVE Membership row for (user_id, organization_id), or None.

    When ``role`` is provided, only rows matching that role (or any of those
    roles, if a collection) are considered valid.
    """
    if not user_id or not isinstance(user_id, int) or user_id <= 0:
        return None
    if not organization_id or not isinstance(organization_id, int) or organization_id <= 0:
        return None
    stmt = select(Membership).where(
        Membership.user_id == user_id,
        Membership.organization_id == organization_id,
        Membership.state == MembershipState.ACTIVE,
    )
    if role is not None:
        if isinstance(role, OrganizationRole):
            roles = {role}
        else:
            roles = set(role)
        stmt = stmt.where(Membership.role.in_([r.value for r in roles]))
    return db.execute(stmt).scalar_one_or_none()


def is_owner(db: Session, user_id: int, organization_id: int) -> bool:
    return has_active_membership(db, user_id, organization_id, role=OrganizationRole.OWNER) is not None


def is_secretary(db: Session, user_id: int, organization_id: int) -> bool:
    return has_active_membership(db, user_id, organization_id, role=OrganizationRole.SECRETARY) is not None


def list_active_orgs_for_user(db: Session, user_id: int) -> list[tuple[Organization, Membership]]:
    """Return (Organization, Membership ACTIVE) pairs for every org the user
    currently belongs to under any role."""
    if not user_id or not isinstance(user_id, int) or user_id <= 0:
        return []
    rows = (
        db.query(Organization, Membership)
        .join(Membership, Membership.organization_id == Organization.id)
        .filter(
            Membership.user_id == user_id,
            Membership.state == MembershipState.ACTIVE,
        ).all()
    )
    return [(org, m) for org, m in rows]


def active_owner_count(db: Session, organization_id: int) -> int:
    if not organization_id or not isinstance(organization_id, int) or organization_id <= 0:
        return 0
    return (
        db.query(func.count(Membership.id))
        .filter(
            Membership.organization_id == organization_id,
            Membership.role == OrganizationRole.OWNER,
            Membership.state == MembershipState.ACTIVE,
        ).scalar() or 0
    )


# ---------------------------------------------------------------------------
# Bootstrap — first Owner creates an Organization and becomes its OWNER
# ---------------------------------------------------------------------------

class BootstrapBlocked(ValueError):
    """The user is already bound to an Organization and cannot bootstrap a new one."""


def bootstrap_first_owner(
    db: Session,
    user_id: int,
    org_name: str,
    *,
    now: datetime | None = None,
) -> tuple[Organization, Membership]:
    """Create ``Organization + ACTIVE OWNER Membership`` atomically.

    Idempotency contract:
      * If the user already has **exactly one** ACTIVE OWNER Membership, and
        the caller passes the same organization name, we return the existing
        (Organization, Membership) pair WITHOUT inserting new rows.
      * If the user already holds ANY ACTIVE membership (OWNER or SECRETARY)
        in **any** organization, a different org name raises BootstrapBlocked.

    The User record must already exist and be active. This slice does NOT
    auto-create users; identification happens via the upstream Telegram
    binding pipeline.
    """
    now = now or datetime.now(timezone.utc)
    org_name = (org_name or "").strip()
    if not org_name:
        raise ValueError("org_name is required")
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise LookupError("user does not exist or is inactive")

    existing = list_active_orgs_for_user(db, user_id)
    if existing:
        if len(existing) == 1:
            org, m = existing[0]
            if (m.role == OrganizationRole.OWNER
                    and org.name.casefold() == org_name.casefold()):
                return org, m
        raise BootstrapBlocked(
            f"user_id={user_id!r} already has {len(existing)} active memberships"
        )

    org = Organization(name=org_name)
    db.add(org)
    db.flush()
    record_audit(
        db,
        table_name="organizations",
        record_id=org.id,
        action=AuditAction("org_created"),
        actor_id=user_id,
        new_value={"name": org_name, "display_name": org.display_name},
    )

    membership = Membership(
        organization_id=org.id,
        user_id=user_id,
        role=OrganizationRole.OWNER,
        state=MembershipState.ACTIVE,
        joined_at=now,
    )
    db.add(membership)
    db.flush()
    record_audit(
        db,
        table_name="memberships",
        record_id=membership.id,
        action=AuditAction("org_first_owner_activated"),
        actor_id=user_id,
        new_value={
            "organization_id": org.id,
            "user_id": user_id,
            "role": OrganizationRole.OWNER.value,
            "state": MembershipState.ACTIVE.value,
        },
    )
    db.commit()
    db.refresh(org)
    db.refresh(membership)
    return org, membership


# ---------------------------------------------------------------------------
# Secretary invite lifecycle
# ---------------------------------------------------------------------------

_INVITE_CODE_BYTES = 24


def _generate_invite_code() -> str:
    return secrets.token_urlsafe(_INVITE_CODE_BYTES)


class InviteBlocked(PermissionError):
    """The caller is not authorized to create/manage this invite."""


class InviteConsumed(LookupError):
    """The invite code is no longer PENDING (already used/cancelled/expired)."""


def create_secretary_invite(
    db: Session,
    owner_user_id: int,
    organization_id: int,
    *,
    expires_at: datetime | None = None,
    invited_name_hint: str | None = None,
    note: str | None = None,
    ttl: timedelta | None = None,
    now: datetime | None = None,
) -> SecretaryInvite:
    """Create a PENDING Secretary invite code for ``organization_id``.

    Only an ACTIVE OWNER within the target organization may create invites.
    """
    now = now or datetime.now(timezone.utc)
    if expires_at is None:
        expires_at = now + (ttl or DEFAULT_INVITE_TTL)
    if expires_at <= now:
        raise ValueError("expires_at must be in the future")

    owner_membership = has_active_membership(
        db, owner_user_id, organization_id, role=OrganizationRole.OWNER,
    )
    if owner_membership is None:
        raise InviteBlocked(
            f"caller user_id={owner_user_id!r} is not an ACTIVE OWNER in org={organization_id!r}"
        )

    # Guarantee a globally unique code. Practically speaking, 24 URL-safe bytes
    # will never collide, but the loop keeps the behaviour correct and bounded
    # in the event of freak RNG collisions.
    for _ in range(5):
        code = _generate_invite_code()
        if db.query(SecretaryInvite).filter(SecretaryInvite.code == code).first() is None:
            break
    else:  # pragma: no cover - the probability of 5 successive collisions is ~0
        raise RuntimeError("unable to allocate a unique invite code")

    invite = SecretaryInvite(
        code=code,
        organization_id=organization_id,
        created_by_membership_id=owner_membership.id,
        invited_name_hint=(invited_name_hint or "").strip() or None,
        state=InviteState.PENDING,
        expires_at=expires_at,
        note=note,
    )
    db.add(invite)
    db.flush()
    record_audit(
        db,
        table_name="secretary_invites",
        record_id=invite.id,
        action=AuditAction("secretary_invited"),
        actor_id=owner_user_id,
        new_value={
            "code": code,
            "organization_id": organization_id,
            "expires_at": expires_at.isoformat(),
            "invited_name_hint": invited_name_hint,
            "note": note,
        },
    )
    db.commit()
    db.refresh(invite)
    return invite


def get_invite_by_code(db: Session, code: str) -> SecretaryInvite | None:
    code = (code or "").strip()
    if not code:
        return None
    return db.query(SecretaryInvite).filter(SecretaryInvite.code == code).one_or_none()


def _mark_invite_expired_if_stale(db: Session, invite: SecretaryInvite, now: datetime) -> None:
    if invite.state == InviteState.PENDING and invite.expires_at <= now:
        invite.state = InviteState.EXPIRED


def accept_secretary_invite(
    db: Session,
    user_id: int,
    code: str,
    *,
    now: datetime | None = None,
) -> Membership:
    """Accept a PENDING invite as ``user_id`` and create an ACTIVE SECRETARY
    Membership.

    Idempotency: if the same user tries to accept **the same** invite code a
    second time, the existing Membership is returned unchanged. Any other
    terminal state (ACCEPTED by a different user, CANCELLED, EXPIRED) raises
    InviteConsumed.
    """
    now = now or datetime.now(timezone.utc)
    invite = get_invite_by_code(db, code)
    if invite is None:
        raise InviteConsumed(f"invite code {code!r} does not exist")

    _mark_invite_expired_if_stale(db, invite, now)
    db.flush()

    if invite.state == InviteState.ACCEPTED:
        if invite.accepted_by_user_id != user_id:
            raise InviteConsumed(
                f"invite {code!r} already consumed by user_id={invite.accepted_by_user_id!r}"
            )
        # Same user re-accepting: deterministically return the membership we
        # already produced for this exact invite. created_membership_id is
        # UNIQUE, so an ACCEPTED invite without a linked row would be a data
        # integrity failure — we do NOT silently paper over that.
        if invite.created_membership_id is None:
            raise RuntimeError(
                f"invite {code!r} is ACCEPTED but has no created_membership_id"
            )
        membership = db.get(Membership, invite.created_membership_id)
        if membership is None:
            raise RuntimeError(
                f"invite {code!r} created_membership_id={invite.created_membership_id!r} no longer exists"
            )
        return membership
    if invite.state != InviteState.PENDING:
        raise InviteConsumed(f"invite {code!r} is {invite.state.value}")

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise LookupError("accepting user does not exist or is inactive")

    # Fail-fast if the user already holds an ACTIVE membership of any role in
    # the target org. Membership is the authority; we do not allow a second
    # ACTIVE row for the same (org, user).
    if has_active_membership(db, user_id, invite.organization_id) is not None:
        raise BootstrapBlocked(
            f"user_id={user_id!r} already has an ACTIVE membership in org={invite.organization_id!r}"
        )

    invite.state = InviteState.ACCEPTED
    invite.accepted_at = now
    invite.accepted_by_user_id = user_id

    creator_membership = db.get(Membership, invite.created_by_membership_id)

    membership = Membership(
        organization_id=invite.organization_id,
        user_id=user_id,
        role=OrganizationRole.SECRETARY,
        state=MembershipState.ACTIVE,
        invited_by_membership_id=(creator_membership.id if creator_membership else None),
        joined_at=now,
    )
    db.add(membership)
    db.flush()

    invite.created_membership_id = membership.id
    db.flush()

    record_audit(
        db,
        table_name="secretary_invites",
        record_id=invite.id,
        action=AuditAction("secretary_invite_accepted"),
        actor_id=user_id,
        new_value={
            "accepted_by_user_id": user_id,
            "accepted_at": now.isoformat(),
            "created_membership_id": membership.id,
        },
    )
    record_audit(
        db,
        table_name="memberships",
        record_id=membership.id,
        action=AuditAction("secretary_invite_accepted"),
        actor_id=user_id,
        new_value={
            "organization_id": membership.organization_id,
            "user_id": membership.user_id,
            "role": OrganizationRole.SECRETARY.value,
            "state": MembershipState.ACTIVE.value,
            "invite_code": code,
        },
    )
    db.commit()
    db.refresh(invite)
    db.refresh(membership)
    return membership


def cancel_secretary_invite(
    db: Session,
    owner_user_id: int,
    invite_id: int,
    *,
    now: datetime | None = None,
) -> SecretaryInvite:
    now = now or datetime.now(timezone.utc)
    invite = db.get(SecretaryInvite, invite_id)
    if invite is None:
        raise LookupError(f"invite id={invite_id!r} not found")
    owner_membership = has_active_membership(
        db, owner_user_id, invite.organization_id, role=OrganizationRole.OWNER,
    )
    if owner_membership is None:
        raise InviteBlocked(
            f"caller user_id={owner_user_id!r} is not an ACTIVE OWNER in org={invite.organization_id!r}"
        )
    _mark_invite_expired_if_stale(db, invite, now)
    if invite.state != InviteState.PENDING:
        raise InviteConsumed(f"invite id={invite_id!r} is {invite.state.value}, cannot cancel")
    invite.state = InviteState.CANCELLED
    invite.cancelled_at = now
    invite.cancelled_by_membership_id = owner_membership.id
    db.flush()
    record_audit(
        db,
        table_name="secretary_invites",
        record_id=invite.id,
        action=AuditAction("secretary_invite_cancelled"),
        actor_id=owner_user_id,
        new_value={
            "cancelled_at": now.isoformat(),
            "cancelled_by_membership_id": owner_membership.id,
        },
    )
    db.commit()
    db.refresh(invite)
    return invite


# ---------------------------------------------------------------------------
# Secretary removal
# ---------------------------------------------------------------------------

class RemovalBlocked(PermissionError):
    """Caller lacks the authority to perform this removal."""


def remove_secretary(
    db: Session,
    owner_user_id: int,
    organization_id: int,
    secretary_user_id: int,
    *,
    reason: str | None = None,
    now: datetime | None = None,
) -> Membership:
    """Transition the target Secretary's Membership to REMOVED.

    Authorities enforced:
      * Caller must be an ACTIVE OWNER in the same organization.
      * Target must be an ACTIVE SECRETARY in that organization.
      * A Secretary may NEVER remove an OWNER (even if somehow the call was
        routed incorrectly — the role check below is exhaustive, and the
        target's SECRETARY role is explicit).
      * Attempting to remove an OWNER via this helper returns RemovalBlocked
        with a distinct message so audits/logs never conflate the two.
    Idempotency: if the same target has already been REMOVED by the same
    owner (within the same org) the existing row is returned unchanged.
    """
    now = now or datetime.now(timezone.utc)
    owner_membership = has_active_membership(
        db, owner_user_id, organization_id, role=OrganizationRole.OWNER,
    )
    if owner_membership is None:
        raise RemovalBlocked(
            f"caller user_id={owner_user_id!r} is not an ACTIVE OWNER in org={organization_id!r}"
        )

    target_membership = db.query(Membership).filter(
        Membership.organization_id == organization_id,
        Membership.user_id == secretary_user_id,
    ).order_by(Membership.id.desc()).first()
    if target_membership is None:
        raise LookupError(
            f"user_id={secretary_user_id!r} has no membership in org={organization_id!r}"
        )

    if target_membership.role == OrganizationRole.OWNER:
        raise RemovalBlocked(
            f"refusing to remove OWNER user_id={secretary_user_id!r}; "
            "this helper only removes SECRETARY roles"
        )
    if target_membership.role != OrganizationRole.SECRETARY:
        raise RemovalBlocked(
            f"target membership role is {target_membership.role.value!r}; "
            "expected SECRETARY"
        )

    if target_membership.state == MembershipState.REMOVED:
        # Idempotent: a second removal request is a no-op.
        return target_membership
    if target_membership.state != MembershipState.ACTIVE:
        raise RemovalBlocked(
            f"target membership state is {target_membership.state.value!r}; "
            "expected ACTIVE"
        )

    target_membership.state = MembershipState.REMOVED
    target_membership.removed_at = now
    target_membership.removed_by_membership_id = owner_membership.id
    target_membership.removal_reason = reason
    db.flush()
    record_audit(
        db,
        table_name="memberships",
        record_id=target_membership.id,
        action=AuditAction("secretary_removed"),
        actor_id=owner_user_id,
        changed_fields={
            "state": [MembershipState.ACTIVE.value, MembershipState.REMOVED.value],
            "removed_at": [None, now.isoformat()],
            "removed_by_membership_id": [None, owner_membership.id],
            "removal_reason": [None, reason],
        },
    )
    db.commit()
    db.refresh(target_membership)
    return target_membership


# ---------------------------------------------------------------------------
# Canonical Telegram identity → Membership resolution chain
# ---------------------------------------------------------------------------

def resolve_telegram_org_membership(
    db: Session,
    external_telegram_user_id: int,
    organization_id: int,
    *,
    role: OrganizationRole | Iterable[OrganizationRole] | None = None,
) -> tuple[User, Principal, Membership]:
    """Enforce the authoritative identity chain from Telegram to Membership.

    Chain (CONFIRMED BY CODE — never shortened):
        Telegram external_user_id
          -> TelegramIdentityBinding (active / verified / not revoked)
          -> HUMAN Principal (active)
          -> User (active)
          -> Membership (ACTIVE, matching role if provided)

    Raises LookupError with a precise reason when any step of the chain is
    missing or inactive. The Membership helper alone (`has_active_membership`)
    should never be called directly with a Telegram user ID — that would
    short-circuit the identity-binding layer and weaken trust boundaries.
    """
    user, human_principal = resolve_telegram_human(db, external_telegram_user_id)
    membership = has_active_membership(db, user.id, organization_id, role=role)
    if membership is None:
        want = (
            OrganizationRole.OWNER.value
            if isinstance(role, OrganizationRole)
            else ("/".join(r.value for r in role) if role else "ANY")
        )
        raise LookupError(
            f"telegram_id={external_telegram_user_id!r} (user_id={user.id!r}) "
            f"has no ACTIVE {want} membership in org={organization_id!r}"
        )
    return user, human_principal, membership
