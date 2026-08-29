"""PASAY reference implementation — foundational ORM models.

Issue #99 / PR #100 /oc continuation. Staged under
``.opencode-qualification/reference/`` pending Owner-side promotion to
``app/models/`` (split per domain at promotion time).

Foundational entities included in this file:
    * Organization       — top-level tenant boundary; one Telegram identity
                           MAY belong to multiple Organizations.
    * User               — platform-level identity (Telegram or email).
    * Membership         — joins User to Organization with role + state.
    * SecretaryInvite    — one-time invitation; state machine
                           PENDING / ACCEPTED / CANCELLED / EXPIRED.
    * ApiCredential      — hashed API key with org/principal scoping.
    * IdempotencyRecord  — dedup table for state-mutating writes.

Every entity inherits OrgScopedMixin + AuditMixin (except Organization
itself, which IS the scope root). All money columns use ``Numeric(14,2)``
+ Python ``Decimal``. All timestamps use ``DateTime(timezone=True)``.

Reference promotion splits these into ``app/models/organization.py`` etc.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

# In production these imports come from ``app.db.base``.
# In this reference they are imported from the local db-layer module by
# relative path so the file is self-contained for unit tests.
from pasay_db_layer import (
    AuditMixin,
    Base,
    IdempotencyMixin,
    OrgScopedMixin,
)


# ---------------------------------------------------------------------------
# Enumerations (string-backed so DB values are stable across migrations)
# ---------------------------------------------------------------------------

import enum


class RoleEnum(str, enum.Enum):
    OWNER = "OWNER"
    SECRETARY = "SECRETARY"
    TENANT = "TENANT"


class MembershipStateEnum(str, enum.Enum):
    ACTIVE = "ACTIVE"
    REMOVED = "REMOVED"


class InviteStateEnum(str, enum.Enum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


# ---------------------------------------------------------------------------
# Organizations (scope root — does NOT carry org_id)
# ---------------------------------------------------------------------------


class Organization(Base, AuditMixin):
    """Top-level tenant boundary. One Organization = one workspace."""

    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    default_currency: Mapped[str] = mapped_column(
        String(3), nullable=False, server_default=text("'USD'")
    )
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=text("'UTC'")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("TRUE")
    )

    memberships: Mapped[list["Membership"]] = relationship(
        back_populates="organization",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint("length(name) > 0", name="name_nonempty"),
    )


# ---------------------------------------------------------------------------
# Users (platform identity, may belong to multiple orgs)
# ---------------------------------------------------------------------------


class User(Base, AuditMixin):
    """Platform-level identity; Telegram-linked or email-linked."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    telegram_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, unique=True
    )
    email: Mapped[Optional[str]] = mapped_column(
        String(320), nullable=True, unique=True
    )
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("TRUE")
    )

    memberships: Mapped[list["Membership"]] = relationship(back_populates="user")

    __table_args__ = (
        CheckConstraint(
            "telegram_id IS NOT NULL OR email IS NOT NULL",
            name="user_must_have_identity",
        ),
    )


# ---------------------------------------------------------------------------
# Memberships (User × Organization with role + state)
# ---------------------------------------------------------------------------


class Membership(Base, AuditMixin, OrgScopedMixin):
    """Join row between User and Organization with role + state."""

    __tablename__ = "memberships"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    role: Mapped[RoleEnum] = mapped_column(
        SAEnum(RoleEnum, name="role_enum", native_enum=False, length=16),
        nullable=False,
    )
    state: Mapped[MembershipStateEnum] = mapped_column(
        SAEnum(
            MembershipStateEnum,
            name="membership_state_enum",
            native_enum=False,
            length=16,
        ),
        nullable=False,
        server_default=text("'ACTIVE'"),
    )
    removed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped["User"] = relationship(back_populates="memberships")
    organization: Mapped["Organization"] = relationship(back_populates="memberships")

    __table_args__ = (
        # One ACTIVE membership per (user, org). Removed rows are exempt.
        # Enforced via partial-unique index emitted in Alembic migration.
        Index(
            "ix_memberships_user_org_active_unique",
            "user_id",
            "org_id",
            unique=True,
            postgresql_where=text("state = 'ACTIVE'"),
            sqlite_where=text("state = 'ACTIVE'"),
        ),
        CheckConstraint(
            "(state = 'ACTIVE' AND removed_at IS NULL) OR "
            "(state = 'REMOVED' AND removed_at IS NOT NULL)",
            name="ck_memberships_state_timestamps",
        ),
    )


# ---------------------------------------------------------------------------
# Secretary Invitations (one-time use; bootstrap-only)
# ---------------------------------------------------------------------------


class SecretaryInvite(Base, AuditMixin, OrgScopedMixin):
    """One-time Secretary invite. Bootstrap is OWNER-only."""

    __tablename__ = "secretary_invites"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    state: Mapped[InviteStateEnum] = mapped_column(
        SAEnum(
            InviteStateEnum,
            name="invite_state_enum",
            native_enum=False,
            length=16,
        ),
        nullable=False,
        server_default=text("'PENDING'"),
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    accepted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_membership_id: Mapped[Optional[str]] = mapped_column(
        String(64),
        ForeignKey("memberships.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
    )
    inviter_user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    __table_args__ = (
        CheckConstraint("expires_at > created_at", name="ck_invites_expires_after_created"),
        CheckConstraint(
            "(state = 'PENDING' AND accepted_at IS NULL AND cancelled_at IS NULL) OR "
            "(state = 'ACCEPTED' AND accepted_at IS NOT NULL AND cancelled_at IS NULL) OR "
            "(state = 'CANCELLED' AND cancelled_at IS NOT NULL AND accepted_at IS NULL) OR "
            "(state = 'EXPIRED')",
            name="ck_invites_state_timestamps",
        ),
    )


# ---------------------------------------------------------------------------
# API Credentials (hashed; never plaintext)
# ---------------------------------------------------------------------------


class ApiCredential(Base, AuditMixin, OrgScopedMixin):
    """Hashed API key bound to a Principal."""

    __tablename__ = "api_credentials"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    key_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


# ---------------------------------------------------------------------------
# Idempotency records (dedup for state-mutating writes)
# ---------------------------------------------------------------------------


class IdempotencyRecord(Base, AuditMixin, OrgScopedMixin, IdempotencyMixin):
    """Server-side dedup table. Composite unique key lives in IdempotencyMixin."""

    __tablename__ = "idempotency_records"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


__all__ = [
    "RoleEnum",
    "MembershipStateEnum",
    "InviteStateEnum",
    "Organization",
    "User",
    "Membership",
    "SecretaryInvite",
    "ApiCredential",
    "IdempotencyRecord",
]