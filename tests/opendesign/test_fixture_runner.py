"""Unit tests for the PR-stage fixture runner."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
TESTS_DIR = os.path.dirname(THIS_DIR)
_HERE = THIS_DIR
_WORKTREE = os.path.dirname(os.path.dirname(_HERE))
assert os.path.isdir(os.path.join(_WORKTREE, "scripts", "opendesign")), (
    "wrong repository root: " + _WORKTREE
)
assert os.path.isdir(os.path.join(_WORKTREE, ".github", "fixtures", "opendesign-dispatch")), (
    "wrong repository root: " + _WORKTREE
)
_SCRIPT_DIR = os.path.join(_WORKTREE, "scripts")
_FIXTURE_DIR = os.path.join(_WORKTREE, ".github", "fixtures", "opendesign-dispatch")


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


def test_fixture_runner_accept_mode_passes():
    rc, out, err = _run_pr_fixture(_FIXTURE_DIR, mode="accept")
    assert rc == 0, "stdout=" + out + " stderr=" + err
    data = json.loads(out)
    assert data["fixture_count"] >= 7
    assert data["unexpected"] == []
    by_fixture = {r["_fixture"]: r for r in data["results"]}
    assert by_fixture["01_no_approval.json"]["state"] == "NO_DISPATCH"
    assert by_fixture["01_no_approval.json"]["verdict"] == "NO_DISPATCH"
    assert by_fixture["02_wrong_route.json"]["state"] == "NO_DISPATCH"
    assert by_fixture["03_non_whitelisted_actor.json"]["state"] == "BLOCKED_FOR_PRODUCT_DECISION"
    assert by_fixture["04_happy_path.json"]["state"] == "DISPATCHED"
    assert by_fixture["05_duplicate_event.json"]["state"] == "NO_DISPATCH"
    assert by_fixture["07_special_chars_in_body.json"]["state"] == "DISPATCHED"
    assert by_fixture["08_malicious_comment.json"]["state"] == "NO_DISPATCH"


def test_fixture_runner_reject_mode_yields_dispatch_failed():
    rc, out, err = _run_pr_fixture(_FIXTURE_DIR, mode="reject", strict=False)
    assert rc == 0, "stdout=" + out + " stderr=" + err
    data = json.loads(out)
    by_fixture = {r["_fixture"]: r for r in data["results"]}
    assert by_fixture["04_happy_path.json"]["state"] == "DISPATCH_FAILED"
    assert "rejected" in by_fixture["04_happy_path.json"]["reason"].lower()


def test_fixture_runner_strict_failures_actually_exit_nonzero():
    """When a fixture expectation does NOT match the runner verdict/state,
    the --strict runner must exit with a non-zero status. This proves
    the GitHub Action step (which uses `set -o pipefail`) fails too.
    """
    # Build a synthetic broken fixture: legitimate event but the
    # expectation says verdict must be BLOCKED (impossible).
    broken_event = {
        "action": "created",
        "delivery": "fx-strict-neg",
        "_expected": {"verdict": "BLOCKED", "state": "DISPATCHED"},
        "repository": {"name": "pasay-pm", "owner": {"login": "jhackuy"}},
        "issue": {
            "number": 4,
            "state": "open",
            "title": "Intentionally broken expectation",
            "body": "Body.",
            "labels": [{"name": "route:design-dev"}],
        },
        "comment": {
            "id": 1009,
            "body": "Approved.\nOWNER_APPROVED_FOR_OPENDESIGN",
            "created_at": "2026-08-20T00:00:08Z",
        },
        "sender": {"login": "jhackuy", "id": 1, "type": "User"},
    }
    with tempfile.TemporaryDirectory() as td:
        broken_path = os.path.join(td, "broken.json")
        with open(broken_path, "w", encoding="utf-8") as f:
            json.dump(broken_event, f)
        rc, out, err = _run_pr_fixture(td, mode="accept", strict=True)
        assert rc != 0, (
            "strict runner should exit non-zero on broken expectation; "
            "rc=" + str(rc) + " stdout=" + out[:300]
        )


def _run_all():
    failures = []
    fns = [
        test_fixture_runner_accept_mode_passes,
        test_fixture_runner_reject_mode_yields_dispatch_failed,
        test_fixture_runner_strict_failures_actually_exit_nonzero,
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
