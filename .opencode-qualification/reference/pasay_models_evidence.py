"""PASAY reference implementation — Evidence / Attachment / AuditLog ORM models.

Issue #99 / PR #100 /oc continuation. Staged under
``.opencode-qualification/reference/`` pending Owner-side promotion to
``app/models/evidence.py`` and ``app/models/audit.py``.

Entities in this file:
    * Evidence     — generic file evidence attached to any business row.
    * Attachment   — chat/file attachment uploaded via Telegram (or web).
    * AuditLog     — append-only audit trail of every privileged mutation.

All timestamps ``DateTime(timezone=True)``. All business rows carry
``org_id``. AuditLog is INSERT-only — UPDATE/DELETE are forbidden by the
service layer.
"""
from __future__ import annotations

import enum
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from pasay_db_layer import AuditMixin, Base, OrgScopedMixin


class EvidenceKindEnum(str, enum.Enum):
    IMAGE = "IMAGE"
    DOCUMENT = "DOCUMENT"
    AUDIO = "AUDIO"
    VIDEO = "VIDEO"
    OTHER = "OTHER"


class AttachmentSourceEnum(str, enum.Enum):
    TELEGRAM = "TELEGRAM"
    WEB = "WEB"
    API = "API"
    SYSTEM = "SYSTEM"


class AuditActionEnum(str, enum.Enum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    STATE_TRANSITION = "STATE_TRANSITION"
    APPROVAL = "APPROVAL"
    VERIFICATION = "VERIFICATION"
    LOGIN = "LOGIN"
    LOGOUT = "LOGOUT"
    OTHER = "OTHER"


class Evidence(Base, AuditMixin, OrgScopedMixin):
    """Generic file evidence attached to any business row via polymorphic FK."""

    __tablename__ = "evidence"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    related_table: Mapped[str] = mapped_column(String(64), nullable=False)
    related_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    kind: Mapped[EvidenceKindEnum] = mapped_column(
        SAEnum(
            EvidenceKindEnum,
            name="evidence_kind_enum",
            native_enum=False,
            length=16,
        ),
        nullable=False,
        server_default=text("'DOCUMENT'"),
    )
    file_url: Mapped[str] = mapped_column(Text, nullable=False)
    file_hash_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    uploaded_by_user_id: Mapped[Optional[str]] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="evidence_size_nonneg"),
        CheckConstraint(
            "length(file_hash_sha256) = 64", name="evidence_hash_length"
        ),
        Index(
            "ix_evidence_org_related",
            "org_id",
            "related_table",
            "related_id",
        ),
    )


class Attachment(Base, AuditMixin, OrgScopedMixin):
    """Chat/file attachment uploaded via Telegram, Web, or API."""

    __tablename__ = "attachments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[AttachmentSourceEnum] = mapped_column(
        SAEnum(
            AttachmentSourceEnum,
            name="attachment_source_enum",
            native_enum=False,
            length=16,
        ),
        nullable=False,
        server_default=text("'API'"),
    )
    telegram_file_id: Mapped[Optional[str]] = mapped_column(
        String(200), nullable=True, unique=True
    )
    file_url: Mapped[str] = mapped_column(Text, nullable=False)
    file_hash_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="attachments_size_nonneg"),
        Index(
            "ix_attachments_org_chat",
            "org_id",
            "chat_id",
        ),
    )


class AuditLog(Base):
    """Append-only audit trail. INSERT-only at the service layer."""

    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    actor_user_id: Mapped[Optional[str]] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    action: Mapped[AuditActionEnum] = mapped_column(
        SAEnum(
            AuditActionEnum,
            name="audit_action_enum",
            native_enum=False,
            length=32,
        ),
        nullable=False,
    )
    target_table: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'{}'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now() AT TIME ZONE 'UTC'"),
    )

    __table_args__ = (
        CheckConstraint("length(target_table) > 0", name="audit_target_table_nonempty"),
        CheckConstraint("length(target_id) > 0", name="audit_target_id_nonempty"),
        Index(
            "ix_audit_logs_org_time",
            "org_id",
            "created_at",
        ),
    )


__all__ = [
    "EvidenceKindEnum",
    "AttachmentSourceEnum",
    "AuditActionEnum",
    "Evidence",
    "Attachment",
    "AuditLog",
]
