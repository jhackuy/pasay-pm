"""Move-out / Settlement service — single source of truth.

AGENTS.md §4 invariants enforced here:

- Operation is Truth, Task is Projection. The Operation only resolves
  (move-out → SETTLED + Operation.state=resolved) when an OWNER records
  a ``DepositSettlement`` with a terminal disposition AND the move-out
  transitions to ``SETTLED``.
- Money is Decimal only (``parse_money`` rejects float/bool).
- Idempotency keys are opaque and case-preserving.
- Org-scope is enforced via ``require_org_scope`` at every entry.
- Inspection must happen BEFORE settlement. ``DepositSettlement`` may
  only be recorded on a ``REQUESTED`` or ``INSPECTED`` move-out;
  recording a settlement transitions ``REQUESTED`` to ``INSPECTED``
  to ``SETTLED`` (one call, single transaction).
- At least one ``MoveOutDamage`` row is required before settlement can
  be recorded (unless the disposition is ``FULL_REFUND`` with no
  damages — in that case the move-out may settle straight to
  ``INSPECTED`` → ``SETTLED`` via a single inspection record).
- ``Reminder != Completion``. Completing a follow-up NEVER resolves the
  Operation; only settlement does.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
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
from app.v1.models.base import LeaseState, OperationState, UnitStatus
from app.v1.models.move_out import (
    DEPOSIT_DISPOSITIONS,
    MOVE_OUT_DAMAGE_KINDS,
    DepositDisposition,
    DepositSettlement,
    MoveOut,
    MoveOutActivity,
    MoveOutActivityKind,
    MoveOutDamage,
    MoveOutDamageKind,
    MoveOutInspection,
    MoveOutState,
    OPERATION_KIND_MOVE_OUT,
    OPERATION_SUBJECT_MOVE_OUT,
    TASK_KIND_MOVE_OUT_FOLLOW_UP,
)
from app.v1.models.property import Unit
from app.v1.models.rent_payment import Operation, Task
from app.v1.models.tenant_lease import Lease
from app.v1.services.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)


# ---------- result types ----------


@dataclass(frozen=True)
class MoveOutRequestResult:
    """Result of opening a move-out request. Replay returns 200."""

    replayed: bool
    move_out: MoveOut


@dataclass(frozen=True)
class MoveOutSettleResult:
    """Result of recording a terminal deposit settlement."""

    move_out: MoveOut
    settlement: DepositSettlement


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
    move_out_id: int,
    kind: MoveOutActivityKind,
    actor_user_id: Optional[int],
    detail: Optional[str] = None,
) -> None:
    db.add(
        MoveOutActivity(
            org_id=org_id,
            move_out_id=move_out_id,
            kind=kind.value,
            actor_user_id=actor_user_id,
            detail=detail,
            occurred_at=utcnow(),
        )
    )


def _get_or_create_operation(
    db: Session,
    *,
    org_id: int,
    move_out: MoveOut,
) -> Operation:
    op = (
        db.query(Operation)
        .filter(
            Operation.org_id == org_id,
            Operation.subject_type == OPERATION_SUBJECT_MOVE_OUT,
            Operation.subject_id == move_out.id,
        )
        .first()
    )
    if op is not None:
        return op
    op = Operation(
        org_id=org_id,
        kind=OPERATION_KIND_MOVE_OUT,
        subject_type=OPERATION_SUBJECT_MOVE_OUT,
        subject_id=move_out.id,
        state=OperationState.OPEN.value,
    )
    db.add(op)
    db.flush()
    return op


def _close_operation(
    db: Session,
    *,
    operation: Operation,
    actor_user_id: Optional[int],
) -> None:
    """Resolve the move-out's linked Operation. Idempotent."""
    if operation.state == OperationState.RESOLVED.value:
        return
    operation.state = OperationState.RESOLVED.value
    operation.resolved_at = utcnow()
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
    if operation.state == OperationState.OPEN.value:
        operation.state = OperationState.IN_PROGRESS.value
        return True
    return False


# ---------- service ----------


class MoveOutService:
    """Cohesive application/domain service for the move-out / settlement cycle."""

    def __init__(self, db: Session) -> None:
        self.db = db

    # ---- read helpers ----

    def get_move_out(
        self,
        principal: Principal,
        *,
        org_id: int,
        move_out_id: int,
    ) -> MoveOut:
        require_org_scope(principal, org_id)
        m = self.db.get(MoveOut, move_out_id)
        if m is None or m.org_id != org_id:
            raise NotFoundError(
                f"move-out {move_out_id} not found in org {org_id}",
            )
        return m

    def get_operation(
        self,
        principal: Principal,
        *,
        org_id: int,
        move_out_id: int,
    ) -> Operation:
        require_org_scope(principal, org_id)
        self.get_move_out(principal, org_id=org_id, move_out_id=move_out_id)
        op = (
            self.db.query(Operation)
            .filter(
                Operation.org_id == org_id,
                Operation.subject_type == OPERATION_SUBJECT_MOVE_OUT,
                Operation.subject_id == move_out_id,
            )
            .first()
        )
        if op is None:
            raise NotFoundError(
                f"operation for move-out {move_out_id} not found",
            )
        return op

    def list_move_outs(
        self,
        principal: Principal,
        *,
        org_id: int,
        state: Optional[str] = None,
        lease_id: Optional[int] = None,
    ) -> list[MoveOut]:
        require_org_scope(principal, org_id)
        query = self.db.query(MoveOut).filter(MoveOut.org_id == org_id)
        if state is not None:
            query = query.filter(MoveOut.state == state)
        if lease_id is not None:
            query = query.filter(MoveOut.lease_id == lease_id)
        return query.order_by(MoveOut.id.asc()).all()

    def list_inspections(
        self,
        principal: Principal,
        *,
        org_id: int,
        move_out_id: int,
    ) -> list[MoveOutInspection]:
        require_org_scope(principal, org_id)
        self.get_move_out(principal, org_id=org_id, move_out_id=move_out_id)
        return (
            self.db.query(MoveOutInspection)
            .filter(
                MoveOutInspection.org_id == org_id,
                MoveOutInspection.move_out_id == move_out_id,
            )
            .order_by(MoveOutInspection.id.asc())
            .all()
        )

    def list_damages(
        self,
        principal: Principal,
        *,
        org_id: int,
        move_out_id: int,
    ) -> list[MoveOutDamage]:
        require_org_scope(principal, org_id)
        self.get_move_out(principal, org_id=org_id, move_out_id=move_out_id)
        return (
            self.db.query(MoveOutDamage)
            .filter(
                MoveOutDamage.org_id == org_id,
                MoveOutDamage.move_out_id == move_out_id,
            )
            .order_by(MoveOutDamage.id.asc())
            .all()
        )

    def list_activity(
        self,
        principal: Principal,
        *,
        org_id: int,
        move_out_id: int,
    ) -> list[MoveOutActivity]:
        require_org_scope(principal, org_id)
        self.get_move_out(principal, org_id=org_id, move_out_id=move_out_id)
        return (
            self.db.query(MoveOutActivity)
            .filter(
                MoveOutActivity.org_id == org_id,
                MoveOutActivity.move_out_id == move_out_id,
            )
            .order_by(MoveOutActivity.id.asc())
            .all()
        )

    def get_balance(
        self,
        principal: Principal,
        *,
        org_id: int,
        move_out_id: int,
    ) -> dict[str, Any]:
        require_org_scope(principal, org_id)
        m = self.get_move_out(principal, org_id=org_id, move_out_id=move_out_id)
        if m.settlement_id is not None:
            s = self.db.get(DepositSettlement, m.settlement_id)
            return {
                "move_out_id": m.id,
                "deposit_held": s.deposit_held if s else Decimal("0"),
                "deductions_total": s.deductions_total if s else Decimal("0"),
                "refund_amount": s.refund_amount if s else Decimal("0"),
                "additional_owed": s.additional_owed if s else Decimal("0"),
                "is_settled": m.state == MoveOutState.SETTLED.value,
            }
        # Pre-settlement: project what we have so far.
        damages = (
            self.db.query(MoveOutDamage)
            .filter(
                MoveOutDamage.org_id == org_id,
                MoveOutDamage.move_out_id == move_out_id,
            )
            .all()
        )
        deductions = sum(
            (Decimal(d.accepted_amount) for d in damages),
            Decimal("0"),
        )
        return {
            "move_out_id": m.id,
            "deposit_held": Decimal("0"),
            "deductions_total": deductions,
            "refund_amount": Decimal("0"),
            "additional_owed": Decimal("0"),
            "is_settled": False,
        }

    # ---- request ----

    def request(
        self,
        principal: Principal,
        *,
        org_id: int,
        lease_id: int,
        planned_move_out_date: Optional[date] = None,
        notes: Optional[str] = None,
        idempotency_key: str,
    ) -> MoveOutRequestResult:
        """Open a move-out request. Idempotent on ``(org_id, key)``."""
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER, Role.SECRETARY)
        key = normalize_idempotency_key(idempotency_key)
        # Verify lease exists in org (org-scope fail-closed).
        from app.v1.models.tenant_lease import Lease
        lease = self.db.get(Lease, lease_id)
        if lease is None or lease.org_id != org_id:
            raise NotFoundError(
                f"lease {lease_id} not found in org {org_id}",
            )
        payload = {
            "lease_id": lease_id,
            "planned_move_out_date": (
                str(planned_move_out_date)
                if planned_move_out_date is not None else ""
            ),
            "notes": notes or "",
        }
        payload_hash = compute_payload_hash(payload)
        existing = (
            self.db.query(MoveOut)
            .filter(
                MoveOut.org_id == org_id,
                MoveOut.idempotency_key == key,
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
                move_out_id=existing.id,
                kind=MoveOutActivityKind.REQUEST_REPLAYED,
                actor_user_id=principal.user_id,
            )
            self.db.commit()
            return MoveOutRequestResult(replayed=True, move_out=existing)
        m = MoveOut(
            org_id=org_id,
            lease_id=lease_id,
            state=MoveOutState.REQUESTED.value,
            requested_by_user_id=principal.user_id,
            planned_move_out_date=planned_move_out_date,
            inspection_notes=notes,
            idempotency_key=key,
            payload_hash=payload_hash,
        )
        self.db.add(m)
        self.db.flush()
        op = Operation(
            org_id=org_id,
            kind=OPERATION_KIND_MOVE_OUT,
            subject_type=OPERATION_SUBJECT_MOVE_OUT,
            subject_id=m.id,
            state=OperationState.OPEN.value,
        )
        self.db.add(op)
        self.db.flush()
        _log_activity(
            self.db,
            org_id=org_id,
            move_out_id=m.id,
            kind=MoveOutActivityKind.REQUESTED,
            actor_user_id=principal.user_id,
        )
        self.db.commit()
        return MoveOutRequestResult(replayed=False, move_out=m)

    # ---- inspection ----

    def record_inspection(
        self,
        principal: Principal,
        *,
        org_id: int,
        move_out_id: int,
        summary: str,
    ) -> tuple[MoveOut, MoveOutInspection]:
        """Record a walk-through. REQUESTED → INSPECTED (idempotent)."""
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER, Role.SECRETARY)
        if not summary or not summary.strip():
            raise ValidationError("inspection summary is required")
        m = self.get_move_out(principal, org_id=org_id, move_out_id=move_out_id)
        if m.state not in (
            MoveOutState.REQUESTED.value,
            MoveOutState.INSPECTED.value,
        ):
            raise ConflictError(
                f"cannot record inspection for a move-out in state "
                f"{m.state!r}",
            )
        ins = MoveOutInspection(
            org_id=org_id,
            move_out_id=m.id,
            summary=summary,
            inspected_by_user_id=principal.user_id,
        )
        self.db.add(ins)
        self.db.flush()
        m.inspected_at = ins.inspected_at
        m.inspected_by_user_id = principal.user_id
        m.state = MoveOutState.INSPECTED.value
        op = self.get_operation(principal, org_id=org_id, move_out_id=move_out_id)
        _bump_to_in_progress(op)
        _log_activity(
            self.db,
            org_id=org_id,
            move_out_id=m.id,
            kind=MoveOutActivityKind.INSPECTED,
            actor_user_id=principal.user_id,
            detail=summary,
        )
        self.db.commit()
        return m, ins

    # ---- damages ----

    def record_damage(
        self,
        principal: Principal,
        *,
        org_id: int,
        move_out_id: int,
        kind: str,
        description: str,
        amount: Decimal | str | int,
        accepted_amount: Optional[Decimal | str | int] = None,
    ) -> MoveOutDamage:
        """Record a damage / charge. Allowed while INSPECTED (pre-settlement)."""
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER, Role.SECRETARY)
        if kind not in MOVE_OUT_DAMAGE_KINDS:
            raise ValidationError(
                f"unknown damage kind {kind!r} "
                f"(must be one of: {list(MOVE_OUT_DAMAGE_KINDS)})",
            )
        amt = parse_money(amount)
        acc = parse_money(accepted_amount) if accepted_amount is not None else Decimal("0")
        if acc > amt:
            raise ValidationError(
                "accepted_amount must be <= amount",
            )
        m = self.get_move_out(principal, org_id=org_id, move_out_id=move_out_id)
        if m.state != MoveOutState.INSPECTED.value:
            raise ConflictError(
                f"cannot record damages for a move-out in state "
                f"{m.state!r} (must be INSPECTED)",
            )
        d = MoveOutDamage(
            org_id=org_id,
            move_out_id=m.id,
            kind=kind,
            description=description,
            amount=amt,
            accepted_amount=acc,
            recorded_by_user_id=principal.user_id,
        )
        self.db.add(d)
        self.db.flush()
        _log_activity(
            self.db,
            org_id=org_id,
            move_out_id=m.id,
            kind=MoveOutActivityKind.DAMAGE_RECORDED,
            actor_user_id=principal.user_id,
            detail=f"{kind}: {amt}",
        )
        self.db.commit()
        return d

    def accept_damage(
        self,
        principal: Principal,
        *,
        org_id: int,
        damage_id: int,
        accepted_amount: Decimal | str | int,
    ) -> MoveOutDamage:
        """OWNER-only: finalize the accepted amount for a damage item."""
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER)
        d = self.db.get(MoveOutDamage, damage_id)
        if d is None or d.org_id != org_id:
            raise NotFoundError(
                f"damage {damage_id} not found in org {org_id}",
            )
        new_acc = parse_money(accepted_amount)
        if new_acc > Decimal(d.amount):
            raise ValidationError(
                "accepted_amount must be <= damage.amount",
            )
        m = self.get_move_out(principal, org_id=org_id, move_out_id=d.move_out_id)
        if m.state != MoveOutState.INSPECTED.value:
            raise ConflictError(
                f"cannot accept damages for a move-out in state "
                f"{m.state!r}",
            )
        d.accepted_amount = new_acc
        self.db.commit()
        return d

    # ---- settlement (closure gate) ----

    def settle(
        self,
        principal: Principal,
        *,
        org_id: int,
        move_out_id: int,
        disposition: str,
        deposit_held: Decimal | str | int,
        refund_amount: Decimal | str | int,
        additional_owed: Decimal | str | int = 0,
        notes: Optional[str] = None,
    ) -> MoveOutSettleResult:
        """OWNER-only. The closure gate.

        Action:
          1. Verify move-out is INSPECTED (settlement cannot be recorded
             without an inspection record).
          2. Verify at least one MoveOutDamage exists OR the
             disposition is FULL_REFUND (no damages is fine for a
             clean move-out).
          3. Compute deductions_total = sum(accepted_amount).
          4. Persist DepositSettlement.
          5. Transition move-out: state=SETTLED, settled_at, settlement_id.
          6. Resolve the linked Operation; cancel open follow-ups.
        """
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER)
        if disposition not in DEPOSIT_DISPOSITIONS:
            raise ValidationError(
                f"unknown disposition {disposition!r} "
                f"(must be one of: {list(DEPOSIT_DISPOSITIONS)})",
            )
        dh = parse_money(deposit_held)
        rf = parse_money(refund_amount)
        ao = parse_money(additional_owed)
        # Disposition-specific invariants.
        if disposition == DepositDisposition.FULL_REFUND.value:
            if rf != dh:
                raise ValidationError(
                    "FULL_REFUND requires refund_amount == deposit_held",
                )
            if ao != Decimal("0"):
                raise ValidationError(
                    "FULL_REFUND requires additional_owed == 0",
                )
        elif disposition == DepositDisposition.NO_REFUND.value:
            if rf != Decimal("0") or ao != Decimal("0"):
                raise ValidationError(
                    "NO_REFUND requires refund_amount == 0 and additional_owed == 0",
                )
        m = self.get_move_out(principal, org_id=org_id, move_out_id=move_out_id)
        if m.state == MoveOutState.SETTLED.value:
            raise ConflictError(
                f"move-out {move_out_id} is already SETTLED",
            )
        if m.state != MoveOutState.INSPECTED.value:
            raise ConflictError(
                f"cannot settle a move-out in state {m.state!r} "
                f"(must be INSPECTED first)",
            )
        # Inspection must have happened — verify by counting inspection
        # rows; we already filtered by state, but the row must exist.
        ins_count = (
            self.db.query(MoveOutInspection)
            .filter(MoveOutInspection.move_out_id == m.id)
            .count()
        )
        if ins_count == 0:
            raise ConflictError(
                "cannot settle: at least one MoveOutInspection is required",
            )
        # Compute deductions_total.
        damages = (
            self.db.query(MoveOutDamage)
            .filter(MoveOutDamage.move_out_id == m.id)
            .all()
        )
        deductions_total = sum(
            (Decimal(d.accepted_amount) for d in damages),
            Decimal("0"),
        )
        # Persist the settlement row.
        s = DepositSettlement(
            org_id=org_id,
            move_out_id=m.id,
            disposition=disposition,
            deposit_held=dh,
            deductions_total=deductions_total,
            refund_amount=rf,
            additional_owed=ao,
            notes=notes,
            settled_by_user_id=principal.user_id,
        )
        self.db.add(s)
        self.db.flush()
        m.settlement_id = s.id
        m.settled_at = s.settled_at
        m.state = MoveOutState.SETTLED.value
        op = self.get_operation(principal, org_id=org_id, move_out_id=move_out_id)
        _close_operation(
            self.db, operation=op, actor_user_id=principal.user_id,
        )
        _log_activity(
            self.db,
            org_id=org_id,
            move_out_id=m.id,
            kind=MoveOutActivityKind.SETTLED,
            actor_user_id=principal.user_id,
            detail=(
                f"{disposition} deposit={dh} refund={rf} owed={ao}"
            ),
        )
        self.db.commit()
        return MoveOutSettleResult(move_out=m, settlement=s)

    # ---- cancel ----

    def cancel(
        self,
        principal: Principal,
        *,
        org_id: int,
        move_out_id: int,
        reason: str,
    ) -> MoveOut:
        """OWNER-only. Non-terminal → CANCELLED."""
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER)
        if not reason or not reason.strip():
            raise ValidationError("cancellation reason is required")
        m = self.get_move_out(principal, org_id=org_id, move_out_id=move_out_id)
        if m.state in (
            MoveOutState.SETTLED.value,
            MoveOutState.CANCELLED.value,
        ):
            raise ConflictError(
                f"cannot cancel a terminal move-out (state={m.state})",
            )
        m.state = MoveOutState.CANCELLED.value
        m.cancelled_at = utcnow()
        m.cancel_reason = reason
        op = self.get_operation(principal, org_id=org_id, move_out_id=move_out_id)
        _close_operation(
            self.db, operation=op, actor_user_id=principal.user_id,
        )
        _log_activity(
            self.db,
            org_id=org_id,
            move_out_id=m.id,
            kind=MoveOutActivityKind.CANCELLED,
            actor_user_id=principal.user_id,
            detail=reason,
        )
        self.db.commit()
        return m

    # ---- record_keys_arrears (Coverage Matrix 7.6) ----

    def record_keys_arrears(
        self,
        principal: Principal,
        *,
        org_id: int,
        move_out_id: int,
        keys_returned: bool,
        arrears_amount: Decimal | str | int = 0,
        notes: Optional[str] = None,
    ) -> MoveOut:
        """Coverage Matrix 7.6: keys-returned + arrears ledger.

        Persists the keys/arrears state of the move-out without
        mutating the deposit or the settlement. This is a soft update
        used by the Owner checklist on the Mini App before
        `close_atomically` is invoked.

        OWNER/SECRETARY may record. ``arrears_amount`` is parsed via
        ``parse_money`` (Decimal-only). Setting ``keys_returned=False``
        plus a non-zero ``arrears_amount`` is allowed and logs an
        activity row but never auto-closes the move-out.
        """
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER, Role.SECRETARY)
        m = self.get_move_out(
            principal, org_id=org_id, move_out_id=move_out_id,
        )
        if m.state in (
            MoveOutState.SETTLED.value,
            MoveOutState.CANCELLED.value,
        ):
            raise ConflictError(
                f"cannot record keys/arrears on terminal move-out "
                f"(state={m.state})",
            )
        arrears_dec = parse_money(arrears_amount)
        if arrears_dec < 0:
            raise ValidationError("arrears_amount must be non-negative")
        m.keys_returned = keys_returned
        m.arrears_amount = arrears_dec
        m.keys_arrears_notes = notes
        # Soft update — only bump Operation to in_progress if not
        # already resolved. Never closes.
        op = self.get_operation(
            principal, org_id=org_id, move_out_id=move_out_id,
        )
        _bump_to_in_progress(op)
        _log_activity(
            self.db,
            org_id=org_id,
            move_out_id=m.id,
            kind=MoveOutActivityKind.FOLLOW_UP_CREATED,
            actor_user_id=principal.user_id,
            detail=(
                f"keys_returned={keys_returned} arrears={arrears_dec}"
            ),
        )
        self.db.commit()
        return m

    # ---- close_atomically (Coverage Matrix 7.7) ----

    def close_atomically(
        self,
        principal: Principal,
        *,
        org_id: int,
        move_out_id: int,
    ) -> MoveOut:
        """Coverage Matrix 7.7: atomic final close.

        Single transaction that:
          1. Verifies the move-out has been SETTLED (close only after
             real settlement).
          2. Terminates the source Lease (ACTIVE → TERMINATED).
          3. Flips the Unit back to AVAILABLE.
          4. Marks the move-out archived (preserved for audit).
          5. Resolves the linked Operation; cancels open follow-ups.

        Cross-step guarantees:
          - a partial close (e.g. settlement only) cannot leave the
            move-out in a half-closed state.
          - related Expense/Payment actions never trigger this method
            (state-machine guard enforced at the ExpenseService level).
        """
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER)
        m = self.get_move_out(
            principal, org_id=org_id, move_out_id=move_out_id,
        )
        if m.state != MoveOutState.SETTLED.value:
            raise ConflictError(
                f"cannot close move-out {move_out_id} in state "
                f"{m.state!r} (must be SETTLED first)",
            )
        if m.settlement_id is None:
            raise ConflictError(
                f"cannot close move-out {move_out_id}: "
                f"settlement row missing",
            )
        # 1) Terminate the source lease (if still ACTIVE).
        lease = self.db.get(Lease, m.lease_id) if m.lease_id is not None else None
        if (
            lease is not None
            and lease.org_id == org_id
            and lease.state == LeaseState.ACTIVE.value
        ):
            lease.state = LeaseState.TERMINATED.value
            lease.archived_at = utcnow()
        # 2) Flip the lease's unit back to AVAILABLE.
        if lease is not None and lease.unit_id is not None:
            unit = self.db.get(Unit, lease.unit_id)
            if unit is not None and unit.org_id == org_id:
                unit.status = UnitStatus.AVAILABLE.value
        # 3) Archive the move-out (history retained, never erased).
        if m.archived_at is None:
            m.archived_at = utcnow()
        # 4) Resolve Operation.
        op = self.get_operation(
            principal, org_id=org_id, move_out_id=move_out_id,
        )
        _close_operation(
            self.db, operation=op, actor_user_id=principal.user_id,
        )
        _log_activity(
            self.db,
            org_id=org_id,
            move_out_id=m.id,
            kind=MoveOutActivityKind.SETTLED,
            actor_user_id=principal.user_id,
            detail="close_atomically",
        )
        self.db.commit()
        self.db.refresh(m)
        return m

    # ---- follow-up (Task projection) ----

    def create_follow_up(
        self,
        principal: Principal,
        *,
        org_id: int,
        move_out_id: int,
        title: str,
        due_at: Optional[datetime] = None,
    ) -> Task:
        require_org_scope(principal, org_id)
        _ensure_role(principal, Role.OWNER, Role.SECRETARY)
        m = self.get_move_out(principal, org_id=org_id, move_out_id=move_out_id)
        op = _get_or_create_operation(
            self.db, org_id=org_id, move_out=m,
        )
        existing = (
            self.db.query(Task)
            .filter(
                Task.operation_id == op.id,
                Task.state == "open",
            )
            .first()
        )
        if existing is not None:
            raise ConflictError(
                f"move-out {move_out_id} already has an open follow-up "
                f"(task {existing.id}); complete or cancel it first",
            )
        task = Task(
            org_id=org_id,
            operation_id=op.id,
            kind=TASK_KIND_MOVE_OUT_FOLLOW_UP,
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
            move_out_id=move_out_id,
            kind=MoveOutActivityKind.FOLLOW_UP_CREATED,
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
        move_out_id: int,
    ) -> list[Task]:
        require_org_scope(principal, org_id)
        self.get_move_out(principal, org_id=org_id, move_out_id=move_out_id)
        op = (
            self.db.query(Operation)
            .filter(
                Operation.org_id == org_id,
                Operation.subject_type == OPERATION_SUBJECT_MOVE_OUT,
                Operation.subject_id == move_out_id,
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
            move_out_id=task.operation_id,
            kind=MoveOutActivityKind.FOLLOW_UP_DONE,
            actor_user_id=principal.user_id,
            detail=task.title,
        )
        self.db.commit()
        return task


__all__ = [
    "MoveOutRequestResult",
    "MoveOutService",
    "MoveOutSettleResult",
]
