"""V1.3 trusted identities, credentials, Telegram bindings and endpoints."""
from datetime import datetime
from enum import Enum

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import AuditMixin, Base, pg_enum


class PrincipalType(str, Enum):
    HUMAN = "HUMAN"
    SERVICE = "SERVICE"
    AI_AGENT = "AI_AGENT"
    SYSTEM = "SYSTEM"


class CredentialState(str, Enum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class Principal(AuditMixin, Base):
    __tablename__ = "principals"
    __table_args__ = (
        Index("uq_principals_human_user", "user_id", unique=True,
              postgresql_where=text("principal_type = 'HUMAN'")),
        Index("uq_principals_name_type", "name", "principal_type", unique=True),
    )
    principal_type: Mapped[PrincipalType] = mapped_column(
        pg_enum(PrincipalType, "principal_type"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")


class ApiCredential(AuditMixin, Base):
    __tablename__ = "api_credentials"
    __table_args__ = (Index("ix_api_credentials_hash_state", "key_hash", "state"),)
    principal_id: Mapped[int] = mapped_column(ForeignKey("principals.id"), nullable=False, index=True)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    purpose: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    state: Mapped[CredentialState] = mapped_column(
        pg_enum(CredentialState, "credential_state"), nullable=False, default=CredentialState.ACTIVE)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    supersedes_id: Mapped[int | None] = mapped_column(ForeignKey("api_credentials.id"), nullable=True)


class CredentialLifecycle(AuditMixin, Base):
    __tablename__ = "credential_lifecycle_history"
    credential_id: Mapped[int] = mapped_column(ForeignKey("api_credentials.id"), nullable=False, index=True)
    state: Mapped[CredentialState] = mapped_column(pg_enum(CredentialState, "credential_history_state"), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class TelegramIdentityBinding(AuditMixin, Base):
    __tablename__ = "telegram_identity_bindings"
    __table_args__ = (
        Index("uq_telegram_binding_external_active", "external_user_id", unique=True,
              postgresql_where=text("is_active AND revoked_at IS NULL")),
        Index("uq_telegram_binding_human_active", "human_principal_id", unique=True,
              postgresql_where=text("is_active AND revoked_at IS NULL")),
        CheckConstraint("external_user_id > 0", name="ck_telegram_external_user_positive"),
    )
    external_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    human_principal_id: Mapped[int] = mapped_column(ForeignKey("principals.id"), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")


class CommunicationEndpoint(AuditMixin, Base):
    __tablename__ = "communication_endpoints"
    __table_args__ = (Index("ix_endpoint_owner_channel", "human_principal_id", "channel"),)
    human_principal_id: Mapped[int] = mapped_column(ForeignKey("principals.id"), nullable=False)
    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    destination: Mapped[str] = mapped_column(String(200), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")


class SecurityEvent(AuditMixin, Base):
    __tablename__ = "security_events"
    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    channel: Mapped[str | None] = mapped_column(String(30), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
