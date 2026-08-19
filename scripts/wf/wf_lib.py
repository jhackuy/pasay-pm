"""WF-001 deterministic workflow control library.

Program First, LLM Last: every decision here is computed, never asked of a model.
Pure stdlib; Python 3.9+.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RULES_REPO_REL_PATH = "AI_WORKFLOW_RULES.md"
CANONICAL_RULES_PATH = os.path.join(REPO, RULES_REPO_REL_PATH)
CANONICAL_RULES_DISPLAY_PATH = "<repo-root>/AI_WORKFLOW_RULES.md"
WIN_MIRROR_PATH = CANONICAL_RULES_PATH
LEGACY_MIRROR_PATH = os.path.join(REPO, ".ai-control", "RULES.md")
STATE_DIR = os.path.join(REPO, ".ai-control", "state")
RESULTS_DIR = os.path.join(REPO, ".ai-control", "results")
SESSIONS_FILE = os.path.join(STATE_DIR, "sessions.json")
_CANONICAL_CACHE = {}

DEFAULT_SH_TIMEOUT = 60  # WF guardrail G4: git/status/hash/process checks 60s.
TIMEOUT_STATUS = "TIMEOUT"


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _record_run(record_path, record) -> None:
    """Append one JSONL timeout/command record (runtime-only, never committed)."""
    if not record_path:
        return
    os.makedirs(os.path.dirname(record_path), exist_ok=True)
    with open(record_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _kill_process_tree(proc) -> None:
    """Best-effort child-process-tree termination (Windows taskkill /T, POSIX
    process group). Never raises; the caller reaps the process afterwards."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=20,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:  # pragma: no cover - best effort
        try:
            proc.kill()
        except Exception:
            pass


def run_timed(command, timeout, cwd=None, shell=False, record_path=None):
    """Programmatic command wrapper with hard timeout enforcement (G4).

    Returns a structured record. On timeout: status=TIMEOUT, elapsed recorded,
    the child process tree is terminated, and no automatic retry happens.
    `timeout` is required so every workflow command carries an explicit bound.
    """
    start = time.monotonic()
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            shell=shell,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
            start_new_session=(os.name != "nt"),
        )
    except Exception as exc:  # pragma: no cover - defensive
        record = {
            "status": "FAILED_TO_START",
            "command": command,
            "timeout": timeout,
            "elapsed": round(time.monotonic() - start, 3),
            "returncode": None,
            "stdout": "",
            "stderr": str(exc),
        }
        _record_run(record_path, record)
        return record

    base = {"command": command, "timeout": timeout, "pid": proc.pid,
            "started_at": now_iso()}
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        record = {
            "status": "OK",
            "elapsed": round(time.monotonic() - start, 3),
            "returncode": proc.returncode,
            "stdout": stdout or "",
            "stderr": stderr or "",
            **base,
        }
        _record_run(record_path, record)
        return record
    except subprocess.TimeoutExpired:
        elapsed = round(time.monotonic() - start, 3)
        _kill_process_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except Exception:  # pragma: no cover - best effort
            stdout, stderr = "", ""
        record = {
            "status": TIMEOUT_STATUS,
            "elapsed": elapsed,
            "returncode": proc.returncode,
            "stdout": stdout or "",
            "stderr": stderr or "",
            "terminated": True,
            **base,
        }
        _record_run(record_path, record)
        return record


def sh(cmd, cwd=None, timeout=DEFAULT_SH_TIMEOUT):
    try:
        rec = run_timed(cmd, timeout=timeout, cwd=cwd)
        if rec["status"] == TIMEOUT_STATUS:
            return -1, "TIMEOUT command=%s elapsed=%ss" % (
                rec["command"], rec["elapsed"])
        if rec["status"] != "OK":
            return -1, (rec.get("stderr") or rec.get("stdout") or rec["status"])
        return rec["returncode"], (rec["stdout"] or "") + (rec["stderr"] or "")
    except Exception as exc:  # pragma: no cover - defensive
        return -1, str(exc)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest().lower()


def sha256_file(path: str):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest().lower()
    except OSError:
        return None


def read_file(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def write_file(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return None


def read_canonical_remote(path=CANONICAL_RULES_PATH, timeout=40):
    """Read the canonical rules file from the current repository root.

    The `timeout` parameter is kept for API compatibility with older callers.
    """
    del timeout
    content = read_file(path)
    return (True, content) if content is not None else (False, "read failed")


def clear_canonical_cache():
    _CANONICAL_CACHE.clear()


def resolve_canonical(mirror_available=True, force_remote=False):
    """Locate authoritative rules content from the repository root.

    Canonical content is cached per process so repeated preflights only read once.
    """
    if not force_remote and _CANONICAL_CACHE:
        return dict(_CANONICAL_CACHE)
    ok, content = read_canonical_remote()
    if ok and content.strip():
        res = {"source": "canonical", "path": CANONICAL_RULES_DISPLAY_PATH, "content": content,
               "sha256": sha256_text(content)}
        _CANONICAL_CACHE.update(res)
        return res
    if mirror_available:
        for label, path in (("legacy-mirror", LEGACY_MIRROR_PATH),):
            content = read_file(path)
            if content is not None and content.strip():
                res = {"source": label, "path": path, "content": content,
                       "sha256": sha256_text(content)}
                _CANONICAL_CACHE.update(res)
                return res
    return {"source": "missing", "path": None, "content": None, "sha256": None}


def parse_rules_version(content: str):
    m = re.search(r"rules_version:\s*([A-Za-z0-9._\-]+)", content)
    return m.group(1) if m else None


def preflight(expected_sha=None, mirror_available=True):
    """Rules preflight. Status: RULES_PREFLIGHT_OK / BLOCKED_RULES_MISMATCH / BLOCKED_RULES_MISSING."""
    res = resolve_canonical(mirror_available=mirror_available)
    base = {
        "source": res["source"],
        "path": res["path"],
        "sha256": res["sha256"],
        "rules_version": parse_rules_version(res["content"]) if res["content"] else None,
        "expected_sha256": expected_sha.lower() if expected_sha else None,
        "checked_at": now_iso(),
    }
    if res["content"] is None:
        base["status"] = "BLOCKED_RULES_MISSING"
        return base
    if expected_sha and res["sha256"] != expected_sha.lower():
        base["status"] = "BLOCKED_RULES_MISMATCH"
        return base
    base["status"] = "RULES_PREFLIGHT_OK"
    return base


ACK_RE = re.compile(r"RULES_ACK\s+role=(MAX|LILY)\s+sha256=([0-9a-fA-F]{64})")


def parse_ack(text: str, expected_sha=None):
    """Parse a single RULES_ACK line. Returns ACK_VALID / ACK_INVALID / ACK_MISSING."""
    m = ACK_RE.search(text or "")
    if not m:
        return "ACK_MISSING"
    sha = m.group(2).lower()
    if expected_sha is None or sha == expected_sha.lower():
        return "ACK_VALID"
    return "ACK_INVALID"


def git(repo, *args):
    return sh(["git", *args], cwd=repo)


def snapshot(repo=REPO):
    """Programmatic working-tree snapshot."""
    rc, branch = git(repo, "branch", "--show-current")
    rc2, head = git(repo, "rev-parse", "HEAD")
    rc3, out = git(repo, "status", "--porcelain")
    raw = [line for line in out.splitlines() if line.strip()]
    tracked_modified, staged, untracked = [], [], []
    for line in raw:
        if line.startswith("??"):
            untracked.append(line[3:])
        elif line.startswith(" "):
            tracked_modified.append(line[3:])
        elif line.startswith("M") or line.startswith("A") or line.startswith("D") or line.startswith("R"):
            staged.append(line[3:])
        else:
            tracked_modified.append(line[3:])
    return {
        "branch": branch.strip(),
        "head": head.strip(),
        "tracked_modified": sorted(set(tracked_modified)),
        "staged": sorted(set(staged)),
        "untracked": sorted(set(untracked)),
        "raw": raw,
        "captured_at": now_iso(),
    }


def _norm(p: str) -> str:
    return os.path.normpath(p.replace("\\", "/")).replace("\\", "/")


def _within(path: str, allowed_prefixes) -> bool:
    norm = _norm(path)
    for prefix in allowed_prefixes or []:
        p = _norm(prefix).rstrip("/")
        if norm == p or norm.startswith(p + "/"):
            return True
    return False


def allowed_path_check(baseline, current, allowed_paths, forbidden_paths=None):
    """Compare a post-execution snapshot against baseline; enforce allowed_paths.

    Returns {"verdict": PASS|BLOCKED_SCOPE_VIOLATION, "offenders": [...]}.
    """
    offenders = []
    new_modified = set(current.get("tracked_modified", [])) - set(baseline.get("tracked_modified", []))
    new_staged = set(current.get("staged", [])) - set(baseline.get("staged", []))
    new_untracked = set(current.get("untracked", [])) - set(baseline.get("untracked", []))
    for path in sorted(new_modified | new_staged | new_untracked):
        if _within(path, forbidden_paths):
            offenders.append({"path": path, "reason": "forbidden_paths"})
        elif not _within(path, allowed_paths):
            offenders.append({"path": path, "reason": "outside_allowed_paths"})
    return {"verdict": "PASS" if not offenders else "BLOCKED_SCOPE_VIOLATION",
            "offenders": offenders}


def load_sessions():
    data = read_json(SESSIONS_FILE)
    return data if isinstance(data, list) else []


def save_sessions(records) -> None:
    write_json(SESSIONS_FILE, records)


def pid_alive(pid) -> bool:
    if os.name == "nt":
        # Windows os.kill(pid, 0) is unreliable (EINVAL for processes outside
        # the current session/elevation). Use OpenProcess + GetExitCodeProcess:
        # deterministic, read-only, no kill semantics.
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            SYNCHRONIZE = 0x00100000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE,
                False,
                int(pid),
            )
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                ok = ctypes.windll.kernel32.GetExitCodeProcess(
                    handle, ctypes.byref(code))
                return bool(ok) and code.value == 259  # STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:  # pragma: no cover - defensive
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True
    except OSError as exc:
        # Windows: os.kill(pid, 0) on a missing process raises OSError errno=22.
        if getattr(exc, "errno", None) in (22, 3):
            return False
        return True


def register_session(task_id, session_id, pid, worktree) -> dict:
    records = load_sessions()
    record = {
        "task_id": task_id,
        "session_id": session_id,
        "pid": int(pid),
        "worktree": worktree,
        "status": "OPEN",
        "start_time": now_iso(),
        "closed_at": None,
    }
    records.append(record)
    save_sessions(records)
    return record


def close_session(task_id, session_id) -> dict:
    records = load_sessions()
    for rec in records:
        if rec["task_id"] == task_id and rec["session_id"] == session_id:
            rec["status"] = "CLOSED"
            rec["closed_at"] = now_iso()
            save_sessions(records)
            return rec
    return None


def find_orphans():
    return [rec for rec in load_sessions() if rec.get("status") == "OPEN" and not pid_alive(rec.get("pid"))]


def guard_worktree(task_id, session_id, worktree):
    """Verify a worker session may act inside the given task worktree."""
    rec = next((r for r in load_sessions() if r.get("session_id") == session_id), None)
    if rec is None:
        return {"verdict": "BLOCKED_SESSION_MISMATCH", "reason": "session not found"}
    if rec.get("task_id") != task_id:
        return {"verdict": "BLOCKED_SESSION_MISMATCH", "reason": "task_id mismatch"}
    if _norm(rec.get("worktree", "")) != _norm(worktree):
        return {"verdict": "BLOCKED_SESSION_MISMATCH", "reason": "worktree mismatch"}
    if rec.get("status") == "CLOSED":
        return {"verdict": "BLOCKED_CLOSED", "reason": "session closed"}
    if not pid_alive(rec.get("pid")):
        return {"verdict": "BLOCKED_ORPHAN", "reason": "worker pid not alive"}
    return {"verdict": "ALLOWED", "reason": "session bound and alive"}


def worktrees_root(repo=REPO):
    return os.path.join(os.path.dirname(repo), os.path.basename(repo) + "-worktrees")


def make_worktree(task_id, base_ref="HEAD", repo=REPO):
    """Create a task-specific git worktree OUTSIDE the main working tree."""
    root = worktrees_root(repo)
    os.makedirs(root, exist_ok=True)
    wt_path = os.path.join(root, task_id)
    branch = f"wf/{task_id}-{_dt.datetime.now().strftime('%Y%m%d%H%M%S')}"
    rc, out = git(repo, "worktree", "add", "-b", branch, wt_path, base_ref)
    if rc != 0:
        return {"ok": False, "error": out.strip()}
    return {"ok": True, "path": wt_path, "branch": branch}


def remove_worktree(path, repo=REPO):
    """Remove a task worktree we created (path + branch cleanup)."""
    path = os.path.abspath(path)
    if not path.startswith(os.path.abspath(worktrees_root(repo)) + os.sep):
        return {"ok": False, "error": "path outside managed worktrees root"}
    rc, out = git(repo, "worktree", "remove", "--force", path)
    if rc != 0:
        return {"ok": False, "error": out.strip()}
    return {"ok": True, "removed": path}


def main_argv_default() -> None:
    sys.stderr.write("wf_lib is a library; use wf_ctl.py\n")
    sys.exit(2)


if __name__ == "__main__":
    main_argv_default()
