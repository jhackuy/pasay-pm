"""PASAY-TASK-002 FIX1 — Organization / Membership / Secretary Invite foundation.

Implements the minimal personnel identity loop:
    Organization -> Membership (OWNER/SECRETARY) -> User -> HUMAN Principal
                -> Secretary invite (one-time/expirable)

Design rules (CONFIRMED BY ISSUE CONTRACT):
- Telegram ID is never used directly as an Owner/Secretary authority;
  the authoritative chain stays:
      TelegramIdentityBinding -> HUMAN Principal -> User -> Membership
- Membership.role ∈ {OWNER, SECRETARY}; Membership.state ∈ {ACTIVE, REMOVED}.
- A User may hold at most one ACTIVE Membership per Organization
  (uq_memberships_active_user_org partial unique index enforces this at DB layer).
- An Organization allows 1..N OWNER, 0..N SECRETARY.
- Secretary removals flip state -> REMOVED (audit fact preserved; no hard delete).
  REMOVED Secretary can be re-invited later and re-join as ACTIVE SECRETARY
  (historical REMOVED rows are preserved; the uq_memberships_org_user_role
  historical-unique constraint was deliberately removed in FIX1).
- Invites are one-time, single-consumption, expirable; an used/expired
  invite never produces a second Membership.
"""
from datetime import datetime
from enum import Enum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import AuditMixin, Base, pg_enum


class OrganizationRole(str, Enum):
    OWNER = "OWNER"
    SECRETARY = "SECRETARY"


class MembershipState(str, Enum):
    ACTIVE = "ACTIVE"
    REMOVED = "REMOVED"


class InviteState(str, Enum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class Organization(AuditMixin, Base):
    __tablename__ = "organizations"
    __table_args__ = (
        CheckConstraint("length(btrim(name)) > 0", name="ck_organizations_name_nonblank"),
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)


class Membership(AuditMixin, Base):
    __tablename__ = "memberships"
    __table_args__ = (
        CheckConstraint(
            "role IN ('OWNER','SECRETARY')",
            name="ck_memberships_role",
        ),
        CheckConstraint(
            "state IN ('ACTIVE','REMOVED')",
            name="ck_memberships_state",
        ),
        CheckConstraint(
            "(state = 'ACTIVE' AND removed_at IS NULL) OR "
            "(state = 'REMOVED' AND removed_at IS NOT NULL)",
            name="ck_memberships_state_removed_at",
        ),
        Index(
            "uq_memberships_active_user_org",
            "organization_id", "user_id",
            unique=True,
            postgresql_where=text("state = 'ACTIVE'"),
        ),
    )
    organization_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organizations.id"), nullable=False, index=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False, index=True,
    )
    role: Mapped[OrganizationRole] = mapped_column(
        pg_enum(OrganizationRole, "organization_role", length=20),
        nullable=False,
    )
    state: Mapped[MembershipState] = mapped_column(
        pg_enum(MembershipState, "membership_state", length=20),
        nullable=False, default=MembershipState.ACTIVE,
    )
    invited_by_membership_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("memberships.id"), nullable=True,
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False,
    )
    removed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    removed_by_membership_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("memberships.id"), nullable=True,
    )
    removal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class SecretaryInvite(AuditMixin, Base):
    __tablename__ = "secretary_invites"
    __table_args__ = (
        CheckConstraint(
            "state IN ('PENDING','ACCEPTED','CANCELLED','EXPIRED')",
            name="ck_secretary_invites_state",
        ),
        CheckConstraint(
            "(state = 'PENDING' AND accepted_at IS NULL AND cancelled_at IS NULL) OR "
            "(state = 'ACCEPTED' AND accepted_at IS NOT NULL) OR "
            "(state = 'CANCELLED' AND cancelled_at IS NOT NULL) OR "
            "(state = 'EXPIRED')",
            name="ck_secretary_invites_state_timestamps",
        ),
        CheckConstraint("expires_at > created_at", name="ck_secretary_invites_expires_after_created"),
    )
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    organization_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organizations.id"), nullable=False, index=True,
    )
    created_by_membership_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("memberships.id"), nullable=False, index=True,
    )
    invited_name_hint: Mapped[str | None] = mapped_column(String(200), nullable=True)
    state: Mapped[InviteState] = mapped_column(
        pg_enum(InviteState, "secretary_invite_state", length=20),
        nullable=False, default=InviteState.PENDING,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    accepted_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=True, index=True,
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    cancelled_by_membership_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("memberships.id"), nullable=True,
    )
    created_membership_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("memberships.id"), nullable=True, unique=True,
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
