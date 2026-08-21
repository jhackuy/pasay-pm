"""Unit ↔ Telegram Channel minimal binding (PASAY-TASK-007 Issue #25 §4).

Domain contract — exactly the Issue #25 P0 minimal binding, no archive
article or render-publish scaffolding. Each row describes one Telegram
destination (channel / group / supergroup / topic thread) that a Unit is
currently or was previously bound to, under a declared purpose.

Authoritative minimal fields per contract:
  - organization_id        (FK, NOT NULL, tenant boundary)
  - unit_id                (FK, NOT NULL, the "一房一档" core)
  - purpose                (enum, archive | business_group)
  - channel_chat_id        (BigInteger, NOT NULL for ACTIVE, Telegram destination)
  - thread_topic_id        (BigInteger, NULL — optional thread/topic locator)
  - status                 (ACTIVE | REVOKED lifecycle)
  - revoked_at / revoked_by_membership_id  (REVOKED audit trail)

DB guarantees enforced here:
  * uq_unit_binding_active_unit_purpose:
        partial UNIQUE on (unit_id, purpose) WHERE status = 'ACTIVE'
    → exactly zero or one ACTIVE binding per (unit, purpose); you can
      still have arbitrary REVOKED history rows.
  * ck_unit_binding_active_has_chat_id:
        ACTIVE → channel_chat_id IS NOT NULL
  * ck_unit_binding_revoked_has_timestamp:
        REVOKED ↔ revoked_at IS NOT NULL
  * ck_unit_binding_purpose_enum / ck_unit_binding_status_enum
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import AuditMixin, Base, pg_enum


class ChannelPurpose(str, Enum):
    archive = "archive"
    business_group = "business_group"


class BindingStatus(str, Enum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class UnitChannelBinding(AuditMixin, Base):
    __tablename__ = "unit_channel_bindings"
    __table_args__ = (
        Index(
            "uq_unit_binding_active_unit_purpose",
            "unit_id",
            "purpose",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        CheckConstraint(
            "purpose IN ('archive','business_group')",
            name="ck_unit_binding_purpose_enum",
        ),
        CheckConstraint(
            "status IN ('ACTIVE','REVOKED')",
            name="ck_unit_binding_status_enum",
        ),
        CheckConstraint(
            "(status = 'ACTIVE' AND channel_chat_id IS NOT NULL) OR status <> 'ACTIVE'",
            name="ck_unit_binding_active_has_chat_id",
        ),
        CheckConstraint(
            "(status = 'REVOKED' AND revoked_at IS NOT NULL) OR status <> 'REVOKED'",
            name="ck_unit_binding_revoked_has_timestamp",
        ),
        Index(
            "ix_unit_bindings_org_unit_status",
            "organization_id", "unit_id", "status",
        ),
    )

    organization_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organizations.id"), nullable=False, index=True
    )
    unit_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("units.id"), nullable=False, index=True
    )
    purpose: Mapped[str] = mapped_column(
        pg_enum(ChannelPurpose, "channel_purpose", length=30),
        nullable=False,
    )
    channel_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    thread_topic_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(
        pg_enum(BindingStatus, "binding_status", length=20),
        nullable=False,
        default=BindingStatus.ACTIVE,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_by_membership_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("memberships.id"), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
