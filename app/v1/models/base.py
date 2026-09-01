"""V1 SQLAlchemy declarative base + shared enums."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated

from sqlalchemy import BigInteger, DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


BigPK = Annotated[int, mapped_column(BigInteger, primary_key=True, autoincrement=True)]


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class V1Base(DeclarativeBase):
    pass


# DATA_CONTRACT §2.5 — exactly two roles, two states. No TENANT, no ADMIN, no SUSPENDED.
class MembershipRole(StrEnum):
    OWNER = "OWNER"
    SECRETARY = "SECRETARY"


class MembershipState(StrEnum):
    ACTIVE = "ACTIVE"
    REMOVED = "REMOVED"


class LeaseState(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    TERMINATED = "TERMINATED"


class UnitStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    OCCUPIED = "OCCUPIED"
    MAINTENANCE = "MAINTENANCE"


class OperationState(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"


class TaskState(StrEnum):
    OPEN = "open"
    DONE = "done"
    CANCELLED = "cancelled"


class RentPaymentStatus(StrEnum):
    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    REVERSED = "REVERSED"
