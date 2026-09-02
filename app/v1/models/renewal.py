"""Lease Renewal ORM.

AGENTS.md §4 invariants encoded here:

- Money is ``NUMERIC(14, 2)``; never float.
- Timestamps are ``timestamptz``; never naive.
- Every table is org-scoped (``org_id`` FK RESTRICT + index).
- ``LeaseRenewal`` carries a typed renewal proposal attached to an ACTIVE
  lease. AGENTS.md §3 + Issue #112 §"Lease Renewal" freeze the
  7-stage lifecycle:

  ``DETECT_EXPIRY → CONTACT_TENANT → TENANT_RESPONSE →
   OWNER_DECISION → EXECUTE → VERIFY → CLOSED``

  Two pipelines share one row type, distinguished by their entry path:
  - **Legacy pipeline** (PROPOSED → APPROVED → EXECUTED; plus REJECTED
    / CANCELLED). Entry via ``propose`` with proposed terms. Remains
    untouched for backward compatibility with the existing 34-test
    suite.
  - **7-stage pipeline** (DETECT_EXPIRY → CONTACT_TENANT →
    TENANT_RESPONSE → OWNER_DECISION → EXECUTE → VERIFY → CLOSED).
    Entry via ``detect_upcoming`` (system scan, idempotent on
    ``(org_id, source_lease_id, scan_window_days)``). The legacy
    ``execute`` closure gate is shared — for the 7-stage pipeline
    ``EXECUTED`` is followed by an owner ``verify`` step before the
    renewal terminates (CLOSED).

  The Operation's resolution point differs between pipelines:
  - Legacy: resolves at EXECUTED.
  - 7-stage: stays OPEN through EXECUTE, resolves at CLOSED (post
    verify).

- ``(org_id, idempotency_key)`` is UNIQUE on ``v1_lease_renewals`` so a
  propose can never be duplicated by a replay.
- For 7-stage scans, ``(org_id, source_lease_id, scan_window_days)``
  is also UNIQUE; NULL ``scan_window_days`` is treated as distinct by
  PostgreSQL so legacy propose-created rows are unaffected.
- ``Operation`` is reused polymorphically (``subject_type='lease_renewal'``,
  ``kind='LEASE_RENEWAL'``). The Operation is the business truth: a
  renewal row is the business flow; the Operation is its resolution.
- ``Task`` is the projection of a follow-up (e.g. "call tenant",
  "collect new terms"). A Task can never resolve an Operation by
  itself.
- ``Approval != Execution``. A proposal may be APPROVED without ever
  being EXECUTED (e.g. tenant changes mind); the source lease keeps
  its original terms until ``execute`` is called.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.v1.models.base import BigPK, TimestampMixin, V1Base, utcnow


# --- domain vocabulary ------------------------------------------------


class RenewalState(StrEnum):
    """11-state renewal lifecycle (legacy 5 + 7-stage 6 + CLOSED + CANCELLED).

    Legacy states (kept for backward compatibility — the existing
    ``tests/test_v1_api_renewals.py`` asserts these literals):

    PROPOSED      -> tenant/secretary submitted a typed renewal proposal
    APPROVED      -> OWNER accepted the proposed terms
    REJECTED      -> OWNER rejected the proposal (terminal, source lease
                     unaffected)
    EXECUTED      -> source lease terminated + new lease activated + unit
                     status flipped (legacy closure gate)

    7-stage states (Issue #112 §"Lease Renewal"):

    DETECT_EXPIRY    -> system scan picked up an upcoming lease expiry
    CONTACT_TENANT   -> owner/secretary reached out to tenant
    TENANT_RESPONSE  -> tenant replied (RENEW / TERMINATE / DEFER)
    OWNER_DECISION   -> owner decided based on tenant response
    EXECUTE          -> shared closure gate reached; source lease
                        terminated, new lease activated; Operation
                        stays OPEN until CLOSED.
    VERIFY           -> owner confirmed execution (post-EXECUTE)
    CLOSED           -> terminal, Operation resolved (distinct from EXECUTED)

    Shared terminal states (any pipeline):

    CANCELLED     -> pipeline withdrawn before execution (terminal,
                     source lease unaffected)
    """

    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTED = "EXECUTED"
    DETECT_EXPIRY = "DETECT_EXPIRY"
    CONTACT_TENANT = "CONTACT_TENANT"
    TENANT_RESPONSE = "TENANT_RESPONSE"
    OWNER_DECISION = "OWNER_DECISION"
    EXECUTE = "EXECUTE"
    VERIFY = "VERIFY"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class RenewalTenantResponse(StrEnum):
    """Tenant's reply recorded at TENANT_RESPONSE.

    Per Issue #112 FR-8: tenant must answer with one of three outcomes.
    RENEW keeps the 7-stage pipeline running toward OWNER_DECISION +
    EXECUTE; TERMINATE is an early terminal (lease ends as planned);
    DEFER keeps the lease pending with the owner deciding later.
    """

    RENEW = "RENEW"
    TERMINATE = "TERMINATE"
    DEFER = "DEFER"


class RenewalOwnerDecision(StrEnum):
    """Owner's decision recorded at OWNER_DECISION.

    Same shape as tenant response (also per FR-8). RENEW proceeds to
    EXECUTE; TERMINATE is an early terminal; DEFER keeps the renewal
    open pending further input.
    """

    RENEW = "RENEW"
    TERMINATE = "TERMINATE"
    DEFER = "DEFER"


class RenewalActivityKind(StrEnum):
    """Append-only renewal history / activity feed."""

    # Legacy pipeline kinds.
    PROPOSED = "PROPOSED"
    PROPOSAL_REPLAYED = "PROPOSAL_REPLAYED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTED = "EXECUTED"
    CANCELLED = "CANCELLED"
    SOURCE_LEASE_TERMINATED = "SOURCE_LEASE_TERMINATED"
    NEW_LEASE_CREATED = "NEW_LEASE_CREATED"
    NEW_LEASE_ACTIVATED = "NEW_LEASE_ACTIVATED"
    UNIT_REASSIGNED = "UNIT_REASSIGNED"
    REOPENED = "REOPENED"
    FOLLOW_UP_CREATED = "FOLLOW_UP_CREATED"
    FOLLOW_UP_DONE = "FOLLOW_UP_DONE"

    # 7-stage pipeline kinds (Issue #112 §"Lease Renewal").
    DETECTED = "DETECTED"
    SCAN_REPLAYED = "SCAN_REPLAYED"
    TENANT_CONTACTED = "TENANT_CONTACTED"
    TENANT_RESPONDED = "TENANT_RESPONDED"
    OWNER_DECIDED = "OWNER_DECIDED"
    EXECUTION_VERIFIED = "EXECUTION_VERIFIED"
    CLOSED = "CLOSED"


RENEWAL_STATES = tuple(s.value for s in RenewalState)
RENEWAL_ACTIVITY_KINDS = tuple(k.value for k in RenewalActivityKind)

OPERATION_KIND_LEASE_RENEWAL = "LEASE_RENEWAL"
OPERATION_SUBJECT_LEASE_RENEWAL = "lease_renewal"
TASK_KIND_RENEWAL_FOLLOW_UP = "RENEWAL_FOLLOW_UP"


# --- tables -----------------------------------------------------------


class LeaseRenewal(V1Base, TimestampMixin):
    """A typed renewal proposal attached to an ACTIVE lease.

    Single row type carries both pipelines. The pipeline is implicit
    in how the row entered: a row created via ``detect_upcoming``
    carries a non-null ``scan_window_days`` (and a
    ``scan_key = (org_id, source_lease_id, scan_window_days)``
    unique-together); a row created via ``propose`` keeps
    ``scan_window_days = NULL`` (PostgreSQL distinct NULLs rule
    preserves the existing idempotency semantics).

    ``idempotency_key`` is opaque, case-preserving; mandatory for
    propose, NULL for detect-creates (since scan idempotency uses
    the ``scan_key`` UNIQUE index).
    ``new_lease_id`` is populated only when ``execute`` has produced
    the replacement lease; until then it is NULL.
    """

    __tablename__ = "v1_lease_renewals"
    __table_args__ = (
        CheckConstraint(
            "state IN ("
            "'PROPOSED','APPROVED','REJECTED','EXECUTED',"
            "'DETECT_EXPIRY','CONTACT_TENANT','TENANT_RESPONSE',"
            "'OWNER_DECISION','EXECUTE','VERIFY','CLOSED',"
            "'CANCELLED'"
            ")",
            name="ck_v1_lease_renewals_state",
        ),
        CheckConstraint(
            "proposed_end_date > proposed_start_date",
            name="ck_v1_lease_renewals_dates",
        ),
        CheckConstraint(
            "proposed_monthly_rent > 0",
            name="ck_v1_lease_renewals_rent_positive",
        ),
        CheckConstraint(
            "proposed_deposit >= 0",
            name="ck_v1_lease_renewals_deposit_nonneg",
        ),
        CheckConstraint(
            "scan_window_days IS NULL OR scan_window_days > 0",
            name="ck_v1_lease_renewals_scan_window_positive",
        ),
        UniqueConstraint(
            "org_id",
            "idempotency_key",
            name="uq_v1_lease_renewals_org_idempotency_key",
        ),
        # Scan idempotency: PostgreSQL treats NULL distinct, so legacy
        # propose-created rows with NULL scan_window_days are
        # unaffected by this UNIQUE. The migration adds this index
        # with NULLS DISTINCT (PG default) only when scan_window_days
        # is non-NULL.
        UniqueConstraint(
            "org_id",
            "source_lease_id",
            "scan_window_days",
            name="uq_v1_lease_renewals_scan_key",
        ),
        Index("ix_v1_lease_renewals_org_id", "org_id"),
        Index(
            "ix_v1_lease_renewals_org_state", "org_id", "state",
        ),
        Index(
            "ix_v1_lease_renewals_source_lease_id", "source_lease_id",
        ),
        Index(
            "ix_v1_lease_renewals_new_lease_id", "new_lease_id",
        ),
        Index(
            "ix_v1_lease_renewals_org_window",
            "org_id", "scan_window_days",
        ),
    )

    id: Mapped[BigPK]
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_lease_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_leases.id", ondelete="RESTRICT"),
        nullable=False,
    )
    new_lease_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_leases.id", ondelete="RESTRICT"),
        nullable=True,
    )
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default=RenewalState.PROPOSED.value,
    )
    proposed_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    proposed_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    proposed_monthly_rent: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False,
    )
    proposed_deposit: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0"),
    )
    proposed_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    proposed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    decided_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    decision_reason: Mapped[str | None] = mapped_column(
        String(500), nullable=True,
    )
    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    idempotency_key: Mapped[str | None] = mapped_column(
        String(128), nullable=True,
    )
    payload_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, default="",
    )

    # ---- 7-stage pipeline additions (Issue #112 §"Lease Renewal") ----
    # Nullable on legacy rows; the additive migration introduces them
    # one at a time so no destructive rewrite is required.

    scan_window_days: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
    )
    scan_key: Mapped[str | None] = mapped_column(
        String(128), nullable=True,
    )
    contact_method: Mapped[str | None] = mapped_column(
        String(40), nullable=True,
    )
    contacted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    tenant_response: Mapped[str | None] = mapped_column(
        String(16), nullable=True,
    )
    tenant_response_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    owner_decision: Mapped[str | None] = mapped_column(
        String(16), nullable=True,
    )
    owner_decision_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    verified_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    closed_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_users.id", ondelete="SET NULL"),
        nullable=True,
    )


class RenewalActivity(V1Base, TimestampMixin):
    """Append-only renewal history / activity feed."""

    __tablename__ = "v1_renewal_activities"
    __table_args__ = (
        Index("ix_v1_renewal_activities_org_id", "org_id"),
        Index(
            "ix_v1_renewal_activities_renewal_id", "renewal_id",
        ),
    )

    id: Mapped[BigPK]
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    renewal_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_lease_renewals.id", ondelete="RESTRICT"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


__all__ = [
    "OPERATION_KIND_LEASE_RENEWAL",
    "OPERATION_SUBJECT_LEASE_RENEWAL",
    "RENEWAL_ACTIVITY_KINDS",
    "RENEWAL_STATES",
    "RenewalActivity",
    "RenewalActivityKind",
    "RenewalOwnerDecision",
    "RenewalState",
    "RenewalTenantResponse",
    "TASK_KIND_RENEWAL_FOLLOW_UP",
]
