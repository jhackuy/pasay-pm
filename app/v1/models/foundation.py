"""Foundation ORM: Organization, User, Membership, ApiCredential.

AGENTS.md §4 invariants:
- Organization: BIGSERIAL pk, name UNIQUE.
- User: telegram_id UNIQUE NULLABLE (a user may not have a Telegram account).
- Membership: composite UNIQUE (org_id, user_id); role CHECK;
  state CHECK in ('ACTIVE','SUSPENDED','REMOVED'); org_id indexed.
- ApiCredential: key_hash UNIQUE; user_id indexed.
"""
from __future__ import annotations

import enum

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

from app.v1.models.base import TimestampMixin, V1Base, big_pk


class MembershipState(str, enum.Enum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    REMOVED = "REMOVED"


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
        UniqueConstraint("telegram_id", name="uq_v1_users_telegram_id"),
    )

    id = big_pk()
    telegram_id = Column(BigInteger, nullable=True)
    username = Column(String(64), nullable=True)
    display_name = Column(String(120), nullable=True)

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
            "role IN ('owner','secretary','tenant')",
            name="ck_v1_memberships_role",
        ),
        CheckConstraint(
            "state IN ('ACTIVE','SUSPENDED','REMOVED')",
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