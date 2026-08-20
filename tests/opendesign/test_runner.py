"""Unit tests for scripts/opendesign/runner.py."""

from __future__ import annotations

import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
TESTS_DIR = os.path.dirname(THIS_DIR)
SCRIPTS_DIR = os.path.dirname(TESTS_DIR)
REPO = os.path.dirname(SCRIPTS_DIR)
for p in (SCRIPTS_DIR, REPO):
    if p not in sys.path:
        sys.path.insert(0, p)

from scripts.opendesign import runner as R  # noqa: E402
from scripts.opendesign import contract as C  # noqa: E402
from scripts.opendesign.dispatch_stub import StubTransport  # noqa: E402


def _base_event():
    return {
        "action": "created",
        "delivery": "t-1",
        "repository": {"name": "pasay-pm", "owner": {"login": "jhackuy"}},
        "issue": {
            "number": 4,
            "state": "open",
            "title": "PASAY-EXPENSE-VNEXT-001",
            "body": "Body.",
            "labels": [{"name": "route:design-dev"}],
        },
        "comment": {
            "id": 1000,
            "body": "Owner-approved for OpenDesign.\n" + C.APPROVAL_MARKER,
            "created_at": "2026-08-20T00:00:00Z",
        },
        "sender": {"login": "jhackuy", "id": 1, "type": "User"},
    }


def _allowlist():
    return ["jhackuy"]


def _writeback_log():
    log = []
    def _w(r):
        log.append(r)
    return log, _w


def test_runner_no_transport_returns_blocked():
    """If transport is None, runner reports BLOCKED_FOR_PRODUCT_DECISION."""
    event = _base_event()
    log, w = _writeback_log()
    rec = R.run(
        event=event,
        owner_allowlist=_allowlist(),
        idempotency_records=[],
        run_id="r-1",
        expected_repo_full_name="jhackuy/pasay-pm",
        transport=None,
        writeback_fn=w,
    )
    assert rec["state"] == C.STATE_BLOCKED_FOR_PRODUCT_DECISION
    assert "Owner must set" in rec["reason"]
    assert any(r["state"] == C.STATE_BLOCKED_FOR_PRODUCT_DECISION for r in log)


def test_runner_stub_transport_accept_records_dispatch():
    """A passing stub transport yields DISPATCHED."""
    event = _base_event()
    log, w = _writeback_log()
    rec = R.run(
        event=event,
        owner_allowlist=_allowlist(),
        idempotency_records=[],
        run_id="r-1",
        expected_repo_full_name="jhackuy/pasay-pm",
        transport=StubTransport(mode="accept"),
        writeback_fn=w,
    )
    assert rec["state"] == C.STATE_DISPATCHED
    assert rec["open_design_ack"]["ok"] is True
    assert rec["open_design_target"] == "D:\\AI-DESIGN\\projects\\pasay-stub"
    assert any(r["state"] == C.STATE_DISPATCHED for r in log)


def test_runner_stub_transport_reject_records_dispatch_failed():
    """A rejecting stub yields DISPATCH_FAILED."""
    event = _base_event()
    log, w = _writeback_log()
    rec = R.run(
        event=event,
        owner_allowlist=_allowlist(),
        idempotency_records=[],
        run_id="r-1",
        expected_repo_full_name="jhackuy/pasay-pm",
        transport=StubTransport(mode="reject"),
        writeback_fn=w,
    )
    assert rec["state"] == C.STATE_DISPATCH_FAILED
    assert "rejected" in rec["reason"].lower()
    assert any(r["state"] == C.STATE_DISPATCH_FAILED for r in log)


def test_runner_stub_transport_fail_records_dispatch_failed():
    """A failing stub yields DISPATCH_FAILED."""
    event = _base_event()
    log, w = _writeback_log()
    rec = R.run(
        event=event,
        owner_allowlist=_allowlist(),
        idempotency_records=[],
        run_id="r-1",
        expected_repo_full_name="jhackuy/pasay-pm",
        transport=StubTransport(mode="fail"),
        writeback_fn=w,
    )
    assert rec["state"] == C.STATE_DISPATCH_FAILED
    assert "transport raised" in rec["reason"].lower()


def test_runner_no_dispatch_path_writes_record():
    """Non-dispatch path still emits a writeback record."""
    event = _base_event()
    event["comment"]["body"] = "no marker here"
    log, w = _writeback_log()
    rec = R.run(
        event=event,
        owner_allowlist=_allowlist(),
        idempotency_records=[],
        run_id="r-1",
        expected_repo_full_name="jhackuy/pasay-pm",
        transport=StubTransport(mode="accept"),
        writeback_fn=w,
    )
    assert rec["verdict"] == C.VERDICT_NO_DISPATCH
    assert rec["state"] == C.STATE_APPROVED_NOT_DISPATCHED
    assert any(r["state"] == C.STATE_APPROVED_NOT_DISPATCHED for r in log)


def test_runner_writeback_exception_does_not_crash():
    """A buggy writeback_fn must not crash the runner."""
    event = _base_event()
    def bad_w(_):
        raise RuntimeError("writeback boom")
    rec = R.run(
        event=event,
        owner_allowlist=_allowlist(),
        idempotency_records=[],
        run_id="r-1",
        expected_repo_full_name="jhackuy/pasay-pm",
        transport=StubTransport(mode="accept"),
        writeback_fn=bad_w,
    )
    assert rec["state"] == C.STATE_DISPATCHED
    assert "writeback_error" in rec


def _run_all():
    failures = []
    fns = [
        test_runner_no_transport_returns_blocked,
        test_runner_stub_transport_accept_records_dispatch,
        test_runner_stub_transport_reject_records_dispatch_failed,
        test_runner_stub_transport_fail_records_dispatch_failed,
        test_runner_no_dispatch_path_writes_record,
        test_runner_writeback_exception_does_not_crash,
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
    print("OK: tests/opendesign/test_runner.py passed")
    sys.exit(0)
