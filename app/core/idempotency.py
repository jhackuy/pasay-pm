"""Idempotency key normalization and payload hashing.

AGENTS.md §4: idempotent requests return same result on retry. The
composite key (org_id, key, kind) is enforced by IdempotencyMixin in
app/db/base.py. This module provides the canonical normalization +
hash used by services and the middleware layer.

Owner invariant (PR #100 review): opaque client idempotency keys MUST
preserve case and MUST be rejected (not truncated) when they exceed
the storage cap. Two client requests that differ only in case are
distinct idempotency keys; silent normalization would let one erase
the other. Silent truncation would let a malicious or buggy client
collapse distinct keys into the same stored value.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


class IdempotencyConflictError(ValueError):
    """Raised when an idempotency key is invalid or reused with a different payload."""


# Matches IdempotencyMixin.key column width in app/db/base.py.
MAX_IDEMPOTENCY_KEY_LEN = 128


def normalize_idempotency_key(raw: Any) -> str:
    """Canonicalize an idempotency key.

    Rules (Owner-mandated):
    - Reject non-str input with IdempotencyConflictError.
    - Strip leading/trailing whitespace.
    - Preserve case exactly (NO lowercasing — opaque client keys must
      not collide with their case variants).
    - Reject empty keys after trim.
    - Reject keys longer than MAX_IDEMPOTENCY_KEY_LEN (no truncation).
    """
    if not isinstance(raw, str):
        raise IdempotencyConflictError(
            f"idempotency key must be str, got {type(raw).__name__}"
        )
    key = raw.strip()
    if not key:
        raise IdempotencyConflictError(
            "idempotency key is empty after normalization"
        )
    if len(key) > MAX_IDEMPOTENCY_KEY_LEN:
        raise IdempotencyConflictError(
            f"idempotency key too long: {len(key)} chars exceeds "
            f"max {MAX_IDEMPOTENCY_KEY_LEN}"
        )
    return key


def compute_payload_hash(payload: Mapping[str, Any]) -> str:
    """Compute a stable SHA-256 hex digest of a JSON payload.

    Keys are sorted for determinism. The default JSON serializer
    coerces non-JSON scalars via str(); services should pre-normalize
    datetimes to ISO-8601 strings before passing in.
    """
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
