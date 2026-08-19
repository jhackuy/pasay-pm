"""WF-002 regression tests (Program First; all assertions are deterministic)."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "scripts", "wf"))
import wf_lib as wf  # noqa: E402
import wf_ctl  # noqa: E402

CANONICAL_REPO = REPO
SYNC_PS1 = os.path.join(REPO, "scripts", "wf", "sync-pasay.ps1")
CANONICAL_RULES = os.path.join(REPO, "AI_WORKFLOW_RULES.md")
LEGACY_RULES = os.path.join(REPO, ".ai-control", "RULES.md")
AGENTS_MD = os.path.join(REPO, "AGENTS.md")
RESULTS_DIR = os.path.join(REPO, ".ai-control", "results", "WF-002")
OLD_RULES_SHA = "58f1357f6e811f0a3ac93f1951a5751ee426a5223314f1d61e0933252d674a66"
WORKTREE_OVERLAY_FILES = [
    "AI_WORKFLOW_RULES.md",
    "scripts/wf/sync-pasay.ps1",
]


def sh(cmd, timeout=120):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as exc:
        return -1, str(exc)


def sha_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def read(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write(path: str, content: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def repo_path(root: str, rel: str) -> str:
    return os.path.join(root, rel.replace("/", os.sep))


def repo_sha(root: str, rel: str):
    return wf.sha256_file(repo_path(root, rel))


def repo_read(root: str, rel: str):
    path = repo_path(root, rel)
    return read(path) if os.path.isfile(path) else None


def repo_write(root: str, rel: str, content: str) -> bool:
    path = repo_path(root, rel)
    write(path, content)
    return True


def run_sync(root: str):
    sync_ps1 = repo_path(root, "scripts/wf/sync-pasay.ps1")
    rc, out = sh(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", sync_ps1], timeout=180)
    return rc, out


def reset_reflog_count(root: str):
    rc, out = sh(["git", "-C", root, "reflog"], timeout=30)
    return sum(1 for line in out.splitlines() if "reset: moving" in line)


def governance_snapshot():
    return {
        "AGENTS.md": wf.sha256_file(AGENTS_MD),
        "AI_WORKFLOW_RULES.md": wf.sha256_file(CANONICAL_RULES),
    }


def overlay_current_governance_sources(root: str) -> None:
    for rel in WORKTREE_OVERLAY_FILES:
        src = repo_path(REPO, rel)
        dst = repo_path(root, rel)
        write(dst, read(src))


@contextmanager
def temporary_worktree():
    with tempfile.TemporaryDirectory(prefix="wf002_") as tmpdir:
        rc, out = sh(["git", "-C", REPO, "worktree", "add", "--detach", tmpdir, "HEAD"], timeout=180)
        if rc != 0:
            raise RuntimeError(f"git worktree add failed: {out.strip()}")
        try:
            overlay_current_governance_sources(tmpdir)
            yield tmpdir
        finally:
            sh(["git", "-C", REPO, "worktree", "remove", "--force", tmpdir], timeout=180)


def t1_no_old_head_restore():
    # Static: the hardened sync script must not contain a reset/forced-checkout command.
    content = read(os.path.join(REPO, "scripts", "wf", "sync-pasay.ps1"))
    cmd_lines = [l.strip() for l in content.splitlines()
                 if l.strip().startswith("& git") and not l.strip().startswith("#")]
    forbidden = [l for l in cmd_lines
                 if re.search(r"reset\s+(--hard|--soft|--mixed|HEAD\b)", l)
                 or re.search(r"checkout[^\n]*\s+-f\b", l)
                 or "--force" in l]
    static_ok = not forbidden

    with temporary_worktree() as root:
        canonical = read(repo_path(root, "AI_WORKFLOW_RULES.md"))
        legacy_rules = repo_path(root, ".ai-control/RULES.md")
        orig = read(legacy_rules) if os.path.isfile(legacy_rules) else ""
        marker = "\n<!-- WF002-T1-MARKER -->\n"
        write(legacy_rules, orig + marker)
        before_reset_count = reset_reflog_count(root)
        rc, out = run_sync(root)
        final = read(legacy_rules)
        after_reset_count = reset_reflog_count(root)

    ok = (
        static_ok
        and sha_text(final) == sha_text(canonical)
        and after_reset_count == before_reset_count
        and "WF002-T1-MARKER" not in final
    )
    return ok, {
        "static_forbidden_commands": forbidden,
        "sync_exit": rc,
        "final_matches_canonical": sha_text(final) == sha_text(canonical),
        "new_reset_reflog_entries": after_reset_count - before_reset_count,
    }


def t2_canonical_to_windows():
    with temporary_worktree() as root:
        backup = repo_read(root, "AI_WORKFLOW_RULES.md")
        if backup is None:
            return False, {"error": "cannot read canonical rules"}
        marker = "\n<!-- WF002-T2-MARKER -->\n"
        ok_write = repo_write(root, "AI_WORKFLOW_RULES.md", backup + marker)
        canonical_after_write = repo_read(root, "AI_WORKFLOW_RULES.md")
        rc, _ = run_sync(root)
        mirror_after = repo_read(root, ".ai-control/RULES.md")
        synced = ("WF002-T2-MARKER" in mirror_after) and (sha_text(mirror_after) == sha_text(canonical_after_write))
        mirror_restored = sha_text(mirror_after) == sha_text(canonical_after_write)
    return (ok_write and synced and mirror_restored), {
        "mac_write_ok": ok_write,
        "legacy_mirror_received_marker": synced,
        "legacy_mirror_matches_isolated_canonical": mirror_restored,
    }


def t3_no_windows_to_canonical():
    with temporary_worktree() as root:
        canonical_before = repo_sha(root, "AI_WORKFLOW_RULES.md")
        legacy_rules = repo_path(root, ".ai-control/RULES.md")
        orig = read(legacy_rules) if os.path.isfile(legacy_rules) else ""
        write(legacy_rules, orig + "\n<!-- WF002-T3-MARKER -->\n")
        rc, _ = run_sync(root)
        canonical_after = repo_sha(root, "AI_WORKFLOW_RULES.md")
        mirror_final = read(legacy_rules)
    ok = (canonical_before is not None and canonical_after == canonical_before and "WF002-T3-MARKER" not in mirror_final)
    return ok, {
        "canonical_before": canonical_before,
        "canonical_after": canonical_after,
        "canonical_unchanged": canonical_before == canonical_after,
        "legacy_marker_removed_by_canonical_sync": "WF002-T3-MARKER" not in mirror_final,
        "sync_exit": rc,
    }


def t4_rules_preflight():
    canonical = wf.resolve_canonical(mirror_available=True)
    sha = canonical["sha256"]
    ok_pre = wf.preflight(expected_sha=sha, mirror_available=True)
    bad_pre = wf.preflight(expected_sha="0" * 64, mirror_available=True)
    ok = ok_pre["status"] == "RULES_PREFLIGHT_OK" and bad_pre["status"] == "BLOCKED_RULES_MISMATCH"
    worker_started = bad_pre["status"] == "RULES_PREFLIGHT_OK"
    return ok and not worker_started, {
        "correct_hash": ok_pre["status"],
        "wrong_hash": bad_pre["status"],
        "worker_started_on_mismatch": worker_started,
    }


def t5_wf001_suite():
    # In-process re-run of the WF-001 isolation checks (worktree / allowed_paths /
    # old_worker / ACK) — no SSH, fully deterministic.
    results = {}
    for name, fn in (("worktree_isolation", wf_ctl._test3),
                     ("old_worker_isolation", wf_ctl._test4),
                     ("ack_parser", wf_ctl._test5)):
        ok, detail = fn()
        results[name] = {"ok": ok, "detail": detail}
    all_ok = all(v["ok"] for v in results.values())
    return all_ok, results


SUITES = [
    ("TEST-1", "no-old-head-restore", t1_no_old_head_restore),
    ("TEST-2", "canonical-to-windows-sync", t2_canonical_to_windows),
    ("TEST-3", "no-windows-to-canonical", t3_no_windows_to_canonical),
    ("TEST-4", "rules-preflight", t4_rules_preflight),
    ("TEST-5", "wf001-isolation-suite", t5_wf001_suite),
]


def main():
    summary = {"task_id": "WF-002", "run_at": wf.now_iso(), "tests": {}}
    os.makedirs(RESULTS_DIR, exist_ok=True)
    baseline_snapshot = governance_snapshot()
    for tid, name, fn in SUITES:
        try:
            ok, detail = fn()
        except Exception as exc:  # pragma: no cover - defensive
            ok, detail = False, {"exception": str(exc)}
        current_snapshot = governance_snapshot()
        if current_snapshot != baseline_snapshot:
            ok = False
            detail = dict(detail)
            detail["main_repo_side_effect"] = {
                "baseline": baseline_snapshot,
                "current": current_snapshot,
            }
        summary["tests"][tid] = {"name": name, "result": "PASS" if ok else "FAIL", "detail": detail}
        with open(os.path.join(RESULTS_DIR, "tests.json"), "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
    all_pass = all(t["result"] == "PASS" for t in summary["tests"].values())
    summary["overall"] = "PASS" if all_pass else "FAIL"
    with open(os.path.join(RESULTS_DIR, "tests.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
