"""Deterministic-first TODAY brief (V1.2.2 Phase C1.1 FAST UX).

``build_today_deterministic`` is the DEFAULT ``/today`` response: it builds the
same grounded A+B context, runs the existing deterministic priority engine
(``ranking.rank_items`` — reused, NOT rewritten), and renders the top-3 brief
with a versioned, deterministic summary.

There is NO LLM on this critical path: no provider call, no timeout, no
failure path. The only DB write anywhere in this surface is the optional
``copilot_runs`` audit row written by the router.

Latency instrumentation (Deliverable D) is returned in every result:
``context_build_ms / priority_ms / grounding_ms / llm_ms(=0) / render_ms /
total_ms``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.user import User
from app.services.copilot.ranking import RankedItem, rank_items
from app.services.copilot.shared import (
    LatencyBreakdown,
    TodayItem,
    fallback_item,
    grounded_refs,
)
from app.services.operations.copilot import build_copilot_context
from app.services.operations.timeclock import clock

TODAY_MAX_ITEMS = 3
DETERMINISTIC_PROVIDER = "deterministic"
DETERMINISTIC_MODEL = "deterministic-v1"
SUMMARY_VERSION = "det_v1"
_LEASE_URGENT_DAYS = 7
_NON_URGENT_KINDS = frozenset({"settlement_pending", "recurring_rule"})


@dataclass(frozen=True)
class TodayFastResult:
    """Deterministic TODAY brief (no LLM involved; ``enriched=False``)."""

    top_items: list[TodayItem]
    summary: str
    summary_version: str
    context_schema_version: str
    provider: str
    model: str
    latency_ms: int
    latency: LatencyBreakdown
    flags: list[str] = field(default_factory=list)
    deterministic_top_refs: list[str] = field(default_factory=list)
    enriched: bool = False
    context: dict = field(default_factory=dict)


def _fmt_amount(value: Decimal) -> str:
    """PHP amount with thousands separators + 2dp (grounded by construction)."""
    return f"PHP {value:,.2f}"


def _count(items: list) -> int:
    return len(items)


def _plural_suffix(items: list) -> str:
    return "" if len(items) == 1 else "s"


def _summarize_top(items: list[RankedItem]) -> str:
    """Versioned deterministic summary — a PURE function of the ranked items.

    Format ``det_v1``: one sentence naming the urgent groups with their
    grounded totals, plus (when relevant) a second sentence naming the
    maintenance/operational backlog. No LLM, no ungrounded facts.
    """
    if not items:
        return "No urgent operational items today. Your portfolio is on track."

    # Urgency is defined by the priority engine's tier: only tier >= 1000
    # (severe/overdue rent, expiring lease, maintenance-tier ops) is "urgent".
    urgent = [i for i in items if i.tier >= 1000]
    if not urgent:
        return "No urgent operational items today. Your portfolio is on track."

    severe = [i for i in urgent if i.kind == "severe_overdue_rent"]
    overdue = [i for i in urgent if i.kind == "overdue_rent"]
    expiring = [i for i in urgent if i.kind == "lease_expiring"]
    maintenance = [i for i in items if i.kind in ("maintenance", "pending_task")]
    other = [i for i in urgent if i.kind in _NON_URGENT_KINDS]

    urgent_parts: list[str] = []
    if severe:
        total = sum(
            (Decimal(str(i.payload.get("total_outstanding") or 0)) for i in severe),
            Decimal("0"),
        )
        urgent_parts.append(
            f"{_count(severe)} severe overdue rent{_plural_suffix(severe)} "
            f"totaling {_fmt_amount(total)}"
        )
    if overdue:
        total = sum(
            (Decimal(str(i.payload.get("total_outstanding") or 0)) for i in overdue),
            Decimal("0"),
        )
        urgent_parts.append(
            f"{_count(overdue)} overdue rent{_plural_suffix(overdue)} "
            f"totaling {_fmt_amount(total)}"
        )
    if expiring:
        urgent_parts.append(
            f"{_count(expiring)} lease{_plural_suffix(expiring)} expiring within "
            f"{_LEASE_URGENT_DAYS} days"
        )
    if other:
        urgent_parts.append(
            f"{_count(other)} pending settlement/recurring "
            f"item{_plural_suffix(other)}"
        )

    if not urgent_parts:
        if maintenance:
            return (
                f"{_count(maintenance)} maintenance/operational "
                f"item{_plural_suffix(maintenance)} need attention today. "
                "No overdue rents or expiring leases."
            )
        return "No urgent operational items today. Your portfolio is on track."

    summary = f"{_count(urgent)} urgent item{_plural_suffix(urgent)}: " + ", ".join(urgent_parts) + "."
    if maintenance:
        summary += (
            f" Plus {_count(maintenance)} maintenance/operational "
            f"item{_plural_suffix(maintenance)} pending."
        )
    return summary


def build_today_deterministic(
    db: Session,
    user: User,
    *,
    now=None,
) -> TodayFastResult:
    """Build TODAY's brief deterministically — NO LLM, returns in milliseconds.

    T0: build the grounded A+B context (``build_copilot_context``).
    T1: rank with the existing deterministic priority engine (``rank_items``).
    Top-3 -> ``TodayItem``; summary is a pure function of the ranked items.
    """
    t_start = time.monotonic()
    now = now if now is not None else clock.now()
    t_ctx = time.monotonic()
    context = build_copilot_context(db, user, now=now)
    context_build_ms = int((time.monotonic() - t_ctx) * 1000)

    t_ground = time.monotonic()
    refs = grounded_refs(context)
    grounding_ms = int((time.monotonic() - t_ground) * 1000)

    t_rank = time.monotonic()
    ranked = rank_items(context)
    priority_ms = int((time.monotonic() - t_rank) * 1000)

    t_render = time.monotonic()
    top = ranked[:TODAY_MAX_ITEMS]
    top_items = [fallback_item(item) for item in top]
    summary = _summarize_top(top)
    top_refs = [item.item_ref for item in top]
    render_ms = int((time.monotonic() - t_render) * 1000)

    total_ms = int((time.monotonic() - t_start) * 1000)
    return TodayFastResult(
        top_items=top_items,
        summary=summary,
        summary_version=SUMMARY_VERSION,
        context_schema_version=str(context.get("context_schema_version", "1.0")),
        provider=DETERMINISTIC_PROVIDER,
        model=DETERMINISTIC_MODEL,
        latency_ms=total_ms,
        latency=LatencyBreakdown(
            context_build_ms=context_build_ms,
            priority_ms=priority_ms,
            grounding_ms=grounding_ms,
            llm_ms=0,
            render_ms=render_ms,
            total_ms=total_ms,
        ),
        flags=[],
        deterministic_top_refs=top_refs,
        enriched=False,
        context=context,
    )
