"""Unit tests for the PR-stage fixture runner."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
TESTS_DIR = os.path.dirname(THIS_DIR)
SCRIPTS_DIR = os.path.dirname(TESTS_DIR)
REPO = os.path.dirname(SCRIPTS_DIR)
for p in (SCRIPTS_DIR, REPO):
    if p not in sys.path:
        sys.path.insert(0, p)


def _run_pr_fixture(fixture_dir, mode, strict=True):
    D = os.path.join(_SCRIPT_DIR, "opendesign", "run_pr_fixture.py")
    env = os.environ.copy()
    env["OD_STUB_MODE"] = mode
    env["OD_RUN_ID"] = "fixture-test-" + mode
    args = [sys.executable, D, "--fixture-dir", fixture_dir, "--mode", mode]
    if strict:
        args.append("--strict")
    proc = subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8", env=env, timeout=30,
    )
    return proc.returncode, proc.stdout, proc.stderr


# REPO at this point may have been resolved to a parent dir without the worktree.
# Use the worktree path explicitly when running these tests from a different cwd.
_HERE = os.path.dirname(os.path.abspath(__file__))
# _HERE = .../tests/opendesign
# dirname -> .../tests
# dirname -> worktree root (OPENDESIGN-AUTO-DISPATCH-005)
_WORKTREE = os.path.dirname(os.path.dirname(_HERE))
assert os.path.basename(_WORKTREE).startswith("OPENDESIGN"), "wrong worktree: " + _WORKTREE
_FIXTURE_DIR = os.path.join(_WORKTREE, ".github", "fixtures", "opendesign-dispatch")
_SCRIPT_DIR = os.path.join(_WORKTREE, "scripts")


def test_fixture_runner_accept_mode_passes():
    """Accept mode: happy path + special chars dispatch; non-dispatch cases
    correctly emit NO_DISPATCH / BLOCKED."""
    rc, out, err = _run_pr_fixture(_FIXTURE_DIR, mode="accept")
    data = json.loads(out)
    assert rc == 0, "stdout=" + out + " stderr=" + err
    assert data["fixture_count"] >= 7
    assert data["unexpected"] == []
    # Verify each result has the right state.
    by_fixture = {r["_fixture"]: r for r in data["results"]}
    assert by_fixture["01_no_approval.json"]["state"] == "APPROVED_NOT_DISPATCHED"
    assert by_fixture["01_no_approval.json"]["verdict"] == "NO_DISPATCH"
    assert by_fixture["02_wrong_route.json"]["state"] == "NO_DISPATCH"
    assert by_fixture["03_non_whitelisted_actor.json"]["state"] == "BLOCKED_FOR_PRODUCT_DECISION"
    assert by_fixture["04_happy_path.json"]["state"] == "DISPATCHED"
    assert by_fixture["05_duplicate_event.json"]["state"] == "NO_DISPATCH"
    assert by_fixture["07_special_chars_in_body.json"]["state"] == "DISPATCHED"
    assert by_fixture["08_malicious_comment.json"]["state"] == "APPROVED_NOT_DISPATCHED"
    assert by_fixture["08_malicious_comment.json"]["verdict"] == "NO_DISPATCH"
    # The happy path should produce exactly one stub attempt per unique dispatch.
    assert data["stub_attempts"] == 2  # fixture 04 + 07


def test_fixture_runner_reject_mode_yields_dispatch_failed():
    """Reject mode: happy path yields DISPATCH_FAILED (endpoint unreachable)."""
    rc, out, err = _run_pr_fixture(_FIXTURE_DIR, mode="reject")
    data = json.loads(out)
    # The strict check will fail because fixture 04 expects DISPATCHED, but
    # we got DISPATCH_FAILED. We re-run non-strict to capture the summary.
    assert rc != 0 or data["unexpected"] != []
    by_fixture = {r["_fixture"]: r for r in data["results"]}
    assert by_fixture["04_happy_path.json"]["state"] == "DISPATCH_FAILED"
    assert "rejected" in by_fixture["04_happy_path.json"]["reason"].lower()


def _run_all():
    failures = []
    fns = [
        test_fixture_runner_accept_mode_passes,
        test_fixture_runner_reject_mode_yields_dispatch_failed,
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
    print("OK: tests/opendesign/test_fixture_runner.py passed")
    sys.exit(0)
