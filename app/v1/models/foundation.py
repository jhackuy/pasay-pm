"""Foundation ORM: Organization, User, Membership, ApiCredential.

DATA_CONTRACT invariants:
- Organization: BIGSERIAL-compatible primary key, name UNIQUE.
- User: telegram_user_id UNIQUE NULLABLE; default_language is constrained.
- Membership: UNIQUE (org_id, user_id), exact OWNER/SECRETARY roles and ACTIVE/REMOVED states.
- ApiCredential: key_hash UNIQUE; user_id indexed.
"""
from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.v1.models.base import BigPK, MembershipState, TimestampMixin, V1Base


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
    )

    id: Mapped[BigPK]
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("v1_users.id", ondelete="RESTRICT"), nullable=False
    )
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    user = relationship("User", back_populates="credentials")
