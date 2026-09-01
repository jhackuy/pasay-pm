"""Focused tests for app/core/security.py invariants (V1 rewrite).

Canonical contract:
- HMAC requires an EXPLICIT secret; missing or empty secret raises
  SecretMissingError (fail-closed). No implicit fallback key.
- `verify_hmac(message, signature, secret)` is the single contract.
- HMAC verification is constant-time; mismatch raises HmacMismatchError.
- JWT only uses algorithms in the whitelist (HS256/HS384/512); alg=none
  is rejected; `verify_jwt` raises on any failure.
- `verify_webhook_signature(body, signature_header, secret)` is the
  single webhook entry point.
- API key hashing is SHA-256 hex; empty/non-str rejected.
"""
from __future__ import annotations

import time

import pytest

from app.core.security import (
    DEFAULT_JWT_ALG,
    JWT_ALG_WHITELIST,
    HmacMismatchError,
    JwtError,
    SecretMissingError,
    SecurityError,
    generate_api_key,
    hash_api_key,
    sign_hmac,
    sign_jwt,
    verify_hmac,
    verify_jwt,
    verify_webhook_signature,
)


SECRET = b"pasay-test-secret-v1-32-bytes!"


class TestHmac:
    def test_sign_and_verify_roundtrip(self) -> None:
        msg = "claim-id-001"
        sig = sign_hmac(msg, SECRET)
        assert verify_hmac(msg, sig, SECRET) is True

    def test_wrong_secret_raises_mismatch(self) -> None:
        msg = "claim-id-001"
        sig = sign_hmac(msg, SECRET)
        with pytest.raises(HmacMismatchError):
            verify_hmac(msg, sig, b"different-secret")

    def test_wrong_message_raises_mismatch(self) -> None:
        sig = sign_hmac("a", SECRET)
        with pytest.raises(HmacMismatchError):
            verify_hmac("b", sig, SECRET)

    def test_missing_secret_raises_closed(self) -> None:
        with pytest.raises(SecretMissingError):
            sign_hmac("msg", None)  # type: ignore[arg-type]
        with pytest.raises(SecretMissingError):
            verify_hmac("msg", "abc", None)  # type: ignore[arg-type]

    def test_empty_secret_raises_closed(self) -> None:
        with pytest.raises(SecretMissingError):
            sign_hmac("msg", b"")
        with pytest.raises(SecretMissingError):
            verify_hmac("msg", "abc", "")

    def test_string_secret_is_accepted(self) -> None:
        sig = sign_hmac("m", "secret-string")
        assert verify_hmac("m", sig, "secret-string") is True

    def test_signature_is_hex_sha256(self) -> None:
        sig = sign_hmac("m", SECRET)
        assert len(sig) == 64
        int(sig, 16)

    def test_bytes_message_is_accepted(self) -> None:
        msg = b"raw-bytes-message"
        sig = sign_hmac(msg, SECRET)
        assert verify_hmac(msg, sig, SECRET) is True

    def test_empty_signature_rejected(self) -> None:
        with pytest.raises(HmacMismatchError):
            verify_hmac("m", "", SECRET)

    def test_non_string_signature_rejected(self) -> None:
        with pytest.raises(HmacMismatchError):
            verify_hmac("m", 123, SECRET)  # type: ignore[arg-type]


class TestNoLegacyCompat:
    def test_no_default_key_constant(self) -> None:
        import app.core.security as mod

        assert not hasattr(mod, "HMAC_DEFAULT_KEY")
        assert "HMAC_DEFAULT_KEY" not in mod.__all__

    def test_canonical_verify_hmac_requires_secret(self) -> None:
        # The canonical contract is (message, signature, secret).
        # Calling without an explicit secret must raise SecretMissingError.
        with pytest.raises(SecretMissingError):
            verify_hmac("m", "deadbeef", None)  # type: ignore[arg-type]


class TestWebhook:
    def test_verify_webhook_with_sha256_prefix(self) -> None:
        body = b'{"event":"ping"}'
        sig = sign_hmac(body, SECRET)
        header = f"sha256={sig}"
        assert verify_webhook_signature(body, header, SECRET) is True

    def test_verify_webhook_without_prefix(self) -> None:
        body = b'{"event":"ping"}'
        sig = sign_hmac(body, SECRET)
        assert verify_webhook_signature(body, sig, SECRET) is True

    def test_verify_webhook_bad_sig(self) -> None:
        body = b'{"event":"ping"}'
        with pytest.raises(HmacMismatchError):
            verify_webhook_signature(body, "sha256=deadbeef", SECRET)

    def test_verify_webhook_missing_secret(self) -> None:
        body = b"abc"
        with pytest.raises(SecretMissingError):
            verify_webhook_signature(body, "sha256=abc", None)  # type: ignore[arg-type]


class TestJwt:
    def test_sign_verify_roundtrip(self) -> None:
        now = int(time.time())
        payload = {"sub": "u1", "iat": now, "exp": now + 3600}
        token = sign_jwt(payload, SECRET)
        decoded = verify_jwt(token, SECRET)
        assert decoded["sub"] == "u1"

    def test_alg_none_rejected_on_verify(self) -> None:
        bad_token = (
            "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0"
            ".eyJzdWIiOiJ1MSJ9."
        )
        with pytest.raises(JwtError):
            verify_jwt(bad_token, SECRET)

    def test_unsupported_algorithm_rejected_on_sign(self) -> None:
        with pytest.raises(JwtError):
            sign_jwt({"x": 1}, SECRET, algorithm="RS256")
        with pytest.raises(JwtError):
            sign_jwt({"x": 1}, SECRET, algorithm="none")

    def test_missing_secret_rejected_on_sign(self) -> None:
        with pytest.raises(SecretMissingError):
            sign_jwt({"x": 1}, None)  # type: ignore[arg-type]

    def test_missing_secret_rejected_on_verify(self) -> None:
        with pytest.raises(SecretMissingError):
            verify_jwt("anything", None)  # type: ignore[arg-type]

    def test_empty_token_rejected_on_verify(self) -> None:
        with pytest.raises(JwtError):
            verify_jwt("", SECRET)

    def test_default_alg_is_hs256(self) -> None:
        assert DEFAULT_JWT_ALG == "HS256"
        assert "HS256" in JWT_ALG_WHITELIST
        assert "HS384" in JWT_ALG_WHITELIST
        assert "HS512" in JWT_ALG_WHITELIST
        assert "none" not in JWT_ALG_WHITELIST
        assert "RS256" not in JWT_ALG_WHITELIST


class TestApiKey:
    def test_hash_is_sha256_hex(self) -> None:
        h = hash_api_key("pasay_test_key_abc123")
        assert len(h) == 64
        int(h, 16)

    def test_hash_deterministic(self) -> None:
        a = hash_api_key("k")
        b = hash_api_key("k")
        assert a == b

    def test_hash_empty_rejected(self) -> None:
        with pytest.raises(SecurityError):
            hash_api_key("")

    def test_hash_non_string_rejected(self) -> None:
        with pytest.raises(SecurityError):
            hash_api_key(123)  # type: ignore[arg-type]

    def test_generate_key_unique(self) -> None:
        a = generate_api_key()
        b = generate_api_key()
        assert a != b
        assert isinstance(a, str)
        assert len(a) >= 32
