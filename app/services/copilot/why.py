"""Per-item WHY enrichment (V1.2.2 Phase C1.1 Deliverable B).

``explain_item`` grounds to the deterministic A+B context, validates the
requested ``item_ref`` is in the grounded set, and calls the EXPLAIN profile
(centralized provider map — fast non-reasoning by default) with a scoped WHY
prompt.

Fail-closed (Requirement 8): on provider error / timeout / malformed output the
endpoint returns HTTP 200 with ``fallback=True`` and the DETERMINISTIC reason +
suggested action from the existing priority engine — never a fabricated
answer. LLM-produced amounts/dates are post-validated against the grounded
facts and stripped + flagged when invented.

Read-only by construction: no DB writes here; the optional ``copilot_runs``
audit row is written by the router.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.user import User
from app.services.copilot import llm
from app.services.copilot.prompts import build_why_messages
from app.services.copilot.ranking import rank_items
from app.services.copilot.shared import (
    LatencyBreakdown,
    collect_grounded_facts,
    default_action,
    ground_text,
    grounded_refs,
    parse_json_object,
)
from app.services.operations.copilot import build_copilot_context
from app.services.operations.timeclock import clock

WHY_MAX_TOKENS = 2000
FALLBACK_MODEL = "deterministic-fallback"


class WhyError(RuntimeError):
    """Structured WHY failure (item-level; never a fabricated answer)."""


class WhyItemNotGrounded(WhyError):
    """The requested item_ref is not in the grounded context set."""


@dataclass(frozen=True)
class WhyResult:
    item_ref: str
    explanation: str
    recommendation: str
    grounded_refs: list[str]
    provider: str
    model: str
    latency_ms: int
    fallback: bool
    flags: list[str] = field(default_factory=list)
    latency: LatencyBreakdown = field(default_factory=LatencyBreakdown)
    context: dict = field(default_factory=dict)


def _deterministic_texts(context: dict, item_ref: str) -> tuple[str, str]:
    """Deterministic reason + action for ``item_ref`` (ranking is ground truth).

    Items that are grounded but not actionable (e.g. a property/tenant ref)
    get a grounded generic explanation instead of an invented one.
    """
    ranked_by_ref = {r.item_ref: r for r in rank_items(context)}
    item = ranked_by_ref.get(item_ref)
    if item is None:
        return (
            "This item is part of the current operational context.",
            "Review and follow up today",
        )
    return item.reason, default_action(item)


def explain_item(
    db: Session,
    user: User,
    item_ref: str,
    provider: str | None = None,
    *,
    client: llm.LLMClient | None = None,
    now=None,
) -> WhyResult:
    """Explain one grounded item (LLM-backed, deterministic fail-closed)."""
    t_start = time.monotonic()
    now = now if now is not None else clock.now()

    t_ctx = time.monotonic()
    context = build_copilot_context(db, user, now=now)
    context_build_ms = int((time.monotonic() - t_ctx) * 1000)

    t_ground = time.monotonic()
    refs = grounded_refs(context)
    facts = collect_grounded_facts(context)
    grounding_ms = int((time.monotonic() - t_ground) * 1000)

    if item_ref not in refs:
        raise WhyItemNotGrounded(
            f"item_ref {item_ref!r} is not in the grounded context set"
        )

    t_rank = time.monotonic()
    det_reason, det_action = _deterministic_texts(context, item_ref)
    priority_ms = int((time.monotonic() - t_rank) * 1000)

    resolved = llm.profile_provider("EXPLAIN", provider)
    messages = build_why_messages(context, item_ref, det_reason, det_action)

    t_llm = time.monotonic()
    try:
        if client is None:
            client = llm.get_llm_client(resolved)
        result = client.complete(
            messages,
            temperature=0.2,
            max_tokens=WHY_MAX_TOKENS,
            response_format={"type": "json_object"},
        )
        llm_ms = int((time.monotonic() - t_llm) * 1000)
        parsed = parse_json_object(result.text)
        raw_explanation = str(parsed.get("explanation") or "")
        raw_recommendation = str(parsed.get("recommendation") or "")
    except (llm.LLMProviderError, ValueError) as exc:
        llm_ms = int((time.monotonic() - t_llm) * 1000)
        t_render = time.monotonic()
        result = None
        render_ms = int((time.monotonic() - t_render) * 1000)
        return WhyResult(
            item_ref=item_ref,
            explanation=det_reason,
            recommendation=det_action,
            grounded_refs=sorted(refs),
            provider=resolved or "deterministic",
            model=FALLBACK_MODEL,
            latency_ms=int((time.monotonic() - t_start) * 1000),
            fallback=True,
            flags=["provider_error"],
            latency=LatencyBreakdown(
                context_build_ms=context_build_ms,
                priority_ms=priority_ms,
                grounding_ms=grounding_ms,
                llm_ms=llm_ms,
                render_ms=render_ms,
                total_ms=int((time.monotonic() - t_start) * 1000),
            ),
            context=context,
        )

    t_render = time.monotonic()
    flags: list[str] = []
    explanation, expl_flags = ground_text(raw_explanation, facts)
    recommendation, rec_flags = ground_text(raw_recommendation, facts)
    flags.extend(expl_flags)
    flags.extend(rec_flags)
    if not explanation:
        explanation = det_reason
        flags.append("empty_explanation")
    if not recommendation:
        recommendation = det_action
        flags.append("empty_recommendation")
    render_ms = int((time.monotonic() - t_render) * 1000)

    return WhyResult(
        item_ref=item_ref,
        explanation=explanation,
        recommendation=recommendation,
        grounded_refs=sorted(refs),
        provider=result.provider,
        model=result.model,
        latency_ms=result.latency_ms,
        fallback=False,
        flags=flags,
        latency=LatencyBreakdown(
            context_build_ms=context_build_ms,
            priority_ms=priority_ms,
            grounding_ms=grounding_ms,
            llm_ms=llm_ms,
            render_ms=render_ms,
            total_ms=int((time.monotonic() - t_start) * 1000),
        ),
        context=context,
    )
