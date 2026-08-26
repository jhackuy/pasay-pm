"""Tests for PASAY-OPENCODE-QUAL-001 changed-files scope guard.

Standalone: python scripts/wf/wf_scope_guard_tests.py
Pytest:    .venv/Scripts/python.exe -m pytest scripts/wf/wf_scope_guard_tests.py -q

The guard is a pure-stdlib helper used by the OpenCode qualification Slice
to enforce a ≤2-file changed scope. No production coupling.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

guard = importlib.import_module("wf_scope_guard")


def t1_all_in_allowlist() -> tuple[bool, dict]:
    changed = ["scripts/wf/wf_scope_guard.py",
               "scripts/wf/wf_scope_guard_tests.py"]
    rules = ["scripts/wf/**"]
    result = guard.check_scope(changed, rules)
    ok = result["ok"] is True and result["violations"] == []
    return ok, result


def t2_one_violation() -> tuple[bool, dict]:
    changed = ["scripts/wf/wf_scope_guard.py",
               "app/api/foo.py"]
    rules = ["scripts/wf/**"]
    result = guard.check_scope(changed, rules)
    ok = (result["ok"] is False
          and result["violations"] == ["app/api/foo.py"])
    return ok, result


def t3_multiple_violations() -> tuple[bool, dict]:
    changed = ["scripts/wf/wf_scope_guard.py",
               "app/api/foo.py",
               "migrations/versions/abc.py",
               "tests/test_business.py"]
    rules = ["scripts/wf/**"]
    result = guard.check_scope(changed, rules)
    expected = ["app/api/foo.py",
                "migrations/versions/abc.py",
                "tests/test_business.py"]
    ok = (result["ok"] is False
          and result["violations"] == expected)
    return ok, result


def t4_empty_changed_set() -> tuple[bool, dict]:
    result = guard.check_scope([], ["scripts/wf/**"])
    ok = result["ok"] is True and result["violations"] == []
    return ok, result


def t5_invalid_rules_fail_closed() -> tuple[bool, dict]:
    cases = [
        ([], ["app/api/foo.py"]),
        ([""], ["app/api/foo.py"]),
        (["   "], ["app/api/foo.py"]),
        (["\t", "\n"], ["app/api/foo.py"]),
    ]
    details = []
    for rules, changed in cases:
        r = guard.check_scope(changed, rules)
        details.append({"rules": rules, "changed": changed,
                        "ok": r["ok"],
                        "violations": r["violations"]})
    ok = all(d["ok"] is False for d in details)
    return ok, {"cases": details}


def t6_path_normalization() -> tuple[bool, dict]:
    changed = ["scripts\\wf\\wf_scope_guard.py",
               "scripts/wf/wf_scope_guard_tests.py"]
    rules = ["scripts/wf/**"]
    result = guard.check_scope(changed, rules)
    ok = result["ok"] is True and result["violations"] == []
    norm_in = guard.normalize_path("a\\b/c.py")
    norm_out = guard.normalize_path("a/b/c.py")
    return ok, {"result": result,
                "normalized_eq": norm_in == norm_out,
                "norm": norm_in}


def test_1_all_in_allowlist():
    ok, detail = t1_all_in_allowlist()
    assert ok, detail


def test_2_one_violation():
    ok, detail = t2_one_violation()
    assert ok, detail


def test_3_multiple_violations():
    ok, detail = t3_multiple_violations()
    assert ok, detail


def test_4_empty_changed_set():
    ok, detail = t4_empty_changed_set()
    assert ok, detail


def test_5_invalid_rules_fail_closed():
    ok, detail = t5_invalid_rules_fail_closed()
    assert ok, detail


def test_6_path_normalization():
    ok, detail = t6_path_normalization()
    assert ok, detail


def _run_cli(args):
    py = sys.executable
    cmd = [py, os.path.join(HERE, "wf_scope_guard.py")] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def test_cli_pass_returns_zero():
    proc = _run_cli(["--files", "scripts/wf/x.py",
                     "--allow", "scripts/wf/**",
                     "--json"])
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["ok"] is True


def test_cli_fail_returns_nonzero():
    proc = _run_cli(["--files", "app/api/x.py",
                     "--allow", "scripts/wf/**",
                     "--json"])
    assert proc.returncode != 0, proc.stdout
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert "app/api/x.py" in payload["violations"]


def _standalone_report():
    out = {"results": []}
    for name, fn in [("t1", t1_all_in_allowlist),
                     ("t2", t2_one_violation),
                     ("t3", t3_multiple_violations),
                     ("t4", t4_empty_changed_set),
                     ("t5", t5_invalid_rules_fail_closed),
                     ("t6", t6_path_normalization)]:
        ok, detail = fn()
        out["results"].append({"name": name, "ok": ok, "detail": detail})
    out["all_ok"] = all(r["ok"] for r in out["results"])
    return out


if __name__ == "__main__":
    rep = _standalone_report()
    print(json.dumps(rep, indent=2, ensure_ascii=False))
    sys.exit(0 if rep["all_ok"] else 1)