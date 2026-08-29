"""Idempotency key normalization and payload hashing.

AGENTS.md §4: idempotent requests return same result on retry. The
composite key (org_id, key, kind) is enforced by IdempotencyMixin in
app/db/base.py. This module provides the canonical normalization +
hash used by services and the middleware layer.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


class IdempotencyConflictError(ValueError):
    """Raised when the same idempotency key is reused with a different payload."""


def normalize_idempotency_key(raw: Any) -> str:
    """Normalize an idempotency key to a canonical string.

    - Reject empty / non-str input.
    - Trim whitespace.
    - Lowercase.
    - Cap at 128 chars (matches IdempotencyMixin.key column).
    """
    if not isinstance(raw, str):
        raise IdempotencyConflictError(
            f"idempotency key must be str, got {type(raw).__name__}"
        )
    key = raw.strip().lower()
    if not key:
        raise IdempotencyConflictError(
            "idempotency key is empty after normalization"
        )
    if len(key) > 128:
        key = key[:128]
    return key


def compute_payload_hash(payload: Mapping[str, Any]) -> str:
    """Compute a stable SHA-256 hash of a JSON payload.

    Keys are sorted for determinism. Default serializer coerces non-JSON
    scalars via str() (services should pre-normalize datetimes to ISO).
    """
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()