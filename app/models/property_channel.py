"""Property Channel publishing bindings (PASAY-TASK-007).

Domain model for the "per-unit persistent archive article" that the bot
edits in-place whenever the business truth changes. The Telegram message
bytes live in the free storage layer of the Telegram Archive channel the
same way ``evidence`` does; PostgreSQL stays the only authority for the
``property_id / unit_id`` -> ``message_id`` mapping and the deterministic
render hash used to decide whether a re-edit is actually needed.
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
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import AuditMixin, Base, pg_enum


class ChannelScope(str, Enum):
    property = "property"
    unit = "unit"


class ChannelPlatform(str, Enum):
    telegram_channel = "telegram_channel"
    telegram_group = "telegram_group"
    discord = "discord"


class ArchiveArticleStatus(str, Enum):
    draft = "draft"
    published = "published"
    archived = "archived"


class PropertyArchiveChannel(AuditMixin, Base):
    """One property-level publishable "file" article in the Property Channel.

    Exactly one PUBLISHED row per (platform, property_id) is allowed so the
    bot can always find the current canonical message to edit in-place
    whenever a lifecycle event, rent payment, repair, or lease change
    happens. Rows use SoftDelete-style status transitions (PUBLISHED ->
    ARCHIVED) instead of hard deletes so the edit history survives.
    """

    __tablename__ = "property_archive_channels"
    __table_args__ = (
        UniqueConstraint(
            "platform",
            "property_id",
            name="uq_property_archive_active_property",
        ),
        CheckConstraint(
            "platform IN ('telegram_channel','telegram_group','discord')",
            name="ck_property_archive_platform",
        ),
        CheckConstraint(
            "status IN ('draft','published','archived')",
            name="ck_property_archive_status",
        ),
        CheckConstraint(
            "(status = 'published' AND external_message_id IS NOT NULL) OR status <> 'published'",
            name="ck_property_archive_published_has_message",
        ),
    )

    property_id: Mapped[int] = mapped_column(
        ForeignKey("properties.id"), nullable=False, index=True
    )
    platform: Mapped[str] = mapped_column(String(30), nullable=False, default="telegram_channel")
    channel_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    external_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(
        pg_enum(ArchiveArticleStatus, "archive_article_status", length=20),
        nullable=False,
        default=ArchiveArticleStatus.draft,
    )
    render_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_rendered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    render_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    editor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)


class UnitArchiveArticle(AuditMixin, Base):
    """One unit-level persistent "digital file" article.

    The bot's Property Channel renderers call this table the source of
    truth: when a unit truth changes (rent/payment/lease/repair/evidence),
    the service calls ``render_unit_archive(db, unit_id)`` and if the new
    hash differs from ``render_hash`` the bot performs a single
    ``editMessageText`` on ``external_message_id`` instead of spamming the
    channel. The product contract (PRODUCT_CONFORMANCE_AUDIT_001 §02) is
    exactly "one dynamic file per unit + one file per property" and both
    rows here keep the mapping durable across bot restarts.
    """

    __tablename__ = "unit_archive_articles"
    __table_args__ = (
        UniqueConstraint(
            "platform",
            "unit_id",
            name="uq_unit_archive_active_unit",
        ),
        Index("ix_unit_archive_property_status", "unit_id", "status"),
        CheckConstraint(
            "platform IN ('telegram_channel','telegram_group','discord')",
            name="ck_unit_archive_platform",
        ),
        CheckConstraint(
            "status IN ('draft','published','archived')",
            name="ck_unit_archive_status",
        ),
        CheckConstraint(
            "(status = 'published' AND external_message_id IS NOT NULL) OR status <> 'published'",
            name="ck_unit_archive_published_has_message",
        ),
    )

    unit_id: Mapped[int] = mapped_column(
        ForeignKey("units.id"), nullable=False, index=True
    )
    property_id: Mapped[int] = mapped_column(
        ForeignKey("properties.id"), nullable=False, index=True
    )
    platform: Mapped[str] = mapped_column(String(30), nullable=False, default="telegram_channel")
    channel_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    external_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(
        pg_enum(ArchiveArticleStatus, "archive_article_status", length=20),
        nullable=False,
        default=ArchiveArticleStatus.draft,
    )
    render_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    render_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_rendered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    render_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    editor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    event_count_at_publish: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
