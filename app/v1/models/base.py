"""V1 SQLAlchemy declarative base + helpers."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Column, DateTime
from sqlalchemy.orm import DeclarativeBase


def utcnow() -> datetime:
    """Return the current UTC time as a tz-aware datetime."""
    return datetime.now(tz=timezone.utc)


class V1Base(DeclarativeBase):
    """Declarative base for V1 ORM models."""


class TimestampMixin:
    """Adds created_at / updated_at columns (UTC-aware)."""

    created_at = Column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow,
    )


def big_pk() -> Column:
    """BigInteger primary key column factory (BIGSERIAL)."""
    return Column(BigInteger, primary_key=True, autoincrement=True)