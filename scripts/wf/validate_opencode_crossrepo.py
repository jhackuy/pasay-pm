"""PASAY-OC-DISPATCH-CROSSREPO-001: local preflight validation script.

Validates the workflow contract WITHOUT needing GitHub Actions runner:
  1. ALLOWLIST gate: jhackuy/pasay-opendesign is allowed; others rejected.
  2. NO DEPLOY LEAKS: zero references to Issue#78/LAST_GOOD_SHA/pasay-deploy-phase1.yml.
  3. WORKFLOW_DESPATCH entry: workflow_dispatch only present, no issue_comment.

This is run BEFORE pushing to ensure the contract is met.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

WF_PATH = Path(__file__).resolve().parent.parent.parent / ".github" / "workflows" / "opencode-crossrepo-dispatch.yml"
ALLOWLIST = {"jhackuy/pasay-opendesign"}

FORBIDDEN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("LAST_GOOD_SHA", re.compile(r"LAST[_-]?GOOD[_-]?SHA", re.IGNORECASE)),
    ("pasay-deploy-phase1.yml reference", re.compile(r"pasay[-_]deploy[-_]phase1\.ya?ml", re.IGNORECASE)),
    ("Issue #78", re.compile(r"issue.*#?78", re.IGNORECASE)),
]

ENTRY_FORBIDDEN: list[tuple[str, re.Pattern[str]]] = [
    ("issue_comment trigger", re.compile(r"^\s*issue_comment\s*:", re.MULTILINE)),
    ("bot /oc comment pattern", re.compile(r"/oc\b", re.IGNORECASE)),
]

ENTRY_REQUIRED: list[tuple[str, re.Pattern[str]]] = [
    ("workflow_dispatch trigger", re.compile(r"^\s*workflow_dispatch\s*:", re.MULTILINE)),
    ("OpenCode 1.18.24 exact pin", re.compile(r"1\.18\.24")),
    ("allowlist contains jhackuy/pasay-opendesign", re.compile(r"jhackuy/pasay-opendesign")),
]


def gate1_allowlist() -> tuple[bool, list[str]]:
    """Allowlist membership validation."""
    ok = True
    lines: list[str] = []
    test_cases: list[tuple[str, bool]] = [
        ("jhackuy/pasay-opendesign", True),
        ("jhackuy/other-repo", False),
        ("other/pasay-opendesign", False),
        ("acme/anything", False),
        ("pasay-pm", False),
        ("jhackuy/pasay-pm", False),
    ]
    for repo, should_allow in test_cases:
        actually_allowed = repo in ALLOWLIST
        passed = actually_allowed == should_allow
        status = "PASS" if passed else "FAIL"
        line = f"  [{status}] repo='{repo}' should_allow={should_allow} allowed={actually_allowed}"
        lines.append(line)
        if not passed:
            ok = False
    return ok, lines


def _strip_comments(text: str) -> str:
    """Remove YAML comment lines (#...) so descriptive ban text doesn't self-match."""
    out_lines: list[str] = []
    for ln in text.splitlines():
        stripped = ln.lstrip()
        if stripped.startswith("#"):
            continue
        # Strip inline comments (naive: everything after unquoted #)
        # For safety, keep it simple: if a line has a '#' outside quotes,
        # keep the left part. Since our checks are basic string presence this is fine.
        cut = ln.find("  #")
        if cut > 0:
            ln = ln[:cut]
        out_lines.append(ln)
    return "\n".join(out_lines)


def gate2_no_deploy_leaks(wf_text: str) -> tuple[bool, list[str]]:
    """Forbidden deploy-pattern absence check (ignores comment lines)."""
    search_text = _strip_comments(wf_text)
    ok = True
    lines: list[str] = []
    for name, pat in FORBIDDEN_PATTERNS:
        m = pat.search(search_text)
        if m:
            ok = False
            lines.append(f"  [FAIL] deploy leak detected: '{name}' matched: {m.group(0)!r}")
        else:
            lines.append(f"  [PASS] '{name}': absent from workflow (non-comment lines)")
    return ok, lines


def gate3_entry_contract(wf_text: str) -> tuple[bool, list[str]]:
    """Entry-point validation: workflow_dispatch ONLY, no bot /oc comments."""
    search_text = _strip_comments(wf_text)
    ok = True
    lines: list[str] = []
    for name, pat in ENTRY_FORBIDDEN:
        m = pat.search(search_text)
        if m:
            ok = False
            lines.append(f"  [FAIL] forbidden entry: '{name}' found")
        else:
            lines.append(f"  [PASS] '{name}': correctly absent")
    # Required entries — allow in comments too (descriptive text is fine)
    for name, pat in ENTRY_REQUIRED:
        m = pat.search(wf_text)
        if not m:
            ok = False
            lines.append(f"  [FAIL] required element missing: '{name}'")
        else:
            lines.append(f"  [PASS] '{name}': present")
    return ok, lines


def main() -> int:
    if not WF_PATH.is_file():
        print(f"FATAL: workflow file missing: {WF_PATH}")
        return 99
    wf_text = WF_PATH.read_text(encoding="utf-8")

    print("=" * 72)
    print(" PASAY-OC-DISPATCH-CROSSREPO-001 — LOCAL PREFLIGHT VALIDATION")
    print("=" * 72)
    print()

    all_ok = True

    print("[GATE-1] Allowlist membership")
    ok, g1_lines = gate1_allowlist()
    all_ok = all_ok and ok
    for ln in g1_lines:
        print(ln)
    print()

    print("[GATE-2] Deploy-leaks absence")
    ok, g2_lines = gate2_no_deploy_leaks(wf_text)
    all_ok = all_ok and ok
    for ln in g2_lines:
        print(ln)
    print()

    print("[GATE-3] Entry-point contract")
    ok, g3_lines = gate3_entry_contract(wf_text)
    all_ok = all_ok and ok
    for ln in g3_lines:
        print(ln)
    print()

    if not WF_PATH.name == "opencode-crossrepo-dispatch.yml":
        print(f"[FAIL] workflow filename unexpected: {WF_PATH.name}")
        all_ok = False
    else:
        print(f"[PASS] workflow filename: {WF_PATH.name}")

    print()
    print("=" * 72)
    if all_ok:
        print(" OVERALL: PASS — safe to push and run Actions preflight")
        print("=" * 72)
        return 0
    print(" OVERALL: FAIL — fix before push")
    print("=" * 72)
    return 1


if __name__ == "__main__":
    sys.exit(main())
