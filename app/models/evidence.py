"""AI-OPS-FOUNDATION-001 universal evidence index + unit viewings.

``evidence`` is the portable index for every media/document the business needs
(property photos, before/after repair evidence, quotes, receipts, payment
proof, lease, move-in/out, other operational documents). The media bytes live
in a storage layer (initially the free Telegram private archive channel);
PostgreSQL stays the authoritative index and relationship store, and
``storage_provider``/``external_file_id`` keep the layer portable (future
migration/mirror to R2/NAS/S3 without touching domain models).

``viewings`` persists scheduled unit viewings with the minimal outcome data
needed for future vacancy/pricing analysis (interested / not interested /
follow-up + rejection reason).
"""
from datetime import datetime
from enum import Enum

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import AuditMixin, Base, pg_enum


class EvidenceCategory(str, Enum):
    property_photo = "property_photo"
    property_video = "property_video"
    before_repair = "before_repair"
    diagnosis = "diagnosis"
    quote = "quote"
    receipt = "receipt"
    payment_proof = "payment_proof"
    after_repair = "after_repair"
    lease = "lease"
    move_in = "move_in"
    move_out = "move_out"
    other = "other"


class Evidence(AuditMixin, Base):
    """One indexed media/document record (portable storage metadata)."""

    __tablename__ = "evidence"
    __table_args__ = (
        Index("ix_evidence_entity", "entity_type", "entity_id"),
        Index("ix_evidence_unit_created", "unit_id", "created_at"),
        CheckConstraint(
            "storage_provider IN ('telegram_channel','r2','nas','s3','local')",
            name="ck_evidence_storage_provider",
        ),
    )

    storage_provider: Mapped[str] = mapped_column(
        String(30), nullable=False, default="telegram_channel"
    )
    external_file_id: Mapped[str] = mapped_column(String(300), nullable=False)
    external_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
    category: Mapped[EvidenceCategory | None] = mapped_column(
        pg_enum(EvidenceCategory, "evidence_category"), nullable=True
    )
    uploaded_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    property_id: Mapped[int | None] = mapped_column(
        ForeignKey("properties.id"), nullable=True, index=True
    )
    unit_id: Mapped[int | None] = mapped_column(ForeignKey("units.id"), nullable=True, index=True)
    entity_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    entity_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class ViewingStatus(str, Enum):
    scheduled = "scheduled"
    done = "done"
    cancelled = "cancelled"


class ViewingOutcome(str, Enum):
    interested = "interested"
    not_interested = "not_interested"
    follow_up = "follow_up"


class Viewing(AuditMixin, Base):
    """One scheduled unit viewing with its minimal outcome."""

    __tablename__ = "viewings"
    __table_args__ = (
        Index("ix_viewings_status_at", "status", "scheduled_at"),
        CheckConstraint(
            "status IN ('scheduled','done','cancelled')",
            name="ck_viewings_status",
        ),
        CheckConstraint(
            "outcome IN ('interested','not_interested','follow_up') OR outcome IS NULL",
            name="ck_viewings_outcome",
        ),
    )

    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id"), nullable=False, index=True)
    property_id: Mapped[int | None] = mapped_column(
        ForeignKey("properties.id"), nullable=True, index=True
    )
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[ViewingStatus] = mapped_column(
        pg_enum(ViewingStatus, "viewing_status"),
        nullable=False,
        default=ViewingStatus.scheduled,
    )
    outcome: Mapped[ViewingOutcome | None] = mapped_column(
        pg_enum(ViewingOutcome, "viewing_outcome"), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
