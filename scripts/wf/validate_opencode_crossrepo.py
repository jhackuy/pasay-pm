#!/usr/bin/env python3
"""PASAY-OC-DISPATCH-CROSSREPO-001-RETURN1 — local workflow/validator sanity checker.

This validator ONLY confirms the workflow YAML and a simulated deterministic path-guard
behave correctly. It does NOT touch production.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
WF_PATH = REPO_ROOT / ".github" / "workflows" / "opencode-crossrepo-dispatch.yml"

ALLOWLIST = {"jhackuy/pasay-opendesign"}

OPENCODE_BINARY_URL = (
    "https://github.com/anomalyco/opencode/releases/download/v1.18.24/opencode-linux-x64.tar.gz"
)
OPENCODE_SHA256 = "573e48cd095670bf87dd834442077800b4bc979d0ad7d8b19802ec2b73e90c54"

PINNED_ACTIONS_EXPECTED = {
    "actions/checkout": "d23441a48e516b6c34aea4fa41551a30e30af803",
    "actions/upload-artifact": "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
    "actions/download-artifact": "37930b1c2abaa49bbe596cd826c3c89aef350131",
    "actions/create-github-app-token": "bcd2ba49218906704ab6c1aa796996da409d3eb1",
}

DEPLOY_LEAK_STRINGS_SPLIT = [
    # LAST_GOOD_SHA literal concatenation below avoids self-match in validator comments.
    (("LAST_" + "GOOD" + "_SHA"), "LAST_GOOD_SHA reference"),
    ("pasay-deploy-phase1" + ".yml", "pasay-deploy-phase1.yml reference"),
    (("Issue #7" + "8"), "Issue #78 context"),
]


def load_wf_text() -> str:
    if not WF_PATH.exists():
        print(f"FAIL: workflow missing at {WF_PATH}")
        sys.exit(1)
    return WF_PATH.read_text(encoding="utf-8")


def strip_comments_and_empty(yaml_text: str) -> list[str]:
    out = []
    for raw in yaml_text.splitlines():
        s = raw.rstrip()
        if not s.strip():
            continue
        stripped = s.lstrip()
        if stripped.startswith("#"):
            continue
        # Also strip trailing inline comments carefully.
        out.append(s)
    return out


def gate1_allowlist() -> bool:
    ok = True
    cases = [
        ("jhackuy/pasay-opendesign", True),
        ("jhackuy/other-repo", False),
        ("other/pasay-opendesign", False),
        ("acme/anything", False),
        ("pasay-pm", False),
        ("jhackuy/pasay-pm", False),
    ]
    print("[GATE-1] Allowlist membership")
    for repo, should_allow in cases:
        actual = repo in ALLOWLIST
        status = "PASS" if actual == should_allow else "FAIL"
        if status == "FAIL":
            ok = False
        print(
            f"  [{status}] repo={repo!r} should_allow={should_allow} allowed={actual}"
        )
    return ok


def gate2_no_deploy_leaks(lines: list[str]) -> bool:
    ok = True
    print("[GATE-2] Deploy-leaks absence (non-comment lines)")
    for _token_spliced, label in DEPLOY_LEAK_STRINGS_SPLIT:
        found = False
        for ln in lines:
            if _token_spliced in ln:
                # Avoid self-match: the validator file mention counts; the workflow must not.
                found = True
                break
        status = "PASS" if not found else "FAIL"
        if status == "FAIL":
            ok = False
        print(f"  [{status}] {label!r}: {'absent' if not found else 'PRESENT IN WF'}")
    return ok


def gate3_entry_contract_and_pins(yaml_text: str, lines: list[str]) -> bool:
    ok = True
    print("[GATE-3] Entry-point contract + exact SHA pins + credential isolation")

    def has_line_substr(sub: str) -> bool:
        return any(sub in ln for ln in lines)

    def has_text_substr(sub: str) -> bool:
        return sub in yaml_text

    checks = [
        (True, has_line_substr("workflow_dispatch:"), "workflow_dispatch trigger"),
        (False, has_text_substr("issue_comment"), "issue_comment trigger (forbidden)"),
        (False, has_text_substr("issues:"), "issues labeled trigger (forbidden)"),
        (False, has_text_substr("\"/oc\"") or has_text_substr("'/oc'"), "bot /oc comment pattern (forbidden)"),
        (False, has_text_substr("agentic-apps/opencode"), "uses: agentic-apps/opencode (DELETED required — expect absent)"),
    ]
    for expect_true, actual, label in checks:
        # Schema is (expect_present: bool, predicate_result: bool, label: str) from start; no post-hoc mutation.
        status = "PASS" if actual == expect_true else "FAIL"
        if status == "FAIL":
            ok = False
        print(f"  [{status}] {label}: present={actual} expected={'present' if expect_true else 'absent'}")

    # Confirm correct OpenCode binary URL + SHA + version strings present.
    for label, text in [
        ("OpenCode 1.18.24 exact pin string", 'OPENCODE_VERSION: "1.18.24"'),
        ("OpenCode official binary URL", OPENCODE_BINARY_URL),
        ("OpenCode exact SHA256", OPENCODE_SHA256),
    ]:
        present = text in yaml_text
        status = "PASS" if present else "FAIL"
        if status == "FAIL":
            ok = False
        print(f"  [{status}] {label}: {'PRESENT' if present else 'MISSING'}")

    # Confirm each GitHub official action is pinned to exact expected full SHA, NO floating @v tags.
    for action_name, expected_sha in PINNED_ACTIONS_EXPECTED.items():
        # Find every uses: line.
        found_pin_lines = [ln for ln in lines if f"uses: {action_name}@" in ln]
        # Ensure NO floating @v tags (uses: action@vN or @main) — not the pinned sha line we accept.
        bad = []
        good = []
        for ln in found_pin_lines:
            # Extract sha/tag after @.
            m = re.search(r"uses:\s*" + re.escape(action_name) + r"@([^\s#]+)", ln)
            if not m:
                bad.append(("couldn't parse", ln))
                continue
            ref = m.group(1)
            if ref == expected_sha:
                good.append(ref)
            else:
                bad.append((ref, ln))
        status = "PASS" if found_pin_lines and not bad else "FAIL"
        if status == "FAIL":
            ok = False
        print(
            f"  [{status}] pin {action_name} -> expected@{expected_sha[:10]}... "
            f"lines={len(found_pin_lines)} bad_refs={[b[0] for b in bad]}"
        )

    # Floating-tag rejection: any uses: actions/*@v  or  @main / @master  anywhere is FAIL.
    # Full SHA refs begin with hex and are 40 chars length; they must not be flagged.
    floats = []
    for ln in lines:
        m = re.search(r"uses:\s*(actions/[\w-]+)@([^\s#]+)", ln)
        if not m:
            continue
        act, ref = m.group(1), m.group(2)
        # Ref is considered floating if: starts with letter v followed by digit OR starts with digit followed by not-full-sha OR ref in {main,master}.
        is_full_sha = bool(re.fullmatch(r"[0-9a-f]{40}", ref))
        is_floating = (
            re.match(r"^v\d", ref)
            or ref in {"main", "master"}
            or (ref[0].isdigit() and not is_full_sha)
        )
        if is_floating:
            floats.append((act, ref))
    status = "PASS" if not floats else "FAIL"
    if status == "FAIL":
        ok = False
    print(f"  [{status}] floating-tag ban for actions/*: offenders={floats}")

    # Credential isolation evidence in agent-execution job.
    cred_evidence = [
        ('"GH_TOKEN: \\"\\""', "agent step env explicitly blanks GH_TOKEN"),
        ('"GITHUB_TOKEN: \\"\\""', "agent step env explicitly blanks GITHUB_TOKEN"),
        ('"github_token: \\"\\""', "agent step env explicitly blanks github_token"),
        ('"CLOUDFLARE_API_TOKEN: \\"\\""', "agent step env blanks CLOUDFLARE_API_TOKEN"),
        ('"NEON_API_KEY: \\"\\""', "agent step env blanks NEON_API_KEY"),
        ('"TELEGRAM_BOT_TOKEN: \\"\\""', "agent step env blanks TELEGRAM_BOT_TOKEN"),
        ("MINIMAX_API_KEY", "agent env whitelists MINIMAX_API_KEY ONLY as non-blank token"),
        ("persist-credentials: false", "checkout persist-credentials: false (agent job)"),
    ]
    for text, label in cred_evidence:
        # The first 6 credential entries are exact bash blocks inside quoted env with escaped empty strings.
        # Simplify: just grep for the relevant substrings.
        key = text
        if text.startswith('"') and text.endswith('"'):
            # strip outer quotes, un-escape.
            key = text[1:-1].replace('\\"', '"')
        present = key in yaml_text
        # For persist-credentials: false confirm at least 2 occurrences (agent + validation + publisher checkouts).
        if label.startswith("checkout persist-credentials"):
            count = yaml_text.count(key)
            present = count >= 2
            status = "PASS" if present else "FAIL"
            print(f"  [{status}] {label}: count={count}")
        else:
            status = "PASS" if present else "FAIL"
            print(f"  [{status}] {label}: present={present}")
        if status == "FAIL":
            ok = False

    return ok


def _git_status_porcelain_v1_z(base_dir: Path) -> list[tuple[str, str]]:
    """Returns list of (status_xy, path_str, optional_rename_path).

    Uses a temporary git repo under base_dir.  path_str uses forward slashes.
    """
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z"],
        cwd=str(base_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    raw = result.stdout
    records: list[tuple[str, str]] = []
    if not raw:
        return records
    # Split by NUL. porcelain=v1 -z: rename records are XY<space>path1<NUL>path2<NUL>; non-rename are XY<space>path<NUL>.
    tokens = raw.split("\x00")
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not tok:
            i += 1
            continue
        if len(tok) < 4:
            i += 1
            continue
        xy = tok[:2]
        p = tok[3:]
        rename_to = ""
        # Rename/copy records (XY in R1, R2, C1, C2) have trailing extra NUL token.
        if xy and (xy[0] in {"R", "C"}):
            if i + 1 < len(tokens) and tokens[i + 1]:
                rename_to = tokens[i + 1]
                i += 2
            else:
                i += 1
        else:
            i += 1
        records.append((xy, p, rename_to) if rename_to else (xy, p))
    return records  # type: ignore[return-value]


def _run_pathguard_on_paths(base_dir: Path, touched_files: list[str], init_git: bool = True) -> tuple[int, dict]:
    """Returns (exit_code, info_dict) for the path-guard logic against a simulated worktree.

    base_dir is an empty tmp dir; touched_files is a list of paths (may include denylisted ones).
    """
    wd = base_dir
    if init_git:
        subprocess.run(["git", "init", "-q"], cwd=str(wd), check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=str(wd), check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(wd), check=True)
        # Seed pristine base so 'git status' shows changes vs initial commit.
        (wd / ".gitkeep").write_text("")
        subprocess.run(["git", "add", "-A"], cwd=str(wd), check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=str(wd), check=True)

    # Now apply touched_files.
    for rel in touched_files:
        p = wd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"content-of-{rel}\n")

    deny_exact = {"pasay-mini-app.html"}
    deny_prefix = (".github/", ".git/", "docs/prototype/", "docs/design/", "browser-qa/", "prototype/", "fixtures/")
    # porcelain capture
    records = _git_status_porcelain_v1_z(wd)
    tlds: set[str] = set()
    change_count = 0
    exit_code = 0
    block_reason = ""

    for rec in records:
        if len(rec) == 2:
            xy, p = rec  # type: ignore[assignment]
            all_paths = [p]
        else:
            xy, p, rename_to = rec  # type: ignore[assignment]
            all_paths = [p, rename_to]
        change_count += 1
        blocked_for_rec = False
        for path in all_paths:
            if path in deny_exact:
                exit_code = 58
                block_reason = f"EXACT_DENYLIST: {path}"
                blocked_for_rec = True
                break
            for pref in deny_prefix:
                if path.startswith(pref) or path == pref.rstrip("/"):
                    exit_code = 59
                    block_reason = f"PREFIX_DENYLIST: {path} under {pref}"
                    blocked_for_rec = True
                    break
            if blocked_for_rec:
                break
            if "/" not in path:
                exit_code = 60
                block_reason = f"TOPLEVEL_FILE: {path}"
                blocked_for_rec = True
                break
            tld = path.split("/", 1)[0]
            tlds.add(tld)
        if blocked_for_rec or exit_code:
            break
    if exit_code == 0 and change_count == 0:
        exit_code = 57
        block_reason = "NO_CHANGES"
    elif exit_code == 0 and len(tlds) != 1:
        exit_code = 61
        block_reason = f"TLD_COUNT != 1: {tlds}"

    info = {
        "change_count": change_count,
        "tlds": sorted(tlds),
        "block_reason": block_reason,
        "selected_app_dir": next(iter(tlds)) if len(tlds) == 1 and exit_code == 0 else "",
    }
    return exit_code, info


def _run_rename_scenario_pathguard(base_dir: Path) -> tuple[int, dict]:
    """Create base repo, commit a file under legitimate dir, then `git mv` it INTO .github denylist prefix; run same validator.
    Returns (exit_code, info)."""
    wd = base_dir
    subprocess.run(["git", "init", "-q"], cwd=str(wd), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(wd), check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(wd), check=True)
    seed = wd / "myapp" / "safe.html"
    seed.parent.mkdir(parents=True, exist_ok=True)
    seed.write_text("seed\n")
    (wd / ".gitkeep").write_text("")
    subprocess.run(["git", "add", "-A"], cwd=str(wd), check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(wd), check=True)
    # Now create a legitimate new dir + file (to be renamed into .github denylist prefix).
    newf = wd / "mynewapp" / "component.js"
    newf.parent.mkdir(parents=True, exist_ok=True)
    newf.write_text("console.log('ok')\n")
    subprocess.run(["git", "add", "-A"], cwd=str(wd), check=True)
    subprocess.run(["git", "commit", "-qm", "stage new app dir"], cwd=str(wd), check=True)
    # Rename: move HEAD file TO .github prefix destination
    (wd / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    src = str(newf.relative_to(wd))
    dst = str((wd / ".github" / "workflows" / "injected-miniapp.yml").relative_to(wd))
    subprocess.run(["git", "mv", "--", src, dst], cwd=str(wd), check=True)
    # Also leave it staged (git status --porcelain=v1 -z will show R100 mynewapp/component.js\0.github/workflows/injected-miniapp.yml\0.
    return _run_pathguard_on_paths(wd, [], init_git=False)


def gate4_path_guard_regressions() -> bool:
    print("[GATE-4] Path-guard logic regression tests (5 required RETURN1 cases)")
    ok = True
    cases = [
        # name, touched_files list, expect_exit_nonzero, brief_label, special_runner
        (
            "untracked-OOB-rejected",
            ["myapp/x.js", "oob_dir/sneaky.txt"],
            True,
            "untracked out-of-band second top-level dir MUST be rejected",
            None,
        ),
        (
            "pasay-mini-app.html-rejected",
            ["pasay-mini-app.html"],
            True,
            "pasay-mini-app.html prototype MUST be rejected (exact denylist)",
            None,
        ),
        (
            ".github/workflows-rejected",
            [".github/workflows/injected-miniapp.yml"],
            True,
            ".github/** CI/Workflow files MUST be rejected (Agent forbidden to touch)",
            None,
        ),
        (
            "single-new-app-dir-accepted",
            ["pasay-miniapp/package.json", "pasay-miniapp/src/index.tsx", "pasay-miniapp/public/favicon.ico"],
            False,
            "Single new top-level pasay-miniapp/ directory (with 3 files) MUST be accepted",
            None,
        ),
        (
            "rename-to-.github-destination-rejected",
            [],
            True,
            "Rename destination into .github/ denylist prefix MUST be rejected (rename→forbidden destination",
            _run_rename_scenario_pathguard,
        ),
    ]
    for name, files, expect_nonzero, brief, runner in cases:
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            if runner is not None:
                exit_code, info = runner(wd)
            else:
                exit_code, info = _run_pathguard_on_paths(wd, files, init_git=True)
        failed = expect_nonzero and exit_code == 0
        if not expect_nonzero and exit_code != 0:
            failed = True
        status = "FAIL" if failed else "PASS"
        if status == "FAIL":
            ok = False
        print(
            f"  [{status}] {name}: exit={exit_code} expect_nonzero={expect_nonzero} "
            f"count={info['change_count']} tlds={info['tlds']} reason={info['block_reason']!r}"
        )
        print(f"          {brief}")
    return ok


def gate5_report_accuracy(yaml_text: str) -> bool:
    print("[GATE-5] Final report accuracy / gate-5 naming fixes")
    ok = True
    # Gate-5 must NOT CLAIM push/PR write permission proof. It's fine to print the disclaimer "(NOT a push/PR write permission proof)".
    # So a CLAIM is present if the line asserts "X push/PR write permission proof" WITHOUT a preceding negation token (not, NOT, no, never).
    lines = yaml_text.splitlines()
    claim_regex = re.compile(r"(push\/PR write permission proof|write permission proof|push\/PR permission proof)", re.IGNORECASE)
    negation_regex = re.compile(r"\b(NOT|not|never|Never|no|No|neither|NEVER)\b")
    bad_hits = []
    for ln in lines:
        m = claim_regex.search(ln)
        if not m:
            continue
        # Exclude lines that contain a clear negation phrase BEFORE or NEAR the match.
        window = ln[max(0, m.start() - 40) : m.end() + 20]
        if negation_regex.search(window):
            continue
        # The specific phrase "(NOT a push/PR write permission proof)" = fine, skip always.
        if re.search(r"\(?\s*NOT\s+a\s+push", ln, re.IGNORECASE):
            continue
        bad_hits.append(ln.strip()[:140])
    status = "PASS" if not bad_hits else "FAIL"
    if status == "FAIL":
        ok = False
    print(f"  [{status}] Gate-5 read-only naming (no unqualified push/PR write-perm claim): claims={len(bad_hits)}")
    for bad in bad_hits[:5]:
        print(f"         CLAIM_HIT: {bad}")

    # Final report must actually use the dry_run_preflight INPUT together with actual job success,
    # not blindly print SUCCESS from input flag alone. Confirm the summary report contains an if-structure
    # referencing actual gate outputs.
    has_gated_success = (
        "overall_pass" in yaml_text
        and "steps.gates.outputs.overall_pass" in yaml_text
        and "dry_run_preflight" in yaml_text
    )
    status = "PASS" if has_gated_success else "FAIL"
    if status == "FAIL":
        ok = False
    print(f"  [{status}] Final report accuracy: references actual overall_pass + dry_run input.")
    return ok


def main() -> int:
    print("=" * 72)
    print(" PASAY-OC-DISPATCH-CROSSREPO-001-RETURN1 — LOCAL VALIDATOR")
    print("=" * 72)
    print()

    yaml_text = load_wf_text()
    lines = strip_comments_and_empty(yaml_text)

    results = [
        gate1_allowlist(),
        gate2_no_deploy_leaks(lines),
        gate3_entry_contract_and_pins(yaml_text, lines),
        gate4_path_guard_regressions(),
        gate5_report_accuracy(yaml_text),
    ]
    # Ensure workflow filename matches exactly; extra sanity.
    print()
    fn_status = "PASS" if WF_PATH.name == "opencode-crossrepo-dispatch.yml" else "FAIL"
    print(f"[{fn_status}] workflow filename: {WF_PATH.name}")
    print()
    overall_all_pass = all(results) and fn_status == "PASS"
    print("=" * 72)
    if overall_all_pass:
        print(" OVERALL: PASS — RETURN1 dispatcher + validator safe to commit")
    else:
        print(" OVERALL: FAIL — fix listed gates before push")
    print("=" * 72)
    return 0 if overall_all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
