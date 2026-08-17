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
from contextvars import ContextVar
from typing import Any, Optional


class PhaseProbe:
    """Per-callback phase accumulator (PASAY-AI-EMPLOYEE-FOUNDATION-007A §A).

    A fresh probe is bound per callback; the shared render/edit and backend
    request helpers add their stage times into it so ``handle_callback`` can
    record the honest breakdown (callback_ack_ms / backend_fetch_ms /
    render_ms / telegram_edit_ms / total_ms) instead of one opaque total.
    """

    __slots__ = ("_started", "callback_ack_ms", "backend_fetch_ms",
                 "render_ms", "telegram_edit_ms")

    def __init__(self) -> None:
        self._started = time.monotonic()
        self.callback_ack_ms = 0.0
        self.backend_fetch_ms = 0.0
        self.render_ms = 0.0
        self.telegram_edit_ms = 0.0

    def mark_ack(self) -> None:
        self.callback_ack_ms = (time.monotonic() - self._started) * 1000

    def add_backend(self, ms: float) -> None:
        self.backend_fetch_ms += ms

    def add_render(self, ms: float) -> None:
        self.render_ms += ms

    def add_telegram(self, ms: float) -> None:
        self.telegram_edit_ms += ms

    def total(self) -> float:
        return (time.monotonic() - self._started) * 1000


_phase: ContextVar[Optional[PhaseProbe]] = ContextVar("pasay_phase_probe", default=None)


def bind_phase(probe: PhaseProbe) -> None:
    _phase.set(probe)


def current_phase() -> Optional[PhaseProbe]:
    return _phase.get()


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

    def record_phases(
        self,
        kind: str,
        label: str,
        *,
        callback_ack_ms: float = 0.0,
        backend_fetch_ms: float = 0.0,
        render_ms: float = 0.0,
        telegram_edit_ms: float = 0.0,
        total_ms: float = 0.0,
        outcome: str = "ok",
        detail: str = "",
    ) -> None:
        """Record a callback's PASAY-AI-EMPLOYEE-FOUNDATION-007A phase profile.

        Each deterministic callback reports its stages so the true bottleneck
        is visible (callback_ack_ms / backend_fetch_ms / render_ms /
        telegram_edit_ms / total_ms) instead of being hidden behind a single
        elapsed_ms or a cache. ``callback_ack_ms`` is the Telegram ACK latency
        (server-side target <300ms), ``backend_fetch_ms`` the API round-trips,
        ``render_ms`` the card build, ``telegram_edit_ms`` the edit send."""
        sample = {
            "kind": kind,
            "label": label,
            "callback_ack_ms": round(float(callback_ack_ms), 3),
            "backend_fetch_ms": round(float(backend_fetch_ms), 3),
            "render_ms": round(float(render_ms), 3),
            "telegram_edit_ms": round(float(telegram_edit_ms), 3),
            "total_ms": round(float(total_ms), 3),
            "elapsed_ms": round(float(total_ms), 3),
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
