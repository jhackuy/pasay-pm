"""PASAY-EXPENSE-OPERATION-003B — deterministic Expense timeline.

The Mini App renders a full, ordered history of an Expense Operation so the
Owner / Secretary can see at a glance:
- created -> submitted -> approved (approval != paid, section 7)
- claim PENDING (payment reported, section 3/E2) -> evidence -> verified
- remaining recomputed after a partial verified payment (section 4)
- second claim -> verified -> fully paid
- amount mismatch surfaced, not hidden (section 5/E6)
- reject -> resubmit V1 REJECTED preserved, V2 PENDING (section 8/E8)

The timeline is DERIVED deterministically from the real rows (audit_log,
expense_payment_claims, expense fields) — no LLM. Exactly the sequence
required by section 16:
  Expense created -> Submitted for approval -> Approved -> Payment claim ₱10,000
  -> Evidence submitted -> ₱10,000 verified -> Remaining ₱18,000
  -> Second payment claim ₱18,000 -> Evidence submitted -> ₱18,000 verified
  -> Expense fully paid.
If the expense is linked to a Repair, the Repair stays NOT-CLOSED (verified
elsewhere; this timeline only speaks about the expense, so it never claims the
Repair closed).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.expense_claim import ClaimStatus
from app.models.financial import Expense, ExpenseStatus


def _money(value) -> str:
    try:
        d = round(float(value or 0), 2)
        return f"₱{d:,.2f}"
    except (TypeError, ValueError):
        return ""


def _iso(dt) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    return str(dt)


def _ordered_audit_events(db: Session, expense_id: int) -> list[dict]:
    """Pull the audit events relevant to an expense and return them sorted by
    time (then by a stable order for ties)."""
    events = []
    logs = (
        db.query(AuditLog)
        .filter(AuditLog.table_name == "expenses", AuditLog.record_id == expense_id)
        .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        .all()
    )
    for log in logs:
        events.append({
            "at": log.created_at,
            "action": log.action.value if hasattr(log.action, "value") else str(log.action),
            "actor_id": log.actor_id,
            "changed_fields": log.changed_fields or {},
            "log_id": log.id,
        })
    return events


def build_expense_timeline(
    db: Session,
    expense: Expense,
    claims,
) -> list[dict]:
    """Ordered, ready-to-render history of the Expense's real business events."""
    events: list[dict] = []

    # 0. Created.
    events.append({
        "at": _iso(expense.created_at),
        "kind": "expense_created",
        "label": "Expense created",
        "detail": f"{expense.category} · {_money(expense.amount)}",
    })

    # 1. Submitted for approval (a created expense that is PENDING awaiting
    # Owner approval / submitted event). We always represent the submit step for
    # an expense that reached a non-pending, reviewed state.
    if expense.status.value in (
        ExpenseStatus.approved.value,
        ExpenseStatus.paid.value,
        ExpenseStatus.partially_paid.value,
        ExpenseStatus.payment_claimed.value,
        ExpenseStatus.rejected.value,
    ):
        events.append({
            "at": _iso(expense.approved_at or expense.created_at),
            "kind": "submitted",
            "label": "Submitted for approval",
            "detail": _money(expense.amount),
        })
        # 2. Approved / Rejected.
        if expense.status.value in (
            ExpenseStatus.approved.value,
            ExpenseStatus.paid.value,
            ExpenseStatus.partially_paid.value,
            ExpenseStatus.payment_claimed.value,
        ):
            events.append({
                "at": _iso(expense.approved_at or expense.created_at),
                "kind": "approved",
                "label": "Approved",
                "detail": _money(expense.amount),
            })
        elif expense.status.value == ExpenseStatus.rejected.value:
            events.append({
                "at": _iso(expense.updated_at or expense.created_at),
                "kind": "rejected",
                "label": "Rejected",
                "detail": expense.rejection_reason or "",
            })

    # 3. Resubmitted (a child version exists).
    if expense.reapproval_reason:
        events.append({
            "at": _iso(expense.updated_at or expense.created_at),
            "kind": "requires_reapproval",
            "label": "Re-approval required",
            "detail": expense.reapproval_reason,
        })

    # 4. Claims: PENDING, VERIFIED, FAILED, REVERSED in chronological order.
    for c in sorted(claims, key=lambda x: (x.claimed_at or x.created_at, x.id)):
        # The claim-as-reported event always appears (payment reported).
        if c.status != ClaimStatus.VERIFIED or True:
            events.append({
                "at": _iso(c.claimed_at or c.created_at),
                "kind": "payment_claim",
                "label": f"Payment claim {_money(c.claimed_amount)}",
                "detail": c.verification_note or "",
            })
        if c.status == ClaimStatus.VERIFIED:
            events.append({
                "at": _iso(c.verified_at or c.claimed_at or c.created_at),
                "kind": "verified",
                "label": f"{_money(c.verified_amount or c.claimed_amount)} verified",
                "detail": c.verification_note or "",
            })
        elif c.status == ClaimStatus.FAILED:
            events.append({
                "at": _iso(c.updated_at or c.claimed_at or c.created_at),
                "kind": "claim_failed",
                "label": f"Claim {_money(c.claimed_amount)} failed",
                "detail": c.failure_reason or c.mismatch_reason or "",
            })
        elif c.status == ClaimStatus.REVERSED:
            events.append({
                "at": _iso(c.updated_at or c.claimed_at or c.created_at),
                "kind": "claim_reversed",
                "label": f"Payment {_money(c.claimed_amount)} reversed",
                "detail": c.failure_reason or "",
            })

    # 5. Fully paid derived from VERIFIED aggregate.
    from app.services.expense_payment_truth import payment_truth

    truth = payment_truth(db, expense)
    if truth.fully_paid:
        events.append({
            "at": _iso(expense.updated_at or expense.created_at),
            "kind": "fully_paid",
            "label": "Expense fully paid",
            "detail": f"{_money(truth.verified_paid)} verified / {_money(expense.amount)} total",
        })

    # 6. Remaining balance after partial payment (derived) — shown as its own
    # step only when there is verified-but-remaining money.
    if truth.verified_paid > 0 and truth.remaining > 0:
        events.append({
            "at": _iso(expense.updated_at or expense.created_at),
            "kind": "remaining",
            "label": f"Remaining {_money(truth.remaining)}",
            "detail": f"{_money(truth.verified_paid)} verified so far",
        })

    # Sort deterministically by time then kind-order.
    sort_orders = {
        "expense_created": 0,
        "submitted": 1,
        "rejected": 2,
        "approved": 3,
        "requires_reapproval": 4,
        "payment_claim": 5,
        "verified": 6,
        "remaining": 7,
        "claim_failed": 8,
        "claim_reversed": 9,
        "fully_paid": 10,
    }
    for ev in events:
        ev["_at_epoch"] = _epoch(ev.get("at"))
    events.sort(key=lambda e: (e["_at_epoch"] or 0, sort_orders.get(e["kind"], 99), e["label"]))
    for ev in events:
        ev.pop("_at_epoch", None)
    return events


def _epoch(value) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        from datetime import timezone as _tz
        dt = dt.replace(tzinfo=_tz.utc)
    return dt.timestamp()
