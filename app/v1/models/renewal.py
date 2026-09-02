"""Lease Renewal ORM.

AGENTS.md §4 invariants encoded here:

- Money is ``NUMERIC(14, 2)``; never float.
- Timestamps are ``timestamptz``; never naive.
- Every table is org-scoped (``org_id`` FK RESTRICT + index).
- ``LeaseRenewal`` is a typed proposal: someone proposes a renewal of an
  ACTIVE lease with new dates, new monthly rent, new deposit. The proposal
  goes through ``PROPOSED -> APPROVED -> EXECUTED`` (or REJECTED /
  CANCELLED). Only ``execute`` mutates the source lease and creates the
  new lease; this is the single transition path that touches
  ``v1_leases``.
- ``(org_id, idempotency_key)`` is UNIQUE on ``v1_lease_renewals`` so a
  proposal can never be duplicated by a replay.
- ``Operation`` is reused polymorphically (``subject_type='lease_renewal'``,
  ``kind='LEASE_RENEWAL'``). The Operation is the business truth: a
  renewal resolves ONLY when ``execute`` has produced the new lease and
  flipped the unit status to OCCUPIED for the new lease.
- ``Task`` is the projection of a follow-up (e.g. "send new terms to
  tenant for signature"). A Task can never resolve an Operation by
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
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.v1.models.base import BigPK, TimestampMixin, V1Base, utcnow


# --- domain vocabulary ------------------------------------------------


class RenewalState(StrEnum):
    """Lease Renewal lifecycle.

    Frozen Issue #112 lifecycle::

        DETECT_EXPIRY → CONTACT_TENANT → TENANT_RESPONSE
            → OWNER_DECISION → EXECUTE → VERIFY → CLOSED

    Plus the legacy 5-state proposal pipeline and the universal
    REJECTED / CANCELLED terminals. The 7-stage lifecycle is the
    canonical path that satisfies FR-8 (Issue #112 §"Lease Renewal");
    the legacy states are retained for backward compatibility with
    renewal rows created via ``propose`` and with the existing
    ``tests/test_v1_api_renewals.py`` test-suite.

    Legacy 5-state pipeline (unchanged, backward-compatible):
        PROPOSED  -> tenant/secretary submitted a typed renewal proposal
        APPROVED  -> OWNER accepted the proposed terms
        REJECTED  -> OWNER rejected the proposal (terminal, source lease
                     unaffected)
        EXECUTED  -> source lease terminated + new lease activated + unit
                     status flipped (closure gate; operation resolved)
        CANCELLED -> proposal withdrawn before execution (terminal,
                     source lease unaffected)

    Frozen 7-stage pipeline (Issue #112):
        DETECT_EXPIRY    -> system detected lease end_date within scan window
        CONTACT_TENANT   -> outbound contact recorded for the renewal
        TENANT_RESPONSE  -> tenant reply (renew / terminate / defer) recorded
        OWNER_DECISION   -> owner decides based on tenant reply; equivalent
                            semantic to APPROVED for ``decision=renew`` or
                            REJECTED for ``decision=terminate``
        EXECUTE          -> business effects applied (legacy EXECUTED state
                            reused; closure gate; operation resolves here
                            for the legacy path, stays OPEN for the new
                            pipeline until CLOSED)
        VERIFY           -> owner confirms the executed change matches the
                            decision; post-execution reconciliation
        CLOSED           -> terminal administrative closure (operation
                            resolves for the new pipeline here)

    State machine (new pipeline, see ``app.v1.services.renewal``):
        DETECT_EXPIRY → CONTACT_TENANT → TENANT_RESPONSE → OWNER_DECISION
            → EXECUTED → VERIFY → CLOSED
        OWNER_DECISION → REJECTED (terminal, when decision=terminate)
        any non-terminal → CANCELLED (terminal, universal escape hatch)
    """

    # Legacy 5-state proposal pipeline (unchanged, backward-compatible)
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTED = "EXECUTED"
    CANCELLED = "CANCELLED"

    # Frozen 7-stage pipeline (Issue #112 §"Lease Renewal")
    DETECT_EXPIRY = "DETECT_EXPIRY"
    CONTACT_TENANT = "CONTACT_TENANT"
    TENANT_RESPONSE = "TENANT_RESPONSE"
    OWNER_DECISION = "OWNER_DECISION"
    VERIFY = "VERIFY"
    CLOSED = "CLOSED"


class RenewalActivityKind(StrEnum):
    """Append-only renewal history / activity feed."""

    # Legacy proposal pipeline activity kinds
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

    # Frozen 7-stage pipeline activity kinds (Issue #112 §"Lease Renewal")
    DETECTED = "DETECTED"
    TENANT_CONTACTED = "TENANT_CONTACTED"
    TENANT_RESPONDED = "TENANT_RESPONDED"
    OWNER_DECIDED = "OWNER_DECIDED"
    EXECUTION_VERIFIED = "EXECUTION_VERIFIED"
    CLOSED = "CLOSED"
    SCAN_REPLAYED = "SCAN_REPLAYED"


RENEWAL_STATES = tuple(s.value for s in RenewalState)
RENEWAL_ACTIVITY_KINDS = tuple(k.value for k in RenewalActivityKind)


# Frozen 7-stage pipeline (Issue #112 §"Lease Renewal") domain vocabulary.
# Tenant reply + owner decision vocabulary for the
# TENANT_RESPONSE / OWNER_DECISION stages (FR-8: "Decision is either
# ``renew``, ``terminate``, or ``defer``"). Stored as a single column of
# ``String(16)`` so the vocabulary is bounded and forward-compatible.
class RenewalTenantResponse(StrEnum):
    RENEW = "RENEW"
    TERMINATE = "TERMINATE"
    DEFER = "DEFER"


class RenewalOwnerDecision(StrEnum):
    RENEW = "RENEW"
    TERMINATE = "TERMINATE"
    DEFER = "DEFER"


RENEWAL_TENANT_RESPONSES = tuple(r.value for r in RenewalTenantResponse)
RENEWAL_OWNER_DECISIONS = tuple(r.value for r in RenewalOwnerDecision)


OPERATION_KIND_LEASE_RENEWAL = "LEASE_RENEWAL"
OPERATION_SUBJECT_LEASE_RENEWAL = "lease_renewal"
TASK_KIND_RENEWAL_FOLLOW_UP = "RENEWAL_FOLLOW_UP"


# --- tables -----------------------------------------------------------


class LeaseRenewal(V1Base, TimestampMixin):
    """A typed renewal proposal attached to an ACTIVE lease.

    ``idempotency_key`` is opaque, case-preserving; it carries the
    ``propose`` replay/conflict surface for the legacy 5-state pipeline.
    ``scan_key`` is the dedicated idempotency surface for the frozen
    7-stage pipeline's ``detect_upcoming`` entry point and is keyed on
    ``(org_id, source_lease_id, scan_window_days)`` so a system-driven
    scan never duplicates a renewal.

    ``new_lease_id`` is populated only when ``execute`` has produced the
    replacement lease; until then it is NULL.

    ``scan_window_days`` records the window size used by the scan that
    produced the renewal (NULL when the renewal was created via the
    legacy ``propose`` endpoint).
    """

    __tablename__ = "v1_lease_renewals"
    __table_args__ = (
        CheckConstraint(
            "state IN ("
            "'PROPOSED','APPROVED','REJECTED','EXECUTED','CANCELLED',"
            "'DETECT_EXPIRY','CONTACT_TENANT','TENANT_RESPONSE',"
            "'OWNER_DECISION','VERIFY','CLOSED'"
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
        # Frozen 7-stage pipeline (Issue #112) idempotency surface: a
        # scan is keyed on (org_id, source_lease_id, scan_window_days)
        # so a system-driven detect_upcoming never duplicates a renewal
        # for the same lease within the same window. PostgreSQL treats
        # NULL ``scan_window_days`` values as distinct, so legacy
        # ``propose``-created rows (which leave ``scan_window_days``
        # NULL) are unaffected and may still appear multiple times for
        # the same lease.
        Index(
            "uq_v1_lease_renewals_org_source_scan",
            "org_id", "source_lease_id", "scan_window_days",
            unique=True,
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
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Frozen 7-stage pipeline (Issue #112 §"Lease Renewal") fields.
    # ``scan_window_days`` records the window size used by the scan that
    # produced the renewal; ``scan_key`` is the deterministic idempotency
    # key for ``detect_upcoming`` (NULL on legacy ``propose``-created rows).
    scan_window_days: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True,
    )
    scan_key: Mapped[str | None] = mapped_column(
        String(160), nullable=True,
    )
    # Tenant reply recorded during the TENANT_RESPONSE stage. One of
    # {RENEW, TERMINATE, DEFER} (see ``RenewalTenantResponse``); NULL
    # until the tenant has actually replied. This is NOT the owner's
    # decision — that is tracked separately via ``decision_reason`` and
    # the OWNER_DECISION transition.
    tenant_response: Mapped[str | None] = mapped_column(
        String(16), nullable=True,
    )
    tenant_response_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # Owner's decision recorded during the OWNER_DECISION stage. One of
    # {RENEW, TERMINATE, DEFER}; NULL until the owner has actually decided.
    owner_decision: Mapped[str | None] = mapped_column(
        String(16), nullable=True,
    )
    owner_decision_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # Verification evidence recorded during the VERIFY stage. Optional
    # human-readable confirmation note from the owner.
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    verified_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Administrative closure (CLOSED stage) timestamp + actor.
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
    "RENEWAL_OWNER_DECISIONS",
    "RENEWAL_STATES",
    "RENEWAL_TENANT_RESPONSES",
    "RenewalActivity",
    "RenewalActivityKind",
    "RenewalOwnerDecision",
    "RenewalState",
    "RenewalTenantResponse",
    "TASK_KIND_RENEWAL_FOLLOW_UP",
]
