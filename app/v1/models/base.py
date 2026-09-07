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


# Issue #119 P0 v1_api_credential_bootstrap: V1 SYSTEM principal.
#
# The clean rewrite never had a SYSTEM principal: every credential must
# resolve to a HUMAN membership via v1_memberships (Role.OWNER /
# Role.SECRETARY only). The legacy V1.3 identity model carried
# Principal.principal_type = {HUMAN, SERVICE, SYSTEM} + ApiCredential.purpose
# = {telegram_bot, internal:scheduler, ...} so the scheduled jobs could
# authenticate as a non-human SYSTEM principal without binding any
# Telegram id and without being accepted on Owner-only writes.
#
# V1 adds principal_type + purpose as additive columns on
# v1_api_credentials. The default is HUMAN; only credentials explicitly
# created with principal_type='SYSTEM' can authenticate against the
# narrow SYSTEM reader endpoints (JOB-SERVICE-AUTH-002 carryover).
# SYSTEM credentials are NEVER accepted by ``get_current_principal`` —
# the HUMAN-membership dep stays fail-closed for SYSTEM callers.
class V1PrincipalType(StrEnum):
    HUMAN = "HUMAN"
    SYSTEM = "SYSTEM"


# Single canonical SYSTEM purpose string (JOB-SERVICE-AUTH-002 carryover).
# Tightening: any SYSTEM credential must carry purpose == this exact value
# to be accepted by ``app.v1.deps.get_system_principal``. Adding more
# SYSTEM purposes (reconcile / notifier / backfill) requires an explicit
# NameAllowList update here — there is no implicit fallback.
V1_SYSTEM_PURPOSES = frozenset({"internal:scheduler"})
