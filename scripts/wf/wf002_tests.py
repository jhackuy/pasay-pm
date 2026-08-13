"""WF-002 regression tests (Program First; all assertions are deterministic)."""

from __future__ import annotations

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

MAC_HOST = "macmini"
MAC_REPO = "/Users/jhackuy/Projects/pasay-pm"
SYNC_PS1 = r"D:\AI-Review\sync-pasay.ps1"
AGENTS_WIN = os.path.join(REPO, "AGENTS.md")
RESULTS_DIR = os.path.join(REPO, ".ai-control", "results", "WF-002")
OLD_AGENTS_SHA = "58f1357f6e811f0a3ac93f1951a5751ee426a5223314f1d61e0933252d674a66"


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
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def mac_sha(rel: str):
    rc, out = sh(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", MAC_HOST,
                  f"sha256sum {MAC_REPO}/{rel}"], timeout=40)
    return out.split()[0].lower() if rc == 0 and out.strip() else None


def mac_read(rel: str):
    tmp = tempfile.NamedTemporaryFile(prefix="wf002_mac_", delete=False)
    tmp.close()
    try:
        rc, _ = sh(["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                    f"{MAC_HOST}:{MAC_REPO}/{rel}", tmp.name], timeout=40)
        if rc != 0:
            return None
        return read(tmp.name)
    finally:
        os.unlink(tmp.name)


def mac_write(rel: str, content: str) -> bool:
    tmp = tempfile.NamedTemporaryFile(prefix="wf002_win_", suffix=".tmp", delete=False)
    tmp.close()
    write(tmp.name, content)
    try:
        rc, _ = sh(["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                    tmp.name, f"{MAC_HOST}:{MAC_REPO}/{rel}"], timeout=40)
        return rc == 0
    finally:
        os.unlink(tmp.name)


def run_sync():
    rc, out = sh(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", SYNC_PS1], timeout=180)
    return rc, out


def reset_reflog_count():
    rc, out = sh(["git", "-C", REPO, "reflog"], timeout=30)
    return sum(1 for line in out.splitlines() if "reset: moving" in line)


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

    canonical = mac_read("AGENTS.md")
    orig = read(AGENTS_WIN)
    marker = "\n<!-- WF002-T1-MARKER -->\n"
    write(AGENTS_WIN, orig + marker)
    before_reset_count = reset_reflog_count()

    # Observe up to 90s: the OLD mechanism must not restore AGENTS.md to old Git HEAD.
    reverted = False
    deadline = time.time() + 90
    while time.time() < deadline:
        cur = read(AGENTS_WIN)
        if sha_text(cur) == OLD_AGENTS_SHA:
            reverted = True
            break
        time.sleep(5)

    rc, out = run_sync()  # simulate the bridge reconcile trigger with the fixed script
    final = read(AGENTS_WIN)
    after_reset_count = reset_reflog_count()
    ok = (
        static_ok
        and not reverted
        and sha_text(final) == sha_text(canonical)
        and after_reset_count == before_reset_count
        and "WF002-T1-MARKER" not in final
    )
    return ok, {
        "static_forbidden_commands": forbidden,
        "auto_reverted_to_old_head": reverted,
        "sync_exit": rc,
        "final_matches_canonical": sha_text(final) == sha_text(canonical),
        "new_reset_reflog_entries": after_reset_count - before_reset_count,
    }


def t2_canonical_to_windows():
    backup = mac_read("AGENTS.md")
    if backup is None:
        return False, {"error": "cannot read Mac AGENTS.md"}
    marker = "\n<!-- WF002-T2-MARKER -->\n"
    ok_write = mac_write("AGENTS.md", backup + marker)
    mac_after_write = mac_read("AGENTS.md")
    synced = False
    try:
        rc, _ = run_sync()
        win_after = read(AGENTS_WIN)
        synced = ("WF002-T2-MARKER" in win_after) and (sha_text(win_after) == sha_text(mac_after_write))
    finally:
        # restore canonical on Mac, then re-sync Windows
        mac_write("AGENTS.md", backup)
        rc2, _ = run_sync()
        win_restored = sha_text(read(AGENTS_WIN)) == sha_text(backup)
    return (ok_write and synced and win_restored), {
        "mac_write_ok": ok_write,
        "windows_received_marker": synced,
        "windows_restored_after_canonical_restore": win_restored,
    }


def t3_no_windows_to_canonical():
    mac_before = mac_sha("AGENTS.md")
    orig = read(AGENTS_WIN)
    write(AGENTS_WIN, orig + "\n<!-- WF002-T3-MARKER -->\n")
    rc, _ = run_sync()
    mac_after = mac_sha("AGENTS.md")
    win_final = read(AGENTS_WIN)
    ok = (mac_before is not None and mac_after == mac_before and "WF002-T3-MARKER" not in win_final)
    return ok, {
        "mac_before": mac_before,
        "mac_after": mac_after,
        "mac_unchanged": mac_before == mac_after,
        "windows_marker_removed_by_canonical_sync": "WF002-T3-MARKER" not in win_final,
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
    for tid, name, fn in SUITES:
        try:
            ok, detail = fn()
        except Exception as exc:  # pragma: no cover - defensive
            ok, detail = False, {"exception": str(exc)}
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
