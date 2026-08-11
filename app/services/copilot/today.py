"""TODAY brief orchestration for the read-only copilot (V1.2.2 Phase C1).

``build_today`` grounds the LLM to the deterministic A+B context, calls the
provider, then POST-VALIDATES the raw model output server-side:

- ``item_ref`` must be in the grounded reference set -> hallucinated refs are
  DROPPED and flagged.
- Items are additionally restricted to the deterministic top-K (rank_items):
  the LLM may reorder within the top-K, but a low-risk item can never displace
  a high-risk item -> violations are dropped and flagged, and the brief is
  backfilled from the deterministic ranking.
- ``top_items`` is capped at 3 and ``summary`` at 2 sentences — enforced here,
  not by prompt hope.

C1.1: this LLM path is ENRICHMENT ONLY. The default ``/today`` response is the
deterministic fast path in ``today_fast.py`` — this module is only reached
when a client explicitly requests an LLM provider (eval/measurement).

Read-only by construction: this module performs no DB writes. The only write
in the whole TODAY surface is the optional ``copilot_runs`` audit row, written
by the router (reusing A+B ``log_context_run``).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.user import User
from app.services.copilot import llm
from app.services.copilot.prompts import build_today_messages
from app.services.copilot.ranking import rank_items
from app.services.copilot.shared import (
    LatencyBreakdown,
    TodayItem,
    cap_summary,
    clean_human_text,
    fallback_item,
    grounded_refs,
    parse_json_object,
)
from app.services.operations.copilot import build_copilot_context
from app.services.operations.timeclock import clock

TODAY_MAX_ITEMS = 3
SUMMARY_MAX_SENTENCES = 2
SUMMARY_MAX_CHARS = 320
HUMAN_TEXT_MAX_CHARS = 240

# Backward-compatible aliases (shared module is the source of truth in C1.1).
_clean_human_text = clean_human_text
_cap_summary = cap_summary
_grounded_refs = grounded_refs
_fallback_item = fallback_item


class TodayError(RuntimeError):
    """Structured, fail-closed TODAY failure (never a fabricated answer)."""


class TodayParseError(TodayError):
    """The provider returned text that is not a usable TODAY JSON object."""


@dataclass(frozen=True)
class TodayResult:
    top_items: list[TodayItem]
    summary: str
    context_schema_version: str
    provider: str
    model: str
    latency_ms: int
    flags: list[str] = field(default_factory=list)
    deterministic_top_refs: list[str] = field(default_factory=list)
    context: dict = field(default_factory=dict)
    latency: LatencyBreakdown = field(default_factory=LatencyBreakdown)
    enriched: bool = True


def _extract_json(text: str) -> dict:
    """Robustly extract a JSON object from a provider's raw text."""
    try:
        return parse_json_object(text)
    except ValueError as exc:
        raise TodayParseError(str(exc)) from exc


def build_today(
    db: Session,
    user: User,
    provider: str | None = None,
    *,
    client: llm.LLMClient | None = None,
    now=None,
) -> TodayResult:
    """Ground, call the LLM, and post-validate TODAY's brief (fail-closed).

    ``client`` is injectable for tests; defaults to the named/env provider.
    Raises ``llm.LLMProviderError`` (router maps to 503) or ``TodayParseError``.
    """
    import time

    t_start = time.monotonic()
    t_ctx = time.monotonic()
    now = now if now is not None else clock.now()
    context = build_copilot_context(db, user, now=now)
    context_build_ms = int((time.monotonic() - t_ctx) * 1000)
    schema_version = str(context.get("context_schema_version", "1.0"))
    t_ground = time.monotonic()
    grounded = _grounded_refs(context)
    grounding_ms = int((time.monotonic() - t_ground) * 1000)
    t_rank = time.monotonic()
    det_ranked = rank_items(context)
    det_top = det_ranked[:TODAY_MAX_ITEMS]
    det_top_refs = [item.item_ref for item in det_top]
    priority_ms = int((time.monotonic() - t_rank) * 1000)

    if client is None:
        client = llm.get_llm_client(provider)
    messages = build_today_messages(context)
    t_llm = time.monotonic()
    result = client.complete(
        messages,
        temperature=0.2,
        max_tokens=8000,  # reasoning models consume tokens on reasoning_content;
        # 4000 was too low for the real grounded prompt (empty content); 8000 fits reasoning + JSON.
        response_format={"type": "json_object"},
    )
    llm_ms = int((time.monotonic() - t_llm) * 1000)

    t_render = time.monotonic()
    parsed = _extract_json(result.text)
    raw_items = parsed.get("top_items") if isinstance(parsed.get("top_items"), list) else []
    raw_summary = str(parsed.get("summary") or "")

    flags: list[str] = []
    top_items: list[TodayItem] = []
    seen: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        ref = str(raw.get("item_ref") or "").strip()
        if not ref:
            flags.append("missing_ref")
            continue
        if ref not in grounded:
            flags.append(f"hallucination:{ref}")
            continue
        if ref not in det_top_refs:
            flags.append(f"rank_violation:{ref}")
            continue
        if ref in seen:
            flags.append(f"duplicate:{ref}")
            continue
        seen.add(ref)
        if len(top_items) < TODAY_MAX_ITEMS:
            top_items.append(
                TodayItem(
                    item_ref=ref,
                    reason_why_important=_clean_human_text(
                        str(raw.get("reason_why_important") or "")
                    ),
                    suggested_action=_clean_human_text(
                        str(raw.get("suggested_action") or "")
                    ),
                )
            )
        else:
            # keep validating the rest (hallucinations/duplicates still flag)
            flags.append("truncated_extra_items")

    # Backfill from the deterministic ranking so the brief always shows the
    # real top risks even when the model under-reports.
    for ranked in det_top:
        if len(top_items) >= TODAY_MAX_ITEMS:
            break
        if ranked.item_ref in seen:
            continue
        top_items.append(_fallback_item(ranked))
        seen.add(ranked.item_ref)
        flags.append(f"backfilled:{ranked.item_ref}")

    summary, truncated = _cap_summary(raw_summary)
    if truncated:
        flags.append("summary_truncated")
    render_ms = int((time.monotonic() - t_render) * 1000)

    latency = LatencyBreakdown(
        context_build_ms=context_build_ms,
        priority_ms=priority_ms,
        grounding_ms=grounding_ms,
        llm_ms=llm_ms,
        render_ms=render_ms,
        total_ms=int((time.monotonic() - t_start) * 1000),
    )
    return TodayResult(
        top_items=top_items,
        summary=summary,
        context_schema_version=schema_version,
        provider=result.provider,
        model=result.model,
        latency_ms=result.latency_ms,
        flags=flags,
        deterministic_top_refs=det_top_refs,
        context=context,
        latency=latency,
        enriched=True,
    )
