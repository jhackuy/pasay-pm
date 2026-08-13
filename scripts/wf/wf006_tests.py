"""WF-006 acceptance tests for the four workflow guardrails
(UX-ACCEPTANCE-FREEZE-AND-GUARDRAILS-001).

Program First, LLM Last: every assertion is deterministic. Real subprocesses
are only local python (the timeout tests deliberately hang for 1s and must be
terminated by the wrapper).

Standalone: python scripts/wf/wf006_tests.py
    -> writes .ai-control/results/WF-006/tests.json
Pytest: pytest scripts/wf/wf006_tests.py -q
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wf_lib as wf  # noqa: E402
import wf_guardrails as wg  # noqa: E402

RESULTS_DIR = os.path.join(wf.RESULTS_DIR, "WF-006")


# ---------------------------------------------------------------------------
# TEST-1 (G1): platform semantics must live in the FakeBot test double
# ---------------------------------------------------------------------------

def t1_platform_semantics():
    ok = True
    details = {}

    # Positive: the frozen FakeBot conftest still enforces the Telegram rule.
    real_violations = wg.scan_platform_semantics()
    ok = ok and real_violations == []
    details["real_conftest"] = {"violations": real_violations}

    # Negative: deleting the guard from the fake must be a scan failure.
    tmpdir = tempfile.mkdtemp(prefix="wf006-g1-")
    try:
        fake = os.path.join(tmpdir, "conftest.py")
        wf.write_file(
            fake,
            "class FakeBot:\n"
            "    def edit_message_text(self, **kw):\n"
            "        return None\n",
        )
        violations = wg.scan_platform_semantics(fake)
        hit = any(v["rule"] == "telegram_reply_keyboard_not_editable"
                  for v in violations)
        ok = ok and hit
        details["deleted_guard"] = {"violations": violations, "flagged": hit}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return ok, details


def test_1_platform_semantics():
    ok, detail = t1_platform_semantics()
    assert ok, detail


# ---------------------------------------------------------------------------
# TEST-2 (G2): silent exception scanner + explicit allowlist
# ---------------------------------------------------------------------------

def t2_silent_exception_scanner():
    ok = True
    details = {}
    tmpdir = tempfile.mkdtemp(prefix="wf006-g2-")
    try:
        handlers = os.path.join(tmpdir, "handlers")
        os.makedirs(handlers)
        bad = os.path.join(handlers, "bad.py")
        wf.write_file(
            bad,
            "def a():\n"
            "    try:\n"
            "        x()\n"
            "    except:\n"
            "        pass\n"
            "\n"
            "def b():\n"
            "    try:\n"
            "        y()\n"
            "    except Exception:\n"
            "        pass  # silent\n",
        )
        violations, allowlisted = wg.scan_silent_exceptions(handlers, [])
        kinds = sorted(v["kind"] for v in violations)
        ok = ok and kinds == ["bare_except", "silent_pass"]
        details["no_allowlist"] = {"violations": violations, "kinds": kinds}

        allow = [
            {"file": wg.relpath(bad),
             "line": v["line"], "kind": v["kind"], "reason": "test fixture"}
            for v in violations
        ]
        violations2, allowlisted2 = wg.scan_silent_exceptions(handlers, allow)
        ok = ok and violations2 == [] and len(allowlisted2) == 2
        details["with_allowlist"] = {"violations": violations2,
                                     "allowlisted": allowlisted2}

        real_violations, real_allowed = wg.scan_silent_exceptions()
        ok = ok and real_violations == []
        details["real_handlers"] = {"violations": real_violations,
                                    "allowlisted_count": len(real_allowed)}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return ok, details


def test_2_silent_exception_scanner():
    ok, detail = t2_silent_exception_scanner()
    assert ok, detail


# ---------------------------------------------------------------------------
# TEST-3 (G3): READY_FOR_OWNER_UX_RETEST gate
# ---------------------------------------------------------------------------

def _fake_run_tests(result):
    def _run(task, tests, timeout):
        if not tests:
            return {"result": "FAIL", "detail": "no tests"}
        return {"result": result, "detail": {"tests": list(tests)}}
    return _run


def _fake_hash(sha):
    return lambda: sha


def _healthy_probe():
    return {"result": "PASS",
            "single_polling_instance": {"result": "PASS"},
            "getme_ok": {"result": "PASS"},
            "no_409_conflict": {"result": "PASS"},
            "backend_health": {"result": "PASS"}}


def t3_owner_ux_gate():
    ok = True
    details = {}
    target = "a" * 64
    base = {
        "task_id": "WF006-T3",
        "targeted_tests": ["tests/test_button_determinism.py"],
        "regression_tests": ["tests/test_button_determinism.py"],
        "target_workspace_sha256": target,
    }

    # YES: every condition green.
    yes = wg.owner_ux_gate(
        dict(base),
        run_tests=_fake_run_tests("PASS"),
        workspace_hash=_fake_hash(target),
        runtime_probe_fn=lambda task: _healthy_probe(),
    )
    ok = ok and yes["READY_FOR_OWNER_UX_RETEST"] == "YES"
    details["yes"] = {c: yes["conditions"][c]["result"] for c in wg.GATE_CONDITIONS}

    # Targeted tests fail -> NO.
    no1 = wg.owner_ux_gate(
        dict(base),
        run_tests=_fake_run_tests("FAIL"),
        workspace_hash=_fake_hash(target),
        runtime_probe_fn=lambda task: _healthy_probe(),
    )
    ok = ok and no1["READY_FOR_OWNER_UX_RETEST"] == "NO"

    # Regression proof missing -> NO.
    no2 = wg.owner_ux_gate(
        dict(base, regression_tests=[]),
        run_tests=_fake_run_tests("PASS"),
        workspace_hash=_fake_hash(target),
        runtime_probe_fn=lambda task: _healthy_probe(),
    )
    ok = ok and no2["READY_FOR_OWNER_UX_RETEST"] == "NO"
    ok = ok and no2["conditions"]["FAILURE_REGRESSION_PROVEN"]["result"] == "FAIL"

    # Live version mismatch -> NO.
    no3 = wg.owner_ux_gate(
        dict(base),
        run_tests=_fake_run_tests("PASS"),
        workspace_hash=_fake_hash("b" * 64),
        runtime_probe_fn=lambda task: _healthy_probe(),
    )
    ok = ok and no3["READY_FOR_OWNER_UX_RETEST"] == "NO"

    # Runtime unhealthy (409) -> NO.
    def _unhealthy(task):
        probe = _healthy_probe()
        probe["no_409_conflict"] = {"result": "FAIL", "found": True}
        probe["result"] = "FAIL"
        return probe

    no4 = wg.owner_ux_gate(
        dict(base),
        run_tests=_fake_run_tests("PASS"),
        workspace_hash=_fake_hash(target),
        runtime_probe_fn=_unhealthy,
    )
    ok = ok and no4["READY_FOR_OWNER_UX_RETEST"] == "NO"
    details["no_cases"] = {"targeted_fail": no1["READY_FOR_OWNER_UX_RETEST"],
                           "no_regression": no2["READY_FOR_OWNER_UX_RETEST"],
                           "version_mismatch": no3["READY_FOR_OWNER_UX_RETEST"],
                           "runtime_409": no4["READY_FOR_OWNER_UX_RETEST"]}
    return ok, details


def test_3_owner_ux_gate():
    ok, detail = t3_owner_ux_gate()
    assert ok, detail


# ---------------------------------------------------------------------------
# TEST-4 (G4): timeout wrapper terminates a deliberately hanging command
# ---------------------------------------------------------------------------

def t4_timeout_wrapper():
    details = {}
    ok = True
    rec = wf.run_timed(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        timeout=1,
    )
    timed = (rec["status"] == wf.TIMEOUT_STATUS
             and rec["elapsed"] >= 1.0
             and rec.get("terminated") is True)
    ok = ok and timed
    time.sleep(0.3)
    alive = wf.pid_alive(rec["pid"])
    ok = ok and not alive
    details["timeout"] = {k: rec[k] for k in ("status", "elapsed", "terminated", "pid")}
    details["child_alive_after_timeout"] = alive

    rec2 = wf.run_timed([sys.executable, "-c", "print('ok')"], timeout=5)
    ok = ok and rec2["status"] == "OK" and rec2["returncode"] == 0
    details["normal"] = {"status": rec2["status"], "returncode": rec2["returncode"]}
    return ok, details


def test_4_timeout_wrapper():
    ok, detail = t4_timeout_wrapper()
    assert ok, detail


# ---------------------------------------------------------------------------
# TEST-5 (G4): sh() contract + timeout defaults + long-task declaration
# ---------------------------------------------------------------------------

def t5_timeout_defaults_and_sh():
    details = {}
    ok = True

    rc, out = wf.sh([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
    ok = ok and rc == -1 and "TIMEOUT" in out and "elapsed" in out
    details["sh_timeout"] = {"returncode": rc, "output": out[:120]}

    valid_ok, why = wg.validate_long_task_timeout({"long_running": True})
    ok = ok and not valid_ok and "timeout_seconds" in why
    valid_ok2, _ = wg.validate_long_task_timeout(
        {"long_running": True, "timeout_seconds": 600}
    )
    ok = ok and valid_ok2
    details["long_task"] = {"missing_timeout_blocked": not valid_ok,
                            "declared_allowed": valid_ok2}

    t_shell = wg.resolve_timeout(None, "shell")
    t_git = wg.resolve_timeout(None, "git/status/hash/process")
    t_tests = wg.resolve_timeout(None, "targeted_tests")
    t_override = wg.resolve_timeout(None, "targeted_tests", explicit=7)
    ok = ok and (t_shell, t_git, t_tests) == (60, 60, 180) and t_override == 7
    details["defaults"] = {"shell": t_shell, "git": t_git,
                           "targeted_tests": t_tests, "override": t_override}
    return ok, details


def test_5_timeout_defaults_and_sh():
    ok, detail = t5_timeout_defaults_and_sh()
    assert ok, detail


# ---------------------------------------------------------------------------
# TEST-6: real-repo static guardrail report is green
# ---------------------------------------------------------------------------

def t6_real_repo_report():
    report = wg.guardrail_report()
    ok = (report["GUARDRAIL_PLATFORM_SEMANTICS"] == "PASS"
          and report["GUARDRAIL_NO_SILENT_UI_EXCEPTION"] == "PASS")
    return ok, report


def test_6_real_repo_report():
    ok, detail = t6_real_repo_report()
    assert ok, detail


SUITES = [
    ("TEST-1", "platform-semantics-in-fake", t1_platform_semantics),
    ("TEST-2", "no-silent-ui-exception-scan", t2_silent_exception_scanner),
    ("TEST-3", "owner-ux-gate", t3_owner_ux_gate),
    ("TEST-4", "timeout-wrapper", t4_timeout_wrapper),
    ("TEST-5", "timeout-defaults-sh", t5_timeout_defaults_and_sh),
    ("TEST-6", "real-repo-static-report", t6_real_repo_report),
]


def main():
    summary = {"task_id": "WF-006", "run_at": wf.now_iso(), "tests": {}}
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
