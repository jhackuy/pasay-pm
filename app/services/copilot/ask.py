"""On-demand Q&A enrichment (V1.2.2 Phase C1.1 Deliverable C).

``ask_question`` grounds to the current deterministic A+B context and calls
the ASK profile (centralized provider map — strong model by default).

Deterministic guard: LLM output is post-validated server-side — any amount or
date that is NOT resolvable in the grounded context is stripped and flagged,
backend refs are removed, and nothing is ever executed or written. On
provider-down the endpoint returns a friendly deterministic fallback, never a
fabricated answer.

Read-only by construction: no DB writes here; the optional ``copilot_runs``
audit row is written by the router.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.user import User
from app.services.copilot import llm
from app.services.copilot.prompts import build_ask_messages
from app.services.copilot.shared import (
    LatencyBreakdown,
    collect_grounded_facts,
    ground_text,
    parse_json_object,
)
from app.services.operations.copilot import build_copilot_context
from app.services.operations.timeclock import clock

ASK_MAX_TOKENS = 2000
FALLBACK_MODEL = "deterministic-fallback"
# Friendly deterministic fallback when the provider is down (Requirement 8):
# never fabricated, points the user at the deterministic surfaces.
FALLBACK_ANSWER = "运营助手暂时无法联网分析，请重试或到 /overdue、/finance 查看。"


class AskError(RuntimeError):
    """Structured ASK failure (never a fabricated answer)."""


class AskQuestionRequired(AskError):
    """The question body was empty."""


@dataclass(frozen=True)
class AskResult:
    answer: str
    provider: str
    model: str
    latency_ms: int
    fallback: bool
    flags: list[str] = field(default_factory=list)
    latency: LatencyBreakdown = field(default_factory=LatencyBreakdown)
    context: dict = field(default_factory=dict)


def ask_question(
    db: Session,
    user: User,
    question: str,
    provider: str | None = None,
    *,
    client: llm.LLMClient | None = None,
    now=None,
) -> AskResult:
    """Answer a grounded question (LLM-backed, deterministic fail-closed)."""
    if not question or not str(question).strip():
        raise AskQuestionRequired("question must not be empty")
    t_start = time.monotonic()
    now = now if now is not None else clock.now()

    t_ctx = time.monotonic()
    context = build_copilot_context(db, user, now=now)
    context_build_ms = int((time.monotonic() - t_ctx) * 1000)

    t_ground = time.monotonic()
    facts = collect_grounded_facts(context)
    grounding_ms = int((time.monotonic() - t_ground) * 1000)

    resolved = llm.profile_provider("ASK", provider)
    messages = build_ask_messages(context, question)

    t_llm = time.monotonic()
    try:
        if client is None:
            client = llm.get_llm_client(resolved)
        result = client.complete(
            messages,
            temperature=0.2,
            max_tokens=ASK_MAX_TOKENS,
            response_format={"type": "json_object"},
        )
        llm_ms = int((time.monotonic() - t_llm) * 1000)
        parsed = parse_json_object(result.text)
        raw_answer = str(parsed.get("answer") or "")
    except (llm.LLMProviderError, ValueError) as exc:
        llm_ms = int((time.monotonic() - t_llm) * 1000)
        t_render = time.monotonic()
        render_ms = int((time.monotonic() - t_render) * 1000)
        return AskResult(
            answer=FALLBACK_ANSWER,
            provider=resolved or "deterministic",
            model=FALLBACK_MODEL,
            latency_ms=int((time.monotonic() - t_start) * 1000),
            fallback=True,
            flags=["provider_error"],
            latency=LatencyBreakdown(
                context_build_ms=context_build_ms,
                grounding_ms=grounding_ms,
                llm_ms=llm_ms,
                render_ms=render_ms,
                total_ms=int((time.monotonic() - t_start) * 1000),
            ),
            context=context,
        )

    t_render = time.monotonic()
    answer, flags = ground_text(raw_answer, facts, max_chars=600)
    if not answer:
        answer = FALLBACK_ANSWER
        flags.append("empty_answer")
    render_ms = int((time.monotonic() - t_render) * 1000)

    return AskResult(
        answer=answer,
        provider=result.provider,
        model=result.model,
        latency_ms=result.latency_ms,
        fallback=False,
        flags=flags,
        latency=LatencyBreakdown(
            context_build_ms=context_build_ms,
            grounding_ms=grounding_ms,
            llm_ms=llm_ms,
            render_ms=render_ms,
            total_ms=int((time.monotonic() - t_start) * 1000),
        ),
        context=context,
    )
