"""Security primitives: HMAC verification, JWT, API key hashing, webhook.

AGENTS.md §4: permission boundary is Org/Membership. This module provides
the cryptographic primitives; services compose them with Principal/OrgScope.

PyJWT is a hard runtime dependency (declared in requirements.txt). If
PyJWT is missing at import time we raise ImportError — never silently
return None on verify (reviewer finding on PR #100: silent None return
masks misconfiguration).

Reviewer finding: JWT dependency must be declared and tested; sign_jwt
must not return a string when PyJWT is absent; verify_jwt must not
silently return None on failure. Both raise.

Two calling conventions for verify_hmac are supported so callers using
either legacy `(secret, message, signature)` or the new test-friendly
`(message, signature, secret=None)` can both work without source churn.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any, Mapping

import jwt as pyjwt
from jwt import InvalidTokenError

# Algorithm whitelist — refuse "none" and asymmetric algorithms unless
# explicitly configured elsewhere. HS256 is the default for internal tokens.
DEFAULT_JWT_ALG = "HS256"
JWT_ALG_WHITELIST = frozenset({"HS256", "HS384", "HS512"})

# Module-level default key used when sign_hmac / verify_hmac are called
# without an explicit secret. This is for dev/test convenience only;
# production code MUST pass a real secret.
HMAC_DEFAULT_KEY = b"pasay-dev-hmac-key-v1"


class SecurityError(Exception):
    """Base class for security-module errors."""


class HmacMismatchError(SecurityError):
    """Raised when HMAC verification fails (constant-time compare)."""


class JwtError(SecurityError):
    """Raised on JWT sign/verify failure (missing lib, bad token, alg)."""


def sign_hmac(message: bytes | str, secret: bytes | None = None) -> str:
    """Sign a message with HMAC-SHA256 and return the hex digest.

    `secret` defaults to the module-level `HMAC_DEFAULT_KEY` when None;
    pass an explicit secret in production code.
    """
    if secret is None:
        secret = HMAC_DEFAULT_KEY
    if isinstance(message, str):
        message = message.encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def verify_hmac(*args, secret: str | bytes | None = None, **kwargs) -> bool:
    """Verify an HMAC-SHA256 signature using constant-time comparison.

    Two calling conventions are supported; dispatch is by arity +
    argument type so existing callers do not need to be rewritten.

    New contract (used by tests, dev convenience):
        verify_hmac(message, signature, secret=None)
        verify_hmac(message, signature, secret=secret)

    Legacy contract (3 positional args, first is the secret):
        verify_hmac(secret, message, signature)

    Returns True on match, raises HmacMismatchError on mismatch or invalid
    input.
    """
    # Dispatch by arity + argument shape.
    if len(args) == 2:
        # New contract: (message, signature[, secret=...])
        message, signature = args
    elif len(args) == 3:
        # Legacy contract: (secret, message, signature)
        secret, message, signature = args
    elif len(args) == 1:
        # New contract called positionally with message + secret kwarg.
        message = args[0]
        if "signature" in kwargs:
            signature = kwargs.pop("signature")
        elif len(args) >= 2:
            signature = args[1]
        else:
            signature = None
    else:
        raise HmacMismatchError(
            f"verify_hmac requires 2 or 3 positional args "
            f"((message, signature[, secret]) or (secret, message, signature)); "
            f"got {len(args)}"
        )

    if secret is None:
        secret = HMAC_DEFAULT_KEY

    # Coerce secret to bytes.
    if isinstance(secret, str):
        secret_bytes = secret.encode("utf-8")
    elif isinstance(secret, bytes):
        secret_bytes = secret
    else:
        raise HmacMismatchError(
            f"secret must be str or bytes, got {type(secret).__name__}"
        )

    if isinstance(message, str):
        message = message.encode("utf-8")
    if signature is None:
        raise HmacMismatchError("signature is required")

    expected = hmac.new(
        secret_bytes, message, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, str(signature)):
        raise HmacMismatchError("HMAC signature mismatch")
    return True


def sign_jwt(
    payload: Mapping[str, Any],
    secret: str,
    *,
    algorithm: str = DEFAULT_JWT_ALG,
    expires_in_seconds: int | None = None,
    ttl_seconds: int | None = None,
) -> str:
    """Sign a JWT with the given payload and secret.

    `algorithm` must be in JWT_ALG_WHITELIST.
    `ttl_seconds` is an alias for `expires_in_seconds`; if both are
    provided, `ttl_seconds` wins.
    """
    if ttl_seconds is not None:
        lifetime = int(ttl_seconds)
    elif expires_in_seconds is not None:
        lifetime = int(expires_in_seconds)
    else:
        lifetime = 3600
    if algorithm not in JWT_ALG_WHITELIST:
        raise JwtError(
            f"algorithm {algorithm!r} not in whitelist "
            f"{sorted(JWT_ALG_WHITELIST)}"
        )
    import time
    payload = dict(payload)
    payload["iat"] = int(time.time())
    payload["exp"] = int(time.time()) + lifetime
    try:
        return pyjwt.encode(payload, secret, algorithm=algorithm)
    except Exception as exc:
        raise JwtError(f"JWT sign failed: {exc}") from exc


def verify_jwt(
    token: str,
    secret: str,
    *,
    algorithms: tuple[str, ...] = (DEFAULT_JWT_ALG,),
) -> dict[str, Any]:
    """Verify a JWT and return its decoded payload.

    Raises JwtError on any failure (missing lib, bad signature, expired,
    alg not in whitelist). NEVER silently returns None.
    """
    allowed = tuple(a for a in algorithms if a in JWT_ALG_WHITELIST)
    if not allowed:
        raise JwtError(
            f"no whitelisted algorithms in {algorithms!r} "
            f"(whitelist={sorted(JWT_ALG_WHITELIST)})"
        )
    try:
        return pyjwt.decode(token, secret, algorithms=list(allowed))
    except InvalidTokenError as exc:
        raise JwtError(f"JWT verify failed: {exc}") from exc


def generate_api_key() -> str:
    """Generate a random URL-safe API key.

    Returns 32 bytes (256 bits) of entropy, base64url-encoded without
    padding. Suitable for secretary / API-credential use.
    """
    return secrets.token_urlsafe(32)


def hash_api_key(api_key: str) -> str:
    """Hash an API key with SHA-256 for at-rest storage.

    AGENTS.md §4: never store plaintext keys. Compare via constant-time.
    """
    if not isinstance(api_key, str):
        raise SecurityError("api_key must be str")
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def verify_webhook_signature(
    secret: str,
    body: bytes,
    signature_header: str,
    *,
    header_prefix: str = "sha256=",
) -> bool:
    """Verify a webhook signature header (e.g. Telegram-style `sha256=...`).

    Returns True on match, raises HmacMismatchError otherwise.
    """
    if signature_header.startswith(header_prefix):
        sig = signature_header[len(header_prefix):]
    else:
        sig = signature_header
    # Reuse the legacy dispatch by feeding the legacy 3-arg shape.
    return verify_hmac(secret, body, sig)
