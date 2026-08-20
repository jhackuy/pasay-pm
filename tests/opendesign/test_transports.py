"""Unit tests for the HTTP and stub transports."""

from __future__ import annotations

import json
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
TESTS_DIR = os.path.dirname(THIS_DIR)
SCRIPTS_DIR = os.path.dirname(TESTS_DIR)
REPO = os.path.dirname(SCRIPTS_DIR)
for p in (SCRIPTS_DIR, REPO):
    if p not in sys.path:
        sys.path.insert(0, p)

from scripts.opendesign.dispatch_stub import StubTransport
from scripts.opendesign.dispatch_http import HttpTransport, _validate_url


def _stub_dispatch_input():
    return {
        "schema": "pasay.opendesign.dispatch/1",
        "dispatch_id": "abc123",
        "run_id": "r-1",
        "repository": "jhackuy/pasay-pm",
        "issue": {"number": 4, "title": "T", "body": "B"},
        "route": "route:design-dev",
        "approval": {
            "marker": "OWNER_APPROVED_FOR_OPENDESIGN",
            "actor": "jhackuy",
            "comment_id": 1,
        },
        "frozen_rules_path": "AI_WORKFLOW_RULES.md",
    }


# ----- StubTransport -----


def test_stub_accept_records_attempt():
    s = StubTransport(mode="accept")
    ack = s.submit(_stub_dispatch_input())
    assert ack["ok"] is True
    assert ack["run_id"].startswith("stub-run-")
    assert len(s.attempts) == 1
    assert s.attempts[0]["dispatch_id"] == "abc123"


def test_stub_reject_yields_failure():
    s = StubTransport(mode="reject")
    ack = s.submit(_stub_dispatch_input())
    assert ack["ok"] is False
    assert "endpoint unreachable" in ack["error"]


def test_stub_fail_raises_runtime_error():
    s = StubTransport(mode="fail")
    try:
        s.submit(_stub_dispatch_input())
    except RuntimeError as exc:
        assert "StubTransport" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_stub_unknown_mode_raises_value_error():
    try:
        StubTransport(mode="bogus")
    except ValueError as exc:
        assert "unsupported mode" in str(exc)
    else:
        raise AssertionError("expected ValueError for unknown stub mode")


def test_stub_appends_log_file():
    log = os.path.join(".ai-control", "results", "opendesign-dispatch", "_test_stub.jsonl")
    if os.path.exists(log):
        os.remove(log)
    s = StubTransport(mode="accept", log_path=log)
    s.submit(_stub_dispatch_input())
    assert os.path.exists(log)
    with open(log, encoding="utf-8") as f:
        lines = f.readlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["dispatch_id"] == "abc123"
    os.remove(log)


# ----- HttpTransport -----


def test_http_validate_url_accepts_http_and_https():
    assert _validate_url("http://127.0.0.1:7456") == "http://127.0.0.1:7456"
    assert _validate_url("https://example.com/path") == "https://example.com/path"


def test_http_validate_url_rejects_empty():
    try:
        _validate_url("")
    except ValueError as exc:
        assert "empty" in str(exc)
    else:
        raise AssertionError("expected ValueError for empty URL")


def test_http_validate_url_rejects_file_scheme():
    try:
        _validate_url("file:///etc/passwd")
    except ValueError as exc:
        assert "scheme not allowed" in str(exc)
    else:
        raise AssertionError("expected ValueError for file:// scheme")


def test_http_validate_url_rejects_no_host():
    try:
        _validate_url("http:///path")
    except ValueError as exc:
        assert "missing host" in str(exc)
    else:
        raise AssertionError("expected ValueError for URL without host")


def test_http_transport_constructor_rejects_bad_url():
    try:
        HttpTransport(base_url="file:///etc/passwd", token="x")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError at construction")


def test_http_transport_refuses_to_unreachable_host():
    t = HttpTransport(base_url="http://127.0.0.1:1", token="x", timeout=2)
    ack = t.submit(_stub_dispatch_input())
    assert ack["ok"] is False
    assert "endpoint unreachable" in ack["error"]


def _run_all():
    failures = []
    fns = [
        test_stub_accept_records_attempt,
        test_stub_reject_yields_failure,
        test_stub_fail_raises_runtime_error,
        test_stub_unknown_mode_raises_value_error,
        test_stub_appends_log_file,
        test_http_validate_url_accepts_http_and_https,
        test_http_validate_url_rejects_empty,
        test_http_validate_url_rejects_file_scheme,
        test_http_validate_url_rejects_no_host,
        test_http_transport_constructor_rejects_bad_url,
        test_http_transport_refuses_to_unreachable_host,
    ]
    for fn in fns:
        try:
            fn()
        except AssertionError as exc:
            failures.append((fn.__name__, str(exc)))
        except Exception as exc:
            failures.append((fn.__name__, "EXC:" + repr(exc)))
    return failures


if __name__ == "__main__":
    failures = _run_all()
    if failures:
        print("FAIL:")
        for name, msg in failures:
            print("  - " + name + ": " + msg)
        sys.exit(1)
    print("OK: tests/opendesign/test_transports.py passed")
    sys.exit(0)
