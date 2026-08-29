"""PASAY reference implementation — security helpers.

Issue #99 / PR #100 /oc continuation. Staged under
``.opencode-qualification/reference/`` pending Owner-side promotion to
``app/core/security.py``.

Hard invariants enforced by this module:
    * API keys / webhook secrets are stored ONLY as SHA-256 digests.
      Plaintext keys are never persisted, never logged.
    * JWT verification uses constant-time comparison and enforces issuer,
      audience, algorithm whitelist (``HS256`` only), and exp/nbf/iat
      validation. PyJWT or its equivalent must be added to requirements
      before promotion; this module imports lazily so unit tests can mock.
    * HMAC verification uses :func:`hmac.compare_digest` only — NEVER
      ``==`` for secret-bearing comparisons.
    * Every verification path returns ``False`` (not raise) for invalid
      input so callers can choose to surface a 401 themselves.

Reference promotion to ``app/core/security.py`` requires no behavioural
change. JWT/HMAC backends may be swapped during promotion as long as the
public function signatures stay identical.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import Any, Mapping, Optional

# JWT algorithm whitelist — never accept ``none`` or asymmetric algs here.
ALLOWED_JWT_ALGS: frozenset[str] = frozenset({"HS256"})

# Application-level claim names used throughout the codebase.
CLAIM_ORG_ID = "org_id"
CLAIM_USER_ID = "user_id"
CLAIM_ROLE = "role"
CLAIM_TG_ID = "telegram_id"
CLAIM_ISS = "iss"
CLAIM_AUD = "aud"
CLAIM_EXP = "exp"
CLAIM_NBF = "nbf"
CLAIM_IAT = "iat"


def hash_api_key(api_key: str) -> str:
    """Deterministic SHA-256 digest of an API key. Stored, never the key.

    Plain keys are never written to the DB. The hex digest is what gets
    compared during authentication.
    """
    if not isinstance(api_key, str) or not api_key:
        raise ValueError("api_key must be a non-empty string")
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def generate_api_key(prefix: str = "pk", *, nbytes: int = 32) -> str:
    """Generate a new opaque API key. The plaintext is returned ONCE."""
    if not prefix or not prefix.replace("_", "").isalnum():
        raise ValueError("prefix must be alphanumeric (underscores ok)")
    body = secrets.token_urlsafe(nbytes)
    return f"{prefix}_{body}"


def hmac_sha256(key: bytes, message: bytes) -> bytes:
    """Compute HMAC-SHA256. ``key`` must be material the verifier also knows."""
    if not isinstance(key, (bytes, bytearray)):
        raise TypeError("hmac key must be bytes")
    return hmac.new(key, message, hashlib.sha256).digest()


def hmac_verify(key: bytes, message: bytes, signature: bytes) -> bool:
    """Constant-time HMAC verification. NEVER raises on bad signature."""
    if not isinstance(signature, (bytes, bytearray)):
        return False
    expected = hmac_sha256(key, message)
    return hmac.compare_digest(expected, bytes(signature))


def webhook_signature_verify(
    secret: str,
    payload: bytes,
    provided_signature: str,
    *,
    header_prefix: str = "sha256=",
) -> bool:
    """Verify a ``X-Signature: sha256=…`` style header using HMAC-SHA256.

    Empty ``secret`` fails closed. Returns False on any malformed input.
    """
    if not isinstance(secret, str) or not secret.strip():
        return False
    if not isinstance(payload, (bytes, bytearray)):
        return False
    if not isinstance(provided_signature, str):
        return False
    sig = provided_signature.strip()
    if header_prefix and sig.startswith(header_prefix):
        sig = sig[len(header_prefix):]
    try:
        sig_bytes = bytes.fromhex(sig)
    except ValueError:
        return False
    return hmac_verify(secret.encode("utf-8"), bytes(payload), sig_bytes)


def sign_jwt(
    payload: Mapping[str, Any],
    secret: str,
    *,
    algorithm: str = "HS256",
    ttl_seconds: int = 3600,
    issuer: Optional[str] = None,
    audience: Optional[str] = None,
) -> str:
    """Sign a JWT. Adds ``iat`` and ``exp`` automatically.

    Requires ``PyJWT`` to be importable. Lazy import keeps unit tests cheap.
    """
    if algorithm not in ALLOWED_JWT_ALGS:
        raise ValueError(f"algorithm {algorithm!r} not in whitelist {ALLOWED_JWT_ALGS}")
    if not isinstance(secret, str) or not secret.strip():
        raise ValueError("JWT secret must be a non-empty string")
    try:
        import jwt  # type: ignore
    except ImportError as exc:  # pragma: no cover - guard for promotion
        raise RuntimeError(
            "PyJWT is required for sign_jwt; add PyJWT to requirements before "
            "promoting pasay_core_security to app/core/security.py"
        ) from exc

    now = _now_seconds()
    claims: dict[str, Any] = dict(payload)
    claims[CLAIM_IAT] = now
    claims[CLAIM_EXP] = now + int(ttl_seconds)
    if issuer is not None:
        claims[CLAIM_ISS] = issuer
    if audience is not None:
        claims[CLAIM_AUD] = audience
    return jwt.encode(claims, secret, algorithm=algorithm)


def verify_jwt(
    token: str,
    secret: str,
    *,
    issuer: Optional[str] = None,
    audience: Optional[str] = None,
    algorithms: Optional[frozenset[str]] = None,
) -> Optional[dict[str, Any]]:
    """Verify a JWT and return claims dict, or ``None`` on any failure.

    Failures are NEVER raised. Callers should treat ``None`` as 401.
    """
    if not isinstance(token, str) or not token.strip():
        return None
    if not isinstance(secret, str) or not secret.strip():
        return None
    algs = algorithms or ALLOWED_JWT_ALGS
    try:
        import jwt  # type: ignore
    except ImportError:
        return None
    options: dict[str, Any] = {"require": [CLAIM_EXP, CLAIM_IAT]}
    try:
        decoded = jwt.decode(
            token,
            secret,
            algorithms=list(algs),
            issuer=issuer,
            audience=audience,
            options=options,
        )
    except Exception:
        return None
    if not isinstance(decoded, dict):
        return None
    return decoded


def _now_seconds() -> int:
    # Imported lazily to avoid a circular dep with ``time`` module during tests.
    import time as _time

    return int(_time.time())


__all__ = [
    "ALLOWED_JWT_ALGS",
    "CLAIM_ORG_ID",
    "CLAIM_USER_ID",
    "CLAIM_ROLE",
    "CLAIM_TG_ID",
    "CLAIM_ISS",
    "CLAIM_AUD",
    "CLAIM_EXP",
    "CLAIM_NBF",
    "CLAIM_IAT",
    "hash_api_key",
    "generate_api_key",
    "hmac_sha256",
    "hmac_verify",
    "webhook_signature_verify",
    "sign_jwt",
    "verify_jwt",
]
