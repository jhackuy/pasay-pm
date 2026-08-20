"""PASAY-OPENDESIGN-AUTO-DISPATCH-001 PR-stage fixture runner.

Usage:
    python scripts/opendesign/run_pr_fixture.py [--fixture-dir DIR] [--mode accept|reject|fail]
"""

from __future__ import annotations

import argparse
import datetime as _dt
import glob
import json
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
REPO = os.path.dirname(SCRIPTS_DIR)

if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from opendesign import runner as R
from opendesign import contract as C
from opendesign.dispatch_stub import StubTransport


def _load_fixture(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    p = argparse.ArgumentParser(description="PR-stage fixture runner")
    p.add_argument(
        "--fixture-dir",
        default=os.path.join(REPO, ".github", "fixtures", "opendesign-dispatch"),
        help="directory containing issue_comment fixture JSON files",
    )
    p.add_argument(
        "--mode",
        default=os.environ.get("OD_STUB_MODE", "accept"),
        help="StubTransport mode: accept|reject|fail",
    )
    p.add_argument(
        "--owner-allowlist",
        default=os.environ.get("OD_OWNER_ALLOWLIST", "jhackuy"),
        help="comma-separated GitHub logins allowed to trigger dispatch",
    )
    p.add_argument(
        "--expected-repo",
        default=os.environ.get("OD_EXPECTED_REPO", "jhackuy/pasay-pm"),
    )
    p.add_argument(
        "--run-id",
        default=os.environ.get(
            "OD_RUN_ID",
            "fixture-" + _dt.datetime.now().strftime("%Y%m%d-%H%M%S"),
        ),
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero on any unexpected verdict",
    )
    args = p.parse_args()

    allowlist = [s.strip() for s in args.owner_allowlist.split(",") if s.strip()]
    fixtures = sorted(glob.glob(os.path.join(args.fixture_dir, "*.json")))
    if not fixtures:
        print("ERROR: no fixtures found in " + args.fixture_dir)
        return 2

    transport = StubTransport(mode=args.mode)
    results = []
    unexpected = []

    writeback_log = []

    def _writeback(record):
        writeback_log.append(record)

    for path in fixtures:
        event = _load_fixture(path)
        rec = R.run(
            event=event,
            owner_allowlist=allowlist,
            idempotency_records=[w for w in writeback_log if w.get("dispatch_id")],
            run_id=args.run_id,
            expected_repo_full_name=args.expected_repo,
            transport=transport,
            writeback_fn=_writeback,
        )
        rec["_fixture"] = os.path.basename(path)
        results.append(rec)

        expected = event.get("_expected")
        if expected:
            want_verdict = expected.get("verdict")
            want_state = expected.get("state")
            if want_verdict and rec.get("verdict") != want_verdict:
                unexpected.append({
                    "fixture": os.path.basename(path),
                    "got_verdict": rec.get("verdict"),
                    "want_verdict": want_verdict,
                })
            if want_state and rec.get("state") != want_state:
                unexpected.append({
                    "fixture": os.path.basename(path),
                    "got_state": rec.get("state"),
                    "want_state": want_state,
                })

    summary = {
        "run_id": args.run_id,
        "fixture_dir": args.fixture_dir,
        "mode": args.mode,
        "expected_repo": args.expected_repo,
        "owner_allowlist": allowlist,
        "ran_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "fixture_count": len(results),
        "stub_attempts": len(transport.attempts),
        "results": results,
        "unexpected": unexpected,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if args.strict and unexpected:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
