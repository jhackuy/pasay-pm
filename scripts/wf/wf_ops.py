"""WF-003: programmatic workflow operations (router, retry, metrics, test levels,
log reducer, human-test gate, task lock, state machine, safety regression).

Program First, LLM Last: every decision here is deterministic.
"""

from __future__ import annotations

import json
import os
import re
import time

import wf_lib as wf

OPS_STATE_DIR = os.path.join(wf.STATE_DIR, "wf003")
TASK_LOCKS_FILE = os.path.join(OPS_STATE_DIR, "task_locks.json")
TASK_STATES_FILE = os.path.join(OPS_STATE_DIR, "task_states.json")


def _ensure_dir(path):
    os.makedirs(path, exist_ok=True)
    return path


def _read_json(path, default):
    data = wf.read_json(path)
    return data if isinstance(data, default.__class__) else default


# ---------------------------------------------------------------------------
# Task schema
# ---------------------------------------------------------------------------

REQUIRED_TASK_FIELDS = [
    "task_id",
    "task_type",
    "risk_level",
    "objective",
    "allowed_paths",
    "acceptance_criteria",
]


def validate_task(task):
    """Return (ok, errors). Worker dispatch is FAIL CLOSED without a task_id
    or allowed_paths."""
    errors = []
    for field in REQUIRED_TASK_FIELDS:
        if task.get(field) in (None, "", []):
            errors.append(f"missing field: {field}")
    if not isinstance(task.get("allowed_paths", []), list) or not task.get("allowed_paths"):
        errors.append("worker without allowed_paths")
    if not task.get("task_id"):
        errors.append("worker without task_id")
    return (not errors), errors


# ---------------------------------------------------------------------------
# 1. Task Router
# ---------------------------------------------------------------------------

LILY_ROUTE_CONDITIONS = [
    ("requires_supervisor=true", lambda t: t.get("requires_supervisor") is True),
    ("max_retry_reached", lambda t: t.get("_max_attempts", 0) >= int(t.get("max_retry", 2))),
    ("worker_returned_NEEDS_SUPERVISOR", lambda t: t.get("_needs_supervisor") is True),
    ("unresolved_architecture_conflict", lambda t: t.get("_architecture_conflict") is True),
    ("long_running_multistage_requires_supervisor", lambda t: t.get("_supervisor_required_by_task") is True),
]


def route_task(task):
    """Deterministic route. Returns (route, route_reason)."""
    ok, errors = validate_task(task)
    if not ok:
        return ("FAILED", "invalid task: " + "; ".join(errors))
    # Explicit escalation wins.
    for reason, cond in LILY_ROUTE_CONDITIONS:
        if cond(task):
            return ("LILY", reason)
    ttype = task.get("task_type", "")
    if ttype in ("check", "query", "report", "verify", "test", "inspection"):
        return ("PROGRAMMATIC", "task_type in check/query/report/verify/test; scripts/CLI/SQL can complete it")
    if ttype in ("code", "fix", "feature", "refactor", "api", "bot_ux", "test_authoring"):
        return ("MAX", "ordinary code-change task; single Max session default")
    if ttype in ("migration", "release", "merge", "audit", "architecture"):
        return ("MAX", "code-adjacent task; Max first, escalate only on evidence")
    return ("PROGRAMMATIC", "task_type unknown; lowest-cost default")


def should_escalate(attempts, max_retry=None):
    """Max retry threshold. Default max_retry=2."""
    if max_retry is None:
        max_retry = 2
    return attempts >= int(max_retry)


# ---------------------------------------------------------------------------
# 3. Metrics
# ---------------------------------------------------------------------------

def record_metrics(task, route, start_time=None, end_time=None, result="UNKNOWN",
                   max_sessions=0, lily_sessions=0, fugui_llm_calls=None,
                   max_attempts=None, human_interventions=0, test_runs=0, test_failures=0):
    """Write .ai-control/results/<task_id>/metrics.json (runtime only, not committed)."""
    task_id = task.get("task_id", "UNKNOWN")
    start = start_time if start_time is not None else time.time()
    end = end_time if end_time is not None else time.time()
    metrics = {
        "task_id": task_id,
        "route": route,
        "fugui_llm_calls": fugui_llm_calls if fugui_llm_calls is not None else 0,
        "max_sessions": int(max_sessions),
        "lily_sessions": int(lily_sessions),
        "max_attempts": int(max_attempts) if max_attempts is not None else int(task.get("max_retry", 2)),
        "start_time": start,
        "end_time": end,
        "duration_seconds": round(float(end) - float(start), 3),
        "human_interventions": int(human_interventions),
        "test_runs": int(test_runs),
        "test_failures": int(test_failures),
        "result": result,
        "input_tokens": "UNKNOWN",
        "output_tokens": "UNKNOWN",
        "total_tokens": "UNKNOWN",
        "token_usage_source": "UNKNOWN",
    }
    out = _ensure_dir(os.path.join(wf.RESULTS_DIR, task_id))
    wf.write_json(os.path.join(out, "metrics.json"), metrics)
    return metrics


# ---------------------------------------------------------------------------
# 4. Test levels
# ---------------------------------------------------------------------------

L3_TRIGGERS = {
    "migration": "migration",
    "release": "merge/release gate",
    "merge": "merge/release gate",
    "full-regression": "explicit full regression",
}


def select_test_level(task, l2_cross_module_risk=False):
    """Returns (level, reason). Default ordinary change: L1 -> L2. L3 only on triggers."""
    ttype = task.get("task_type", "")
    risk = task.get("risk_level", "LOW")
    explicit = task.get("test_level")
    if explicit in ("L3", "full"):
        return ("L3", "explicit test_level=L3")
    if ttype in L3_TRIGGERS:
        return ("L3", L3_TRIGGERS[ttype])
    if risk == "HIGH" and ttype in ("auth", "identity", "rbac", "finance", "financial_write", "core_infrastructure"):
        return ("L3", f"high-risk {ttype}")
    if risk == "HIGH" and ttype in ("code", "api", "bot_ux") and l2_cross_module_risk:
        return ("L3", "L2 showed cross-module risk")
    return ("L1", "ordinary change: L1 targeted, then L2 regression")


def test_result(exit_code, tests_passed=0, tests_failed=0, test_level_used=None):
    return {
        "test_level_used": test_level_used,
        "tests_passed": tests_passed,
        "tests_failed": tests_failed,
        "exit_code": exit_code,
        "verdict": "PASS" if (exit_code == 0 and tests_failed == 0) else "FAIL",
    }


# ---------------------------------------------------------------------------
# 5. Log reducer
# ---------------------------------------------------------------------------

ERROR_RE = re.compile(r"^(ERROR|FAILED|Traceback|.*Error:|.*Exception:|.*assert)", re.IGNORECASE)


def reduce_log(raw_log, task_id, command=None, exit_code=None, max_chars=4000, max_lines=80):
    """Save raw log to disk; return a compact LLM payload (logs live on disk, not in prompt)."""
    logs_dir = _ensure_dir(os.path.join(wf.RESULTS_DIR, task_id, "logs"))
    raw_path = os.path.join(logs_dir, "raw.log")
    wf.write_file(raw_path, raw_log)

    lines = raw_log.splitlines()
    failed_tests = []
    errors = []
    traceback = []
    in_tb = False
    for i, line in enumerate(lines):
        if "FAILED" in line and ("test" in line.lower() or "::" in line):
            failed_tests.append(line.strip())
        if ERROR_RE.match(line):
            errors.append(line.strip())
        if line.startswith("Traceback"):
            in_tb = True
        if in_tb:
            traceback.append(line)
            if i + 1 < len(lines) and lines[i + 1] and not lines[i + 1][0].isspace():
                in_tb = False
    if not traceback and errors:
        traceback = errors[:10]

    payload = []
    if command is not None:
        payload.append(f"command: {command}")
    if exit_code is not None:
        payload.append(f"exit_code: {exit_code}")
    if failed_tests:
        payload.append("failed_test_names:")
        payload.extend(f"- {t}" for t in failed_tests[:20])
    if errors:
        payload.append("ERROR/FAILED lines:")
        payload.extend(errors[:30])
    if traceback:
        payload.append("traceback (relevant section):")
        payload.extend(traceback[:25])
    payload.append("last relevant lines:")
    payload.extend(lines[-min(15, max_lines):])

    text = "\n".join(payload)
    truncated = False
    if len(text) > max_chars or len(payload) > max_lines:
        text = "\n".join(payload)[:max_chars]
        truncated = True
    if truncated:
        text += f"\n[log truncated; full raw log: {raw_path}]"
    return {
        "payload": text,
        "raw_log_path": raw_path,
        "raw_log_chars": len(raw_log),
        "payload_chars": len(text),
        "reduction_ratio": round(len(text) / max(1, len(raw_log)), 4),
        "failed_test_names": failed_tests,
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# 6. Human test gate
# ---------------------------------------------------------------------------

HUMAN_TEST_TYPES = (
    "OWNER_TELEGRAM_UX",
    "WINDOWS_VISUAL",
    "AUTHORIZATION",
    "PRODUCT_DECISION",
    "PHYSICAL_ACTION",
)


def human_test_notification(task, steps=None):
    """requires_human_test=false -> no notification (None).
    true -> minimal fixed-format instructions only."""
    if task.get("requires_human_test") is not True:
        return None
    htype = task.get("human_test_type")
    if htype and htype not in HUMAN_TEST_TYPES:
        raise ValueError(f"invalid human_test_type: {htype}")
    steps = steps or task.get("human_test_steps") or ["执行 <动作>", "检查 <检查点>"]
    lines = ["需要你完成 1 个测试："]
    lines += [f"{i + 1}. {s}" for i, s in enumerate(steps[:5])]
    lines += ["", "完成后只回复：", "正常", "或", "异常截图"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 7. Task lock / duplicate protection
# ---------------------------------------------------------------------------

def task_locks():
    return _read_json(TASK_LOCKS_FILE, {})


def acquire_task_lock(task_id, session_id, pid, worktree):
    locks = task_locks()
    existing = locks.get(task_id)
    if existing and existing.get("status") == "RUNNING":
        return ("BLOCKED_DUPLICATE_TASK", existing)
    lock = {
        "task_id": task_id,
        "session_id": session_id,
        "pid": int(pid),
        "worktree": worktree,
        "status": "RUNNING",
        "locked_at": wf.now_iso(),
    }
    locks[task_id] = lock
    _ensure_dir(OPS_STATE_DIR)
    wf.write_json(TASK_LOCKS_FILE, locks)
    return ("ALLOWED", lock)


def release_task_lock(task_id, session_id):
    locks = task_locks()
    rec = locks.get(task_id)
    if rec and rec.get("session_id") == session_id:
        rec["status"] = "CLOSED"
        rec["closed_at"] = wf.now_iso()
        wf.write_json(TASK_LOCKS_FILE, locks)
        return rec
    return None


# ---------------------------------------------------------------------------
# 8. State machine
# ---------------------------------------------------------------------------

STATES = ["CREATED", "PREFLIGHT", "RUNNING", "TESTING", "HUMAN_TEST_REQUIRED",
          "REVIEW_READY", "DONE"]
BLOCKED_STATES = ["BLOCKED_RULES_MISMATCH", "BLOCKED_SCOPE_VIOLATION",
                  "BLOCKED_DUPLICATE_TASK", "NEEDS_SUPERVISOR", "FAILED"]
ALL_STATES = STATES + BLOCKED_STATES

TRANSITIONS = {
    "CREATED": ["PREFLIGHT", "BLOCKED_RULES_MISMATCH", "BLOCKED_DUPLICATE_TASK", "FAILED"],
    "PREFLIGHT": ["RUNNING", "BLOCKED_RULES_MISMATCH", "BLOCKED_SCOPE_VIOLATION", "FAILED"],
    "RUNNING": ["TESTING", "HUMAN_TEST_REQUIRED", "NEEDS_SUPERVISOR", "BLOCKED_SCOPE_VIOLATION", "FAILED"],
    "TESTING": ["REVIEW_READY", "HUMAN_TEST_REQUIRED", "NEEDS_SUPERVISOR", "FAILED"],
    "HUMAN_TEST_REQUIRED": ["TESTING", "REVIEW_READY", "FAILED"],
    "REVIEW_READY": ["DONE", "FAILED"],
    "DONE": [],
}


def task_states():
    return _read_json(TASK_STATES_FILE, {})


def transition_state(task_id, from_state, to_state, expected_from=None):
    """Programmatic state transition. Illegal jump -> BLOCKED_ILLEGAL_TRANSITION."""
    if from_state not in ALL_STATES or to_state not in ALL_STATES:
        return {"verdict": "BLOCKED_ILLEGAL_TRANSITION", "reason": "unknown state"}
    if expected_from is not None and from_state != expected_from:
        return {"verdict": "BLOCKED_ILLEGAL_TRANSITION",
                "reason": f"stale state {from_state}, expected {expected_from}"}
    if to_state not in TRANSITIONS.get(from_state, []):
        return {"verdict": "BLOCKED_ILLEGAL_TRANSITION",
                "reason": f"{from_state} -> {to_state} not allowed"}
    states = task_states()
    states[task_id] = {"state": to_state, "updated_at": wf.now_iso()}
    _ensure_dir(OPS_STATE_DIR)
    wf.write_json(TASK_STATES_FILE, states)
    return {"verdict": "ALLOWED", "state": to_state}


def set_state(task_id, state, expected_from=None):
    states = task_states()
    current = (states.get(task_id) or {}).get("state", "CREATED")
    if expected_from is not None:
        current = expected_from
    res = transition_state(task_id, current, state, expected_from=None)
    return res


# ---------------------------------------------------------------------------
# 9. Safety regression (static scan)
# ---------------------------------------------------------------------------

DANGEROUS_PATTERNS = [
    ("git reset --hard", re.compile(r"git\s+(?:-C\s+\S+\s+)?reset\s+--hard"), "unscoped hard reset"),
    ("git checkout -B", re.compile(r"git\s+(?:-C\s+\S+\s+)?checkout\s+-B"), "force branch checkout"),
]

# Explicit, reviewed safety exceptions (hardened sync alignment is guarded: no force,
# fail-closed dirty tree, protected-file content sync only Mac -> Windows).
SAFETY_ALLOWLIST = []


def safety_scan(roots=None):
    """Scan workflow/sync/runner code for dangerous patterns. Returns violations list.

    Comments and the scanner's own pattern definitions are not executable code and
    are excluded; explicitly allowlisted safety exceptions are honored.
    """
    roots = roots or [
        os.path.join(wf.REPO, "scripts", "wf"),
    ]
    violations = []
    seen_files = set()
    for root in roots:
        if os.path.isfile(root):
            files = [root]
        elif os.path.isdir(root):
            files = [os.path.join(root, f) for f in os.listdir(root)
                     if f.endswith((".py", ".ps1", ".sh", ".cmd", ".bat"))]
        else:
            continue
        for path in files:
            if path in seen_files:
                continue
            seen_files.add(path)
            try:
                content = wf.read_file(path) or ""
            except OSError:
                continue
            rel = os.path.relpath(path, wf.REPO).replace("\\", "/")
            if os.path.basename(path) == "wf_ops.py":
                continue  # scanner itself only defines patterns as literals
            code_lines = []
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith(("#", "//", "rem ", "'", '"', "<!--")):
                    continue  # comments / docstrings are not executable
                code_lines.append(line)
            code = "\n".join(code_lines)
            for name, pattern, why in DANGEROUS_PATTERNS:
                for m in pattern.finditer(code):
                    allowed = any(
                        a["file"] == rel and a["pattern"] == name
                        for a in SAFETY_ALLOWLIST
                    )
                    if not allowed:
                        violations.append({
                            "file": rel,
                            "pattern": name,
                            "reason": why,
                            "line": code[:m.start()].count("\n") + 1,
                            "snippet": m.group(0),
                        })
    return violations


# ---------------------------------------------------------------------------
# Workflow self-test
# ---------------------------------------------------------------------------

def workflow_selftest():
    checks = {}
    checks["safety_scan"] = safety_scan()
    checks["rules_preflight"] = wf.preflight(expected_sha=wf.resolve_canonical()["sha256"])["status"]
    checks["task_lock"] = None
    return checks
