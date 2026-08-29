"""SQLAlchemy declarative base and mixins.

Mixins enforce constitutional invariants (AGENTS.md §4):
- AuditMixin: created_at / updated_at (UTC-aware timestamptz).
- OrgScopedMixin: org_id with a SINGLE named index (no duplicate index).
- IdempotencyMixin: requires OrgScopedMixin; composite unique key on
  (org_id, key, kind); payload_hash for replay detection.

PKs use BigInteger (BIGSERIAL) to match DATA_CONTRACT.md.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, declared_attr


def utcnow() -> datetime:
    """Return a UTC-aware datetime.

    AGENTS.md §4: time is always UTC-aware; reject naive datetimes.
    """
    return datetime.now(tz=timezone.utc)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class AuditMixin:
    """Adds created_at / updated_at columns (UTC-aware).

    AGENTS.md §4: never store naive datetimes.
    """

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )


class OrgScopedMixin:
    """Adds org_id column and a single named index.

    AGENTS.md §4: permission boundary is Organization. The single named
    index (`ix_<table>_org_id`) is the canonical index for org-scope
    queries — do NOT also pass `index=True` on the column (that would
    create a duplicate index).
    """

    org_id = Column(
        BigInteger,
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )

    @declared_attr
    def __table_args__(cls) -> Any:
        return (
            Index(f"ix_{cls.__tablename__}_org_id", "org_id"),
        )


class IdempotencyMixin(OrgScopedMixin):
    """Adds idempotency columns and composite unique key.

    Inherits from OrgScopedMixin so the org_id column and its index are
    inherited. We redeclare __table_args__ here to ALSO add the composite
    unique constraint on (org_id, key, kind) — this is correct because
    SQLAlchemy's MRO replaces (not merges) __table_args__, so we must
    include the org_id index here as well to keep it on the table.

    Concrete tables that use this mixin MUST inherit both IdempotencyMixin
    AND OrgScopedMixin (this is enforced by inheritance itself).
    """

    key = Column(String(128), nullable=False)
    kind = Column(String(64), nullable=False)
    payload_hash = Column(String(64), nullable=False)

    @declared_attr
    def __table_args__(cls) -> Any:
        return (
            Index(f"ix_{cls.__tablename__}_org_id", "org_id"),
            UniqueConstraint(
                "org_id",
                "key",
                "kind",
                name=f"uq_{cls.__tablename__}_idempotency",
            ),
        )
