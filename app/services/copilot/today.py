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

Read-only by construction: this module performs no DB writes. The only write
in the whole TODAY surface is the optional ``copilot_runs`` audit row, written
by the router (reusing A+B ``log_context_run``).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.user import User
from app.services.copilot import llm
from app.services.copilot.prompts import build_today_messages
from app.services.copilot.ranking import RankedItem, rank_items
from app.services.operations.copilot import build_copilot_context
from app.services.operations.timeclock import clock

TODAY_MAX_ITEMS = 3
SUMMARY_MAX_SENTENCES = 2
SUMMARY_MAX_CHARS = 320
HUMAN_TEXT_MAX_CHARS = 240

# Backend refs (task:12, lease:3, ...) must never leak into displayed text.
_REF_PATTERN = re.compile(
    r"\b(?:task|lease|property|expense|income|settlement|rule|tenant):\d+\b"
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_JSON_ARTIFACTS = re.compile(r"[{}\[\]]")


class TodayError(RuntimeError):
    """Structured, fail-closed TODAY failure (never a fabricated answer)."""


class TodayParseError(TodayError):
    """The provider returned text that is not a usable TODAY JSON object."""


@dataclass(frozen=True)
class TodayItem:
    item_ref: str
    reason_why_important: str
    suggested_action: str

    def to_dict(self) -> dict:
        return {
            "item_ref": self.item_ref,
            "reason_why_important": self.reason_why_important,
            "suggested_action": self.suggested_action,
        }


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


def _clean_human_text(value: str, *, max_chars: int = HUMAN_TEXT_MAX_CHARS) -> str:
    """Sanitize displayed text: canonicalize, strip control/JSON artifacts,
    and remove backend entity-ref tokens (``task:12`` -> ``task``)."""
    text = _CONTROL_CHARS.sub("", str(value))
    text = _REF_PATTERN.sub(lambda m: m.group(1), text)
    text = _JSON_ARTIFACTS.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars].rstrip() if len(text) > max_chars else text


def _cap_summary(text: str) -> tuple[str, bool]:
    """Enforce summary <= 2 sentences and a hard char cap (server-side)."""
    clean = _clean_human_text(text, max_chars=SUMMARY_MAX_CHARS + 40)
    if not clean:
        return "", False
    sentences = _SENTENCE_SPLIT.split(clean)
    truncated = False
    if len(sentences) > SUMMARY_MAX_SENTENCES:
        sentences = sentences[:SUMMARY_MAX_SENTENCES]
        truncated = True
    summary = " ".join(sentences).strip()
    if len(summary) > SUMMARY_MAX_CHARS:
        summary = summary[: SUMMARY_MAX_CHARS - 1].rstrip() + "…"
        truncated = True
    return summary, truncated


def _extract_json(text: str) -> dict:
    """Robustly extract a JSON object from a provider's raw text."""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise TodayParseError("provider response contained no JSON object")
    try:
        parsed = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise TodayParseError(f"provider response was not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise TodayParseError("provider response JSON is not an object")
    return parsed


# Sections whose rows carry their own ``reference`` field (grounded entities).
_REF_SECTIONS = (
    "pending_tasks", "overdue_rents", "leases_expiring",
    "pending_expense_approvals", "pending_settlements",
    "maintenance_tasks", "recurring_rules", "properties", "tenants",
)


def _grounded_refs(context: dict) -> set[str]:
    """Every grounded entity ref: the A+B ``references`` groups plus each
    section row's own ``reference`` field (maintenance tasks and recurring
    rules are grounded entities but are not listed in ``references``)."""
    refs: set[str] = set()
    for group in (context.get("references") or {}).values():
        for ref in group or []:
            refs.add(str(ref))
    for section in _REF_SECTIONS:
        for row in context.get(section) or []:
            if isinstance(row, dict) and row.get("reference"):
                refs.add(str(row["reference"]))
    return refs


def _fallback_item(ranked: RankedItem) -> TodayItem:
    return TodayItem(
        item_ref=ranked.item_ref,
        reason_why_important=ranked.reason,
        suggested_action=_default_action(ranked),
    )


def _default_action(ranked: RankedItem) -> str:
    """Deterministic, read-only suggested action per risk kind."""
    return {
        "severe_overdue_rent": "Follow up with the tenant on the outstanding rent",
        "overdue_rent": "Follow up with the tenant on the outstanding rent",
        "lease_expiring": "Review renewal or handover plan before the lease ends",
        "maintenance": "Schedule and track the pending maintenance work",
        "pending_task": "Process the pending operational task today",
        "expense_approval": "Review and approve or reject the pending expense",
        "settlement_pending": "Review the pending commission settlement",
        "recurring_rule": "Note the upcoming recurring task",
    }.get(ranked.kind, "Review and follow up today")


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
    now = now if now is not None else clock.now()
    context = build_copilot_context(db, user, now=now)
    schema_version = str(context.get("context_schema_version", "1.0"))
    grounded = _grounded_refs(context)
    det_ranked = rank_items(context)
    det_top = det_ranked[:TODAY_MAX_ITEMS]
    det_top_refs = [item.item_ref for item in det_top]

    if client is None:
        client = llm.get_llm_client(provider)
    messages = build_today_messages(context)
    result = client.complete(
        messages,
        temperature=0.2,
        max_tokens=4000,  # reasoning models consume tokens on reasoning_content
        response_format={"type": "json_object"},
    )

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
    )


