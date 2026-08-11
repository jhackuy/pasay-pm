"""Deterministic business-risk ranker for the read-only copilot (V1.2.2 C1).

Ground truth for TODAY's top items. The LLM may reorder items WITHIN the
deterministic top-K, but a low-risk item can never displace a high-risk one —
``rank_items(context)`` produces the ordered list the TODAY layer vets the
LLM output against.

The score is a PURE function of the structured fields in the A+B context JSON
(amounts, due dates, periods, status, priority, task_type) — it never reads
title/description/note text, so a long-worded low-urgency item can never
out-rank a severe overdue rent.

Tiers (high risk first, documented weights/caps):

    tier 4000  severe overdue rent   overdue_months >= 2 OR
                                     total_outstanding >= 2x amount_per_month
    tier 3000  overdue rent          single-period overdue
    tier 2000  lease expiring        active lease ending within the A+B window;
                                     urgency bonus ramps as end_date -> now
    tier 1000  maintenance /         open maintenance tasks, high/critical
               moderate ops          pending operational tasks, pending
                                     expense approvals
    tier  100  low urgency           low-priority todos, pending settlements,
                                     upcoming recurring rules

Within-tier refinements (all structured, each capped so no refinement can
cross a tier gap of 1000):

    overdue        + 40/mo overdue (cap 12 mo) + amount pts (PHP500 -> 1,
                   cap 300)
    lease expiring + 70/dy closer than the 7-day urgent mark (cap 7) +
                   rent amount pts (PHP1000 -> 1, cap 200)
    maintenance    + priority pts (critical 150 / high 100 / medium 50) +
                   due-date proximity pts (25/dy inside the next 7 days, cap)
    pending ops    + priority pts + due-date proximity pts (same scheme)
    expense        + amount pts (PHP1000 -> 1, cap 300)
    settlement     + amount pts (PHP1000 -> 1, cap 200)
    recurring      + next-run proximity pts (25/dy inside the next 7 days)

Tie-break is stable: (kind order, item_ref) after descending score.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

# Tier bases (gaps are >= 1000; refinements are capped below 1000).
TIER_SEVERE_OVERDUE = 4000
TIER_OVERDUE = 3000
TIER_LEASE_EXPIRING = 2000
TIER_MAINTENANCE = 1000
TIER_LOW = 100

# Severe-overdue definition (req 6: "multiple periods / large outstanding").
SEVERE_OVERDUE_MIN_MONTHS = 2
SEVERE_OVERDUE_MULTIPLE = 2  # total_outstanding >= 2x amount_per_month

# Lease-expiry urgency window (days).
LEASE_URGENT_DAYS = 7
PROXIMITY_WINDOW_DAYS = 7

# Refinement constants (capped per dimension; documented above).
OVERDUE_MONTHS_PER_POINT = 40
OVERDUE_MONTHS_CAP = 12
OVERDUE_AMOUNT_PER_POINT = Decimal("500")
OVERDUE_AMOUNT_POINTS_CAP = 300
LEASE_URGENCY_PER_POINT = 70
LEASE_AMOUNT_PER_POINT = Decimal("1000")
LEASE_AMOUNT_POINTS_CAP = 200
MAINTENANCE_PRIORITY_POINTS = {"critical": 150, "high": 100, "medium": 50, "low": 0}
DUE_PROXIMITY_PER_POINT = 25
EXPENSE_AMOUNT_PER_POINT = Decimal("1000")
EXPENSE_AMOUNT_POINTS_CAP = 300
SETTLEMENT_AMOUNT_PER_POINT = Decimal("1000")
SETTLEMENT_AMOUNT_POINTS_CAP = 200

# Stable kind ordering for tie-breaks (business-priority aware).
KIND_ORDER = {
    "severe_overdue_rent": 0,
    "overdue_rent": 1,
    "lease_expiring": 2,
    "maintenance": 3,
    "pending_task": 4,
    "expense_approval": 5,
    "settlement_pending": 6,
    "recurring_rule": 7,
}

# Operational task types that are inherently moderate-risk business events.
_MODERATE_TASK_TYPES = frozenset({
    "RENT_OVERDUE", "LEASE_EXPIRING", "RENT_DUE", "PROPERTY_FEE_DUE",
    "APPROVAL_PENDING", "PAYMENT_PENDING",
})


@dataclass(frozen=True)
class RankedItem:
    """One deterministically ranked, grounded item."""

    item_ref: str
    kind: str
    tier: int
    score: int
    label: str
    reason: str
    payload: dict = field(default_factory=dict, compare=False)

    def to_dict(self) -> dict:
        return {
            "item_ref": self.item_ref,
            "kind": self.kind,
            "tier": self.tier,
            "score": self.score,
            "label": self.label,
            "reason": self.reason,
        }


def _money(value) -> Decimal:
    return Decimal(str(value))


def _cap(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))


def _date(value: str | date | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    parsed = datetime.fromisoformat(str(value))
    return parsed.date()


def _parse_now(context: dict) -> datetime:
    """Reference instant from the context (deterministic per context)."""
    raw = context.get("current_time") or context.get("generated_at")
    if raw is None:
        return datetime.now().astimezone()
    parsed = datetime.fromisoformat(str(raw))
    if parsed.tzinfo is None:
        from app.services.operations.timeclock import MANILA_TZ

        parsed = parsed.replace(tzinfo=MANILA_TZ)
    return parsed


def _proximity_points(due: date | None, today: date) -> int:
    """25 pts per day inside the next 7 days (sooner = more points, capped)."""
    if due is None:
        return 0
    days = (due - today).days
    return _cap((PROXIMITY_WINDOW_DAYS - days) * DUE_PROXIMITY_PER_POINT, 0, 175)


def _overdue_item(row: dict, today: date) -> RankedItem:
    months = int(row.get("overdue_months") or 0)
    per_month = _money(row.get("amount_per_month") or 0)
    outstanding = _money(row.get("total_outstanding") or 0)
    severe = (
        months >= SEVERE_OVERDUE_MIN_MONTHS
        or (per_month > 0 and outstanding >= SEVERE_OVERDUE_MULTIPLE * per_month)
    )
    tier = TIER_SEVERE_OVERDUE if severe else TIER_OVERDUE
    kind = "severe_overdue_rent" if severe else "overdue_rent"
    month_pts = _cap(months, 0, OVERDUE_MONTHS_CAP) * OVERDUE_MONTHS_PER_POINT
    amount_pts = min(int(outstanding / OVERDUE_AMOUNT_PER_POINT), OVERDUE_AMOUNT_POINTS_CAP)
    score = tier + month_pts + amount_pts
    label = (
        f"Rent overdue {months}mo ({outstanding} PHP)"
        if severe
        else f"Rent overdue 1mo ({outstanding} PHP)"
    )
    reason = (
        f"{months} rent period(s) overdue; {outstanding} PHP outstanding"
        f" (monthly {per_month} PHP)"
    )
    return RankedItem(
        item_ref=str(row["reference"]),
        kind=kind,
        tier=tier,
        score=score,
        label=label,
        reason=reason,
        payload=row,
    )


def _lease_item(row: dict, today: date) -> RankedItem:
    end = _date(row.get("end_date"))
    days_left = max((end - today).days, 0) if end is not None else 999
    urgency = _cap(LEASE_URGENT_DAYS - days_left, 0, LEASE_URGENT_DAYS)
    rent = _money(row.get("monthly_rent") or 0)
    amount_pts = min(int(rent / LEASE_AMOUNT_PER_POINT), LEASE_AMOUNT_POINTS_CAP)
    score = TIER_LEASE_EXPIRING + urgency * LEASE_URGENCY_PER_POINT + amount_pts
    return RankedItem(
        item_ref=str(row["reference"]),
        kind="lease_expiring",
        tier=TIER_LEASE_EXPIRING,
        score=score,
        label=f"Lease expires {end.isoformat() if end else 'soon'} ({days_left}d)",
        reason=f"Active lease ends in {days_left} day(s); vacancy risk",
        payload=row,
    )


def _maintenance_item(row: dict, today: date) -> RankedItem:
    priority = str(row.get("priority") or "medium")
    priority_pts = MAINTENANCE_PRIORITY_POINTS.get(priority, 0)
    due = _date(row.get("due_date"))
    score = TIER_MAINTENANCE + priority_pts + _proximity_points(due, today)
    return RankedItem(
        item_ref=str(row["reference"]),
        kind="maintenance",
        tier=TIER_MAINTENANCE,
        score=score,
        label=f"Maintenance '{row.get('title') or 'task'}' ({priority})",
        reason=(
            "Open maintenance work pending"
            + (f", due {due.isoformat()}" if due else "")
        ),
        payload=row,
    )


def _pending_task_item(row: dict, today: date) -> RankedItem:
    priority = str(row.get("priority") or "medium")
    task_type = str(row.get("task_type") or "")
    moderate = (
        priority in ("high", "critical") or task_type in _MODERATE_TASK_TYPES
    )
    tier = TIER_MAINTENANCE if moderate else TIER_LOW
    kind = "pending_task"
    priority_pts = MAINTENANCE_PRIORITY_POINTS.get(priority, 0)
    due = _date(row.get("due_at"))
    score = tier + priority_pts + _proximity_points(due, today)
    return RankedItem(
        item_ref=str(row["reference"]),
        kind=kind,
        tier=tier,
        score=score,
        label=f"Operational task '{row.get('title') or 'task'}' ({priority})",
        reason=(
            f"{'Moderate' if moderate else 'Low'}-priority operational todo"
            + (f", due {due.isoformat()}" if due else "")
        ),
        payload=row,
    )


def _expense_item(row: dict, today: date) -> RankedItem:
    amount = _money(row.get("amount") or 0)
    amount_pts = min(int(amount / EXPENSE_AMOUNT_PER_POINT), EXPENSE_AMOUNT_POINTS_CAP)
    score = TIER_MAINTENANCE + amount_pts
    return RankedItem(
        item_ref=str(row["reference"]),
        kind="expense_approval",
        tier=TIER_MAINTENANCE,
        score=score,
        label=f"Expense approval pending ({amount} PHP)",
        reason=f"Pending expense approval of {amount} PHP",
        payload=row,
    )


def _settlement_item(row: dict, today: date) -> RankedItem:
    amount = _money(row.get("computed_amount") or 0)
    amount_pts = min(int(amount / SETTLEMENT_AMOUNT_PER_POINT), SETTLEMENT_AMOUNT_POINTS_CAP)
    score = TIER_LOW + amount_pts
    return RankedItem(
        item_ref=str(row["reference"]),
        kind="settlement_pending",
        tier=TIER_LOW,
        score=score,
        label=f"Settlement pending ({amount} PHP)",
        reason=f"Pending commission settlement of {amount} PHP",
        payload=row,
    )


def _recurring_item(row: dict, today: date) -> RankedItem:
    next_run = _date(row.get("next_run_at"))
    score = TIER_LOW + _proximity_points(next_run, today)
    return RankedItem(
        item_ref=str(row["reference"]),
        kind="recurring_rule",
        tier=TIER_LOW,
        score=score,
        label=f"Recurring '{row.get('title') or 'rule'}' next run",
        reason=(
            "Upcoming recurring task"
            + (f" (next {next_run.isoformat()})" if next_run else "")
        ),
        payload=row,
    )


_SECTION_BUILDERS = (
    ("overdue_rents", _overdue_item),
    ("leases_expiring", _lease_item),
    ("maintenance_tasks", _maintenance_item),
    ("pending_tasks", _pending_task_item),
    ("pending_expense_approvals", _expense_item),
    ("pending_settlements", _settlement_item),
    ("recurring_rules", _recurring_item),
)


def rank_items(context: dict) -> list[RankedItem]:
    """Order every actionable item in the grounded context by business risk.

    Pure function of the context's structured fields. Stable ordering: score
    descending, then kind priority, then item_ref.
    """
    today = _parse_now(context).date()
    items: list[RankedItem] = []
    for section, builder in _SECTION_BUILDERS:
        for row in context.get(section) or []:
            items.append(builder(row, today))
    items.sort(key=lambda item: (-item.score, KIND_ORDER.get(item.kind, 99), item.item_ref))
    return items


def top_k(context: dict, k: int = 3) -> list[RankedItem]:
    """The deterministic top-K (the set the LLM may reorder within)."""
    return rank_items(context)[: max(k, 0)]


def top_refs(context: dict, k: int = 3) -> list[str]:
    """Stable list of the deterministic top-K item refs."""
    return [item.item_ref for item in top_k(context, k=k)]
