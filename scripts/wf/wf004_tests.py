"""WF-004 acceptance tests for the minimal deterministic Bridge Router.

Program First, LLM Last: all assertions are deterministic; no network, no SSH.

Standalone: python scripts/wf/wf004_tests.py
    -> writes .ai-control/results/WF-004/tests.json
Pytest: pytest scripts/wf/wf004_tests.py -q
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wf_lib as wf  # noqa: E402
import bridge_router as br  # noqa: E402

RESULTS_DIR = os.path.join(wf.RESULTS_DIR, "WF-004")


def base_task(**kw):
    t = {
        "task_id": "WF004-T",
        "type": "code",
        "risk": "LOW",
        "capabilities": ["code"],
        "constraints": [],
        "objective": "BRIDGE-ROUTER-001 deterministic acceptance",
        "acceptance": ["all assertions pass"],
    }
    t.update(kw)
    return t


# ---------------------------------------------------------------------------
# TEST-1: LOW code change -> DIRECT_MAX
# ---------------------------------------------------------------------------

def t1_direct_max():
    task = base_task(task_id="WF004-T1", type="code", risk="LOW")
    res = br.route_task(task)
    ok = (res.route == br.DIRECT_MAX
          and res.planner == "none"
          and res.executor == "MAX"
          and res.acceptance == "automated tests"
          and res.approval == "AUTO")
    return ok, res.as_dict()


def test_1_direct_max():
    ok, detail = t1_direct_max()
    assert ok, detail


# ---------------------------------------------------------------------------
# TEST-2: architecture / RBAC / finance / DB migration -> HERMES_THEN_MAX
# ---------------------------------------------------------------------------

def t2_hermes_then_max():
    cases = {
        "architecture_change": base_task(task_id="WF004-T2a", constraints=["architecture_change"]),
        "rbac_change": base_task(task_id="WF004-T2b", constraints=["rbac_change"]),
        "financial_logic": base_task(task_id="WF004-T2c", constraints=["financial_logic"]),
        "db_migration_constraint": base_task(task_id="WF004-T2d", constraints=["db_migration"]),
        "db_migration_flag": base_task(task_id="WF004-T2e", db_migration=True),
        "high_risk_code": base_task(task_id="WF004-T2f", risk="HIGH"),
    }
    details = {}
    ok = True
    for name, task in cases.items():
        res = br.route_task(task)
        details[name] = res.as_dict()
        ok = ok and res.route == br.HERMES_THEN_MAX
        ok = ok and res.planner == "HERMES"
        ok = ok and res.executor == "MAX"
    return ok, details


def test_2_hermes_then_max():
    ok, detail = t2_hermes_then_max()
    assert ok, detail


# ---------------------------------------------------------------------------
# TEST-3: LOW code + real_device_test=true -> executor still MAX,
#         acceptance=FUGUI (Fugui is acceptance only, never the executor).
# ---------------------------------------------------------------------------

def t3_fugui_acceptance_only():
    cases = {
        "constraint_form": base_task(task_id="WF004-T3a", constraints=["real_device_test"]),
        "boolean_form": base_task(task_id="WF004-T3b", real_device_test=True),
        "medium_bot_ux": base_task(task_id="WF004-T3c", type="bot_ux", risk="MEDIUM",
                                   constraints=["real_device_test"]),
    }
    details = {}
    ok = True
    for name, task in cases.items():
        res = br.route_task(task)
        details[name] = res.as_dict()
        ok = ok and res.route == br.MAX_THEN_FUGUI_ACCEPTANCE
        ok = ok and res.executor == "MAX"
        ok = ok and res.acceptance == "FUGUI"
        ok = ok and res.executor != "FUGUI"
    return ok, details


def test_3_fugui_acceptance_only():
    ok, detail = t3_fugui_acceptance_only()
    assert ok, detail


# ---------------------------------------------------------------------------
# TEST-4: production / manual approval -> OWNER_APPROVAL_REQUIRED
# ---------------------------------------------------------------------------

def t4_owner_approval():
    cases = {
        "production_access": base_task(task_id="WF004-T4a", constraints=["production_access"]),
        "destructive": base_task(task_id="WF004-T4b", constraints=["destructive"]),
        "manual_approval": base_task(task_id="WF004-T4c", constraints=["manual_approval"]),
        "production_flag": base_task(task_id="WF004-T4d", production_access=True),
    }
    details = {}
    ok = True
    for name, task in cases.items():
        res = br.route_task(task)
        details[name] = res.as_dict()
        ok = ok and res.route == br.OWNER_APPROVAL_REQUIRED
        ok = ok and res.approval == "OWNER"
        ok = ok and res.executor == "none"  # blocked: no executor dispatched
    return ok, details


def test_4_owner_approval():
    ok, detail = t4_owner_approval()
    assert ok, detail


# ---------------------------------------------------------------------------
# TEST-5: undeterminable task -> HERMES_TRIAGE
# ---------------------------------------------------------------------------

def t5_hermes_triage():
    cases = {
        "unknown_type": base_task(task_id="WF004-T5a", type="mystery"),
        "unknown_risk": base_task(task_id="WF004-T5b", risk="UNKNOWN"),
        "missing_risk": base_task(task_id="WF004-T5c", risk=None),
        "unsupported_constraint": base_task(task_id="WF004-T5d", constraints=["mystery_thing"]),
    }
    details = {}
    ok = True
    for name, task in cases.items():
        res = br.route_task(task)
        details[name] = res.as_dict()
        ok = ok and res.route == br.HERMES_TRIAGE
        ok = ok and res.planner == "HERMES"  # triage only; Router re-decides
    return ok, details


def test_5_hermes_triage():
    ok, detail = t5_hermes_triage()
    assert ok, detail


# ---------------------------------------------------------------------------
# TEST-6: permanent regression - Fugui is NEVER a code executor because of
#         Windows UX / real device tests; acceptance stays FUGUI.
# ---------------------------------------------------------------------------

def t6_fugui_never_executor():
    battery = [
        base_task(task_id="WF004-T6a", constraints=["real_device_test"]),
        base_task(task_id="WF004-T6b", real_device_test=True),
        base_task(task_id="WF004-T6c", type="bot_ux", risk="MEDIUM",
                  constraints=["real_device_test"]),
        base_task(task_id="WF004-T6d", type="fix", risk="LOW",
                  real_device_test=True),
    ]
    details = {}
    ok = True
    for task in battery:
        res = br.route_task(task)
        details[task["task_id"]] = res.as_dict()
        ok = ok and res.executor != "FUGUI"
        ok = ok and res.acceptance == "FUGUI"
        ok = ok and res.route == br.MAX_THEN_FUGUI_ACCEPTANCE
    return ok, details


def test_6_fugui_never_executor():
    ok, detail = t6_fugui_never_executor()
    assert ok, detail


# ---------------------------------------------------------------------------
# TEST-7: determinism - identical input always yields an identical RouteResult
# ---------------------------------------------------------------------------

def t7_deterministic():
    tasks = [
        base_task(task_id="WF004-T7a", type="code", risk="LOW"),
        base_task(task_id="WF004-T7b", type="api", risk="MEDIUM",
                  constraints=["real_device_test"]),
        base_task(task_id="WF004-T7c", constraints=["rbac_change"]),
        base_task(task_id="WF004-T7d", production_access=True),
        base_task(task_id="WF004-T7e", type="mystery"),
    ]
    details = {}
    ok = True
    for task in tasks:
        first, second = br.route_task(task), br.route_task(task)
        details[task["task_id"]] = {"route": first.route,
                                    "identical": first == second}
        ok = ok and first == second
        ok = ok and first.as_dict() == second.as_dict()
    return ok, details


def test_7_deterministic():
    ok, detail = t7_deterministic()
    assert ok, detail


# ---------------------------------------------------------------------------
# TEST-8: schema validation FAIL CLOSED - insufficient fields are errors and
#         route_task never dispatches an executor for them.
# ---------------------------------------------------------------------------

def t8_validation_fail_closed():
    bad = [
        {},
        "not-a-dict",
        base_task(task_id="WF004-T8a", risk=None),
        base_task(task_id="WF004-T8b", type=""),
        base_task(task_id="WF004-T8c", objective=None),
        base_task(task_id="WF004-T8d", constraints="not-a-list"),
        base_task(task_id="WF004-T8e", capabilities=None),
    ]
    details = {"validated_ok": []}
    ok = True
    for i, task in enumerate(bad):
        valid, errors = br.validate_task_schema(task)
        details["validated_ok"].append(valid)
        ok = ok and not valid and isinstance(errors, list) and bool(errors)
        res = br.route_task(task)
        # Fail closed: invalid tasks must not reach an agent executor.
        ok = ok and res.route == br.HERMES_TRIAGE
        ok = ok and res.executor == "ROUTER"
    details["all_invalid"] = not any(details["validated_ok"])
    return ok, details


def test_8_validation_fail_closed():
    ok, detail = t8_validation_fail_closed()
    assert ok, detail


SUITES = [
    ("TEST-1", "direct-max", t1_direct_max),
    ("TEST-2", "hermes-then-max", t2_hermes_then_max),
    ("TEST-3", "fugui-acceptance-only", t3_fugui_acceptance_only),
    ("TEST-4", "owner-approval", t4_owner_approval),
    ("TEST-5", "hermes-triage", t5_hermes_triage),
    ("TEST-6", "fugui-never-executor", t6_fugui_never_executor),
    ("TEST-7", "deterministic", t7_deterministic),
    ("TEST-8", "validation-fail-closed", t8_validation_fail_closed),
]


def main():
    summary = {"task_id": "WF-004", "run_at": wf.now_iso(), "tests": {}}
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
