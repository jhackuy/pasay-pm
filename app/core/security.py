"""Security primitives: HMAC, JWT, API key hashing, webhook signature.

AGENTS.md §4: permission boundary is Org/Membership. This module provides
the cryptographic primitives; services compose them with Principal / org-scope.

CANONICAL CONTRACTS (Owner-mandated; no wrappers, no fallbacks):
- HMAC signing/verifying requires an EXPLICIT secret. Missing or empty
  secret raises SecretMissingError — there is no module-level default key
  and no implicit fallback.
- `verify_hmac(message, signature, secret)` is the single contract.
  No arity dispatch. No legacy `(secret, message, signature)` form.
- JWT algorithms are restricted to the whitelist (HS256/HS384/HS512).
  `alg=none` and asymmetric algorithms are rejected.
- `verify_jwt` raises JwtError on any failure. It NEVER silently
  returns None.
- `verify_webhook_signature(secret, body, signature_header)` is the
  single canonical webhook entry point. It strips an optional
  `sha256=` prefix and uses the canonical HMAC contract.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Any, Mapping

import jwt as pyjwt
from jwt import InvalidTokenError


DEFAULT_JWT_ALG = "HS256"
JWT_ALG_WHITELIST = frozenset({"HS256", "HS384", "HS512"})


class SecurityError(Exception):
    """Base class for security-module errors."""


class SecretMissingError(SecurityError, ValueError):
    """Raised when a required HMAC/JWT secret is not provided or is empty.

    There is no implicit fallback secret. Failing closed is mandatory.
    """


class HmacMismatchError(SecurityError):
    """Raised when HMAC verification fails (constant-time compare)."""


class JwtError(SecurityError):
    """Raised on JWT sign/verify failure (bad token, alg, missing lib)."""


__all__ = [
    "DEFAULT_JWT_ALG",
    "JWT_ALG_WHITELIST",
    "SecurityError",
    "SecretMissingError",
    "HmacMismatchError",
    "JwtError",
    "sign_hmac",
    "verify_hmac",
    "sign_jwt",
    "verify_jwt",
    "generate_api_key",
    "hash_api_key",
    "verify_webhook_signature",
]


def _coerce_secret(secret: str | bytes | bytearray) -> bytes:
    if isinstance(secret, str):
        return secret.encode("utf-8")
    if isinstance(secret, (bytes, bytearray)):
        return bytes(secret)
    raise SecretMissingError(
        f"secret must be str or bytes, got {type(secret).__name__}"
    )


def _require_secret(secret: str | bytes | bytearray | None) -> bytes:
    if secret is None:
        raise SecretMissingError("secret is required")
    coerced = _coerce_secret(secret)
    if len(coerced) == 0:
        raise SecretMissingError("secret must be non-empty")
    return coerced


def _coerce_message(message: bytes | str) -> bytes:
    if isinstance(message, str):
        return message.encode("utf-8")
    if isinstance(message, (bytes, bytearray)):
        return bytes(message)
    raise HmacMismatchError(
        f"message must be str or bytes, got {type(message).__name__}"
    )


def sign_hmac(message: bytes | str, secret: str | bytes) -> str:
    """Sign `message` with HMAC-SHA256 using the explicit `secret`.

    The secret is REQUIRED. There is no implicit fallback. Raises
    SecretMissingError if absent or empty.
    """
    key = _require_secret(secret)
    msg = _coerce_message(message)
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def verify_hmac(message: bytes | str, signature: str, secret: str | bytes) -> bool:
    """Verify an HMAC-SHA256 signature using constant-time comparison.

    Canonical contract: `verify_hmac(message, signature, secret)`.
    No arity dispatch. No legacy `(secret, message, signature)` form.

    Returns True on match, raises HmacMismatchError on mismatch.
    Raises SecretMissingError if secret is absent or empty.
    """
    if not isinstance(signature, str) or not signature:
        raise HmacMismatchError("signature must be a non-empty string")
    expected = sign_hmac(message, secret)
    if not hmac.compare_digest(expected, signature):
        raise HmacMismatchError("HMAC signature mismatch")
    return True


def sign_jwt(
    payload: Mapping[str, Any],
    secret: str | bytes,
    *,
    algorithm: str = DEFAULT_JWT_ALG,
    expires_in_seconds: int = 3600,
) -> str:
    """Sign a JWT with the given payload and explicit secret.

    Algorithm must be in JWT_ALG_WHITELIST. Raises JwtError if not.
    """
    if algorithm not in JWT_ALG_WHITELIST:
        raise JwtError(
            f"algorithm {algorithm!r} not in whitelist "
            f"{sorted(JWT_ALG_WHITELIST)}"
        )
    key = _require_secret(secret)
    body = dict(payload)
    body["iat"] = int(time.time())
    body["exp"] = int(time.time()) + int(expires_in_seconds)
    try:
        return pyjwt.encode(body, key, algorithm=algorithm)
    except Exception as exc:
        raise JwtError(f"JWT sign failed: {exc}") from exc


def verify_jwt(
    token: str,
    secret: str | bytes,
    *,
    algorithms: tuple[str, ...] = (DEFAULT_JWT_ALG,),
) -> dict[str, Any]:
    """Verify a JWT and return its decoded payload.

    Raises JwtError on any failure (bad signature, expired, alg=none,
    asymmetric, missing secret). NEVER silently returns None.
    """
    if not isinstance(token, str) or not token:
        raise JwtError("token must be a non-empty string")
    allowed = tuple(a for a in algorithms if a in JWT_ALG_WHITELIST)
    if not allowed:
        raise JwtError(
            f"no whitelisted algorithms in {algorithms!r} "
            f"(whitelist={sorted(JWT_ALG_WHITELIST)})"
        )
    key = _require_secret(secret)
    try:
        return pyjwt.decode(token, key, algorithms=list(allowed))
    except InvalidTokenError as exc:
        raise JwtError(f"JWT verify failed: {exc}") from exc


def generate_api_key() -> str:
    """Generate a random URL-safe API key (256-bit entropy)."""
    return secrets.token_urlsafe(32)


def hash_api_key(api_key: str) -> str:
    """Hash an API key with SHA-256 for at-rest storage."""
    if not isinstance(api_key, str):
        raise SecurityError("api_key must be str")
    if not api_key:
        raise SecurityError("api_key must be non-empty")
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def verify_webhook_signature(
    body: bytes | str,
    signature_header: str,
    secret: str | bytes,
    *,
    header_prefix: str = "sha256=",
) -> bool:
    """Verify a webhook signature header (e.g. Telegram-style `sha256=...`).

    Single canonical contract: message + signature_header + explicit secret.
    Returns True on match, raises HmacMismatchError otherwise.
    """
    if isinstance(signature_header, str) and signature_header.startswith(header_prefix):
        sig = signature_header[len(header_prefix):]
    else:
        sig = signature_header
    return verify_hmac(body, sig, secret)
