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

from scripts.opendesign import runner as R
from scripts.opendesign import contract as C
from scripts.opendesign.dispatch_stub import StubTransport


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
            "pull_request": None,
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
        log.append(dict(r))
    return log, _w


def test_runner_no_transport_returns_blocked():
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


def test_runner_stub_transport_accept_records_dispatch():
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


def test_runner_stub_transport_reject_records_dispatch_failed():
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


def test_runner_stub_transport_fail_records_dispatch_failed():
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
    assert rec["state"] == C.STATE_NO_DISPATCH


def test_runner_writeback_exception_does_not_crash():
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


def test_runner_does_not_mutate_writeback_record():
    """Regression for CodeRabbit finding: runner must NOT pass the same
    dict reference to the writeback consumer. The first snapshot must
    capture APPROVED_NOT_DISPATCHED, not the post-transport DISPATCHED.
    """
    event = _base_event()
    captured = []
    def w(r):
        captured.append(r["state"])
    rec = R.run(
        event=event,
        owner_allowlist=_allowlist(),
        idempotency_records=[],
        run_id="r-1",
        expected_repo_full_name="jhackuy/pasay-pm",
        transport=StubTransport(mode="accept"),
        writeback_fn=w,
    )
    # Two writebacks: APPROVED_NOT_DISPATCHED then DISPATCHED.
    assert captured[0] == C.STATE_APPROVED_NOT_DISPATCHED
    assert captured[1] == C.STATE_DISPATCHED


def test_runner_persisted_records_are_passed_through():
    """If a persisted DISPATCHED record is supplied for the same
    dispatch_id, the runner must NOT re-dispatch.
    """
    event = _base_event()
    probe = R.run(
        event=event,
        owner_allowlist=_allowlist(),
        idempotency_records=[],
        run_id="r-1",
        expected_repo_full_name="jhackuy/pasay-pm",
        transport=None,
        writeback_fn=None,
    )
    persisted = [
        {
            "dispatch_id": probe["dispatch_id"],
            "state": C.STATE_DISPATCHED,
            "ts": "2026-08-20T00:00:00Z",
            "run_id": "earlier-run",
            "trigger_actor": "jhackuy",
        }
    ]
    rec = R.run(
        event=event,
        owner_allowlist=_allowlist(),
        idempotency_records=persisted,
        run_id="r-2",
        expected_repo_full_name="jhackuy/pasay-pm",
        transport=StubTransport(mode="accept"),
        writeback_fn=None,
    )
    assert rec["verdict"] == C.VERDICT_NO_DISPATCH
    assert rec["state"] == C.STATE_NO_DISPATCH
    assert "idempotency" in rec["reason"].lower()


def _run_all():
    failures = []
    fns = [
        test_runner_no_transport_returns_blocked,
        test_runner_stub_transport_accept_records_dispatch,
        test_runner_stub_transport_reject_records_dispatch_failed,
        test_runner_stub_transport_fail_records_dispatch_failed,
        test_runner_no_dispatch_path_writes_record,
        test_runner_writeback_exception_does_not_crash,
        test_runner_does_not_mutate_writeback_record,
        test_runner_persisted_records_are_passed_through,
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
