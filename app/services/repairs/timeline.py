"""REPAIR-AI-EMPLOYEE-WORKFLOW-008A — deterministic Repair timeline.

The Mini App renders a full, ordered history of the Repair Operation so the
Owner / Secretary can see, at a glance, that:
- rejection did NOT close the repair,
- payment did NOT close the repair,
- only verification closed it.

The timeline is DERIVED deterministically from the real rows (repair_operations
audit fields, repair_proposals, repair_actions, expenses, verification). The
ordering is by event time, each with a stable ``kind``, a human ``label`` and a
``detail`` — no LLM. The exact sequence expected by 008A-F §4.1/§7:

Issue reported → Proposal V1 submitted → V1 rejected → Requote requested
→ Proposal V2 submitted → V2 approved → Expense paid → Repair result recorded
→ Verified → Closed.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models.financial import Expense, ExpenseStatus
from app.models.repair import (
    RepairAction,
    RepairOperation,
    RepairProposal,
    RepairProposalStatus,
)


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


def build_timeline(
    db: Session,
    repair: RepairOperation,
    proposals: list[RepairProposal],
    actions: list[RepairAction],
) -> list[dict]:
    """Ordered human-readable timeline of the Repair's real history."""
    events: list[dict] = []

    # 0. Issue reported (the creation time is the earliest real fact).
    events.append({
        "at": _iso(repair.created_at),
        "kind": "repair_created",
        "label": "Issue reported",
        "detail": repair.issue,
    })

    # 1-2. Proposals (submitted / rejected / approved) in version order.
    for p in sorted(proposals, key=lambda x: x.version):
        events.append({
            "at": _iso(p.submitted_at or p.created_at),
            "kind": "proposal_submitted",
            "label": f"Proposal V{p.version} submitted",
            "detail": (
                f"{_money(p.amount)}" + (f" · {p.vendor}" if p.vendor else "")
            ),
        })
        if p.status == RepairProposalStatus.REJECTED:
            events.append({
                "at": _iso(p.decision_at),
                "kind": "proposal_rejected",
                "label": f"Proposal V{p.version} rejected",
                "detail": p.rejection_reason or "",
            })
        elif p.status == RepairProposalStatus.APPROVED:
            events.append({
                "at": _iso(p.decision_at),
                "kind": "proposal_approved",
                "label": f"Proposal V{p.version} approved",
                "detail": _money(p.amount),
            })

    # 3. Requote requested (from the REQUOTE action or the requote context).
    # Anchored to the rejection decision time (same business instant) so the
    # deterministic step order (Reject before Requote) can't be inverted by
    # microsecond timestamps.
    rejected_map = {
        p.version: p
        for p in proposals
        if p.status == RepairProposalStatus.REJECTED and p.decision_at is not None
    }
    for a in actions:
        if a.action_kind == "REQUOTE" and a.source_event and "rejected" in a.source_event:
            anchor = None
            if a.detail and a.detail.get("proposal_id"):
                p = next((x for x in proposals if x.id == a.detail.get("proposal_id")), None)
                if p is not None and p.decision_at is not None:
                    anchor = p.decision_at
            if anchor is None and rejected_map:
                anchor = max((rejected_map[v].decision_at for v in rejected_map), default=None)
            events.append({
                "at": _iso(anchor or a.created_at),
                "kind": "requote_requested",
                "label": "Requote requested",
                "detail": "For the Secretary to get another quote",
            })

    # 4. Expense paid for an approved proposal (PAID != Closed).
    #   a) from an expense linked to a proposal (proposal.expense_id),
    #   b) from the repair's own record written by the payment coordination
    #      (details["expense_paid_at"]) — covers paid expenses recorded without
    #      being linked to a proposal.
    expense_ids = {p.expense_id for p in proposals if p.expense_id is not None}
    paid_at = None
    repair_details = repair.details or {}
    if repair_details.get("expense_paid_at"):
        paid_at = repair_details["expense_paid_at"]
    if not paid_at:
        if expense_ids:
            linked_paid = db.query(Expense).filter(
                Expense.id.in_(expense_ids), Expense.status == ExpenseStatus.paid
            ).first()
            if linked_paid is not None:
                paid_at = linked_paid.updated_at
    if paid_at:
        events.append({
            "at": _iso(paid_at),
            "kind": "expense_paid",
            "label": "Expense paid",
            "detail": "For the approved repair quote",
        })
    elif expense_ids:
        for expense in db.query(Expense).filter(Expense.id.in_(expense_ids)).all():
            if expense.status == ExpenseStatus.approved:
                events.append({
                    "at": _iso(expense.approved_at or expense.created_at),
                    "kind": "expense_approved",
                    "label": "Expense approved",
                    "detail": _money(expense.amount),
                })

    # 5. Repair result recorded (human said the real work is done).
    if repair.verification_result or repair.verified_at is not None:
        # Record-result step (VERIFYING) appears before the verification close.
        if repair.status.value == "VERIFYING" or repair.closed_at is not None:
            events.append({
                "at": _iso(repair.verified_at),
                "kind": "repair_result",
                "label": "Repair result recorded",
                "detail": repair.verification_result or "",
            })

    # 6. Verified + Closed (>only after real verification).
    if repair.closed_at is not None and repair.status.value == "CLOSED":
        events.append({
            "at": _iso(repair.closed_at),
            "kind": "verified",
            "label": "Verified",
            "detail": repair.verification_result or "",
        })
        events.append({
            "at": _iso(repair.closed_at),
            "kind": "closed",
            "label": "Closed",
            "detail": repair.closure_reason or "",
        })

    # Sort deterministically: by time (normalized to a sortable epoch), then by
    # the canonical step order for events sharing the same instant (e.g.
    # Verified + Closed both at closed_at). ``at`` values may mix DB server
    # defaults (+) and stored isoformat() (±) offsets, so we normalize to a
    # UTC epoch for comparison instead of string-sorting.
    sort_orders = {
        "repair_created": 0,
        "proposal_submitted": 1,
        "proposal_rejected": 2,
        "requote_requested": 3,
        "proposal_approved": 4,
        "expense_approved": 5,
        "expense_paid": 6,
        "repair_result": 7,
        "verified": 8,
        "closed": 9,
    }
    for ev in events:
        ev["_at_epoch"] = _epoch(ev.get("at"))
    events.sort(key=lambda e: (e["_at_epoch"] or 0, sort_orders.get(e["kind"], 99), e["label"]))
    for ev in events:
        ev.pop("_at_epoch", None)
    return events


def _epoch(value) -> float | None:
    """Best-effort epoch seconds from a datetime or ISO string (any offset)."""
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
