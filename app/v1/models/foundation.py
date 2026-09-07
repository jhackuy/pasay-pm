"""Foundation ORM: Organization, User, Membership, ApiCredential, SecretaryInvite.

DATA_CONTRACT invariants:
- Organization: BIGSERIAL-compatible primary key, name UNIQUE.
- User: telegram_user_id UNIQUE NULLABLE; default_language is constrained.
- Membership: UNIQUE (org_id, user_id), exact OWNER/SECRETARY roles and ACTIVE/REMOVED states.
- ApiCredential: key_hash UNIQUE; user_id indexed. Issue #119 P0
  v1_api_credential_bootstrap adds ``principal_type`` (HUMAN/SYSTEM,
  default HUMAN) and ``purpose`` (nullable; ``internal:scheduler`` is the
  only SYSTEM purpose) so the scheduled jobs authenticate as a SYSTEM
  principal without binding any Telegram id, mirroring JOB-SERVICE-AUTH-002.
- SecretaryInvite: 4-state lifecycle (PENDING/ACCEPTED/CANCELLED/EXPIRED) keyed by
  (org_id, invite_token) UNIQUE; invitee_telegram_id nullable for non-telegram invites.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.v1.models.base import (
    BigPK,
    MembershipState,
    TimestampMixin,
    V1Base,
    V1PrincipalType,
)


class Organization(V1Base, TimestampMixin):
    __tablename__ = "v1_organizations"
    __table_args__ = (UniqueConstraint("name", name="uq_v1_organizations_name"),)

    id: Mapped[BigPK]
    name: Mapped[str] = mapped_column(String(120), nullable=False)

    memberships = relationship("Membership", back_populates="organization", cascade="all, delete-orphan")
    properties = relationship("Property", back_populates="organization", cascade="all, delete-orphan")


class User(V1Base, TimestampMixin):
    __tablename__ = "v1_users"
    __table_args__ = (
        UniqueConstraint("telegram_user_id", name="uq_v1_users_telegram_user_id"),
        CheckConstraint(
            "default_language IN ('zh-CN','en-US','tl-PH')",
            name="ck_v1_users_default_language",
        ),
    )

    id: Mapped[BigPK]
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    default_language: Mapped[str] = mapped_column(String(8), nullable=False, default="zh-CN")

    memberships = relationship("Membership", back_populates="user", cascade="all, delete-orphan")
    credentials = relationship("ApiCredential", back_populates="user", cascade="all, delete-orphan")


class Membership(V1Base, TimestampMixin):
    __tablename__ = "v1_memberships"
    __table_args__ = (
        UniqueConstraint("org_id", "user_id", name="uq_v1_memberships_org_user"),
        CheckConstraint("role IN ('OWNER','SECRETARY')", name="ck_v1_memberships_role"),
        CheckConstraint("state IN ('ACTIVE','REMOVED')", name="ck_v1_memberships_state"),
        Index("ix_v1_memberships_org_id", "org_id"),
        Index("ix_v1_memberships_user_id", "user_id"),
    )

    id: Mapped[BigPK]
    org_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("v1_organizations.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("v1_users.id", ondelete="RESTRICT"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default=MembershipState.ACTIVE.value)
    is_bootstrap: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    organization = relationship("Organization", back_populates="memberships")
    user = relationship("User", back_populates="memberships")


class ApiCredential(V1Base, TimestampMixin):
    __tablename__ = "v1_api_credentials"
    __table_args__ = (
        UniqueConstraint("key_hash", name="uq_v1_api_credentials_key_hash"),
        Index("ix_v1_api_credentials_user_id", "user_id"),
        # Issue #119 P0 v1_api_credential_bootstrap: the SYSTEM lookup is
        # the hot path for the JOB-SERVICE-AUTH-002 carryover, so the
        # (principal_type, purpose) compound is indexed explicitly.
        Index(
            "ix_v1_api_credentials_principal_type_purpose",
            "principal_type", "purpose",
        ),
        # Issue #119 P0 (independent review follow-up): the SYSTEM
        # credentials must be bound to exactly one organization. The
        # compound (principal_type, purpose, trusted_organization_id)
        # index serves both the auth-time lookup and the org-bound
        # reader query.
        Index(
            "ix_v1_api_credentials_system_org_binding",
            "principal_type", "purpose", "trusted_organization_id",
        ),
        CheckConstraint(
            "principal_type IN ('HUMAN','SYSTEM')",
            name="ck_v1_api_credentials_principal_type",
        ),
    )

    id: Mapped[BigPK]
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("v1_users.id", ondelete="RESTRICT"), nullable=False
    )
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Issue #119 P0 v1_api_credential_bootstrap: HUMAN (default) is the
    # interactive manager/secretary/admin key. SYSTEM is the scheduled-job
    # credential (purpose MUST be 'internal:scheduler').
    principal_type: Mapped[str] = mapped_column(
        String(16), nullable=False,
        default=V1PrincipalType.HUMAN.value,
        server_default=V1PrincipalType.HUMAN.value,
    )
    purpose: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Issue #119 P0 (independent review follow-up): the SYSTEM credential
    # is bound to exactly one organization. A NULL binding is rejected by
    # ``get_system_principal`` (fail closed) — the bootstrap script must
    # set this for every SYSTEM credential it creates. HUMAN credentials
    # leave this NULL (their org scope comes from the membership).
    trusted_organization_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
        nullable=True,
    )

    user = relationship("User", back_populates="credentials")
    trusted_organization = relationship("Organization")


class SecretaryInvite(V1Base, TimestampMixin):
    """Secretary invite with 4-state lifecycle (PENDING/ACCEPTED/CANCELLED/EXPIRED).

    Invite token is opaque and used as the unique key for accept/cancel.
    """
    __tablename__ = "v1_secretary_invites"
    __table_args__ = (
        UniqueConstraint("invite_token", name="uq_v1_secretary_invites_token"),
        CheckConstraint(
            "state IN ('PENDING','ACCEPTED','CANCELLED','EXPIRED')",
            name="ck_v1_secretary_invites_state",
        ),
        Index("ix_v1_secretary_invites_org_id", "org_id"),
        Index("ix_v1_secretary_invites_state", "state"),
    )

    id: Mapped[BigPK]
    org_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("v1_organizations.id", ondelete="RESTRICT"), nullable=False
    )
    invited_by_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("v1_users.id", ondelete="RESTRICT"), nullable=False
    )
    invite_token: Mapped[str] = mapped_column(String(64), nullable=False)
    invitee_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    invitee_telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="SECRETARY")
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("v1_users.id", ondelete="RESTRICT"), nullable=True
    )


SECRETARY_INVITE_STATES = ("PENDING", "ACCEPTED", "CANCELLED", "EXPIRED")
