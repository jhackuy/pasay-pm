"""Cross-run idempotency regression (Issue #5 requirement).

Spawns two independent Python subprocesses (separate processes, separate
state) that both run the dispatcher for the same approval event. The
second subprocess is given the persisted status records that the first
subprocess produced. The second run MUST report NO_DISPATCH, proving
the dispatcher is not just consulting a per-process list.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
TESTS_DIR = os.path.dirname(THIS_DIR)
SCRIPTS_DIR = os.path.dirname(TESTS_DIR)
WORKTREE = os.path.dirname(os.path.dirname(THIS_DIR))
assert os.path.isdir(os.path.join(WORKTREE, "scripts", "opendesign"))


_RUNNER_DRIVER = os.path.join(WORKTREE, "tests", "opendesign", "_cross_run_driver.py")


def _write_driver():
    # Always rewrite so the hardcoded path inside stays in sync.
    try:
        os.remove(_RUNNER_DRIVER)
    except OSError:
        pass
    driver = (
        "import json, os, sys\n"
        "sys.path.insert(0, os.getcwd())\n"
        "from scripts.opendesign import runner as R\n"
        "from scripts.opendesign import contract as C\n"
        "from scripts.opendesign.dispatch_stub import StubTransport\n"
        "event = json.load(sys.stdin)\n"
        "persisted_path = os.environ['OD_PERSISTED_JSON']\n"
        "records = json.load(open(persisted_path, encoding='utf-8')) if os.path.exists(persisted_path) else []\n"
        "rec = R.run(\n"
        "    event=event,\n"
        "    owner_allowlist=['jhackuy'],\n"
        "    idempotency_records=records,\n"
        "    run_id='cross-run-' + os.environ.get('OD_RUN_TAG', '?'),\n"
        "    expected_repo_full_name='jhackuy/pasay-pm',\n"
        "    transport=StubTransport(mode='accept'),\n"
        "    writeback_fn=None,\n"
        ")\n"
        "print(json.dumps(rec, ensure_ascii=False))\n"
    )
    with open(_RUNNER_DRIVER, "w", encoding="utf-8", newline="\n") as f:
        f.write(driver)


def _base_event():
    return {
        "action": "created",
        "delivery": "cross-1",
        "repository": {"name": "pasay-pm", "owner": {"login": "jhackuy"}},
        "issue": {
            "number": 4,
            "state": "open",
            "title": "Cross-run idempotency",
            "body": "Body.",
            "labels": [{"name": "route:design-dev"}],
            "pull_request": None,
        },
        "comment": {
            "id": 1000,
            "body": "Approved.\n" + "OWNER_APPROVED_FOR_OPENDESIGN",
            "created_at": "2026-08-20T00:00:00Z",
        },
        "sender": {"login": "jhackuy", "id": 1, "type": "User"},
    }


def _run_subprocess(event, persisted_records, run_tag):
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".json", delete=False, dir=tempfile.gettempdir()
    ) as f:
        json.dump(persisted_records, f)
        persisted_path = f.name
    try:
        env = os.environ.copy()
        env["OD_PERSISTED_JSON"] = persisted_path
        env["OD_RUN_TAG"] = run_tag
        proc = subprocess.run(
            [sys.executable, _RUNNER_DRIVER],
            input=json.dumps(event),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=30,
        )
        assert proc.returncode == 0, "stderr=" + proc.stderr
        return json.loads(proc.stdout.strip().splitlines()[-1])
    finally:
        try:
            os.remove(persisted_path)
        except OSError:
            pass


def test_cross_run_idempotency_via_subprocess():
    """Two independent subprocesses; second must report NO_DISPATCH."""
    _write_driver()
    event = _base_event()

    rec1 = _run_subprocess(event, [], "first")
    assert rec1["verdict"] == "DISPATCH"
    assert rec1["state"] == "DISPATCHED"
    assert rec1["dispatch_id"]

    persisted = [
        {
            "dispatch_id": rec1["dispatch_id"],
            "state": "DISPATCHED",
            "ts": "2026-08-20T00:00:00Z",
            "run_id": "earlier-run",
            "trigger_actor": "jhackuy",
        }
    ]

    rec2 = _run_subprocess(event, persisted, "second")
    assert rec2["verdict"] == "NO_DISPATCH", "second run verdict: " + rec2["verdict"]
    assert rec2["state"] == "NO_DISPATCH"
    assert "idempotency" in rec2["reason"].lower()


def _run_all():
    failures = []
    fns = [
        test_cross_run_idempotency_via_subprocess,
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
    print("OK: tests/opendesign/test_cross_run_idempotency.py passed")
    sys.exit(0)
