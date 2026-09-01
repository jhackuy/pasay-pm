"""UTC time helpers.

AGENTS.md §4: Time = timestamptz / UTC-aware datetime. NEVER naive.

API:
- utcnow() -> datetime: current UTC time (tz-aware).
- assert_utc(dt) -> datetime: raise if naive or non-UTC.
- to_utc(dt) -> datetime: convert any tz-aware datetime to UTC.
- parse_iso(s) -> datetime: parse ISO-8601 string, ensure tz-aware.
- format_iso(dt) -> str: format as ISO-8601 with UTC tz.
"""
from __future__ import annotations

from datetime import datetime, timezone


class NaiveDatetimeError(ValueError):
    """Raised when a naive (tz-less) datetime is used."""


def utcnow() -> datetime:
    """Return the current UTC time as a tz-aware datetime."""
    return datetime.now(tz=timezone.utc)


def assert_utc(dt: datetime) -> datetime:
    """Ensure dt is tz-aware and in UTC. Returns dt unchanged."""
    if dt.tzinfo is None:
        raise NaiveDatetimeError("naive datetime is not allowed (AGENTS.md §4)")
    if dt.utcoffset() != timezone.utc.utcoffset(dt):
        raise NaiveDatetimeError(
            f"non-UTC tz-aware datetime is not allowed: {dt.tzinfo!r}"
        )
    return dt


def to_utc(dt: datetime) -> datetime:
    """Convert any tz-aware datetime to UTC.

    Raises NaiveDatetimeError on naive input.
    """
    if dt.tzinfo is None:
        raise NaiveDatetimeError(
            "naive datetime cannot be converted (AGENTS.md §4)"
        )
    return dt.astimezone(timezone.utc)


def parse_iso(s: str) -> datetime:
    """Parse an ISO-8601 string and ensure the result is UTC-aware."""
    if not isinstance(s, str):
        raise NaiveDatetimeError(
            f"parse_iso requires str, got {type(s).__name__}"
        )
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as exc:
        raise NaiveDatetimeError(f"invalid ISO-8601 string: {s!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return to_utc(dt)


def format_iso(dt: datetime) -> str:
    """Format dt as ISO-8601 in UTC."""
    if not isinstance(dt, datetime):
        raise NaiveDatetimeError(
            f"format_iso requires datetime, got {type(dt).__name__}"
        )
    return to_utc(dt).isoformat()