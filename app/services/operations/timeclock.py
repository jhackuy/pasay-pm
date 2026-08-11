"""Manila-aware clock boundary (V1.2.2 Phase C1).

Every NEW C1 time calculation (TODAY buckets, overdue/upcoming windows,
lease-expiry windows, eval fixtures) MUST go through the module singleton
``clock`` — never ``datetime.now()`` / ``date.today()`` directly. Older
operations modules keep their own time handling this phase.

Timezone semantics:
- Asia/Manila is fixed UTC+08:00 year-round (the Philippines observes NO DST),
  so a Manila-local instant is always ``utc + 8h`` and never shifts by season.
- ``Clock.now()`` returns a tz-aware ``datetime`` with ``ZoneInfo("Asia/Manila")``.
- ``Clock.set_override(now)`` freezes ``now()`` for deterministic tests; pass
  ``None`` to reset. The override may be UTC-aware, Manila-aware, or naive
  (naive is interpreted as UTC, matching the A+B convention), and is always
  normalized to Manila before being returned. Thread-safe via a lock.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

MANILA_TZ = ZoneInfo("Asia/Manila")
_UTC = timezone.utc


def _to_manila(value: datetime) -> datetime:
    """Normalize any input datetime to an aware Asia/Manila datetime."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=_UTC)
    return value.astimezone(MANILA_TZ)


class Clock:
    """Single time boundary for all new C1 code (thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._override: datetime | None = None

    def now(self) -> datetime:
        """Current wall-clock time in Asia/Manila (tz-aware), or the override."""
        with self._lock:
            if self._override is not None:
                return _to_manila(self._override)
            return datetime.now(MANILA_TZ)

    def date(self):
        """Today's date in Asia/Manila."""
        return self.now().date()

    def set_override(self, now: datetime | None) -> None:
        """Freeze time for tests (``None`` resets to the real clock)."""
        with self._lock:
            self._override = now


# Module singleton — import and use everywhere in new C1 code.
clock = Clock()
