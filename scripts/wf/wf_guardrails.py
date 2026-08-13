"""WF-006 guardrails (UX-ACCEPTANCE-FREEZE-AND-GUARDRAILS-001).

Four deterministic workflow guardrails. Program First, LLM Last: every check
below is a static scan, an exit code, a process/log inspection or a workspace
hash comparison - no LLM is called and no network is required except the
optional backend health probe in the owner-UX gate.

G1  Platform semantics enter the test double.
    Rule: when a real incident is caused by Telegram / PostgreSQL / HTTP API /
    third-party semantics, the fix must also be locked in through a fake/stub
    semantic, a contract test or a deterministic integration test. The Telegram
    rule frozen for OWNER-UX-FAILURE-LIVE-TRACE-001: a message sent with a
    non-inline ReplyKeyboardMarkup must NOT be editable via FakeBot
    edit_message_text. Deleting that semantic to "make tests pass" is a
    scan failure, not an option.
G2  No silent exception swallowing in Telegram user-visible paths.
    Scope: fixed menu / inline button / callback / approval-reject / message
    send-edit / NL routing final reply. Bare `except:` and `except ...: pass`
    are scan failures unless explicitly allowlisted with a reason.
G3  READY_FOR_OWNER_UX_RETEST gate.
    READY_FOR_OWNER_UX_RETEST = TARGETED_TEST_PASS AND FAILURE_REGRESSION_PROVEN
    AND LIVE_VERSION_MATCH AND RUNTIME_HEALTHY. Any missing condition -> NO, and
    Owner must not be invited to test.
G4  Command timeout enforcement.
    The command wrapper runs every command with an explicit timeout
    (shell/git/status/hash/process 60s, targeted tests 180s unless the task
    overrides), returns TIMEOUT with command + elapsed, terminates the child
    process tree, never waits forever and never auto-retries indefinitely.

Scope discipline: these guardrails live in scripts/wf/, test infrastructure and
deterministic gates. They do NOT refactor the Telegram bot or change business UX.
"""

from __future__ import annotations

import ast
import json
import os
import sys
import time
import urllib.request

import wf_lib as wf

REPO = wf.REPO
BOT_ROOT = os.path.join(REPO, "pasay-telegram-bot")
HANDLER_DIR = os.path.join(BOT_ROOT, "pasay_bot", "handlers")
TESTS_CONFTEST = os.path.join(BOT_ROOT, "tests", "conftest.py")

WORKSPACE_EXCLUDED_DIRS = {
    ".pytest_cache", ".venv", "__pycache__", "pasay_telegram_bot.egg-info",
    "state",
}
WORKSPACE_EXCLUDED_FILES = {".env"}


def relpath(path):
    """Repo-relative posix path; falls back to the normalized absolute path
    when the target lives on another drive (e.g. temp fixtures on C:)."""
    try:
        return os.path.relpath(path, REPO).replace("\\", "/")
    except ValueError:
        return os.path.normpath(path).replace("\\", "/")


# ---------------------------------------------------------------------------
# Shared: accepted-workspace hash (same canonical algorithm as deploy records)
# ---------------------------------------------------------------------------

def workspace_sha256(root=BOT_ROOT):
    """sha256 over sorted rel-posix-path + TAB + content-sha256 lines, LF
    joined, then sha256 of the blob. Exclusions mirror the deploy records."""
    entries = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in WORKSPACE_EXCLUDED_DIRS]
        for fn in sorted(filenames):
            if fn in WORKSPACE_EXCLUDED_FILES:
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace("\\", "/")
            entries.append((rel, wf.sha256_file(full)))
    entries.sort(key=lambda x: x[0])
    blob = "\n".join(f"{p}\t{s}" for p, s in entries)
    return len(entries), wf.sha256_text(blob)


# ---------------------------------------------------------------------------
# G1: external platform semantics in the test double
# ---------------------------------------------------------------------------

def _fakebot_edit_guard_ok(source: str) -> bool:
    """FakeBot.edit_message_text must keep rejecting edits of messages that were
    sent with a non-inline ReplyKeyboardMarkup (real Telegram 400 semantics)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == "FakeBot"):
            continue
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                    item.name == "edit_message_text":
                segment = ast.get_source_segment(source, item) or ""
                return ("ReplyKeyboardMarkup" in segment
                        and "Message can't be edited" in segment
                        and "isinstance" in segment)
    return False


def scan_platform_semantics(conftest_path=None):
    """Return violations if the Telegram reply-keyboard edit semantic is gone
    from the FakeBot. Empty list == guardrail PASS."""
    path = conftest_path or TESTS_CONFTEST
    source = wf.read_file(path) or ""
    rel = relpath(path)
    violations = []
    if "_sent_by_id" not in source:
        violations.append({
            "rule": "telegram_reply_keyboard_not_editable",
            "platform": "telegram",
            "file": rel,
            "missing": "send-time reply_markup tracking (_sent_by_id)",
            "why": "FakeBot must remember which messages were sent with a reply "
                   "keyboard so edit_message_text can mirror Telegram's 400.",
        })
    if not _fakebot_edit_guard_ok(source):
        violations.append({
            "rule": "telegram_reply_keyboard_not_editable",
            "platform": "telegram",
            "file": rel,
            "missing": "edit_message_text guard (ReplyKeyboardMarkup + "
                       "isinstance + raise BadRequest('Message can't be edited'))",
            "why": "Messages sent with a non-inline ReplyKeyboardMarkup are not "
                   "editable in real Telegram; FakeBot must keep enforcing it so "
                   "the OWNER-UX-FAILURE-LIVE-TRACE-001 bug cannot be hidden.",
        })
    return violations


# ---------------------------------------------------------------------------
# G2: no silent exception swallowing in Telegram user-visible paths
# ---------------------------------------------------------------------------

# Explicit, reviewed exceptions. Every entry is a `pass`-only or bare except in
# the Telegram handler directory; the reason explains why it is not a silent
# user-facing swallow. New occurrences are FAIL; deleting or moving code without
# re-reviewing these entries is also FAIL (line numbers are pinned).
SILENT_EXCEPTION_ALLOWLIST = [
    {"file": "pasay-telegram-bot/pasay_bot/handlers/callback.py", "line": 152,
     "kind": "silent_pass",
     "reason": "Telegram accepts exactly one answerCallbackQuery per query id; a "
               "second answer is invalid. Best-effort ack only: the durable "
               "edit/send fallback below still gives the user visible feedback."},
    {"file": "pasay-telegram-bot/pasay_bot/handlers/callback.py", "line": 216,
     "kind": "silent_pass",
     "reason": "Remembering the last payment method is best-effort non-visible "
               "persistence; failure must never block the visible flow."},
    {"file": "pasay-telegram-bot/pasay_bot/handlers/callback.py", "line": 261,
     "kind": "silent_pass",
     "reason": "Safety-net spinner-clear answer after the primary user-visible "
               "feedback already ran; Telegram single-answer semantics make a "
               "second answer best-effort."},
    {"file": "pasay-telegram-bot/pasay_bot/handlers/callback.py", "line": 271,
     "kind": "silent_pass",
     "reason": "Handler-latency instrumentation is telemetry, not user-visible "
               "output; it must never break the UX path."},
    {"file": "pasay-telegram-bot/pasay_bot/handlers/commands.py", "line": 504,
     "kind": "silent_pass",
     "reason": "Property-name enrichment on the overdue page is decorative; the "
               "page still renders with core data and the API error path above "
               "already shows a visible error card."},
    {"file": "pasay-telegram-bot/pasay_bot/handlers/commands.py", "line": 694,
     "kind": "silent_pass",
     "reason": "Dashboard unit/property enrichment is optional; core dashboard "
               "data renders and other backend failures surface visible errors."},
    {"file": "pasay-telegram-bot/pasay_bot/handlers/commands.py", "line": 698,
     "kind": "silent_pass",
     "reason": "Dashboard lease enrichment is optional; core dashboard data "
               "renders."},
    {"file": "pasay-telegram-bot/pasay_bot/handlers/commands.py", "line": 932,
     "kind": "silent_pass",
     "reason": "Unparseable ISO due date falls back to 'no due date' so the task "
               "still renders in the correct ops section instead of breaking."},
    {"file": "pasay-telegram-bot/pasay_bot/handlers/nl_bridge.py", "line": 440,
     "kind": "silent_pass",
     "reason": "Property-name enrichment in the who-unpaid answer is decorative; "
               "core rows render and the API error path shows a visible error."},
]


def _is_noop(stmt) -> bool:
    """pass, '...' or a docstring-like string expression only."""
    if isinstance(stmt, ast.Pass):
        return True
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
        return isinstance(stmt.value.value, (str, type(None), type(...)))
    return False


def scan_silent_exceptions(handler_root=None, allowlist=None):
    """Scan Telegram handler *.py for bare except / pass-only except handlers.

    Returns (violations, allowlisted): violations are entries without an
    explicit allowlist reason (gate FAIL); allowlisted entries carry reasons.
    """
    root = handler_root or HANDLER_DIR
    allowlist = SILENT_EXCEPTION_ALLOWLIST if allowlist is None else allowlist
    violations, allowlisted = [], []
    if not os.path.isdir(root):
        violations.append({"file": root, "line": 0, "kind": "scan_root_missing",
                           "snippet": "handler directory missing"})
        return violations, allowlisted
    for fn in sorted(os.listdir(root)):
        if not fn.endswith(".py"):
            continue
        path = os.path.join(root, fn)
        rel = relpath(path)
        source = wf.read_file(path) or ""
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            violations.append({"file": rel, "line": exc.lineno or 0,
                               "kind": "syntax_error", "snippet": "unparsable"})
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            for handler in node.handlers:
                bare = handler.type is None
                only_pass = all(_is_noop(stmt) for stmt in handler.body)
                if not (bare or only_pass):
                    continue
                kind = "bare_except" if bare else "silent_pass"
                entry = {
                    "file": rel,
                    "line": handler.lineno,
                    "kind": kind,
                    "snippet": ast.get_source_segment(source, handler) or "",
                }
                allowed = any(
                    a.get("file") == rel
                    and a.get("line") == handler.lineno
                    and a.get("kind") == kind
                    for a in allowlist
                )
                (allowlisted if allowed else violations).append(entry)
    return violations, allowlisted


# ---------------------------------------------------------------------------
# G4: command timeout defaults and long-task declaration
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUTS = {
    "shell": 60,
    "git/status/hash/process": 60,
    "targeted_tests": 180,
}


def resolve_timeout(task=None, kind="shell", explicit=None):
    """Explicit argument wins, then task-level timeout_seconds / timeouts[kind],
    then the guardrail default. Long tasks must declare a timeout (see
    validate_long_task_timeout)."""
    if explicit is not None:
        return int(explicit)
    if isinstance(task, dict):
        declared = task.get("timeout_seconds")
        if declared:
            return int(declared)
        by_kind = task.get("timeouts") or {}
        if isinstance(by_kind, dict) and by_kind.get(kind):
            return int(by_kind[kind])
    return int(DEFAULT_TIMEOUTS.get(kind, 60))


def validate_long_task_timeout(task):
    """Long-running tasks must carry an explicit timeout; no infinite waits."""
    if task and task.get("long_running"):
        declared = task.get("timeout_seconds") or (task.get("timeouts") or {})
        if not declared:
            return False, "long_running task must declare timeout_seconds"
    return True, ""


# ---------------------------------------------------------------------------
# G3: READY_FOR_OWNER_UX_RETEST gate
# ---------------------------------------------------------------------------

GATE_CONDITIONS = ("TARGETED_TEST_PASS", "FAILURE_REGRESSION_PROVEN",
                   "LIVE_VERSION_MATCH", "RUNTIME_HEALTHY")


def _bot_python():
    if os.name == "nt":
        cand = os.path.join(BOT_ROOT, ".venv", "Scripts", "python.exe")
    else:
        cand = os.path.join(BOT_ROOT, ".venv", "bin", "python")
    return cand if os.path.exists(cand) else sys.executable


def run_pytest_for_task(task, tests, timeout):
    """Deterministic targeted pytest run (G3). Passes exit code 0 only."""
    if not tests:
        return {"result": "FAIL", "detail": "no tests provided"}
    cmd = [_bot_python(), "-m", "pytest", "-q"] + list(tests)
    rec = wf.run_timed(cmd, timeout=timeout,
                       cwd=task.get("test_cwd") or BOT_ROOT)
    if rec["status"] == wf.TIMEOUT_STATUS:
        return {"result": "FAIL",
                "detail": "TIMEOUT after %ss" % rec["elapsed"]}
    if rec["status"] != "OK":
        return {"result": "FAIL", "detail": rec.get("stderr")[-500:]}
    tail = (rec.get("stdout") or rec.get("stderr") or "")[-300:]
    return {"result": "PASS" if rec["returncode"] == 0 else "FAIL",
            "detail": {"returncode": rec["returncode"], "tail": tail}}


def _bot_processes():
    """[(pid, ppid)] running `-m pasay_bot.main` (cross-platform, read-only)."""
    if os.name == "nt":
        script = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.Name -like 'python*' -and "
            "$_.CommandLine -like '*pasay_bot.main*' } | "
            "ForEach-Object { \"$($_.ProcessId)`t$($_.ParentProcessId)\" }"
        )
        rec = wf.run_timed(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            timeout=30,
        )
        rows = []
        for line in (rec.get("stdout") or "").splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and parts[0].strip().isdigit():
                rows.append((int(parts[0].strip()),
                             int(parts[1].strip()) if parts[1].strip().isdigit() else 0))
        return rows
    rec = wf.run_timed(["ps", "-ax", "-o", "pid=,ppid=,command="], timeout=30)
    rows = []
    for line in (rec.get("stdout") or "").splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) >= 3 and parts[0].isdigit() and "pasay_bot.main" in parts[2]:
            rows.append((int(parts[0]), int(parts[1])))
    return rows


def runtime_probe(task):
    """RUNTIME_HEALTHY sub-checks: single polling instance, getMe OK, no 409,
    optional backend health. Returns {checks, result}."""
    checks = {}
    rows = _bot_processes()
    pids = {pid for pid, _ in rows}
    roots = [pid for pid, ppid in rows if ppid not in pids]
    single = len(roots) == 1 and len(rows) >= 1
    checks["single_polling_instance"] = {"result": "PASS" if single else "FAIL",
                                         "bot_processes": len(rows),
                                         "bot_instance_roots": len(roots)}

    log_path = task.get("runtime_log")
    log = wf.read_file(log_path) if log_path else None
    getme = log is not None and "getMe OK" in log
    checks["getme_ok"] = {"result": "PASS" if getme else "FAIL",
                          "log": log_path, "found": getme}

    err_path = task.get("runtime_log_err")
    err = (wf.read_file(err_path) or "") if err_path else ""
    conflict = ("409" in err) or ("conflict" in err.lower())
    checks["no_409_conflict"] = {"result": "PASS" if not conflict else "FAIL",
                                 "log": err_path, "found": conflict}

    pid_file = task.get("runtime_pid_file")
    pid_alive = None
    if pid_file:
        raw = wf.read_file(pid_file)
        pid = int(raw.strip()) if raw and raw.strip().isdigit() else None
        pid_alive = pid is not None and wf.pid_alive(pid)
        checks["pid_alive"] = {"result": "PASS" if pid_alive else "FAIL",
                               "pid": pid, "file": pid_file}

    backend_ok = None
    if task.get("backend_dependent"):
        url = task.get("backend_health_url")
        if not url:
            backend_ok = False
            checks["backend_health"] = {"result": "FAIL",
                                        "detail": "backend_dependent=true but "
                                                  "backend_health_url missing"}
        else:
            try:
                with urllib.request.urlopen(url, timeout=5) as resp:
                    backend_ok = 200 <= resp.status < 300
                checks["backend_health"] = {"result": "PASS" if backend_ok else "FAIL",
                                            "url": url, "status": resp.status}
            except Exception as exc:
                backend_ok = False
                checks["backend_health"] = {"result": "FAIL", "url": url,
                                            "detail": str(exc)[:200]}
    else:
        checks["backend_health"] = {"result": "PASS", "detail": "not required"}

    required = ["single_polling_instance", "getme_ok", "no_409_conflict"]
    if pid_file:
        required.append("pid_alive")
    if task.get("backend_dependent"):
        required.append("backend_health")
    all_pass = all(checks[k]["result"] == "PASS" for k in required)
    checks["result"] = "PASS" if all_pass else "FAIL"
    return checks


def _live_workspace_hash():
    _, sha = workspace_sha256()
    return sha


def owner_ux_gate(task, run_tests=None, workspace_hash=None, runtime_probe_fn=None):
    """Compute READY_FOR_OWNER_UX_RETEST. Injected fns keep the logic testable
    without side effects; defaults use the real deterministic implementations."""
    run_tests = run_tests or run_pytest_for_task
    workspace_hash = workspace_hash or _live_workspace_hash
    runtime_probe_fn = runtime_probe_fn or runtime_probe

    test_timeout = resolve_timeout(task, kind="targeted_tests",
                                   explicit=task.get("test_timeout_seconds"))
    targeted = run_tests(task, list(task.get("targeted_tests") or []), test_timeout)
    regression_tests = list(task.get("regression_tests") or [])
    regression = {"result": "FAIL", "detail": "regression_tests must be non-empty"}
    if regression_tests:
        regression = run_tests(task, regression_tests, test_timeout)

    target_sha = task.get("target_workspace_sha256")
    live_sha = workspace_hash()
    version = {"result": "PASS" if (target_sha and live_sha == target_sha) else "FAIL",
               "target": target_sha, "live": live_sha}
    if task.get("runtime_pid"):
        pid_alive = wf.pid_alive(task["runtime_pid"])
        version["runtime_pid_alive"] = pid_alive
        if not pid_alive:
            version["result"] = "FAIL"

    healthy = runtime_probe_fn(task)
    conditions = {
        "TARGETED_TEST_PASS": {"result": targeted["result"], "detail": targeted["detail"]},
        "FAILURE_REGRESSION_PROVEN": {"result": regression["result"], "detail": regression["detail"]},
        "LIVE_VERSION_MATCH": {"result": version["result"],
                               "detail": {"target_sha256": target_sha, "live_sha256": live_sha}},
        "RUNTIME_HEALTHY": {"result": healthy["result"], "checks": healthy},
    }
    ready = all(conditions[c]["result"] == "PASS" for c in GATE_CONDITIONS)
    return {
        "task_id": task.get("task_id", "UNKNOWN"),
        "READY_FOR_OWNER_UX_RETEST": "YES" if ready else "NO",
        "conditions": conditions,
    }


# ---------------------------------------------------------------------------
# Combined static guardrail report
# ---------------------------------------------------------------------------

def guardrail_report(conftest_path=None, handler_root=None, allowlist=None):
    """G1 + G2 static scan report (G3/G4 are runtime gates covered by tests)."""
    platform_violations = scan_platform_semantics(conftest_path)
    silent_violations, allowlisted = scan_silent_exceptions(handler_root, allowlist)
    return {
        "GUARDRAIL_PLATFORM_SEMANTICS": "PASS" if not platform_violations else "FAIL",
        "GUARDRAIL_NO_SILENT_UI_EXCEPTION": "PASS" if not silent_violations else "FAIL",
        "platform_semantics_violations": platform_violations,
        "silent_exception_violations": silent_violations,
        "silent_exception_allowlisted_count": len(allowlisted),
    }
