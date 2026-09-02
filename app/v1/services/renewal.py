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

Frozen Issue #112 §"Lease Renewal" 7-stage pipeline
===================================================

The class exposes both the legacy 5-state proposal pipeline (PROPOSED
→ APPROVED → EXECUTED) and the frozen Issue #112 pipeline
DETECT_EXPIRY → CONTACT_TENANT → TENANT_RESPONSE → OWNER_DECISION →
EXECUTED → VERIFY → CLOSED. The two pipelines share the same EXECUTED
closure gate (the only place that mutates the source lease and creates
the new lease).

Method → state transition map (frozen pipeline):

- ``detect_upcoming(window_days)`` → system emits a fresh renewal at
  ``DETECT_EXPIRY`` for each ACTIVE lease whose ``end_date`` falls in
  the next ``window_days`` days. Idempotent on
  ``(org_id, source_lease_id, scan_window_days)``: a replay returns the
  existing row with ``replayed=True``.
- ``contact_tenant(renewal_id, channel=None, note=None)`` → DETECT_EXPIRY
  → CONTACT_TENANT. Logs ``TENANT_CONTACTED`` activity with the channel
  (e.g. "telegram") and an optional human note.
- ``record_response(renewal_id, response=...)`` → CONTACT_TENANT →
  TENANT_RESPONSE. ``response`` ∈ {RENEW, TERMINATE, DEFER}.
- ``decide_owner(renewal_id, decision=..., note=None)`` → TENANT_RESPONSE
  → OWNER_DECISION. ``decision`` ∈ {RENEW, TERMINATE, DEFER}.
  - decision=RENEW → owner has decided to renew (must then be EXECUTED).
  - decision=TERMINATE → transitions to REJECTED (terminal, source lease
    untouched).
  - decision=DEFER → stays in OWNER_DECISION; no business effect.
- ``verify_execution(renewal_id, note=None)`` → EXECUTED → VERIFY. Owner
  confirms the executed change matches the decision. Idempotent.
- ``close(renewal_id, note=None)`` → VERIFY → CLOSED. Terminal
  administrative closure; resolves the linked Operation.

The legacy 5-state pipeline (``propose``/``approve``/``reject``/``execute``/
``cancel``) is unchanged. The two pipelines converge on the EXECUTED
state and diverge again at VERIFY/CLOSED.
"""
from __future__ import annotations

from dataclasses import dataclass, field
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
    RENEWAL_OWNER_DECISIONS,
    RENEWAL_TENANT_RESPONSES,
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
class RenewalScanResult:
    """Result of ``detect_upcoming`` (system scan).

    ``replayed=True`` means an identical
    ``(org_id, source_lease_id, scan_window_days)`` triple was already
    stored; the existing renewal is returned.

    ``detected`` lists every renewal that was created or replayed during
    this scan call, so the caller (Telegram / scheduler / API) can echo
    the actual work it accomplished back to the user.
    """

    replayed: bool
    renewals: list["RenewalDetection"] = field(default_factory=list)


@dataclass(frozen=True)
class RenewalDetection:
    """One row emitted (or replayed) by ``detect_upcoming``."""

    renewal: LeaseRenewal
    is_new: bool


# ---------- frozen 7-stage pipeline state machine helpers ---------------


# State groups used to guard transitions cleanly without duplicating
# long if/else chains across the service methods.
_NEW_PIPELINE_STATES: frozenset[str] = frozenset({
    RenewalState.DETECT_EXPIRY.value,
    RenewalState.CONTACT_TENANT.value,
    RenewalState.TENANT_RESPONSE.value,
    RenewalState.OWNER_DECISION.value,
    RenewalState.EXECUTED.value,
    RenewalState.VERIFY.value,
    RenewalState.CLOSED.value,
})


_LEGACY_PROPOSAL_STATES: frozenset[str] = frozenset({
    RenewalState.PROPOSED.value,
    RenewalState.APPROVED.value,
})


_TERMINAL_STATES: frozenset[str] = frozenset({
    RenewalState.EXECUTED.value,
    RenewalState.REJECTED.value,
    RenewalState.CANCELLED.value,
    RenewalState.CLOSED.value,
})


def _is_new_pipeline_state(state: str) -> bool:
    return state in _NEW_PIPELINE_STATES


def _is_legacy_proposal_state(state: str) -> bool:
    return state in _LEGACY_PROPOSAL_STATES


def _is_terminal(state: str) -> bool:
    return state in _TERMINAL_STATES


def _validate_tenant_response(response: str) -> str:
    """Coerce + validate a tenant-response vocabulary value."""
    try:
        return RenewalTenantResponse(response).value
    except ValueError as exc:
        raise ValidationError(
            f"tenant_response must be one of {RENEWAL_TENANT_RESPONSES}; "
            f"got {response!r}",
        ) from exc


def _validate_owner_decision(decision: str) -> str:
    """Coerce + validate an owner-decision vocabulary value."""
    try:
        return RenewalOwnerDecision(decision).value
    except ValueError as exc:
        raise ValidationError(
            f"owner_decision must be one of {RENEWAL_OWNER_DECISIONS}; "
            f"got {decision!r}",
        ) from exc


# Reopen the original dataclass block after the new helpers above.
@dataclass(frozen=True)
class RenewalExecuteResult:
    """Result of executing an APPROVED renewal."""

    renewal: LeaseRenewal
    new_lease: Lease


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
        """APPROVED → EXECUTED (or OWNER_DECISION → EXECUTED for the
        frozen 7-stage pipeline).

        The single closure gate. Accepts renewals created via either
        the legacy 5-state proposal pipeline (PROPOSED → APPROVED) or
        the frozen 7-stage pipeline (DETECT_EXPIRY → CONTACT_TENANT →
        TENANT_RESPONSE → OWNER_DECISION).

        Action:
          1. Verify no overlapping ACTIVE lease on the same unit at the
             proposed start date.
          2. Terminate the source lease (ACTIVE → TERMINATED, unit
             becomes AVAILABLE).
          3. Create a new Lease in DRAFT with the proposed terms.
          4. Activate the new lease (DRAFT → ACTIVE, unit becomes
             OCCUPIED).
          5. Attach ``renewal.new_lease_id`` + ``renewal.executed_at``.
          6. Resolve the Operation (legacy path) OR leave it OPEN
             (new pipeline; CLOSED will resolve it).
        """
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER, Role.SECRETARY)
        renewal = self.get_renewal(
            principal, org_id=org_id, renewal_id=renewal_id,
        )
        if renewal.state not in (
            RenewalState.APPROVED.value,
            RenewalState.OWNER_DECISION.value,
        ):
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
        # Capture the prior state so we know which pipeline we are in.
        prior_state = renewal.state
        renewal.state = RenewalState.EXECUTED.value
        _log_activity(
            self.db,
            org_id=org_id,
            renewal_id=renewal.id,
            kind=RenewalActivityKind.EXECUTED,
            actor_user_id=principal.user_id,
        )
        # 5) Resolve Operation only on the legacy path (APPROVED → EXECUTED).
        # The frozen 7-stage pipeline (OWNER_DECISION → EXECUTED) keeps
        # the Operation OPEN until CLOSED; ``close`` will resolve it.
        op = self.get_operation(
            principal, org_id=org_id, renewal_id=renewal_id,
        )
        if prior_state == RenewalState.APPROVED.value:
            _close_operation(
                self.db, operation=op, actor_user_id=principal.user_id,
            )
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

    # =========================================================================
    # Frozen Issue #112 §"Lease Renewal" 7-stage pipeline
    # DETECT_EXPIRY → CONTACT_TENANT → TENANT_RESPONSE → OWNER_DECISION
    #     → EXECUTED → VERIFY → CLOSED
    # =========================================================================

    def detect_upcoming(
        self,
        principal: Principal,
        *,
        org_id: int,
        window_days: int,
        as_of: Optional[date] = None,
    ) -> RenewalScanResult:
        """System scan: emit one ``DETECT_EXPIRY`` renewal per ACTIVE lease
        whose ``end_date`` falls in the next ``window_days`` days.

        Idempotent on ``(org_id, source_lease_id, scan_window_days)``:
        a replay returns the existing row (flagged ``is_new=False``).

        ``as_of`` defaults to today (UTC); provided so tests / cron
        jobs can drive the scan with a deterministic clock without
        monkey-patching ``utcnow``.
        """
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER, Role.SECRETARY)
        if window_days <= 0:
            raise ValidationError(
                "window_days must be a positive integer",
            )
        today = as_of or utcnow().date()
        cutoff = today + timedelta(days=window_days)
        # 1) Find ACTIVE leases inside the window.
        leases_in_window: list[Lease] = (
            self.db.query(Lease)
            .filter(
                Lease.org_id == org_id,
                Lease.state == LeaseState.ACTIVE.value,
                Lease.end_date >= today,
                Lease.end_date <= cutoff,
            )
            .order_by(Lease.id.asc())
            .all()
        )
        detections: list[RenewalDetection] = []
        any_replay = False
        for source_lease in leases_in_window:
            existing = (
                self.db.query(LeaseRenewal)
                .filter(
                    LeaseRenewal.org_id == org_id,
                    LeaseRenewal.source_lease_id == source_lease.id,
                    LeaseRenewal.scan_window_days == window_days,
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
                    detail=f"window={window_days}",
                )
                detections.append(
                    RenewalDetection(renewal=existing, is_new=False),
                )
                any_replay = True
                continue
            # Default proposed terms: keep the same rent + deposit, push
            # the renewal 1 year past the current end_date. The OWNER
            # later adjusts the proposed terms during OWNER_DECISION
            # before calling ``execute``.
            proposed_start = source_lease.end_date
            proposed_end = proposed_start + timedelta(days=365)
            renewal = LeaseRenewal(
                org_id=org_id,
                source_lease_id=source_lease.id,
                new_lease_id=None,
                state=RenewalState.DETECT_EXPIRY.value,
                proposed_start_date=proposed_start,
                proposed_end_date=proposed_end,
                proposed_monthly_rent=Decimal(source_lease.monthly_rent),
                proposed_deposit=Decimal(source_lease.deposit),
                proposed_by_user_id=principal.user_id,
                idempotency_key=_scan_idempotency_key(
                    source_lease.id, window_days,
                ),
                payload_hash=_scan_payload_hash(
                    source_lease.id, window_days, proposed_start, proposed_end,
                ),
                scan_window_days=window_days,
                scan_key=f"{source_lease.id}:{window_days}",
            )
            self.db.add(renewal)
            self.db.flush()
            # The Operation is created up-front (mirror of ``propose``):
            # DETECT_EXPIRY is the first business state, so the Operation
            # is born here and remains OPEN through EXECUTED → VERIFY,
            # resolving only at CLOSED (or REJECTED / CANCELLED).
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
                kind=RenewalActivityKind.DETECTED,
                actor_user_id=principal.user_id,
                detail=(
                    f"lease={source_lease.id} window={window_days}d "
                    f"ends={source_lease.end_date.isoformat()}"
                ),
            )
            detections.append(
                RenewalDetection(renewal=renewal, is_new=True),
            )
        self.db.commit()
        return RenewalScanResult(replayed=any_replay, renewals=detections)

    def contact_tenant(
        self,
        principal: Principal,
        *,
        org_id: int,
        renewal_id: int,
        channel: Optional[str] = None,
        note: Optional[str] = None,
    ) -> LeaseRenewal:
        """``DETECT_EXPIRY → CONTACT_TENANT``."""
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER, Role.SECRETARY)
        renewal = self.get_renewal(
            principal, org_id=org_id, renewal_id=renewal_id,
        )
        if renewal.state != RenewalState.DETECT_EXPIRY.value:
            raise ConflictError(
                f"renewal {renewal_id} cannot transition to "
                f"CONTACT_TENANT from state {renewal.state!r}",
            )
        renewal.state = RenewalState.CONTACT_TENANT.value
        op = self.get_operation(
            principal, org_id=org_id, renewal_id=renewal_id,
        )
        _bump_to_in_progress(op)
        detail_parts = []
        if channel:
            detail_parts.append(f"channel={channel}")
        if note:
            detail_parts.append(note)
        _log_activity(
            self.db,
            org_id=org_id,
            renewal_id=renewal.id,
            kind=RenewalActivityKind.TENANT_CONTACTED,
            actor_user_id=principal.user_id,
            detail="; ".join(detail_parts) or None,
        )
        self.db.commit()
        return renewal

    def record_response(
        self,
        principal: Principal,
        *,
        org_id: int,
        renewal_id: int,
        response: str,
        note: Optional[str] = None,
    ) -> LeaseRenewal:
        """``CONTACT_TENANT → TENANT_RESPONSE``. ``response`` is one of
        ``{RENEW, TERMINATE, DEFER}``.
        """
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER, Role.SECRETARY)
        response_value = _validate_tenant_response(response)
        renewal = self.get_renewal(
            principal, org_id=org_id, renewal_id=renewal_id,
        )
        if renewal.state != RenewalState.CONTACT_TENANT.value:
            raise ConflictError(
                f"renewal {renewal_id} cannot transition to "
                f"TENANT_RESPONSE from state {renewal.state!r}",
            )
        renewal.state = RenewalState.TENANT_RESPONSE.value
        renewal.tenant_response = response_value
        renewal.tenant_response_at = utcnow()
        _log_activity(
            self.db,
            org_id=org_id,
            renewal_id=renewal.id,
            kind=RenewalActivityKind.TENANT_RESPONDED,
            actor_user_id=principal.user_id,
            detail=(
                f"response={response_value}"
                + (f"; {note}" if note else "")
            ),
        )
        self.db.commit()
        return renewal

    def decide_owner(
        self,
        principal: Principal,
        *,
        org_id: int,
        renewal_id: int,
        decision: str,
        note: Optional[str] = None,
    ) -> LeaseRenewal:
        """``TENANT_RESPONSE → OWNER_DECISION`` (or ``REJECTED`` if
        ``decision=TERMINATE``).
        """
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER)
        decision_value = _validate_owner_decision(decision)
        renewal = self.get_renewal(
            principal, org_id=org_id, renewal_id=renewal_id,
        )
        if renewal.state != RenewalState.TENANT_RESPONSE.value:
            raise ConflictError(
                f"renewal {renewal_id} cannot transition to "
                f"OWNER_DECISION from state {renewal.state!r}",
            )
        renewal.owner_decision = decision_value
        renewal.owner_decision_at = utcnow()
        renewal.decided_by_user_id = principal.user_id
        renewal.decided_at = utcnow()
        if decision_value == RenewalOwnerDecision.TERMINATE.value:
            # Terminal branch: equivalent to the legacy REJECTED outcome.
            renewal.state = RenewalState.REJECTED.value
            renewal.decision_reason = note or "owner decided to terminate"
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
                detail=(
                    f"decision={decision_value}"
                    + (f"; {note}" if note else "")
                ),
            )
        else:
            # RENEW or DEFER: enter OWNER_DECISION. For RENEW the
            # caller must then invoke ``execute`` to advance to
            # EXECUTED. For DEFER the caller may re-enter
            # ``decide_owner`` later with a different value.
            renewal.state = RenewalState.OWNER_DECISION.value
            op = self.get_operation(
                principal, org_id=org_id, renewal_id=renewal_id,
            )
            _bump_to_in_progress(op)
            _log_activity(
                self.db,
                org_id=org_id,
                renewal_id=renewal.id,
                kind=RenewalActivityKind.OWNER_DECIDED,
                actor_user_id=principal.user_id,
                detail=(
                    f"decision={decision_value}"
                    + (f"; {note}" if note else "")
                ),
            )
        self.db.commit()
        return renewal

    def verify_execution(
        self,
        principal: Principal,
        *,
        org_id: int,
        renewal_id: int,
        note: Optional[str] = None,
    ) -> LeaseRenewal:
        """``EXECUTED → VERIFY``. Owner confirms the executed change
        matches the recorded decision. Idempotent.

        The legacy ``execute`` closure gate continues to resolve the
        Operation. ``verify_execution`` is a post-execution
        reconciliation step that advances the renewal state but does
        NOT mutate the Operation.
        """
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER)
        renewal = self.get_renewal(
            principal, org_id=org_id, renewal_id=renewal_id,
        )
        if renewal.state == RenewalState.VERIFY.value:
            # Idempotent: re-confirming a verification is a no-op.
            return renewal
        if renewal.state != RenewalState.EXECUTED.value:
            raise ConflictError(
                f"renewal {renewal_id} cannot transition to "
                f"VERIFY from state {renewal.state!r} "
                f"(must be EXECUTED)",
            )
        renewal.state = RenewalState.VERIFY.value
        renewal.verified_at = utcnow()
        renewal.verified_by_user_id = principal.user_id
        _log_activity(
            self.db,
            org_id=org_id,
            renewal_id=renewal.id,
            kind=RenewalActivityKind.EXECUTION_VERIFIED,
            actor_user_id=principal.user_id,
            detail=note,
        )
        self.db.commit()
        return renewal

    def close(
        self,
        principal: Principal,
        *,
        org_id: int,
        renewal_id: int,
        note: Optional[str] = None,
    ) -> LeaseRenewal:
        """``VERIFY → CLOSED``. Terminal administrative closure.

        Resolves the linked Operation when the renewal was created via
        the new pipeline. Legacy renewals (``PROPOSED → EXECUTED``) keep
        their pre-existing semantics: the Operation is already
        resolved at ``EXECUTED``, so this is a no-op for them.
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
                f"renewal {renewal_id} cannot transition to "
                f"CLOSED from state {renewal.state!r} "
                f"(must be VERIFY)",
            )
        renewal.state = RenewalState.CLOSED.value
        renewal.closed_at = utcnow()
        renewal.closed_by_user_id = principal.user_id
        # The Operation only resolves here for the new pipeline
        # (created at DETECT_EXPIRY). Legacy renewals already
        # resolved at EXECUTED — the close below is a defensive no-op.
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
        return renewal


# ---------- scan-key helpers -------------------------------------------


def _scan_idempotency_key(source_lease_id: int, window_days: int) -> str:
    """Deterministic ``idempotency_key`` for ``detect_upcoming``.

    The ``propose`` and ``detect_upcoming`` entry points must not
    collide: ``propose`` accepts an opaque client-supplied key, while
    ``detect_upcoming`` synthesizes its own from
    ``(source_lease_id, window_days)``. Prefix ``scan:`` keeps the two
    namespaces disjoint.
    """
    return normalize_idempotency_key(
        f"scan:{source_lease_id}:{window_days}",
    )


def _scan_payload_hash(
    source_lease_id: int,
    window_days: int,
    proposed_start: date,
    proposed_end: date,
) -> str:
    """Deterministic ``payload_hash`` for scan-generated renewals."""
    payload = {
        "source_lease_id": source_lease_id,
        "scan_window_days": window_days,
        "proposed_start_date": proposed_start.isoformat(),
        "proposed_end_date": proposed_end.isoformat(),
    }
    return compute_payload_hash(payload)


__all__ = [
    "LeaseRenewalService",
    "RenewalDetection",
    "RenewalExecuteResult",
    "RenewalProposeResult",
    "RenewalScanResult",
]
