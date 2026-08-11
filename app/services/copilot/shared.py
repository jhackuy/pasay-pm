"""Shared deterministic helpers + latency instrumentation (V1.2.2 C1.1).

Dependency-free pieces reused by the fast TODAY / WHY / ASK layers:

- ``clean_human_text`` / ``cap_summary`` — the C1 server-side post-validation
  every LLM-produced string goes through (backend refs stripped, JSON/control
  artifacts removed, hard caps enforced).
- ``grounded_refs`` / ``collect_grounded_facts`` / ``ground_text`` — grounded
  entity refs plus the amount/date facts the LLM is allowed to cite; any
  amount/date in LLM text that is NOT resolvable in the grounded context is
  stripped and flagged (never shown as a financial fact).
- ``default_action`` / ``fallback_item`` — deterministic per-kind suggested
  action; ``RankedItem.reason`` is the source of truth for fallback text.
- ``LatencyBreakdown`` — the structured timing object returned and logged by
  every copilot endpoint (``context_build_ms``, ``priority_ms``,
  ``grounding_ms``, ``llm_ms``, ``render_ms``, ``total_ms``).

Read-only by construction: no DB access, no writes, no provider calls.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.services.copilot.ranking import RankedItem

HUMAN_TEXT_MAX_CHARS = 240
SUMMARY_MAX_SENTENCES = 2
SUMMARY_MAX_CHARS = 320

# Backend refs (task:12, lease:3, ...) must never leak into displayed text.
_REF_PATTERN = re.compile(
    r"\b(task|lease|property|expense|income|settlement|rule|tenant):\d+\b"
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_JSON_ARTIFACTS = re.compile(r"[{}\[\]]")


@dataclass(frozen=True)
class TodayItem:
    """One brief item (deterministic by default in C1.1)."""

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
class LatencyBreakdown:
    """Structured timing breakdown exposed in every copilot response."""

    context_build_ms: int = 0
    priority_ms: int = 0
    grounding_ms: int = 0
    llm_ms: int = 0
    render_ms: int = 0
    total_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "context_build_ms": self.context_build_ms,
            "priority_ms": self.priority_ms,
            "grounding_ms": self.grounding_ms,
            "llm_ms": self.llm_ms,
            "render_ms": self.render_ms,
            "total_ms": self.total_ms,
        }


def clean_human_text(value: str, *, max_chars: int = HUMAN_TEXT_MAX_CHARS) -> str:
    """Sanitize displayed text: canonicalize, strip control/JSON artifacts,
    and remove backend entity-ref tokens (``task:12`` -> ``task``)."""
    text = _CONTROL_CHARS.sub("", str(value))
    text = _REF_PATTERN.sub(lambda m: m.group(1), text)
    text = _JSON_ARTIFACTS.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars].rstrip() if len(text) > max_chars else text


def cap_summary(text: str) -> tuple[str, bool]:
    """Enforce summary <= 2 sentences and a hard char cap (server-side)."""
    clean = clean_human_text(text, max_chars=SUMMARY_MAX_CHARS + 40)
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


# Sections whose rows carry their own ``reference`` field (grounded entities).
_REF_SECTIONS = (
    "pending_tasks", "overdue_rents", "leases_expiring",
    "pending_expense_approvals", "pending_settlements",
    "maintenance_tasks", "recurring_rules", "properties", "tenants",
)


def grounded_refs(context: dict) -> set[str]:
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


def parse_json_object(text: str) -> dict:
    """Robustly extract a JSON object from a provider's raw text.

    Raises ``ValueError`` when the text contains no usable JSON object.
    """
    import json

    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("provider response contained no JSON object")
    try:
        parsed = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"provider response was not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("provider response JSON is not an object")
    return parsed


def default_action(ranked: RankedItem) -> str:
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


def fallback_item(ranked: RankedItem) -> TodayItem:
    """Deterministic TodayItem from a ranked item (reason is ground truth)."""
    return TodayItem(
        item_ref=ranked.item_ref,
        reason_why_important=ranked.reason,
        suggested_action=default_action(ranked),
    )


# ---------------------------------------------------------------------------
# Grounded-fact validation (forbid invented amounts/dates in LLM output)
# ---------------------------------------------------------------------------

# Context row keys that hold money (structured, ground truth for amounts).
_AMOUNT_KEYS = frozenset({
    "amount", "computed_amount", "amount_per_month", "total_outstanding",
    "monthly_rent", "deposit", "total",
})
# Context row keys that hold dates/datetimes (ground truth for dates).
_DATE_KEYS = frozenset({
    "end_date", "start_date", "due_date", "due_at", "received_date",
    "next_run_at", "created_at", "expires_at", "confirmed_at",
    "oldest_due_date", "expense_date",
})

_CURRENCY = r"(?:₱|PHP)"
# Money-like tokens: currency-prefixed, comma-grouped, or 2-decimal numbers.
# Un-grouped digits (e.g. ``PHP 36000.00``) are allowed in full after the
# currency marker; bare integers without decimals/commas are NOT treated as
# money (counts like "3 overdue months" stay untouched).
_AMOUNT_RE = re.compile(
    rf"{_CURRENCY}\s*\d+(?:,\d{{3}})*(?:\.\d{{1,2}})?"
    rf"|\d{{1,3}}(?:,\d{{3}})+\.\d{{2}}"
    rf"|\d{{1,3}}(?:,\d{{3}})+"
    rf"|\d+\.\d{{2}}"
    rf"|\d+(?:,\d{{3}})*(?:\.\d{{1,2}})?\s*{_CURRENCY}",
)
# Date-like tokens: ISO dates, CJK full dates, or CJK month-day references.
_DATE_RE = re.compile(
    r"\d{4}-\d{1,2}-\d{1,2}"
    r"|\d{4}年\d{1,2}月\d{1,2}日"
    r"|\d{1,2}月\d{1,2}日"
)


@dataclass(frozen=True)
class GroundedFacts:
    """The set of amounts/dates the LLM is allowed to cite (context ground)."""

    amounts: frozenset[Decimal] = frozenset()
    dates: frozenset[str] = frozenset()       # ISO ``YYYY-MM-DD``
    date_suffixes: frozenset[str] = frozenset()  # ``MM-DD`` for 月日 refs

    def has_amount(self, value: Decimal) -> bool:
        return value in self.amounts

    def has_date(self, iso: str | None, suffix: str | None) -> bool:
        if iso is not None and iso in self.dates:
            return True
        if suffix is not None and suffix in self.date_suffixes:
            return True
        return False


def _normalize_amount(raw: str) -> Decimal | None:
    digits = re.sub(r"[^0-9.]", "", raw)
    try:
        return Decimal(digits)
    except InvalidOperation:
        return None


def _normalize_date(raw: str) -> tuple[str | None, str | None]:
    """Return ``(YYYY-MM-DD or None, MM-DD suffix or None)`` from a match."""
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if m:
        iso = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        return iso, iso[5:]
    m = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日", raw)
    if m:
        iso = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        return iso, iso[5:]
    m = re.fullmatch(r"(\d{1,2})月(\d{1,2})日", raw)
    if m:
        return None, f"{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return None, None


def collect_grounded_facts(context: dict) -> GroundedFacts:
    """Collect every amount/date the grounded context actually contains.

    Walks the structured A+B context rows; free text is never scanned (text is
    DATA, not ground truth). The context's own ``current_time`` date is
    included so "as of <today>" citations validate.
    """
    amounts: set[Decimal] = set()
    dates: set[str] = set()
    suffixes: set[str] = set()

    for section in _REF_SECTIONS:
        for row in context.get(section) or []:
            if not isinstance(row, dict):
                continue
            for key, value in row.items():
                if key in _AMOUNT_KEYS and value not in (None, ""):
                    try:
                        amounts.add(Decimal(str(value)))
                    except InvalidOperation:
                        continue
                if key in _DATE_KEYS and value not in (None, ""):
                    iso, suffix = _normalize_date(str(value)[:10])
                    if iso:
                        dates.add(iso)
                        suffixes.add(iso[5:])
                    elif suffix:
                        suffixes.add(suffix)
    for key in ("current_time", "generated_at"):
        raw = str(context.get(key) or "")
        if raw:
            iso, suffix = _normalize_date(raw[:10])
            if iso:
                dates.add(iso)
                suffixes.add(iso[5:])
    return GroundedFacts(
        amounts=frozenset(amounts),
        dates=frozenset(dates),
        date_suffixes=frozenset(suffixes),
    )


def ground_text(
    text: str,
    facts: GroundedFacts,
    *,
    max_chars: int = HUMAN_TEXT_MAX_CHARS,
) -> tuple[str, list[str]]:
    """Post-validate LLM text against the grounded facts.

    - backend refs / JSON / control chars are removed (``clean_human_text``);
    - any amount not present in the grounded context is removed + flagged;
    - any date not present in the grounded context is removed + flagged;
    - hard char cap applied at the end.

    Returns ``(clean_text, flags)``. Never fabricates: ungrounded facts are
    dropped, never replaced.
    """
    flags: list[str] = []

    def _amount_sub(match: re.Match) -> str:
        value = _normalize_amount(match.group(0))
        if value is not None and facts.has_amount(value):
            return match.group(0)
        flags.append("ungrounded_amount")
        return ""

    def _date_sub(match: re.Match) -> str:
        iso, suffix = _normalize_date(match.group(0))
        if facts.has_date(iso, suffix):
            return match.group(0)
        flags.append("ungrounded_date")
        return ""

    clean = clean_human_text(text, max_chars=max_chars + 80)
    clean = _AMOUNT_RE.sub(_amount_sub, clean)
    clean = _DATE_RE.sub(_date_sub, clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    if len(clean) > max_chars:
        clean = clean[: max_chars - 1].rstrip() + "…"
    return clean, flags


