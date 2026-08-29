"""Focused tests for app/core/idempotency.py invariants (V1 rewrite).

Canonical contract:
- Keys are opaque and case-preserving (no folding, no trimming, no truncation).
- Empty / whitespace-only / non-str / oversize keys are rejected (not dropped).
- MAX_IDEMPOTENCY_KEY_LEN is enforced as a hard limit.
- compute_payload_hash is deterministic on key-sorted JSON input.
- The legacy alias `IdempotencyError` MUST NOT exist (removed in this PR).
"""
from __future__ import annotations

import pytest

from app.core.idempotency import (
    MAX_IDEMPOTENCY_KEY_LEN,
    IdempotencyConflictError,
    IdempotencyKeyError,
    compute_payload_hash,
    normalize_idempotency_key,
)


class TestNormalizeIdempotencyKey:
    def test_returns_input_unchanged(self) -> None:
        raw = "AbC-12345-XYZ"
        assert normalize_idempotency_key(raw) == raw

    def test_preserves_case_distinct_keys(self) -> None:
        a = normalize_idempotency_key("ABC")
        b = normalize_idempotency_key("abc")
        assert a != b
        assert a == "ABC"
        assert b == "abc"

    def test_preserves_leading_and_trailing_whitespace(self) -> None:
        a = normalize_idempotency_key("foo")
        b = normalize_idempotency_key(" foo")
        c = normalize_idempotency_key("foo ")
        assert a != b
        assert a != c
        assert b != c

    def test_does_not_strip_or_lowercase(self) -> None:
        raw = "  PaSsWoRd-77  "
        assert normalize_idempotency_key(raw) == raw

    def test_rejects_empty(self) -> None:
        with pytest.raises(IdempotencyKeyError):
            normalize_idempotency_key("")

    def test_rejects_whitespace_only(self) -> None:
        with pytest.raises(IdempotencyKeyError):
            normalize_idempotency_key("   \t\n  ")

    def test_rejects_non_string(self) -> None:
        with pytest.raises(IdempotencyKeyError):
            normalize_idempotency_key(123)  # type: ignore[arg-type]
        with pytest.raises(IdempotencyKeyError):
            normalize_idempotency_key(None)  # type: ignore[arg-type]

    def test_rejects_oversize(self) -> None:
        oversize = "x" * (MAX_IDEMPOTENCY_KEY_LEN + 1)
        with pytest.raises(IdempotencyKeyError):
            normalize_idempotency_key(oversize)

    def test_does_not_truncate_at_boundary(self) -> None:
        boundary = "x" * MAX_IDEMPOTENCY_KEY_LEN
        assert normalize_idempotency_key(boundary) == boundary
        with pytest.raises(IdempotencyKeyError):
            normalize_idempotency_key(boundary + "x")


class TestComputePayloadHash:
    def test_deterministic_key_order(self) -> None:
        a = {"b": 2, "a": 1, "c": 3}
        b = {"c": 3, "a": 1, "b": 2}
        assert compute_payload_hash(a) == compute_payload_hash(b)

    def test_whitespace_irrelevant_in_payload(self) -> None:
        a = {"a": 1, "b": [1, 2, 3]}
        b = {"a": 1, "b": [1, 2, 3]}
        assert compute_payload_hash(a) == compute_payload_hash(b)

    def test_different_payloads_yield_different_hashes(self) -> None:
        assert compute_payload_hash({"a": 1}) != compute_payload_hash({"a": 2})

    def test_returns_hex_sha256(self) -> None:
        h = compute_payload_hash({"x": 1})
        assert len(h) == 64
        int(h, 16)


class TestExceptionClasses:
    def test_key_error_is_value_error(self) -> None:
        assert issubclass(IdempotencyKeyError, ValueError)

    def test_conflict_error_is_value_error(self) -> None:
        assert issubclass(IdempotencyConflictError, ValueError)

    def test_legacy_alias_removed(self) -> None:
        import app.core.idempotency as mod

        assert not hasattr(mod, "IdempotencyError")
        assert "IdempotencyError" not in mod.__all__
        assert mod.IdempotencyConflictError is not mod.IdempotencyKeyError
