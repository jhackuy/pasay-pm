"""PASAY reference implementation — idempotency helpers.

Issue #99 / PR #100 /oc continuation. Staged under
``.opencode-qualification/reference/`` pending Owner-side promotion to
``app/core/idempotency.py``.

Hard invariants enforced by this module:
    * Idempotency keys are scoped by ``(principal, endpoint, payload_hash)``
      to prevent cross-tenant collisions when two different tenants happen
      to send the same opaque key.
    * Keys are normalised: stripped, length-checked (max 255 chars), and
      required to contain only ``[A-Za-z0-9._\-:]`` to keep them log-safe.
    * The canonical storage hash uses SHA-256 over the JSON-canonicalised
      payload. Sort keys + separators=(',', ':') + ensure_ascii=False.
    * A key seen a SECOND time with a DIFFERENT payload hash is rejected
      (HTTP 409 in the API layer) — duplicate VERIFIED income claims
      must surface, not silently no-op.

Reference promotion to ``app/core/idempotency.py`` requires no behavioural
change.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Optional

# Public header name. Routers read this off ``request.headers``.
HEADER_IDEMPOTENCY_KEY = "X-Idempotency-Key"

# Allowed characters in an idempotency key (after strip).
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._\-:]{1,255}$")

# Endpoints whose idempotency contract is mandatory. Other endpoints may opt
# in via the FastAPI middleware. Listing these here keeps the contract
# discoverable from one place.
MANDATORY_IDEMPOTENCY_ENDPOINTS: frozenset[str] = frozenset({
    "POST /api/v1/payments/rent",
    "POST /api/v1/payments/expense",
    "POST /api/v1/operations/repairs/transition",
    "POST /api/v1/internal/telegram/ingest",
    "POST /internal/ingest",
})


class IdempotencyKeyError(ValueError):
    """Raised when a key is missing, too long, or has invalid characters."""


def normalize_idempotency_key(raw: Optional[str]) -> str:
    """Validate and return the canonical form of an idempotency key.

    Raises :class:`IdempotencyKeyError` on empty / oversize / bad-char input.
    """
    if raw is None:
        raise IdempotencyKeyError("idempotency key is required")
    if not isinstance(raw, str):
        raise IdempotencyKeyError("idempotency key must be a string")
    key = raw.strip()
    if not _KEY_PATTERN.match(key):
        raise IdempotencyKeyError(
            "idempotency key must be 1-255 chars of [A-Za-z0-9._-:]; "
            f"got {raw!r}"
        )
    return key


def canonical_payload(payload: Mapping[str, Any]) -> str:
    """Return a deterministic JSON encoding of ``payload`` for hashing.

    Sort keys, no extra whitespace, UTF-8. Two semantically equivalent
    payloads (regardless of key order) MUST hash identically.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    )


def _json_default(value: Any) -> Any:
    """Fallback encoder for non-trivial types. Keep narrow."""
    # Decimal / datetime / UUID / Enum → string-coerced.
    try:
        from datetime import datetime
        if isinstance(value, datetime):
            return value.isoformat()
    except Exception:
        pass
    try:
        from decimal import Decimal
        if isinstance(value, Decimal):
            return format(value, "f")
    except Exception:
        pass
    try:
        from enum import Enum
        if isinstance(value, Enum):
            return value.value
    except Exception:
        pass
    try:
        from uuid import UUID
        if isinstance(value, UUID):
            return str(value)
    except Exception:
        pass
    raise TypeError(f"cannot canonicalise payload value of type {type(value).__name__}")


def hash_payload(payload: Mapping[str, Any]) -> str:
    """SHA-256 hex digest of the canonicalised payload."""
    encoded = canonical_payload(payload).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def composite_key(
    *,
    principal_org_id: str,
    endpoint: str,
    idempotency_key: str,
    payload: Mapping[str, Any],
) -> str:
    """Build the storage key for the idempotency table.

    Components:
        1. principal_org_id  — prevents cross-tenant collisions.
        2. endpoint          — prevents reuse across endpoints.
        3. idempotency_key   — client-provided opaque key.
        4. payload_hash      — guards against key reuse with different body.

    The composite is itself hashed with SHA-256 to keep the storage key a
    fixed length and to avoid leaking the raw client key into DB logs.
    """
    if not isinstance(principal_org_id, str) or not principal_org_id.strip():
        raise IdempotencyKeyError("principal_org_id is required")
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise IdempotencyKeyError("endpoint is required")
    norm_key = normalize_idempotency_key(idempotency_key)
    payload_h = hash_payload(payload)
    raw = f"{principal_org_id}|{endpoint}|{norm_key}|{payload_h}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def is_mandatory_endpoint(endpoint: str) -> bool:
    """Return True iff ``endpoint`` MUST carry an idempotency key."""
    if not isinstance(endpoint, str):
        return False
    return endpoint.strip() in MANDATORY_IDEMPOTENCY_ENDPOINTS


__all__ = [
    "HEADER_IDEMPOTENCY_KEY",
    "MANDATORY_IDEMPOTENCY_ENDPOINTS",
    "IdempotencyKeyError",
    "normalize_idempotency_key",
    "canonical_payload",
    "hash_payload",
    "composite_key",
    "is_mandatory_endpoint",
]
