"""WF-005 acceptance tests for BRIDGE-ROUTER-002 Router Dispatch Integration.

Program First, LLM Last: every assertion is deterministic; no network, no SSH,
no LLM. Real subprocesses are only local python (wf004 rerun, wf_ctl selftest).

Standalone: python scripts/wf/wf005_tests.py
    -> writes .ai-control/results/WF-005/tests.json
Pytest: pytest scripts/wf/wf005_tests.py -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wf_lib as wf  # noqa: E402
import wf_ops  # noqa: E402
import bridge_router as br  # noqa: E402
import dispatch_controller as dc  # noqa: E402

RESULTS_DIR = os.path.join(wf.RESULTS_DIR, "WF-005")
REPO = wf.REPO


def base_task(**kw):
    t = {
        "task_id": "WF005-T",
        "type": "code",
        "risk": "LOW",
        "capabilities": ["code"],
        "constraints": [],
        "objective": "BRIDGE-ROUTER-002 deterministic dispatch acceptance",
        "acceptance": ["all assertions pass"],
    }
    t.update(kw)
    return t


class Spy:
    """Injected fake for hermes/max/approval; records call order only."""

    def __init__(self):
        self.calls = []
        self.classification = {}

    def hermes_plan(self, *a, **k):
        self.calls.append("HERMES_PLAN")

    def hermes_triage(self, *a, **k):
        self.calls.append("HERMES_TRIAGE")
        return dict(self.classification)

    def max_exec(self, *a, **k):
        self.calls.append("MAX")

    def approval(self, *a, **k):
        self.calls.append("APPROVAL")


# ---------------------------------------------------------------------------
# TEST-1: LOW code task -> DIRECT_MAX -> Max only (hermes_plan=0, max=1)
# ---------------------------------------------------------------------------

def t1_direct_max():
    task = base_task(task_id="WF005-T1", type="code", risk="LOW")
    result = br.route_task(task)
    ok = result.route == br.DIRECT_MAX
    plan = dc.plan_for_route(result)
    ok = ok and plan.stage_names() == ["MAX"]
    spy = Spy()
    report = dc.execute_plan(plan, hermes_plan=spy.hermes_plan,
                             hermes_triage=spy.hermes_triage,
                             max_exec=spy.max_exec)
    ok = ok and report.status == dc.SUCCESS
    ok = ok and report.hermes_plan_calls == 0
    ok = ok and report.hermes_triage_calls == 0
    ok = ok and report.max_calls == 1
    ok = ok and spy.calls == ["MAX"]
    return ok, {"route": result.route, "plan": plan.as_dict(),
                "report": report.as_dict(), "calls": spy.calls}


def test_1_direct_max():
    ok, detail = t1_direct_max()
    assert ok, detail


# ---------------------------------------------------------------------------
# TEST-2: architecture / RBAC / finance / db_migration -> HERMES_THEN_MAX
#         hermes_plan=1, max=1, order HERMES_PLAN -> MAX
# ---------------------------------------------------------------------------

def t2_hermes_then_max():
    cases = {
        "architecture_change": base_task(task_id="WF005-T2a", constraints=["architecture_change"]),
        "rbac_change": base_task(task_id="WF005-T2b", constraints=["rbac_change"]),
        "financial_logic": base_task(task_id="WF005-T2c", constraints=["financial_logic"]),
        "db_migration": base_task(task_id="WF005-T2d", constraints=["db_migration"]),
    }
    details = {}
    ok = True
    for name, task in cases.items():
        result = br.route_task(task)
        plan = dc.plan_for_route(result)
        spy = Spy()
        report = dc.execute_plan(plan, hermes_plan=spy.hermes_plan,
                                 hermes_triage=spy.hermes_triage,
                                 max_exec=spy.max_exec)
        good = (result.route == br.HERMES_THEN_MAX
                and plan.stage_names() == ["HERMES_PLAN", "MAX"]
                and report.status == dc.SUCCESS
                and report.hermes_plan_calls == 1
                and report.hermes_triage_calls == 0
                and report.max_calls == 1
                and spy.calls == ["HERMES_PLAN", "MAX"])
        details[name] = {"route": result.route, "plan": plan.as_dict(),
                         "report": report.as_dict(), "calls": spy.calls}
        ok = ok and good
    return ok, details


def test_2_hermes_then_max():
    ok, detail = t2_hermes_then_max()
    assert ok, detail


# ---------------------------------------------------------------------------
# TEST-3: real_device_test -> MAX_THEN_FUGUI_ACCEPTANCE
#         max=1, hermes=0, acceptance=FUGUI, executor != FUGUI
# ---------------------------------------------------------------------------

def t3_fugui_acceptance_only():
    cases = {
        "constraint_form": base_task(task_id="WF005-T3a", constraints=["real_device_test"]),
        "boolean_form": base_task(task_id="WF005-T3b", real_device_test=True),
    }
    details = {}
    ok = True
    for name, task in cases.items():
        result = br.route_task(task)
        plan = dc.plan_for_route(result)
        spy = Spy()
        report = dc.execute_plan(plan, hermes_plan=spy.hermes_plan,
                                 hermes_triage=spy.hermes_triage,
                                 max_exec=spy.max_exec)
        good = (result.route == br.MAX_THEN_FUGUI_ACCEPTANCE
                and result.executor == "MAX"
                and result.executor != "FUGUI"
                and plan.stage_names() == ["MAX"]
                and report.acceptance_target == "FUGUI"
                and report.hermes_plan_calls == 0
                and report.hermes_triage_calls == 0
                and report.max_calls == 1
                and spy.calls == ["MAX"])
        details[name] = {"route": result.route, "executor": result.executor,
                         "plan": plan.as_dict(), "report": report.as_dict(),
                         "calls": spy.calls}
        ok = ok and good
    return ok, details


def test_3_fugui_acceptance_only():
    ok, detail = t3_fugui_acceptance_only()
    assert ok, detail


# ---------------------------------------------------------------------------
# TEST-4: owner / manual approval -> OWNER_APPROVAL_REQUIRED
#         hermes=0, max=0, approval blocks
# ---------------------------------------------------------------------------

def t4_owner_approval():
    cases = {
        "manual_approval": base_task(task_id="WF005-T4a", constraints=["manual_approval"]),
        "owner_approval": base_task(task_id="WF005-T4b", constraints=["owner_approval"]),
        "production_access": base_task(task_id="WF005-T4c", constraints=["production_access"]),
        "production_flag": base_task(task_id="WF005-T4d", production_access=True),
    }
    details = {}
    ok = True
    for name, task in cases.items():
        result = br.route_task(task)
        plan = dc.plan_for_route(result)
        spy = Spy()
        report = dc.execute_plan(plan, hermes_plan=spy.hermes_plan,
                                 hermes_triage=spy.hermes_triage,
                                 max_exec=spy.max_exec, approval=spy.approval)
        good = (result.route == br.OWNER_APPROVAL_REQUIRED
                and result.executor == "none"
                and plan.stage_names() == []
                and plan.status == dc.MANUAL_APPROVAL_REQUIRED
                and report.status == dc.MANUAL_APPROVAL_REQUIRED
                and report.hermes_started == 0
                and report.max_started == 0
                and report.approval_called is True
                and spy.calls == ["APPROVAL"])
        details[name] = {"route": result.route, "plan": plan.as_dict(),
                         "report": report.as_dict(), "calls": spy.calls}
        ok = ok and good
    return ok, details


def test_4_owner_approval():
    ok, detail = t4_owner_approval()
    assert ok, detail


# ---------------------------------------------------------------------------
# TEST-5: ambiguous -> HERMES_TRIAGE -> triage=1 -> reroute -> final chain once;
#         Hermes never specifies an executor; reroute staying TRIAGE fails closed
# ---------------------------------------------------------------------------

def t5_hermes_triage_reroute():
    ambiguous = base_task(task_id="WF005-T5a", type="mystery", risk="UNKNOWN")
    details = {}
    ok = True

    # 5a: triage classifies to a LOW code task -> final DIRECT_MAX chain once.
    result = br.route_task(ambiguous)
    plan = dc.plan_for_route(result)
    ok = ok and result.route == br.HERMES_TRIAGE
    ok = ok and plan.stage_names() == ["HERMES_TRIAGE", "REROUTE"]
    spy = Spy()
    # The fake LLM tries to sneak an executor in; it must be ignored.
    spy.classification = {"type": "code", "risk": "LOW", "constraints": [],
                          "executor": "FUGUI", "planner": "FUGUI"}
    report = dc.execute_plan(
        plan, hermes_plan=spy.hermes_plan, hermes_triage=spy.hermes_triage,
        max_exec=spy.max_exec,
        reroute=lambda cls: br.route_task(dc.apply_classification(ambiguous, cls)))
    ok = ok and report.status == dc.SUCCESS
    ok = ok and report.hermes_triage_calls == 1
    ok = ok and report.hermes_plan_calls == 0
    ok = ok and report.max_calls == 1
    ok = ok and report.final_route == br.DIRECT_MAX
    # One execution chain: triage -> reroute -> final MAX, each exactly once.
    ok = ok and report.stages_executed == ("HERMES_TRIAGE", "REROUTE", "MAX")
    ok = ok and spy.calls == ["HERMES_TRIAGE", "MAX"]
    details["reroute_direct_max"] = {"report": report.as_dict(), "calls": spy.calls}

    # 5b: reroute still HERMES_TRIAGE -> FAIL CLOSED, nothing after REROUTE.
    spy2 = Spy()
    spy2.classification = {"type": "another_mystery", "risk": "UNKNOWN"}
    report2 = dc.execute_plan(
        plan, hermes_plan=spy2.hermes_plan, hermes_triage=spy2.hermes_triage,
        max_exec=spy2.max_exec,
        reroute=lambda cls: br.route_task(dc.apply_classification(ambiguous, cls)))
    ok = ok and report2.status == dc.FAILED_TRIAGE_NO_PROGRESS
    ok = ok and report2.hermes_triage_calls == 1
    ok = ok and report2.max_calls == 0
    ok = ok and report2.final_route == br.HERMES_TRIAGE
    ok = ok and report2.stages_executed == ("HERMES_TRIAGE", "REROUTE")
    ok = ok and spy2.calls == ["HERMES_TRIAGE"]
    details["reroute_still_triage"] = {"report": report2.as_dict(), "calls": spy2.calls}

    # 5c: executor field in the classification never leaks into the reroute.
    merged = dc.apply_classification(ambiguous, spy.classification)
    ok = ok and "executor" not in merged
    ok = ok and "planner" not in merged
    ok = ok and merged["type"] == "code" and merged["risk"] == "LOW"
    details["sanitized_classification"] = merged
    return ok, details


def test_5_hermes_triage_reroute():
    ok, detail = t5_hermes_triage_reroute()
    assert ok, detail


# ---------------------------------------------------------------------------
# TEST-6: no double-start: single run keeps planner and Max <= 1 each; repeat
#         dispatch is rejected by the existing task lock
# ---------------------------------------------------------------------------

def t6_no_double_start():
    details = {}
    ok = True

    # 6a: HERMES_THEN_MAX plan runs planner once and Max once (no duplicates).
    task = base_task(task_id="WF005-T6", constraints=["architecture_change"])
    plan = dc.plan_for_route(br.route_task(task))
    spy = Spy()
    report = dc.execute_plan(plan, hermes_plan=spy.hermes_plan,
                             hermes_triage=spy.hermes_triage,
                             max_exec=spy.max_exec)
    ok = ok and report.hermes_plan_calls == 1
    ok = ok and report.hermes_triage_calls == 0
    ok = ok and report.max_calls == 1
    ok = ok and spy.calls == ["HERMES_PLAN", "MAX"]
    details["single_run"] = {"report": report.as_dict(), "calls": spy.calls}

    # 6b: a second dispatch for the same task is blocked by the task lock.
    backup = wf.read_json(wf_ops.TASK_LOCKS_FILE)
    try:
        first, rec1 = wf_ops.acquire_task_lock("WF005-T6", "sess-a", os.getpid(), "wt-a")
        second, rec2 = wf_ops.acquire_task_lock("WF005-T6", "sess-b", os.getpid(), "wt-b")
        ok = ok and first == "ALLOWED" and rec1["status"] == "RUNNING"
        ok = ok and second == "BLOCKED_DUPLICATE_TASK"
        details["duplicate_lock"] = {"first": first, "second": second,
                                     "existing_status": rec2.get("status")}
    finally:
        if backup is None:
            try:
                os.remove(wf_ops.TASK_LOCKS_FILE)
            except OSError:
                pass
        else:
            wf.write_json(wf_ops.TASK_LOCKS_FILE, backup)
    return ok, details


def test_6_no_double_start():
    ok, detail = t6_no_double_start()
    assert ok, detail


# ---------------------------------------------------------------------------
# TEST-7: no regression: wf004 reruns PASS, wf_ops route on ordinary code still
#         returns MAX, wf_ctl selftest passes (local/mirror fallback, no SSH
#         dependency asserted here; the external selftest command is also run).
# ---------------------------------------------------------------------------

def _run_py(args, timeout=120):
    return subprocess.run([sys.executable] + args, cwd=REPO, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)


def t7_no_regression():
    details = {}
    ok = True

    route, reason = wf_ops.route_task({
        "task_id": "WF005-T7",
        "task_type": "code",
        "risk_level": "LOW",
        "objective": "ordinary code change",
        "allowed_paths": ["scripts/wf/"],
        "acceptance_criteria": "tests pass",
    })
    ok = ok and route == "MAX"
    details["wf_ops_route"] = {"route": route, "reason": reason}

    wf004 = _run_py(["scripts/wf/wf004_tests.py"])
    try:
        wf004_summary = json.loads(wf004.stdout or "{}")
        wf004_pass = wf004_summary.get("overall") == "PASS" and wf004.returncode == 0
    except (ValueError, json.JSONDecodeError):
        wf004_pass = False
        wf004_summary = {"stdout_tail": (wf004.stdout or "")[-500:]}
    ok = ok and wf004_pass
    details["wf004_rerun"] = {"returncode": wf004.returncode,
                              "overall": wf004_summary.get("overall")}

    selftest = _run_py(["scripts/wf/wf_ctl.py", "selftest"], timeout=180)
    try:
        st = json.loads(selftest.stdout or "{}")
        st_pass = st.get("rules_preflight") == "RULES_PREFLIGHT_OK"
    except (ValueError, json.JSONDecodeError):
        st_pass = False
        st = {"stdout_tail": (selftest.stdout or "")[-500:]}
    ok = ok and st_pass
    details["selftest"] = {"returncode": selftest.returncode,
                           "rules_preflight": st.get("rules_preflight"),
                           "safety_violations": len(st.get("safety_scan", []))}
    return ok, details


def test_7_no_regression():
    ok, detail = t7_no_regression()
    assert ok, detail


# ---------------------------------------------------------------------------
# TEST-8: determinism - identical input always yields an identical plan
# ---------------------------------------------------------------------------

def t8_deterministic():
    tasks = [
        base_task(task_id="WF005-T8a", type="code", risk="LOW"),
        base_task(task_id="WF005-T8b", type="api", risk="MEDIUM",
                  constraints=["real_device_test"]),
        base_task(task_id="WF005-T8c", constraints=["rbac_change"]),
        base_task(task_id="WF005-T8d", production_access=True),
        base_task(task_id="WF005-T8e", type="mystery"),
    ]
    details = {}
    ok = True
    for task in tasks:
        p1 = dc.plan_for_route(br.route_task(task))
        p2 = dc.plan_for_route(br.route_task(task))
        same = p1 == p2 and p1.as_dict() == p2.as_dict()
        c1 = dc.planned_counts(p1)
        c2 = dc.planned_counts(p2)
        details[task["task_id"]] = {"plan": p1.as_dict(), "counts": c1,
                                    "identical": same, "counts_identical": c1 == c2}
        ok = ok and same and c1 == c2
    return ok, details


def test_8_deterministic():
    ok, detail = t8_deterministic()
    assert ok, detail


SUITES = [
    ("TEST-1", "direct-max", t1_direct_max),
    ("TEST-2", "hermes-then-max", t2_hermes_then_max),
    ("TEST-3", "fugui-acceptance-only", t3_fugui_acceptance_only),
    ("TEST-4", "owner-approval", t4_owner_approval),
    ("TEST-5", "hermes-triage-reroute", t5_hermes_triage_reroute),
    ("TEST-6", "no-double-start", t6_no_double_start),
    ("TEST-7", "no-regression", t7_no_regression),
    ("TEST-8", "deterministic", t8_deterministic),
]


def main():
    summary = {"task_id": "WF-005", "run_at": wf.now_iso(), "tests": {}}
    os.makedirs(RESULTS_DIR, exist_ok=True)
    for tid, name, fn in SUITES:
        try:
            ok, detail = fn()
        except Exception as exc:  # pragma: no cover - defensive
            ok, detail = False, {"exception": str(exc)}
        summary["tests"][tid] = {"name": name, "result": "PASS" if ok else "FAIL",
                                 "detail": detail}
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
