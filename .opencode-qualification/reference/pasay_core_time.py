"""PASAY reference implementation — time helpers.

Issue #99 / PR #100 /oc continuation. Staged under
``.opencode-qualification/reference/`` pending Owner-side promotion to
``app/core/time.py``.

Hard invariants enforced by this module:
    * Every datetime produced or accepted by this module is timezone-aware
      and in UTC. Naive datetimes are rejected at runtime via
      ``assert_utc`` / :class:`NaiveDatetimeError`.
    * All persisted timestamps are serialised as ISO-8601 with explicit
      ``+00:00`` offset via :func:`format_iso`.
    * :func:`utcnow` is the SINGLE clock used by the application; do NOT
      call ``datetime.utcnow()`` (deprecated, naive) elsewhere.

Reference promotion to ``app/core/time.py`` requires no behavioural change.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

ISO_FORMAT = "%Y-%m-%dT%H:%M:%S.%f+00:00"
ISO_FORMAT_SEC = "%Y-%m-%dT%H:%M:%S+00:00"


class NaiveDatetimeError(ValueError):
    """Raised when a naive (tz-less) datetime is supplied to a UTC API."""


def utcnow() -> datetime:
    """Return the current UTC time as a timezone-aware datetime.

    This is the canonical clock for the entire application. NEVER call
    ``datetime.utcnow()`` directly; it is naive and violates the AGENTS.md
    §4 ``timestamptz / timezone.utc`` invariant.
    """
    return datetime.now(tz=timezone.utc)


def assert_utc(value: datetime, *, field_name: str = "datetime") -> datetime:
    """Validate that ``value`` is timezone-aware and in UTC.

    Returns ``value`` unchanged on success. Raises
    :class:`NaiveDatetimeError` on naive input or wrong tz.
    """
    if not isinstance(value, datetime):
        raise NaiveDatetimeError(
            f"{field_name}: expected datetime, got {type(value).__name__}"
        )
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise NaiveDatetimeError(
            f"{field_name}: naive datetime is forbidden; use utcnow() "
            "or attach timezone.utc"
        )
    offset = value.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise NaiveDatetimeError(
            f"{field_name}: non-UTC offset {offset}; convert to UTC first"
        )
    return value


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 string into a UTC timezone-aware datetime.

    Accepts strings with ``+00:00``, ``Z``, or numeric offsets. Always
    returns a datetime converted to UTC.
    """
    if not isinstance(value, str):
        raise NaiveDatetimeError(f"expected ISO-8601 string, got {type(value).__name__}")
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise NaiveDatetimeError(f"unparseable ISO-8601: {value!r}") from exc
    if parsed.tzinfo is None:
        raise NaiveDatetimeError(
            f"ISO string lacks timezone offset: {value!r}; AGENTS.md §4 requires UTC"
        )
    return parsed.astimezone(timezone.utc)


def format_iso(value: datetime, *, with_microseconds: bool = True) -> str:
    """Format a datetime as ISO-8601 in UTC with explicit ``+00:00`` offset.

    The DB column is ``timestamptz``; PostgreSQL normalises to UTC on storage
    regardless of input offset, but every value produced at the Python layer
    MUST round-trip through this function so the wire format is stable.
    """
    assert_utc(value)
    fmt = ISO_FORMAT if with_microseconds else ISO_FORMAT_SEC
    return value.strftime(fmt)


def to_utc(value: datetime) -> datetime:
    """Convert any tz-aware datetime to UTC. Rejects naive input."""
    assert_utc(value)
    return value.astimezone(timezone.utc)


def add_seconds(value: datetime, seconds: float) -> datetime:
    """Add ``seconds`` (may be fractional) to a UTC datetime. Rejects naive."""
    assert_utc(value)
    from datetime import timedelta

    return value + timedelta(seconds=seconds)


__all__ = [
    "ISO_FORMAT",
    "ISO_FORMAT_SEC",
    "NaiveDatetimeError",
    "utcnow",
    "assert_utc",
    "parse_iso",
    "format_iso",
    "to_utc",
    "add_seconds",
]