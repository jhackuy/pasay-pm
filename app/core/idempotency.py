"""Idempotency key normalization and payload hashing.

AGENTS.md §4: idempotent requests return the same result on retry. The
composite key (org_id, key, kind) is enforced by IdempotencyMixin in
app/db/base.py. This module provides the canonical normalization +
hash used by services and middleware.

CANONICAL CONTRACT (Owner-mandated; no aliases, no fallbacks):
- Idempotency keys are opaque client-provided strings. They MUST be
  preserved verbatim — no case folding, no whitespace stripping, no
  truncation. Two requests that differ only in case, or only in
  leading/trailing whitespace, are distinct idempotency keys.
- Empty keys and whitespace-only keys are rejected as
  IdempotencyKeyError.
- Keys longer than MAX_IDEMPOTENCY_KEY_LEN are rejected as
  IdempotencyKeyError — never silently truncated.
- Reusing a key with a different payload raises IdempotencyConflictError.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


# Matches IdempotencyMixin.key column width in app/db/base.py.
MAX_IDEMPOTENCY_KEY_LEN = 128


class IdempotencyKeyError(ValueError):
    """Raised when an idempotency key is malformed (empty, oversize, non-str).

    Distinct from IdempotencyConflictError so callers can distinguish a
    client-input problem from a state-conflict problem.
    """


class IdempotencyConflictError(ValueError):
    """Raised when an idempotency key is reused with a different payload
    or already maps to a stored result.
    """


__all__ = [
    "MAX_IDEMPOTENCY_KEY_LEN",
    "IdempotencyKeyError",
    "IdempotencyConflictError",
    "compute_payload_hash",
    "normalize_idempotency_key",
]


def normalize_idempotency_key(raw: Any) -> str:
    """Validate and return an idempotency key verbatim (no normalization).

    Rules (canonical):
    - Must be a `str`.
    - Must not be empty.
    - Must not be whitespace-only.
    - Length must not exceed MAX_IDEMPOTENCY_KEY_LEN (rejected, NOT truncated).
    - Case is preserved exactly.
    - Leading/trailing whitespace is preserved exactly.

    Raises IdempotencyKeyError on any of the above. Returns the input
    string unchanged on success.
    """
    if not isinstance(raw, str):
        raise IdempotencyKeyError(
            f"idempotency key must be str, got {type(raw).__name__}"
        )
    if not raw:
        raise IdempotencyKeyError("idempotency key is empty")
    if raw.isspace():
        raise IdempotencyKeyError("idempotency key is whitespace-only")
    if len(raw) > MAX_IDEMPOTENCY_KEY_LEN:
        raise IdempotencyKeyError(
            f"idempotency key too long: {len(raw)} chars exceeds "
            f"max {MAX_IDEMPOTENCY_KEY_LEN}"
        )
    return raw


def compute_payload_hash(payload: Mapping[str, Any]) -> str:
    """Compute a stable SHA-256 hex digest of a JSON payload.

    Keys are sorted for determinism. Non-JSON scalars are coerced via
    `str()` so services should pre-normalize datetimes to ISO-8601 strings
    before passing in.
    """
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
