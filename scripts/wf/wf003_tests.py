"""WF-003 acceptance tests (programmatic; TEST-1..TEST-9)."""

from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wf_lib as wf  # noqa: E402
import wf_ops  # noqa: E402
import wf_ctl  # noqa: E402

RESULTS_DIR = os.path.join(wf.RESULTS_DIR, "WF-003")
EXPECTED_RULES_VERSION = "2026-08-13.2"


def base_task(**kw):
    t = {
        "task_id": "WF003-T",
        "task_type": "check",
        "risk_level": "LOW",
        "objective": "test",
        "allowed_paths": ["tests/wf003/"],
        "acceptance_criteria": ["all assertions pass"],
        "requires_human_test": False,
        "requires_supervisor": False,
        "max_retry": 2,
        "test_level": "auto",
    }
    t.update(kw)
    return t


def t1_program_route():
    task = base_task(task_id="WF003-T1", task_type="check")
    route, reason = wf_ops.route_task(task)
    ok = route == "PROGRAMMATIC"
    return ok, {"route": route, "route_reason": reason}


def t2_max_route():
    task = base_task(task_id="WF003-T2", task_type="code", risk_level="LOW")
    route, reason = wf_ops.route_task(task)
    ok = route == "MAX"
    return ok, {"route": route, "route_reason": reason, "lily_sessions": 0}


def t3_escalation():
    task = base_task(task_id="WF003-T3", task_type="code", max_retry=2)
    first = wf_ops.route_task(task)
    needs = wf_ops.should_escalate(attempts=1, max_retry=2)
    task["_max_attempts"] = 2
    escalated = wf_ops.route_task(task)
    ok = (first[0] == "MAX" and needs is False
          and escalated[0] == "LILY" and "max_retry_reached" in escalated[1])
    return ok, {"first_route": first, "attempt1_escalate": needs,
                "route_after_max_retry": escalated, "needs_supervisor": "NEEDS_SUPERVISOR"}


def t4_duplicate_task():
    backup = wf_ops.task_locks()
    try:
        wf_ops.acquire_task_lock("WF003-T4", "s1", os.getpid(), "wt4")
        v2, rec2 = wf_ops.acquire_task_lock("WF003-T4", "s2", os.getpid(), "wt4b")
        ok = v2 == "BLOCKED_DUPLICATE_TASK"
        return ok, {"second_acquire": v2, "existing": rec2.get("session_id")}
    finally:
        wf.write_json(wf_ops.TASK_LOCKS_FILE, backup)


def t5_test_level():
    ordinary = base_task(task_id="WF003-T5a", task_type="code", risk_level="LOW")
    high = base_task(task_id="WF003-T5b", task_type="migration", risk_level="HIGH")
    lvl1, _ = wf_ops.select_test_level(ordinary)
    lvl3, reason = wf_ops.select_test_level(high)
    ok = lvl1 == "L1" and lvl3 == "L3"
    return ok, {"ordinary": lvl1, "high_risk_migration": lvl3, "l3_reason": reason}


def t6_log_reducer():
    big = "line ok\n" * 2000
    big += "FAILED tests/test_x.py::test_a\n"
    big += "ERROR    app/main.py:12 boom\n"
    big += "Traceback (most recent call last):\n  File \"app/x.py\", line 3, in <module>\n    raise RuntimeError('x')\nRuntimeError: x\n"
    big += "tail line\n"
    res = wf_ops.reduce_log(big, "WF003-T6", command="pytest -q", exit_code=1)
    ok = (
        os.path.isfile(res["raw_log_path"])
        and res["payload_chars"] < res["raw_log_chars"]
        and any("FAILED" in line for line in res["failed_test_names"])
        and "ERROR" in res["payload"]
        and "Traceback" in res["payload"]
    )
    return ok, {k: res[k] for k in ("raw_log_chars", "payload_chars", "reduction_ratio",
                                    "failed_test_names", "raw_log_path", "truncated")}


def t7_human_test():
    no_human = base_task(task_id="WF003-T7a", requires_human_test=False)
    human = base_task(task_id="WF003-T7b", requires_human_test=True,
                      human_test_type="OWNER_TELEGRAM_UX",
                      human_test_steps=["打开 Bot", "点击 Today", "检查是否出现待办卡片"])
    note_off = wf_ops.human_test_notification(no_human)
    note_on = wf_ops.human_test_notification(human)
    ok = (note_off is None
          and note_on is not None
          and note_on.startswith("需要你完成 1 个测试：")
          and "完成后只回复：" in note_on
          and "正常" in note_on
          and "异常截图" in note_on)
    return ok, {"owner_notification_gate": note_off is None, "message": note_on}


def t8_state_machine():
    backup = wf_ops.task_states()
    try:
        bad = wf_ops.set_state("WF003-T8", "RUNNING", expected_from="CREATED")
        ok_seq = [
            wf_ops.set_state("WF003-T8", "PREFLIGHT")["verdict"],
            wf_ops.set_state("WF003-T8", "RUNNING")["verdict"],
            wf_ops.set_state("WF003-T8", "TESTING")["verdict"],
            wf_ops.set_state("WF003-T8", "REVIEW_READY")["verdict"],
            wf_ops.set_state("WF003-T8", "DONE")["verdict"],
        ]
        ok = bad["verdict"] == "BLOCKED_ILLEGAL_TRANSITION" and all(v == "ALLOWED" for v in ok_seq)
        return ok, {"illegal_jump": bad, "legal_sequence": ok_seq}
    finally:
        wf.write_json(wf_ops.TASK_STATES_FILE, backup)


def t9_workflow_regression():
    results = {}
    # rules preflight + version (mirror == canonical, hash-verified by mirror-sync)
    content = wf.read_file(wf.WIN_MIRROR_PATH)
    sha = wf.sha256_text(content)
    pre = wf.preflight(expected_sha=sha, mirror_available=True)
    results["rules_preflight"] = pre["status"]
    results["rules_version"] = pre["rules_version"]
    ok = pre["status"] == "RULES_PREFLIGHT_OK" and pre["rules_version"] == EXPECTED_RULES_VERSION
    # WF-001 / WF-002 suites (run standalone; results are on disk)
    for label, path in (("wf001_suite", os.path.join(wf.RESULTS_DIR, "WF-001", "tests.json")),
                        ("wf002_suite", os.path.join(wf.RESULTS_DIR, "WF-002", "tests.json"))):
        try:
            suite = json.load(open(path, encoding="utf-8"))
            results[label] = suite.get("overall")
            ok = ok and suite.get("overall") == "PASS"
        except OSError:
            results[label] = "MISSING"
            ok = False
    # static safety regression (in-process, no subprocess)
    violations = wf_ops.safety_scan()
    results["safety_scan_violations"] = len(violations)
    ok = ok and not violations
    # canonical -> Windows sync (WF-002)
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", r"D:\AI-Review\sync-pasay.ps1"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    rc = proc.returncode
    results["canonical_sync_exit"] = rc
    agents_ok = (wf.sha256_file(os.path.join(wf.REPO, "AGENTS.md"))
                 == wf.sha256_text(wf.read_file(os.path.join(wf.REPO, "AGENTS.md"))))
    results["agents_mirror_consistent"] = agents_ok
    ok = ok and rc == 0 and agents_ok
    return ok, results


SUITES = [
    ("TEST-1", "program-route", t1_program_route),
    ("TEST-2", "max-route", t2_max_route),
    ("TEST-3", "escalation", t3_escalation),
    ("TEST-4", "duplicate-task", t4_duplicate_task),
    ("TEST-5", "test-level", t5_test_level),
    ("TEST-6", "log-reducer", t6_log_reducer),
    ("TEST-7", "human-test", t7_human_test),
    ("TEST-8", "state-machine", t8_state_machine),
    ("TEST-9", "workflow-regression", t9_workflow_regression),
]


def main():
    summary = {"task_id": "WF-003", "run_at": wf.now_iso(), "tests": {}}
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
