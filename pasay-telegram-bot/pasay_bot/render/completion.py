"""Deterministic completion-feedback selector (PASAY-V2-OWNER-SECRETARY-
JOURNEY-AUDIT-006, Journey M).

Human completion must not end with a cold mechanical state transition.
Every completion path still reports the deterministic facts (what closed), but
the framing line is drawn from a small in-process pool of safe, respectful,
concise templates per category, cycling to avoid repeating the same one back-
to-back. No LLM is involved — selecting a template is pure code.

Each template keeps the factual headline separate from the varied closing
line, so the facts never change between variants and no fabricated praise is
added. ``context.bot_data["completion_recent"]`` stores the last used
template keys (resets on process restart, which is the allowed scope).

Callers render the deterministic headline themselves and pass ``locale`` +
the business title/amount; this module returns the varied human line.
"""
from __future__ import annotations

from typing import Optional

# Each pool is a list of (key, template). The template is the finite set of
# human closing lines the caller appends AFTER the factual headline. Keep them
# short, positive, not exaggerated, and never fabricated.
_TEMPLATES: dict[str, list[tuple[str, str]]] = {
    # zh (Owner) pools
    "zh.task": [
        ("t1", "已完成，干得好。" ),
        ("t2", "已办结 ✓"),
        ("t3", "这件事闭环了。"),
        ("t4", "处理完成，辛苦了。"),
        ("t5", "任务已收起。"),
        ("t6", "已处理妥当。"),
    ],
    "zh.payment": [
        ("p1", "付款已完成 ✓"),
        ("p2", "已支付，账目已更新。"),
        ("p3", "付款手续办妥了。"),
        ("p4", "已结清，谢谢确认。"),
        ("p5", "支出已完成。"),
    ],
    "zh.rent": [
        ("r1", "已入账。"),
        ("r2", "租金已登记 ✓"),
    ],
    # en (Secretary) pools
    "en.task": [
        ("t1", "Done — nice work." ),
        ("t2", "All closed out."),
        ("t3", "Task wrapped up."),
        ("t4", "That's handled."),
        ("t5", "Complete — thank you."),
        ("t6", "Closed and recorded."),
    ],
    "en.payment": [
        ("p1", "Payment done."),
        ("p2", "Paid and recorded."),
        ("p3", "Payment complete."),
    ],
    "en.rent": [
        ("r1", "Recorded."),
        ("r2", "Rent registered ✓"),
    ],
    # bi (group) — bilingual variants
    "bi.task": [
        ("t1", "Done / 已完成"),
        ("t2", "Closed / 已闭环"),
        ("t3", "Complete / 已完成"),
    ],
    "bi.payment": [
        ("p1", "Paid / 已付款"),
        ("p2", "Payment done / 付款完成"),
    ],
    "bi.rent": [
        ("r1", "Recorded / 已登记"),
    ],
}

_RECENT_LIMIT = 5


def _pool_key(locale: str, category: str) -> str:
    return f"{locale}.{category}"


def select(locale: str, category: str, recent: Optional[set] = None) -> tuple[str, str]:
    """Return ``(key, template)`` for the locale+category, avoiding the
    ``_RECENT_LIMIT`` most recently used keys in ``recent`` (caller keeps it in
    ``context.bot_data``). Falls back to the first template when every variant
    was recently used, and an empty template when no pool exists."""
    pool = _TEMPLATES.get(_pool_key(locale, category))
    if not pool:
        return "", ""
    recent = recent or set()
    unused = [(k, tmpl) for k, tmpl in pool if k not in recent]
    if not unused:
        unused = pool
    return unused[0]


def line(locale: str, category: str, recent: Optional[set] = None) -> str:
    """Shorthand for callers that only need the text."""
    return select(locale, category, recent)[1]
