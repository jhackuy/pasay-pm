"""Foundation ORM: Organization, User, Membership, ApiCredential.

AGENTS.md §4 invariants:
- Organization: BIGSERIAL pk, name UNIQUE.
- User: telegram_user_id UNIQUE NULLABLE (a user may not have a Telegram
  account); default_language NOT NULL with whitelist CHECK.
- Membership: composite UNIQUE (org_id, user_id); role CHECK in
  ('OWNER','SECRETARY'); state CHECK in ('ACTIVE','REMOVED'); is_bootstrap
  flag identifies the original owner (last-Owner protection invariant).
- ApiCredential: key_hash UNIQUE; user_id indexed.

DATA_CONTRACT §2.5 mandates the exact uppercase enum values; the
MembershipRole / MembershipState StrEnums in app.v1.models.base are the
single source of truth and their string values match these CHECK constraints.
"""
from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.v1.models.base import (
    MembershipState,
    TimestampMixin,
    V1Base,
    big_pk,
)


class Organization(V1Base, TimestampMixin):
    __tablename__ = "v1_organizations"
    __table_args__ = (
        UniqueConstraint("name", name="uq_v1_organizations_name"),
    )

    id = big_pk()
    name = Column(String(120), nullable=False)

    memberships = relationship(
        "Membership", back_populates="organization",
        cascade="all, delete-orphan",
    )
    properties = relationship(
        "Property", back_populates="organization",
        cascade="all, delete-orphan",
    )


class User(V1Base, TimestampMixin):
    __tablename__ = "v1_users"
    __table_args__ = (
        UniqueConstraint(
            "telegram_user_id", name="uq_v1_users_telegram_user_id",
        ),
        CheckConstraint(
            "default_language IN ('zh-CN','en-US','tl-PH')",
            name="ck_v1_users_default_language",
        ),
    )

    id = big_pk()
    telegram_user_id = Column(BigInteger, nullable=True)
    username = Column(String(64), nullable=True)
    display_name = Column(String(120), nullable=True)
    # DATA_CONTRACT §2.5: every user has a default UI language.
    default_language = Column(String(8), nullable=False, default="zh-CN")

    memberships = relationship(
        "Membership", back_populates="user",
        cascade="all, delete-orphan",
    )
    credentials = relationship(
        "ApiCredential", back_populates="user",
        cascade="all, delete-orphan",
    )


class Membership(V1Base, TimestampMixin):
    __tablename__ = "v1_memberships"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "user_id",
            name="uq_v1_memberships_org_user",
        ),
        CheckConstraint(
            "role IN ('OWNER','SECRETARY')",
            name="ck_v1_memberships_role",
        ),
        CheckConstraint(
            "state IN ('ACTIVE','REMOVED')",
            name="ck_v1_memberships_state",
        ),
        Index("ix_v1_memberships_org_id", "org_id"),
        Index("ix_v1_memberships_user_id", "user_id"),
    )

    id = big_pk()
    org_id = Column(
        BigInteger,
        ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    user_id = Column(
        BigInteger,
        ForeignKey("v1_users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    role = Column(String(16), nullable=False)
    state = Column(
        String(16), nullable=False,
        default=MembershipState.ACTIVE.value,
    )
    # The bootstrap (founding) OWNER of a workspace; only one per org,
    # enforced at service layer (last-Owner protection).
    is_bootstrap = Column(
        Boolean, nullable=False, default=False, server_default="false",
    )

    organization = relationship("Organization", back_populates="memberships")
    user = relationship("User", back_populates="memberships")


class ApiCredential(V1Base, TimestampMixin):
    __tablename__ = "v1_api_credentials"
    __table_args__ = (
        UniqueConstraint("key_hash", name="uq_v1_api_credentials_key_hash"),
        Index("ix_v1_api_credentials_user_id", "user_id"),
    )

    id = big_pk()
    user_id = Column(
        BigInteger,
        ForeignKey("v1_users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    key_hash = Column(String(64), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)

    user = relationship("User", back_populates="credentials")
