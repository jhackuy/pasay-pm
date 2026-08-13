"""Deterministic handler latency instrumentation (code-path only).

Measures how long each routed UI action takes inside the bot process and
keeps a small bounded in-memory sample window plus per-kind counters. No LLM
is ever involved: the tracker only records wall-clock elapsed_ms and a few
labels the routing code already knows. Tests assert the recorded samples /
counters exist and are numeric; they never ask a model to judge speed.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Any, Optional


class LatencyTracker:
    """Bounded per-process latency sampler.

    ``record`` appends one sample and bumps the kind counter. ``snapshot``
    returns a shallow copy of the samples for tests/metrics; ``counts``
    returns the aggregated per-kind counters.
    """

    def __init__(self, max_samples: int = 200):
        self._max_samples = max(1, int(max_samples))
        self._samples: deque[dict[str, Any]] = deque(maxlen=self._max_samples)
        self._counts: dict[str, int] = defaultdict(int)

    def record(
        self,
        kind: str,
        label: str,
        elapsed_ms: float,
        *,
        outcome: str = "ok",
        detail: str = "",
    ) -> None:
        sample = {
            "kind": kind,
            "label": label,
            "elapsed_ms": round(float(elapsed_ms), 3),
            "outcome": outcome,
            "detail": detail or "",
            "ts": time.time(),
        }
        self._counts[kind] += 1
        self._samples.append(sample)

    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    def snapshot(self) -> list[dict[str, Any]]:
        return list(self._samples)

    def last(self, kind: Optional[str] = None) -> Optional[dict[str, Any]]:
        if kind is None:
            return self._samples[-1] if self._samples else None
        for sample in reversed(self._samples):
            if sample["kind"] == kind:
                return sample
        return None
