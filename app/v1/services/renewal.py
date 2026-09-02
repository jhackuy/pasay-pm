"""Lease Renewal service — single source of truth for the renewal lifecycle.

AGENTS.md §4 invariants enforced here:

- Operation is Truth, Task is Projection. A Task can never resolve an
  Operation by itself. The Operation only resolves (renewal → EXECUTED
  + Operation.state=resolved) when ``execute`` produces the new lease
  and flips the unit status.
- Money is Decimal only (``parse_money`` rejects float/bool with
  ``MoneyError``).
- Idempotency keys are opaque and case-preserving; same key + same
  payload returns the same renewal (``replayed=True``); same key +
  different payload raises ``IdempotencyConflictError``.
- Org-scope is enforced via ``require_org_scope`` at the top of every
  method (fail-closed).
- ``Approval != Execution``. ``approve`` accepts the proposed terms but
  does NOT touch the source lease. Only ``execute`` terminates the
  source lease, creates the new lease, activates it, flips the unit
  status, and resolves the Operation.
- ``Proposal != Quote != Renewal``. The renewal is its own table; we
  delegate to ``app.v1.services.lease`` module-level functions for
  lease-state mutations to avoid duplicate state machines.
- ``Reminder != Completion``. ``complete_follow_up`` NEVER resolves the
  Operation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.core.idempotency import (
    IdempotencyConflictError,
    compute_payload_hash,
    normalize_idempotency_key,
)
from app.core.money import parse_money
from app.core.permissions import (
    PermissionDenied,
    Principal,
    Role,
    require_org_scope,
)
from app.core.time import utcnow
from app.v1.models.base import LeaseState, OperationState
from app.v1.models.renewal import (
    OPERATION_KIND_LEASE_RENEWAL,
    OPERATION_SUBJECT_LEASE_RENEWAL,
    RENEWAL_ACTIVITY_KINDS,
    RENEWAL_STATES,
    RenewalActivity,
    RenewalActivityKind,
    RenewalOwnerDecision,
    RenewalState,
    RenewalTenantResponse,
    TASK_KIND_RENEWAL_FOLLOW_UP,
)
from app.v1.models.rent_payment import Operation, Task
from app.v1.models.tenant_lease import Lease
from app.v1.services.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.v1.services.lease import activate_lease, create_lease, terminate_lease


# ---------- result types ----------


@dataclass(frozen=True)
class RenewalProposeResult:
    """Result of proposing a renewal.

    ``replayed=True`` means an identical ``(org_id, idempotency_key,
    payload_hash)`` was already stored; the existing renewal is
    returned. The router maps this to 200 OK instead of 201 Created.
    """

    replayed: bool
    renewal: LeaseRenewal  # noqa: F821 -- forward ref, real name below


# Local import alias so the result type is self-contained without
# triggering an import cycle in type-checkers.
from app.v1.models.renewal import LeaseRenewal  # noqa: E402


@dataclass(frozen=True)
class RenewalExecuteResult:
    """Result of executing an APPROVED renewal."""

    renewal: LeaseRenewal
    new_lease: Lease


@dataclass(frozen=True)
class RenewalScanResult:
    """Result of ``detect_upcoming`` — 7-stage pipeline entry point.

    ``detected`` carries the new DETECT_EXPIRY rows created in this
    scan; ``replayed`` carries the existing rows that were skipped
    due to the ``(org_id, source_lease_id, scan_window_days)``
    idempotency UNIQUE.
    """

    scan_window_days: int
    detected: list[LeaseRenewal]
    replayed: list[LeaseRenewal]


# ---------- helpers ----------


def _ensure_role(principal: Principal, *allowed: Role) -> None:
    if principal.role not in allowed:
        raise PermissionDenied(
            f"role {principal.role.value} not allowed "
            f"(must be one of: {[r.value for r in allowed]})"
        )


def _log_activity(
    db: Session,
    *,
    org_id: int,
    renewal_id: int,
    kind: RenewalActivityKind,
    actor_user_id: Optional[int],
    detail: Optional[str] = None,
) -> None:
    db.add(
        RenewalActivity(
            org_id=org_id,
            renewal_id=renewal_id,
            kind=kind.value,
            actor_user_id=actor_user_id,
            detail=detail,
            occurred_at=utcnow(),
        )
    )


def _close_operation(
    db: Session,
    *,
    operation: Operation,
    actor_user_id: Optional[int],
) -> None:
    """Resolve the renewal's linked Operation. Idempotent.

    Called only from ``execute`` after the new lease has been activated.
    Cancellation paths call this too if the Operation is still open.
    """
    if operation.state == OperationState.RESOLVED.value:
        return
    operation.state = OperationState.RESOLVED.value
    operation.resolved_at = utcnow()
    # Cancel any remaining open follow-ups (Task is Projection).
    open_tasks = (
        db.query(Task)
        .filter(
            Task.operation_id == operation.id,
            Task.state == "open",
        )
        .all()
    )
    for t in open_tasks:
        t.state = "cancelled"
    db.flush()


def _bump_to_in_progress(operation: Operation) -> bool:
    """OPEN → IN_PROGRESS. Never advances to RESOLVED."""
    if operation.state == OperationState.OPEN.value:
        operation.state = OperationState.IN_PROGRESS.value
        return True
    return False


def _get_or_create_operation(
    db: Session,
    *,
    org_id: int,
    renewal: LeaseRenewal,
) -> Operation:
    """Fetch the renewal's linked Operation, creating it if missing.

    Existing renewals created before the Operation was attached (or in
    migrations) get the operation back-filled lazily.
    """
    op = (
        db.query(Operation)
        .filter(
            Operation.org_id == org_id,
            Operation.subject_type == OPERATION_SUBJECT_LEASE_RENEWAL,
            Operation.subject_id == renewal.id,
        )
        .first()
    )
    if op is not None:
        return op
    op = Operation(
        org_id=org_id,
        kind=OPERATION_KIND_LEASE_RENEWAL,
        subject_type=OPERATION_SUBJECT_LEASE_RENEWAL,
        subject_id=renewal.id,
        state=OperationState.OPEN.value,
    )
    db.add(op)
    db.flush()
    return op


# ---------- service ----------


class LeaseRenewalService:
    """Cohesive application/domain service for the lease-renewal cycle."""

    def __init__(self, db: Session) -> None:
        self.db = db

    # ---- read helpers ----

    def get_renewal(
        self,
        principal: Principal,
        *,
        org_id: int,
        renewal_id: int,
    ) -> LeaseRenewal:
        require_org_scope(principal, org_id)
        renewal = self.db.get(LeaseRenewal, renewal_id)
        if renewal is None or renewal.org_id != org_id:
            raise NotFoundError(
                f"renewal {renewal_id} not found in org {org_id}",
            )
        return renewal

    def get_operation(
        self,
        principal: Principal,
        *,
        org_id: int,
        renewal_id: int,
    ) -> Operation:
        require_org_scope(principal, org_id)
        self.get_renewal(principal, org_id=org_id, renewal_id=renewal_id)
        op = (
            self.db.query(Operation)
            .filter(
                Operation.org_id == org_id,
                Operation.subject_type == OPERATION_SUBJECT_LEASE_RENEWAL,
                Operation.subject_id == renewal_id,
            )
            .first()
        )
        if op is None:
            raise NotFoundError(
                f"operation for renewal {renewal_id} not found",
            )
        return op

    def list_renewals(
        self,
        principal: Principal,
        *,
        org_id: int,
        state: Optional[str] = None,
        source_lease_id: Optional[int] = None,
    ) -> list[LeaseRenewal]:
        require_org_scope(principal, org_id)
        query = self.db.query(LeaseRenewal).filter(
            LeaseRenewal.org_id == org_id,
        )
        if state is not None:
            query = query.filter(LeaseRenewal.state == state)
        if source_lease_id is not None:
            query = query.filter(
                LeaseRenewal.source_lease_id == source_lease_id,
            )
        return query.order_by(LeaseRenewal.id.asc()).all()

    def list_activity(
        self,
        principal: Principal,
        *,
        org_id: int,
        renewal_id: int,
    ) -> list[RenewalActivity]:
        require_org_scope(principal, org_id)
        self.get_renewal(principal, org_id=org_id, renewal_id=renewal_id)
        return (
            self.db.query(RenewalActivity)
            .filter(
                RenewalActivity.org_id == org_id,
                RenewalActivity.renewal_id == renewal_id,
            )
            .order_by(RenewalActivity.id.asc())
            .all()
        )

    # ---- propose ----

    def propose(
        self,
        principal: Principal,
        *,
        org_id: int,
        source_lease_id: int,
        proposed_start_date: Any,
        proposed_end_date: Any,
        proposed_monthly_rent: Decimal | str | int,
        proposed_deposit: Decimal | str | int = 0,
        idempotency_key: str,
    ) -> RenewalProposeResult:
        """Propose a renewal. Idempotent on ``(org_id, key)``."""
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER, Role.SECRETARY)
        key = normalize_idempotency_key(idempotency_key)
        rent = parse_money(proposed_monthly_rent)
        deposit = parse_money(proposed_deposit)
        if proposed_end_date <= proposed_start_date:
            raise ValidationError(
                "proposed_end_date must be strictly after proposed_start_date",
            )
        # Verify source lease exists in this org and is ACTIVE.
        source_lease = self.db.get(Lease, source_lease_id)
        if (
            source_lease is None
            or source_lease.org_id != org_id
        ):
            raise NotFoundError(
                f"source lease {source_lease_id} not found in org {org_id}",
            )
        if source_lease.state != LeaseState.ACTIVE.value:
            raise ConflictError(
                f"source lease {source_lease_id} is not ACTIVE "
                f"(state={source_lease.state})",
            )
        payload = {
            "source_lease_id": source_lease_id,
            "proposed_start_date": str(proposed_start_date),
            "proposed_end_date": str(proposed_end_date),
            "proposed_monthly_rent": str(rent),
            "proposed_deposit": str(deposit),
        }
        payload_hash = compute_payload_hash(payload)
        # Replay?
        existing = (
            self.db.query(LeaseRenewal)
            .filter(
                LeaseRenewal.org_id == org_id,
                LeaseRenewal.idempotency_key == key,
            )
            .first()
        )
        if existing is not None:
            if existing.payload_hash != payload_hash:
                raise IdempotencyConflictError(
                    f"idempotency key {key!r} reused with a different payload",
                )
            _log_activity(
                self.db,
                org_id=org_id,
                renewal_id=existing.id,
                kind=RenewalActivityKind.PROPOSAL_REPLAYED,
                actor_user_id=principal.user_id,
            )
            self.db.commit()
            return RenewalProposeResult(replayed=True, renewal=existing)
        renewal = LeaseRenewal(
            org_id=org_id,
            source_lease_id=source_lease_id,
            new_lease_id=None,
            state=RenewalState.PROPOSED.value,
            proposed_start_date=proposed_start_date,
            proposed_end_date=proposed_end_date,
            proposed_monthly_rent=rent,
            proposed_deposit=deposit,
            proposed_by_user_id=principal.user_id,
            idempotency_key=key,
            payload_hash=payload_hash,
        )
        self.db.add(renewal)
        self.db.flush()
        # Create the linked Operation up-front.
        op = Operation(
            org_id=org_id,
            kind=OPERATION_KIND_LEASE_RENEWAL,
            subject_type=OPERATION_SUBJECT_LEASE_RENEWAL,
            subject_id=renewal.id,
            state=OperationState.OPEN.value,
        )
        self.db.add(op)
        self.db.flush()
        _log_activity(
            self.db,
            org_id=org_id,
            renewal_id=renewal.id,
            kind=RenewalActivityKind.PROPOSED,
            actor_user_id=principal.user_id,
        )
        self.db.commit()
        return RenewalProposeResult(replayed=False, renewal=renewal)

    # ---- approve / reject ----

    def approve(
        self,
        principal: Principal,
        *,
        org_id: int,
        renewal_id: int,
    ) -> LeaseRenewal:
        """OWNER-only. PROPOSED → APPROVED. Does NOT execute."""
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER)
        renewal = self.get_renewal(
            principal, org_id=org_id, renewal_id=renewal_id,
        )
        if renewal.state != RenewalState.PROPOSED.value:
            raise ConflictError(
                f"renewal {renewal_id} cannot be approved from "
                f"state {renewal.state!r}",
            )
        renewal.state = RenewalState.APPROVED.value
        renewal.decided_by_user_id = principal.user_id
        renewal.decided_at = utcnow()
        op = self.get_operation(
            principal, org_id=org_id, renewal_id=renewal_id,
        )
        _bump_to_in_progress(op)
        _log_activity(
            self.db,
            org_id=org_id,
            renewal_id=renewal.id,
            kind=RenewalActivityKind.APPROVED,
            actor_user_id=principal.user_id,
        )
        self.db.commit()
        return renewal

    def reject(
        self,
        principal: Principal,
        *,
        org_id: int,
        renewal_id: int,
        reason: str,
    ) -> LeaseRenewal:
        """OWNER-only. PROPOSED → REJECTED. Terminal, source lease unaffected."""
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER)
        if not reason or not reason.strip():
            raise ValidationError("rejection reason is required")
        renewal = self.get_renewal(
            principal, org_id=org_id, renewal_id=renewal_id,
        )
        if renewal.state != RenewalState.PROPOSED.value:
            raise ConflictError(
                f"renewal {renewal_id} cannot be rejected from "
                f"state {renewal.state!r}",
            )
        renewal.state = RenewalState.REJECTED.value
        renewal.decided_by_user_id = principal.user_id
        renewal.decided_at = utcnow()
        renewal.decision_reason = reason
        op = self.get_operation(
            principal, org_id=org_id, renewal_id=renewal_id,
        )
        _close_operation(
            self.db, operation=op, actor_user_id=principal.user_id,
        )
        _log_activity(
            self.db,
            org_id=org_id,
            renewal_id=renewal.id,
            kind=RenewalActivityKind.REJECTED,
            actor_user_id=principal.user_id,
            detail=reason,
        )
        self.db.commit()
        return renewal

    # ---- execute (closure gate) ----

    def execute(
        self,
        principal: Principal,
        *,
        org_id: int,
        renewal_id: int,
    ) -> RenewalExecuteResult:
        """APPROVED → EXECUTED (legacy) or OWNER_DECISION → EXECUTE
        (7-stage). The single closure gate.

        Action:
          1. Verify no overlapping ACTIVE lease on the same unit at the
             proposed start date.
          2. Terminate the source lease (ACTIVE → TERMINATED, unit
             becomes AVAILABLE).
          3. Create a new Lease in DRAFT with the proposed terms.
          4. Activate the new lease (DRAFT → ACTIVE, unit becomes
             OCCUPIED).
          5. Attach ``renewal.new_lease_id`` + ``renewal.executed_at``.
          6. Resolve the legacy Operation (only when the legacy
             pipeline entered via APPROVED). For the 7-stage pipeline
             the Operation stays OPEN through VERIFY; it is closed by
             ``close()`` after the owner verifies.
        """
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER, Role.SECRETARY)
        renewal = self.get_renewal(
            principal, org_id=org_id, renewal_id=renewal_id,
        )
        # Legacy pipeline enters at APPROVED; 7-stage enters at OWNER_DECISION.
        if renewal.state == RenewalState.APPROVED.value:
            legacy_pipeline = True
        elif renewal.state == RenewalState.OWNER_DECISION.value:
            legacy_pipeline = False
        else:
            raise ConflictError(
                f"renewal {renewal_id} cannot be executed from "
                f"state {renewal.state!r} "
                f"(must be APPROVED or OWNER_DECISION first)",
            )
        source_lease = self.db.get(Lease, renewal.source_lease_id)
        if (
            source_lease is None
            or source_lease.org_id != org_id
            or source_lease.state != LeaseState.ACTIVE.value
        ):
            raise ConflictError(
                f"source lease {renewal.source_lease_id} is no longer ACTIVE; "
                f"cannot execute renewal",
            )
        # Overlap check: no ACTIVE lease on the same unit covering the
        # proposed start date (the source lease itself will be terminated,
        # so any other ACTIVE lease would collide).
        overlapping = (
            self.db.query(Lease)
            .filter(
                Lease.org_id == org_id,
                Lease.unit_id == source_lease.unit_id,
                Lease.id != source_lease.id,
                Lease.state == LeaseState.ACTIVE.value,
                Lease.end_date > renewal.proposed_start_date,
                Lease.start_date < renewal.proposed_end_date,
            )
            .one_or_none()
        )
        if overlapping is not None:
            raise ConflictError(
                f"unit {source_lease.unit_id} already has overlapping "
                f"ACTIVE lease {overlapping.id}",
            )
        # 1) Terminate source lease.
        terminate_lease(
            self.db,
            org_id=org_id,
            lease_id=renewal.source_lease_id,
            actor_user_id=principal.user_id,
            actor_role=principal.role,
        )
        _log_activity(
            self.db,
            org_id=org_id,
            renewal_id=renewal.id,
            kind=RenewalActivityKind.SOURCE_LEASE_TERMINATED,
            actor_user_id=principal.user_id,
        )
        # 2) Create new lease in DRAFT.
        new_lease_obj = create_lease(
            self.db,
            org_id=org_id,
            unit_id=source_lease.unit_id,
            tenant_id=source_lease.tenant_id,
            start_date=renewal.proposed_start_date,
            end_date=renewal.proposed_end_date,
            monthly_rent=renewal.proposed_monthly_rent,
            deposit=renewal.proposed_deposit,
            owner_user_id=principal.user_id,
            actor_role=principal.role,
        )
        _log_activity(
            self.db,
            org_id=org_id,
            renewal_id=renewal.id,
            kind=RenewalActivityKind.NEW_LEASE_CREATED,
            actor_user_id=principal.user_id,
        )
        # 3) Activate new lease.
        activate_lease(
            self.db,
            org_id=org_id,
            lease_id=new_lease_obj.id,
            actor_user_id=principal.user_id,
            actor_role=principal.role,
        )
        _log_activity(
            self.db,
            org_id=org_id,
            renewal_id=renewal.id,
            kind=RenewalActivityKind.NEW_LEASE_ACTIVATED,
            actor_user_id=principal.user_id,
        )
        # 4) Attach to renewal + state transition.
        renewal.new_lease_id = new_lease_obj.id
        renewal.executed_at = utcnow()
        # State is pipeline-aware: legacy uses EXECUTED (terminal);
        # 7-stage moves to its own EXECUTE state so VERIFY can follow.
        renewal.state = (
            RenewalState.EXECUTED.value
            if legacy_pipeline
            else RenewalState.EXECUTE.value
        )
        _log_activity(
            self.db,
            org_id=org_id,
            renewal_id=renewal.id,
            kind=RenewalActivityKind.EXECUTED,
            actor_user_id=principal.user_id,
        )
        if legacy_pipeline:
            # 5) Resolve Operation (legacy resolution point = EXECUTED).
            op = self.get_operation(
                principal, org_id=org_id, renewal_id=renewal_id,
            )
            _close_operation(
                self.db, operation=op, actor_user_id=principal.user_id,
            )
        # For 7-stage: Operation stays OPEN through EXECUTE → VERIFY.
        self.db.commit()
        self.db.refresh(new_lease_obj)
        return RenewalExecuteResult(renewal=renewal, new_lease=new_lease_obj)

    # ---- cancel ----

    def cancel(
        self,
        principal: Principal,
        *,
        org_id: int,
        renewal_id: int,
        reason: str,
    ) -> LeaseRenewal:
        """OWNER-only. Non-terminal → CANCELLED. Resolves the Operation."""
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER)
        if not reason or not reason.strip():
            raise ValidationError("cancellation reason is required")
        renewal = self.get_renewal(
            principal, org_id=org_id, renewal_id=renewal_id,
        )
        if renewal.state in (
            RenewalState.EXECUTED.value,
            RenewalState.CANCELLED.value,
            RenewalState.REJECTED.value,
        ):
            raise ConflictError(
                f"cannot cancel a terminal renewal (state={renewal.state})",
            )
        renewal.state = RenewalState.CANCELLED.value
        renewal.decided_by_user_id = principal.user_id
        renewal.decided_at = utcnow()
        renewal.decision_reason = reason
        op = self.get_operation(
            principal, org_id=org_id, renewal_id=renewal_id,
        )
        _close_operation(
            self.db, operation=op, actor_user_id=principal.user_id,
        )
        _log_activity(
            self.db,
            org_id=org_id,
            renewal_id=renewal.id,
            kind=RenewalActivityKind.CANCELLED,
            actor_user_id=principal.user_id,
            detail=reason,
        )
        self.db.commit()
        return renewal

    # ====================================================================
    # 7-stage pipeline (Issue #112 §"Lease Renewal")
    # ====================================================================
    #
    #   DETECT_EXPIRY → CONTACT_TENANT → TENANT_RESPONSE →
    #   OWNER_DECISION → EXECUTE → VERIFY → CLOSED
    #
    # Lifecycle invariants:
    # - Each forward transition is gated by ``_assert_state``.
    # - Backward transitions are rejected; CLOSED / REJECTED /
    #   CANCELLED are terminal.
    # - DETECT_EXPIRY rows MUST carry a non-null ``scan_window_days``
    #   and a ``scan_key`` so a re-scan with the same window is
    #   idempotent (``SCAN_REPLAYED`` activity is logged).
    # - Operation is the business truth; for the 7-stage pipeline it
    #   stays OPEN across DETECT_EXPIRY → CONTACT_TENANT →
    #   TENANT_RESPONSE → OWNER_DECISION → EXECUTE → VERIFY, and
    #   resolves only at CLOSED.

    # Forward-only transition table for the 7-stage pipeline.
    _7STAGE_NEXT: dict[str, str] = {
        RenewalState.DETECT_EXPIRY.value: RenewalState.CONTACT_TENANT.value,
        RenewalState.CONTACT_TENANT.value: RenewalState.TENANT_RESPONSE.value,
        RenewalState.TENANT_RESPONSE.value: RenewalState.OWNER_DECISION.value,
        RenewalState.OWNER_DECISION.value: RenewalState.EXECUTE.value,
        RenewalState.EXECUTE.value: RenewalState.VERIFY.value,
        RenewalState.VERIFY.value: RenewalState.CLOSED.value,
    }

    def _assert_state(
        self, renewal: LeaseRenewal, *, expected: str, action: str,
    ) -> None:
        if renewal.state != expected:
            raise ConflictError(
                f"renewal {renewal.id} cannot {action} from "
                f"state {renewal.state!r} "
                f"(must be in {expected!r} first)",
            )

    # ---- DETECT_EXPIRY --------------------------------------------------

    def detect_upcoming(
        self,
        principal: Principal,
        *,
        org_id: int,
        scan_window_days: int,
        as_of: Optional[date] = None,
        lease_id: Optional[int] = None,
    ) -> "RenewalScanResult":
        """DETECT_EXPIRY. Pure-read scan.

        Walks ``v1_leases`` in this org, finds ACTIVE leases whose
        ``end_date`` falls inside ``(today, today + scan_window_days]``,
        and either:

        - returns the existing idempotent renewal row (when one already
          exists for ``(org_id, source_lease_id, scan_window_days)`` —
          replay), or
        - creates a new ``LeaseRenewal`` in ``DETECT_EXPIRY`` state and
          writes a ``DETECTED`` activity.

        Defaults: ``proposed_start_date = lease.end_date + 1``,
        ``proposed_end_date = lease.end_date + 365``,
        ``proposed_monthly_rent = lease.monthly_rent``,
        ``proposed_deposit = lease.deposit``.

        No money mutation; proposed terms are placeholders that the
        OWNER_DECISION step may later override via re-execute (or be
        used as-is when the existing terms are kept).
        """
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER, Role.SECRETARY)
        if scan_window_days <= 0:
            raise ValidationError(
                f"scan_window_days must be > 0, got {scan_window_days}",
            )

        cutoff_today = as_of or date.today()
        window_end = cutoff_today + timedelta(days=scan_window_days)

        # Source lease candidates: ACTIVE, end_date within the scan
        # window, optionally narrowed by lease_id.
        candidates_query = (
            self.db.query(Lease)
            .filter(
                Lease.org_id == org_id,
                Lease.state == LeaseState.ACTIVE.value,
                Lease.end_date > cutoff_today,
                Lease.end_date <= window_end,
            )
        )
        if lease_id is not None:
            candidates_query = candidates_query.filter(Lease.id == lease_id)
        candidates = candidates_query.order_by(Lease.end_date.asc()).all()

        detected: list[LeaseRenewal] = []
        replayed: list[LeaseRenewal] = []
        for source_lease in candidates:
            # Idempotency check via the UNIQUE index
            # (org_id, source_lease_id, scan_window_days).
            existing = (
                self.db.query(LeaseRenewal)
                .filter(
                    LeaseRenewal.org_id == org_id,
                    LeaseRenewal.source_lease_id == source_lease.id,
                    LeaseRenewal.scan_window_days == scan_window_days,
                )
                .one_or_none()
            )
            if existing is not None:
                _log_activity(
                    self.db,
                    org_id=org_id,
                    renewal_id=existing.id,
                    kind=RenewalActivityKind.SCAN_REPLAYED,
                    actor_user_id=principal.user_id,
                    detail=f"window_days={scan_window_days}",
                )
                replayed.append(existing)
                continue
            scan_key = (
                f"scan:{org_id}:{source_lease.id}:{scan_window_days}"
            )
            renewal = LeaseRenewal(
                org_id=org_id,
                source_lease_id=source_lease.id,
                new_lease_id=None,
                state=RenewalState.DETECT_EXPIRY.value,
                proposed_start_date=source_lease.end_date
                    + timedelta(days=1),
                proposed_end_date=source_lease.end_date
                    + timedelta(days=365),
                proposed_monthly_rent=source_lease.monthly_rent,
                proposed_deposit=source_lease.deposit,
                proposed_by_user_id=principal.user_id,
                idempotency_key=None,
                payload_hash="",
                scan_window_days=scan_window_days,
                scan_key=scan_key,
            )
            self.db.add(renewal)
            self.db.flush()
            # Linked Operation is the business truth for this
            # renewal; it stays OPEN through VERIFY, resolves at
            # CLOSED.
            op = Operation(
                org_id=org_id,
                kind=OPERATION_KIND_LEASE_RENEWAL,
                subject_type=OPERATION_SUBJECT_LEASE_RENEWAL,
                subject_id=renewal.id,
                state=OperationState.OPEN.value,
            )
            self.db.add(op)
            self.db.flush()
            _bump_to_in_progress(op)
            _log_activity(
                self.db,
                org_id=org_id,
                renewal_id=renewal.id,
                kind=RenewalActivityKind.DETECTED,
                actor_user_id=principal.user_id,
                detail=(
                    f"window_days={scan_window_days} "
                    f"ends_at={source_lease.end_date.isoformat()}"
                ),
            )
            detected.append(renewal)
        self.db.commit()
        # Refresh so callers see the activity / state flush.
        for r in detected + replayed:
            self.db.refresh(r)
        return RenewalScanResult(
            scan_window_days=scan_window_days,
            detected=detected,
            replayed=replayed,
        )

    # ---- CONTACT_TENANT ------------------------------------------------

    def contact_tenant(
        self,
        principal: Principal,
        *,
        org_id: int,
        renewal_id: int,
        contact_method: str,
        note: Optional[str] = None,
    ) -> LeaseRenewal:
        """DETECT_EXPIRY → CONTACT_TENANT.

        Records that the owner/secretary reached out to the tenant.
        Idempotent: repeated calls from CONTACT_TENANT only update the
        contact timestamp.
        """
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER, Role.SECRETARY)
        if not contact_method or not contact_method.strip():
            raise ValidationError("contact_method is required")
        renewal = self.get_renewal(
            principal, org_id=org_id, renewal_id=renewal_id,
        )
        # Forward only: DETECT_EXPIRY → CONTACT_TENANT. Idempotent if
        # already in CONTACT_TENANT (refresh timestamp + log activity).
        if renewal.state == RenewalState.CONTACT_TENANT.value:
            pass
        else:
            self._assert_state(
                renewal,
                expected=RenewalState.DETECT_EXPIRY.value,
                action="contact tenant",
            )
            renewal.state = RenewalState.CONTACT_TENANT.value
        renewal.contact_method = contact_method.strip()
        renewal.contacted_at = utcnow()
        _log_activity(
            self.db,
            org_id=org_id,
            renewal_id=renewal.id,
            kind=RenewalActivityKind.TENANT_CONTACTED,
            actor_user_id=principal.user_id,
            detail=(
                note if note else f"method={contact_method.strip()}"
            ),
        )
        self.db.commit()
        self.db.refresh(renewal)
        return renewal

    # ---- TENANT_RESPONSE -----------------------------------------------

    def record_response(
        self,
        principal: Principal,
        *,
        org_id: int,
        renewal_id: int,
        tenant_response: str,
        note: Optional[str] = None,
    ) -> LeaseRenewal:
        """CONTACT_TENANT → TENANT_RESPONSE.

        ``tenant_response`` must be one of ``RenewalTenantResponse``
        (RENEW / TERMINATE / DEFER). TERMINATE early-exits the
        pipeline to CLOSED (Operation resolves). RENEW proceeds.
        DEFER holds at TENANT_RESPONSE — owner must explicitly call
        ``decide_owner`` to advance.
        """
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER, Role.SECRETARY)
        if tenant_response not in [r.value for r in RenewalTenantResponse]:
            raise ValidationError(
                f"tenant_response must be one of "
                f"{[r.value for r in RenewalTenantResponse]}; "
                f"got {tenant_response!r}",
            )
        renewal = self.get_renewal(
            principal, org_id=org_id, renewal_id=renewal_id,
        )
        self._assert_state(
            renewal,
            expected=RenewalState.CONTACT_TENANT.value,
            action="record tenant response",
        )
        renewal.tenant_response = tenant_response
        renewal.tenant_response_at = utcnow()
        renewal.state = RenewalState.TENANT_RESPONSE.value
        _log_activity(
            self.db,
            org_id=org_id,
            renewal_id=renewal.id,
            kind=RenewalActivityKind.TENANT_RESPONDED,
            actor_user_id=principal.user_id,
            detail=(
                note if note else f"response={tenant_response}"
            ),
        )
        self.db.commit()
        self.db.refresh(renewal)
        return renewal

    # ---- OWNER_DECISION ------------------------------------------------

    def decide_owner(
        self,
        principal: Principal,
        *,
        org_id: int,
        renewal_id: int,
        owner_decision: str,
        proposed_start_date: Optional[date] = None,
        proposed_end_date: Optional[date] = None,
        proposed_monthly_rent: Optional[Any] = None,
        proposed_deposit: Optional[Any] = None,
        note: Optional[str] = None,
    ) -> LeaseRenewal:
        """TENANT_RESPONSE → OWNER_DECISION.

        ``owner_decision`` must be one of ``RenewalOwnerDecision``
        (RENEW / TERMINATE / DEFER).

        RENEW proceeds to OWNER_DECISION (state advance to next stage
        happens only at the closing of this method via the
        ``record_response`` step in the SPEC, but the canonical flow
        goes: tenant says RENEW → owner decides RENEW → state moves
        to OWNER_DECISION → ready for EXECUTE). Optional proposed
        terms override the scan defaults when the owner finalizes
        the new lease terms.

        TERMINATE early-exits to CLOSED (Operation resolves).
        DEFER keeps state at OWNER_DECISION so the owner may
        re-decide later.
        """
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER)
        if owner_decision not in [
            d.value for d in RenewalOwnerDecision
        ]:
            raise ValidationError(
                f"owner_decision must be one of "
                f"{[d.value for d in RenewalOwnerDecision]}; "
                f"got {owner_decision!r}",
            )
        renewal = self.get_renewal(
            principal, org_id=org_id, renewal_id=renewal_id,
        )
        self._assert_state(
            renewal,
            expected=RenewalState.TENANT_RESPONSE.value,
            action="record owner decision",
        )
        if proposed_start_date is not None or proposed_end_date is not None:
            new_start = proposed_start_date or renewal.proposed_start_date
            new_end = proposed_end_date or renewal.proposed_end_date
            if new_end <= new_start:
                raise ValidationError(
                    "proposed_end_date must be strictly after "
                    "proposed_start_date",
                )
            renewal.proposed_start_date = new_start
            renewal.proposed_end_date = new_end
        if proposed_monthly_rent is not None:
            renewal.proposed_monthly_rent = parse_money(
                proposed_monthly_rent,
            )
        if proposed_deposit is not None:
            renewal.proposed_deposit = parse_money(proposed_deposit)
        renewal.owner_decision = owner_decision
        renewal.owner_decision_at = utcnow()
        renewal.decided_by_user_id = principal.user_id
        renewal.decided_at = utcnow()
        renewal.state = RenewalState.OWNER_DECISION.value
        _log_activity(
            self.db,
            org_id=org_id,
            renewal_id=renewal.id,
            kind=RenewalActivityKind.OWNER_DECIDED,
            actor_user_id=principal.user_id,
            detail=(
                note if note else f"decision={owner_decision}"
            ),
        )
        self.db.commit()
        self.db.refresh(renewal)
        return renewal

    # ---- EXECUTE (closure gate, 7-stage) -------------------------------

    # NOTE: the closure-gate logic lives in ``execute()`` above; the
    # 7-stage pipeline reaches EXECUTE by calling the same ``execute``
    # service entry which now reads the current state and dispatches to
    # EXECUTED (legacy) or EXECUTE (7-stage).

    # ---- VERIFY --------------------------------------------------------

    def verify_execution(
        self,
        principal: Principal,
        *,
        org_id: int,
        renewal_id: int,
        note: Optional[str] = None,
    ) -> LeaseRenewal:
        """EXECUTE → VERIFY. OWNER-only post-execution confirmation.

        Idempotent: re-calling from VERIFY only refreshes the verified
        timestamp + activity.
        """
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER)
        renewal = self.get_renewal(
            principal, org_id=org_id, renewal_id=renewal_id,
        )
        if renewal.state == RenewalState.VERIFY.value:
            pass
        else:
            self._assert_state(
                renewal,
                expected=RenewalState.EXECUTE.value,
                action="verify execution",
            )
            renewal.state = RenewalState.VERIFY.value
        renewal.verified_at = utcnow()
        renewal.verified_by_user_id = principal.user_id
        op = self.get_operation(
            principal, org_id=org_id, renewal_id=renewal_id,
        )
        _bump_to_in_progress(op)
        _log_activity(
            self.db,
            org_id=org_id,
            renewal_id=renewal.id,
            kind=RenewalActivityKind.EXECUTION_VERIFIED,
            actor_user_id=principal.user_id,
            detail=note,
        )
        self.db.commit()
        self.db.refresh(renewal)
        return renewal

    # ---- CLOSED (terminal, Operation resolves) -------------------------

    def close(
        self,
        principal: Principal,
        *,
        org_id: int,
        renewal_id: int,
        note: Optional[str] = None,
    ) -> LeaseRenewal:
        """VERIFY → CLOSED. Terminal. Resolves the linked Operation.

        The 7-stage lifecycle is strictly forward-only — CLOSED can
        only be reached by walking the full pipeline
        (DETECT_EXPIRY → CONTACT_TENANT → TENANT_RESPONSE →
        OWNER_DECISION → EXECUTE → VERIFY → CLOSED).

        For early TERMINATE exits from TENANT_RESPONSE /
        OWNER_DECISION, callers must use the legacy ``cancel``
        endpoint (which sets ``CANCELLED`` and resolves the
        Operation); the ``close`` endpoint is the canonical
        verification-failure / completion path.

        Idempotent: re-calling from CLOSED is a no-op.
        """
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER)
        renewal = self.get_renewal(
            principal, org_id=org_id, renewal_id=renewal_id,
        )
        if renewal.state == RenewalState.CLOSED.value:
            return renewal
        if renewal.state != RenewalState.VERIFY.value:
            raise ConflictError(
                f"renewal {renewal.id} cannot be closed from "
                f"state {renewal.state!r} "
                f"(must be in {RenewalState.VERIFY.value!r})",
            )
        renewal.state = RenewalState.CLOSED.value
        renewal.closed_at = utcnow()
        renewal.closed_by_user_id = principal.user_id
        op = self.get_operation(
            principal, org_id=org_id, renewal_id=renewal_id,
        )
        _close_operation(
            self.db, operation=op, actor_user_id=principal.user_id,
        )
        _log_activity(
            self.db,
            org_id=org_id,
            renewal_id=renewal.id,
            kind=RenewalActivityKind.CLOSED,
            actor_user_id=principal.user_id,
            detail=note,
        )
        self.db.commit()
        self.db.refresh(renewal)
        return renewal

    # ---- follow-up (Task projection) ----

    def create_follow_up(
        self,
        principal: Principal,
        *,
        org_id: int,
        renewal_id: int,
        title: str,
        due_at: Optional[datetime] = None,
    ) -> Task:
        """Create a Task projection. Rejected if another open Task exists."""
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER, Role.SECRETARY)
        self.get_renewal(principal, org_id=org_id, renewal_id=renewal_id)
        op = _get_or_create_operation(
            self.db, org_id=org_id, renewal=self.get_renewal(
                principal, org_id=org_id, renewal_id=renewal_id,
            ),
        )
        existing_open = (
            self.db.query(Task)
            .filter(
                Task.operation_id == op.id,
                Task.state == "open",
            )
            .first()
        )
        if existing_open is not None:
            raise ConflictError(
                f"renewal {renewal_id} already has an open follow-up "
                f"(task {existing_open.id}); complete or cancel it first",
            )
        task = Task(
            org_id=org_id,
            operation_id=op.id,
            kind=TASK_KIND_RENEWAL_FOLLOW_UP,
            title=title,
            state="open",
            due_at=due_at,
        )
        self.db.add(task)
        self.db.flush()
        _bump_to_in_progress(op)
        _log_activity(
            self.db,
            org_id=org_id,
            renewal_id=renewal_id,
            kind=RenewalActivityKind.FOLLOW_UP_CREATED,
            actor_user_id=principal.user_id,
            detail=title,
        )
        self.db.commit()
        return task

    def list_follow_ups(
        self,
        principal: Principal,
        *,
        org_id: int,
        renewal_id: int,
    ) -> list[Task]:
        require_org_scope(principal, org_id)
        self.get_renewal(principal, org_id=org_id, renewal_id=renewal_id)
        op = (
            self.db.query(Operation)
            .filter(
                Operation.org_id == org_id,
                Operation.subject_type == OPERATION_SUBJECT_LEASE_RENEWAL,
                Operation.subject_id == renewal_id,
            )
            .first()
        )
        if op is None:
            return []
        return (
            self.db.query(Task)
            .filter(Task.operation_id == op.id)
            .order_by(Task.id.asc())
            .all()
        )

    def complete_follow_up(
        self,
        principal: Principal,
        *,
        org_id: int,
        task_id: int,
    ) -> Task:
        """Mark a Task as DONE. NEVER resolves the Operation."""
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER, Role.SECRETARY)
        task = self.db.get(Task, task_id)
        if task is None or task.org_id != org_id:
            raise NotFoundError(
                f"task {task_id} not found in org {org_id}",
            )
        if task.state != "open":
            raise ConflictError(
                f"task {task_id} is not open (state={task.state})",
            )
        task.state = "done"
        task.done_at = utcnow()
        _log_activity(
            self.db,
            org_id=org_id,
            renewal_id=task.operation_id,
            kind=RenewalActivityKind.FOLLOW_UP_DONE,
            actor_user_id=principal.user_id,
            detail=task.title,
        )
        self.db.commit()
        return task


__all__ = [
    "LeaseRenewalService",
    "RenewalExecuteResult",
    "RenewalProposeResult",
]
