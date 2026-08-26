#!/usr/bin/env python3
"""M005 Milestone C: Targeted suite runner & JSON counter.

Defines exact pytest expressions for M001 through M005 and emits a JSON
summary of pass/fail/skip counts per milestone.

This file is the canonical reference for the five milestone acceptance
command strings; it is intentionally NOT executed in the PREP step.

Usage (future):
    python scripts/m005_run_targeted_suites.py [--run] [--json path]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTEST_CMD = [sys.executable, "-m", "pytest"]
COMMON_FLAGS = ["--ignore=worktrees", "--tb=short", "--no-header", "-q", "--rootdir", str(REPO_ROOT), "tests", "app"]


@dataclass
class SuiteDef:
    key: str
    title: str
    k_expr: str
    description: str


SUITES: list[SuiteDef] = [
    SuiteDef(
        key="M001",
        title="Milestone 1 — Organization Scope P0",
        k_expr="milestone_1_org_scope_p0 or test_milestone_1",
        description="Org-boundary hardening on properties/tenants/expense membership gates.",
    ),
    SuiteDef(
        key="M002",
        title="Milestone 2 — Rent + Repair Closure",
        k_expr="rent_closure_m2 or repair_closure_m2",
        description="Rent payment-claim truth and repair workflow closure invariants.",
    ),
    SuiteDef(
        key="M003",
        title="Milestone 3 — Expense Scope + Operations Truth",
        k_expr="m003_expense_scope_hardening or m003_operations_truth_closure",
        description="Expense org-scope hardening and operations/truth projection closure.",
    ),
    SuiteDef(
        key="M004",
        title="Milestone 4 — Lease/Move-out/Deposit Lifecycle",
        k_expr="m004_",
        description="Lease renewal invariants, move-out truth, deposit settlement closure.",
    ),
    SuiteDef(
        key="M005",
        title="Milestone 5 — V1 Closeout Acceptance",
        k_expr="m005_ or god_view or paginat or envelope_compat or float_safety or i18n",
        description="God-view role, pagination, envelope schema, float-safety and i18n gates.",
    ),
]


def build_command(suite: SuiteDef) -> list[str]:
    return [*PYTEST_CMD, *COMMON_FLAGS, "-k", suite.k_expr]


def _extract_counts(stdout: str, stderr: str) -> dict[str, int]:
    """Parse pytest summary (last lines) without relying on JSON plugins.

    Pytest prints a final summary line like one of:
        ``13 passed in 0.50s``
        ``10 passed, 2 failed in 0.60s``
        ``8 passed, 1 skipped, 1 error in 0.60s``
    """
    combined = (stdout or "") + "\n" + (stderr or "")
    counts = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0, "collected": 0}
    COLLECTED_RE = re.compile(r"(\d+)\s+items?\s+collected")
    for m in COLLECTED_RE.finditer(combined):
        counts["collected"] = max(counts["collected"], int(m.group(1)))
    SUMMARY_RE = re.compile(
        r"(?:^|\n|\r)(\d+)\s+passed"
        r"(?:\s*,\s*(\d+)\s+skipped)?"
        r"(?:\s*,\s*(\d+)\s+failed)?"
        r"(?:\s*,\s*(\d+)\s+error(?:s)?\b)?"
    )
    m = None
    for m in SUMMARY_RE.finditer(combined):
        pass
    if m is not None:
        counts["passed"] = int(m.group(1) or 0)
        counts["skipped"] = int(m.group(2) or 0) if m.group(2) is not None else 0
        counts["failed"] = int(m.group(3) or 0) if m.group(3) is not None else 0
        counts["errors"] = int(m.group(4) or 0) if m.group(4) is not None else 0
    return counts


def _run_one(suite: SuiteDef, _report_json: Path) -> dict[str, Any]:
    real_cmd = build_command(suite)
    env = None
    import os
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        real_cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        env=env,
    )
    stdout_text = (result.stdout or b"").decode("utf-8", errors="replace")
    stderr_text = (result.stderr or b"").decode("utf-8", errors="replace")
    counts = _extract_counts(stdout_text, stderr_text)
    parse_false_green_prevented = False
    rc = result.returncode
    if rc != 0 and counts["failed"] == 0 and counts["errors"] == 0:
        counts["errors"] = max(counts["errors"], 1)
        parse_false_green_prevented = True
    return {
        "key": suite.key,
        "title": suite.title,
        "k_expr": suite.k_expr,
        "command": real_cmd,
        "returncode": rc,
        "collected": counts["collected"],
        "passed": counts["passed"],
        "failed": counts["failed"],
        "skipped": counts["skipped"],
        "errors": counts["errors"],
        "parse_false_green_prevented": parse_false_green_prevented,
        "total_tests": counts["passed"] + counts["failed"] + counts["skipped"] + counts["errors"],
        "stdout_tail": stdout_text[-4000:],
        "stderr_tail": stderr_text[-2000:],
    }


def print_plan() -> dict[str, Any]:
    plan = {
        "suites": [asdict(s) for s in SUITES],
        "common_flags": COMMON_FLAGS,
        "repo_root": str(REPO_ROOT),
    }
    return plan


def run_all() -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    overall = {"collected": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": 0}
    any_parse_false_green_prevented = False
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        for s in SUITES:
            report = tmpdir / f"{s.key}.json"
            row = _run_one(s, report)
            results.append(row)
            if row.get("parse_false_green_prevented"):
                any_parse_false_green_prevented = True
            for k in overall:
                overall[k] += int(row.get(k, 0) or 0)
    return {
        "overall": overall,
        "parse_false_green_prevented_overall": any_parse_false_green_prevented,
        "suites": results,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", action="store_true", help="Actually execute pytest (not done in PREP step)")
    ap.add_argument("--json", type=Path, default=None, help="Write JSON report to this path")
    args = ap.parse_args()

    if args.run:
        payload = run_all()
    else:
        payload = {"plan": print_plan(), "note": "Dry run only; rerun with --run to execute suites."}

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.json is not None:
        args.json.write_text(text, encoding="utf-8")
        print(f"wrote JSON report -> {args.json}")
        return 0
    sys.stdout.write(text)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
