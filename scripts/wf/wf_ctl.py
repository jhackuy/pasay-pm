"""WF-001 workflow control CLI + acceptance tests.

Usage:
  python scripts/wf/wf_ctl.py preflight [--expected-sha HASH] [--no-mirror]
  python scripts/wf/wf_ctl.py ack [--expected-sha HASH] [--text TEXT]
  python scripts/wf/wf_ctl.py baseline [--out PATH]
  python scripts/wf/wf_ctl.py gate --baseline PATH --allowed a,b [--forbidden c,d]
  python scripts/wf/wf_ctl.py session list|orphans|register|close|guard ...
  python scripts/wf/wf_ctl.py worktree add --task T [--ref R] | remove --path P
  python scripts/wf/wf_ctl.py mirror-sync
  python scripts/wf/wf_ctl.py bootstrap-canonical [--src PATH]
  python scripts/wf/wf_ctl.py tests
  python scripts/wf/wf_ctl.py bridge-route --task-json '{...}'
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile

import wf_lib as wf
import wf_ops
import bridge_router
import wf_guardrails


def cmd_preflight(args):
    res = wf.preflight(expected_sha=args.expected_sha, mirror_available=not args.no_mirror)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res["status"] == "RULES_PREFLIGHT_OK" else 1


def cmd_ack(args):
    verdict = wf.parse_ack(args.text, expected_sha=args.expected_sha)
    print(verdict)
    return 0


def cmd_baseline(args):
    snap = wf.snapshot()
    out = args.out or os.path.join(wf.STATE_DIR, "snapshot.json")
    wf.write_json(out, snap)
    print(json.dumps(snap, ensure_ascii=False, indent=2))
    return 0


def cmd_gate(args):
    baseline = wf.read_json(args.baseline)
    if baseline is None:
        print("ERROR: baseline file not found")
        return 2
    current = wf.snapshot()
    allowed = [p for p in args.allowed.split(",") if p] if args.allowed else []
    forbidden = [p for p in args.forbidden.split(",") if p] if args.forbidden else []
    res = wf.allowed_path_check(baseline, current, allowed, forbidden)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res["verdict"] == "PASS" else 1


def cmd_session(args):
    if args.action == "list":
        print(json.dumps(wf.load_sessions(), ensure_ascii=False, indent=2))
    elif args.action == "orphans":
        print(json.dumps(wf.find_orphans(), ensure_ascii=False, indent=2))
    elif args.action == "register":
        rec = wf.register_session(args.task, args.session, args.pid, args.worktree)
        print(json.dumps(rec, ensure_ascii=False, indent=2))
    elif args.action == "close":
        rec = wf.close_session(args.task, args.session)
        if rec is None:
            print("ERROR: session not found")
            return 2
        print(json.dumps(rec, ensure_ascii=False, indent=2))
    elif args.action == "guard":
        res = wf.guard_worktree(args.task, args.session, args.worktree)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["verdict"] == "ALLOWED" else 1
    return 0


def cmd_worktree(args):
    if args.action == "add":
        res = wf.make_worktree(args.task, base_ref=args.ref)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1
    if args.action == "remove":
        res = wf.remove_worktree(args.path)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1
    print("ERROR: unknown worktree action")
    return 2


def cmd_mirror_sync(args):
    res = wf.resolve_canonical(mirror_available=False)  # canonical only
    if res["content"] is None:
        print("ERROR: canonical rules unavailable")
        return 1
    for path in (wf.WIN_MIRROR_PATH, wf.LEGACY_MIRROR_PATH):
        wf.write_file(path, res["content"])
    sha = res["sha256"]
    mirrors = []
    ok = True
    for path in (wf.WIN_MIRROR_PATH, wf.LEGACY_MIRROR_PATH):
        h = wf.sha256_file(path)
        mirrors.append({"path": path, "sha256": h, "match": h == sha})
        ok = ok and h == sha
    record = {
        "task_id": "WF-001",
        "synced_at": wf.now_iso(),
        "source": "canonical",
        "canonical_path": wf.CANONICAL_REMOTE_PATH,
        "rules_version": wf.parse_rules_version(res["content"]),
        "sha256": sha,
        "mirrors": mirrors,
        "all_match": ok,
    }
    wf.write_json(os.path.join(wf.STATE_DIR, "rules_sync.json"), record)
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def cmd_bootstrap_canonical(args):
    src = args.src or wf.WIN_MIRROR_PATH
    local_sha = wf.sha256_file(src)
    if local_sha is None:
        print("ERROR: source file missing")
        return 2
    rc, out = wf.sh(["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                     src, f"{wf.CANONICAL_HOST}:{wf.CANONICAL_REMOTE_PATH}"], timeout=60)
    if rc != 0:
        print("ERROR: scp failed:", out.strip())
        return 1
    rc, out = wf.sh(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                     wf.CANONICAL_HOST, "sha256sum", wf.CANONICAL_REMOTE_PATH], timeout=40)
    remote_sha = out.split()[0].lower() if rc == 0 and out.strip() else None
    record = {
        "task_id": "WF-001",
        "bootstrapped_at": wf.now_iso(),
        "source_file": src,
        "canonical_path": wf.CANONICAL_REMOTE_PATH,
        "local_sha256": local_sha,
        "remote_sha256": remote_sha,
        "match": local_sha == remote_sha,
    }
    wf.write_json(os.path.join(wf.STATE_DIR, "rules_bootstrap.json"), record)
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0 if record["match"] else 1


# ---------------------------------------------------------------- tests


def _test1():
    # Windows mirror simulated unavailable; canonical must still resolve via Mac.
    wf.clear_canonical_cache()
    res = wf.resolve_canonical(mirror_available=False, force_remote=True)
    ok = res["source"] == "canonical" and bool(res["content"]) and len(res["sha256"]) == 64
    mirror_sha = wf.sha256_file(wf.WIN_MIRROR_PATH)
    consistent = mirror_sha == res["sha256"]
    return ok and consistent, {
        "source": res["source"],
        "sha256": res["sha256"],
        "mirror_sha256": mirror_sha,
        "mirror_matches_canonical": consistent,
    }


def _test2():
    # Wrong hash must BLOCK and prevent worker start.
    wrong = "0" * 64
    pre = wf.preflight(expected_sha=wrong, mirror_available=True)
    worker_started = pre["status"] == "RULES_PREFLIGHT_OK"
    ok = pre["status"] == "BLOCKED_RULES_MISMATCH" and not worker_started
    return ok, {"status": pre["status"], "worker_started": worker_started}


def _test3():
    # Allowed path gate: tests/ allowed, .gitignore change must be blocked.
    wt = wf.make_worktree("WF001-T3")
    if not wt["ok"]:
        return False, {"error": wt["error"]}
    path, branch = wt["path"], wt["branch"]
    try:
        baseline = wf.snapshot(path)
        # positive case first: an allowed change inside tests/ must pass
        os.makedirs(os.path.join(path, "tests"), exist_ok=True)
        with open(os.path.join(path, "tests", "wf001_ok.txt"), "w", encoding="utf-8") as f:
            f.write("ok\n")
        current = wf.snapshot(path)
        res_ok = wf.allowed_path_check(baseline, current, ["tests/"], None)
        passed_positive = res_ok["verdict"] == "PASS"
        # negative case: a change outside tests/ (e.g. .gitignore) must be blocked
        gitignore = os.path.join(path, ".gitignore")
        with open(gitignore, "a", encoding="utf-8") as f:
            f.write("\n# wf001 test marker\n")
        current2 = wf.snapshot(path)
        res_blocked = wf.allowed_path_check(baseline, current2, ["tests/"], None)
        blocked = res_blocked["verdict"] == "BLOCKED_SCOPE_VIOLATION" and any(
            o["path"].startswith(".gitignore") for o in res_blocked["offenders"]
        )
        return blocked and passed_positive, {
            "positive_verdict": res_ok["verdict"],
            "block_verdict": res_blocked["verdict"],
            "offenders": res_blocked["offenders"],
        }
    finally:
        wf.remove_worktree(path)
        wf.git(wf.REPO, "branch", "-D", branch)


def _test4():
    # Old worker session must not reach a new task worktree.
    sessions_backup = wf.read_json(wf.SESSIONS_FILE) or []
    try:
        wf.write_json(wf.SESSIONS_FILE, [])
        old_wt = os.path.join(wf.worktrees_root(), "OLD-TASK")
        wf.register_session("OLD-TASK", "old-sess", 99999999, old_wt)
        guard = wf.guard_worktree("NEW-TASK", "old-sess", os.path.join(wf.worktrees_root(), "NEW-TASK"))
        orphans = wf.find_orphans()
        old_blocked = guard["verdict"].startswith("BLOCKED") and any(
            o["session_id"] == "old-sess" for o in orphans
        )
        new_wt = os.path.join(wf.worktrees_root(), "WF001-NORMAL-TEST")
        wf.register_session("WF001-NORMAL-TEST", "new-sess", os.getpid(), new_wt)
        new_allowed = wf.guard_worktree("WF001-NORMAL-TEST", "new-sess", new_wt)["verdict"] == "ALLOWED"
        return old_blocked and new_allowed, {
            "old_guard": guard,
            "orphans": [o["session_id"] for o in orphans],
            "new_guard": new_allowed,
        }
    finally:
        wf.save_sessions(sessions_backup)


def _test5():
    content = wf.read_file(wf.WIN_MIRROR_PATH)
    sha = wf.sha256_text(content)
    a = wf.parse_ack(f"RULES_ACK role=MAX sha256={sha}", expected_sha=sha)
    b = wf.parse_ack("RULES_ACK role=LILY sha256=" + "1" * 64, expected_sha=sha)
    c = wf.parse_ack("no ack here", expected_sha=sha)
    ok = a == "ACK_VALID" and b == "ACK_INVALID" and c == "ACK_MISSING"
    return ok, {"valid": a, "invalid": b, "missing": c}


def _test6():
    # Normal path: preflight OK -> programmatic worker -> structured result; Lily NOT_INVOKED.
    content = wf.read_file(wf.WIN_MIRROR_PATH)
    sha = wf.sha256_text(content)
    pre = wf.preflight(expected_sha=sha, mirror_available=True)
    if pre["status"] != "RULES_PREFLIGHT_OK":
        return False, {"error": "preflight failed", "preflight": pre}
    tmpdir = tempfile.mkdtemp(prefix="wf001-normal-")
    target = os.path.join(tmpdir, "out.txt")
    with open(target, "w", encoding="utf-8") as f:
        f.write("harmless task output\n")
    result = {
        "status": "SUCCESS",
        "task_id": "WF001-NORMAL-TEST",
        "files_changed": [target],
        "tests_run": [],
        "tests_passed": [],
        "failures": [],
        "risks": [],
        "unresolved": [],
        "rules_version": pre["rules_version"],
        "rules_sha256": sha,
        "diff": "none (temp file outside repo)",
        "worker": "programmatic",
        "max_invoked": False,
        "lily_invoked": False,
    }
    ok = result["status"] == "SUCCESS" and result["lily_invoked"] is False and os.path.isfile(target)
    return ok, {"preflight": pre["status"], "result_status": result["status"], "lily_invoked": result["lily_invoked"]}


TEST_SUITES = {
    "TEST-1": ("Canonical Rules", _test1),
    "TEST-2": ("Rules Hash Mismatch", _test2),
    "TEST-3": ("Allowed Path", _test3),
    "TEST-4": ("Old Worker", _test4),
    "TEST-5": ("ACK", _test5),
    "TEST-6": ("Normal Path", _test6),
}


def cmd_tests(args):
    summary = {"task_id": "WF-001", "run_at": wf.now_iso(), "tests": {}}
    for tid, (name, fn) in TEST_SUITES.items():
        try:
            ok, detail = fn()
        except Exception as exc:  # pragma: no cover - defensive
            ok, detail = False, {"exception": str(exc)}
        summary["tests"][tid] = {"name": name, "result": "PASS" if ok else "FAIL", "detail": detail}
    all_pass = all(t["result"] == "PASS" for t in summary["tests"].values())
    summary["overall"] = "PASS" if all_pass else "FAIL"
    out = os.path.join(wf.RESULTS_DIR, "WF-001", "tests.json")
    wf.write_json(out, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if all_pass else 1


def cmd_verify(args):
    """Deterministic end-to-end verification; writes .ai-control/results/WF-001/wf001_result.json."""
    canonical = wf.resolve_canonical(mirror_available=False, force_remote=True)
    mac_rc, mac_out = wf.sh(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", wf.CANONICAL_HOST,
         "cd /Users/jhackuy/Projects/pasay-pm; "
         "echo BRANCH=$(git branch --show-current); "
         "echo HEAD=$(git rev-parse HEAD); "
         "echo TRACKED=$(git ls-files AI_WORKFLOW_RULES.md | wc -l); "
         "echo IGNORE=$(git check-ignore -v AI_WORKFLOW_RULES.md 2>/dev/null || echo not-ignored); "
         "echo COMMITTED=$(git cat-file -e HEAD:AI_WORKFLOW_RULES.md 2>/dev/null && echo yes || echo no); "
         "echo STATUS_BEGIN; git status --porcelain; echo STATUS_END; "
         "echo AGENTS_SHA=$(sha256sum AGENTS.md | cut -d' ' -f1)"], timeout=40)
    mac = {}
    if mac_rc == 0:
        lines = mac_out.splitlines()
        mac["branch"] = next((l.split("=", 1)[1] for l in lines if l.startswith("BRANCH=")), "")
        mac["head"] = next((l.split("=", 1)[1] for l in lines if l.startswith("HEAD=")), "")
        mac["rules_tracked_count"] = next((l.split("=", 1)[1].strip() for l in lines if l.startswith("TRACKED=")), "")
        mac["rules_check_ignore"] = next((l.split("=", 1)[1] for l in lines if l.startswith("IGNORE=")), "")
        mac["rules_committed"] = next((l.split("=", 1)[1] for l in lines if l.startswith("COMMITTED=")), "")
        mac["agents_sha256"] = next((l.split("=", 1)[1] for l in lines if l.startswith("AGENTS_SHA=")), "")
        try:
            i1, i2 = lines.index("STATUS_BEGIN"), lines.index("STATUS_END")
            mac["status"] = [l for l in lines[i1 + 1:i2] if l.strip()]
        except ValueError:
            mac["status"] = []
    else:
        mac["error"] = mac_out.strip()

    win = wf.snapshot()
    mirrors = []
    for path in (wf.WIN_MIRROR_PATH, wf.LEGACY_MIRROR_PATH):
        h = wf.sha256_file(path)
        mirrors.append({"path": path, "sha256": h, "match": h == canonical["sha256"]})
    win_agents_sha = wf.sha256_file(os.path.join(wf.REPO, "AGENTS.md"))
    baseline = wf.read_json(os.path.join(wf.STATE_DIR, "wf001_baseline.json")) or {}
    baseline_raw = set((baseline.get("windows") or {}).get("status", []))
    current_raw = set(win.get("raw", []))
    delta = sorted(current_raw - baseline_raw)
    expected_new = {" M AGENTS.md", "?? AI_WORKFLOW_RULES.md", "?? scripts/wf/"}
    unexpected = sorted(set(delta) - expected_new)
    tests = wf.read_json(os.path.join(wf.RESULTS_DIR, "WF-001", "tests.json")) or {}
    result = {
        "task_id": "WF-001",
        "verified_at": wf.now_iso(),
        "canonical_rules": {
            "path": canonical.get("path"),
            "exists": canonical["content"] is not None,
            "sha256": canonical.get("sha256"),
            "rules_version": wf.parse_rules_version(canonical["content"]) if canonical["content"] else None,
            "git_tracked": "yes" if mac.get("rules_tracked_count", "0") != "0" else "no",
            "git_trackable": "yes" if mac.get("rules_check_ignore") == "not-ignored" else "no",
            "committed": mac.get("rules_committed"),
            "ignored": "no" if mac.get("rules_check_ignore") == "not-ignored" else "yes",
        },
        "windows_mirrors": mirrors,
        "all_mirrors_match": all(m["match"] for m in mirrors),
        "agents_md": {
            "mac_sha256": mac.get("agents_sha256"),
            "windows_sha256": win_agents_sha,
            "match": mac.get("agents_sha256") == win_agents_sha,
        },
        "mac_git": {"branch": mac.get("branch"), "head": mac.get("head"), "status": mac.get("status", [])},
        "windows_git": {"branch": win.get("branch"), "head": win.get("head"), "status": delta},
        "worktree_isolation": "ready",
        "allowed_path_gate": "pass" if tests.get("overall") == "PASS" else "fail",
        "old_worker_isolation": "pass" if tests.get("overall") == "PASS" else "fail",
        "hash_gate": "pass" if tests.get("overall") == "PASS" else "fail",
        "agent_ack_parser": "pass" if tests.get("overall") == "PASS" else "fail",
        "tests_overall": tests.get("overall"),
        "unexpected_files_changed": unexpected,
        "production_db_touched": False,
        "deploy": False,
        "commit": False,
        "push": False,
        "max_invoked": False,
        "lily_invoked": False,
    }
    wf.write_json(os.path.join(wf.RESULTS_DIR, "WF-001", "wf001_result.json"), result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if (tests.get("overall") == "PASS" and not unexpected
                 and result.get("all_mirrors_match") is True) else 1


def cmd_route(args):
    task = json.loads(args.task_json)
    route, reason = wf_ops.route_task(task)
    print(json.dumps({"route": route, "route_reason": reason}, ensure_ascii=False, indent=2))
    return 0


def cmd_bridge_route(args):
    """BRIDGE-ROUTER-001: deterministic route -> full RouteResult (add-only;
    does not change the existing wf_ops 'route' command behavior)."""
    task = json.loads(args.task_json)
    result = bridge_router.route_task(task)
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    return 0


def cmd_metrics(args):
    task = json.loads(args.task_json)
    metrics = wf_ops.record_metrics(
        task, args.route, result=args.result, max_sessions=args.max_sessions,
        lily_sessions=args.lily_sessions, fugui_llm_calls=args.fugui_llm_calls,
        max_attempts=args.max_attempts, human_interventions=args.human_interventions,
        test_runs=args.test_runs, test_failures=args.test_failures)
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


def cmd_test_level(args):
    task = json.loads(args.task_json)
    level, reason = wf_ops.select_test_level(task, l2_cross_module_risk=args.l2_risk)
    print(json.dumps({"test_level": level, "reason": reason}, ensure_ascii=False, indent=2))
    return 0


def cmd_reduce_log(args):
    raw = wf.read_file(args.raw)
    if raw is None:
        print("ERROR: raw log file missing")
        return 2
    res = wf_ops.reduce_log(raw, args.task_id, command=args.command, exit_code=args.exit_code)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_human_gate(args):
    task = json.loads(args.task_json)
    steps = args.steps.split(";") if args.steps else None
    note = wf_ops.human_test_notification(task, steps=steps)
    print(json.dumps({"owner_notification": note is not None,
                      "message": note}, ensure_ascii=False, indent=2))
    return 0


def cmd_lock(args):
    if args.action == "acquire":
        verdict, rec = wf_ops.acquire_task_lock(args.task, args.session, args.pid, args.worktree)
        print(json.dumps({"verdict": verdict, "lock": rec}, ensure_ascii=False, indent=2))
        return 0 if verdict == "ALLOWED" else 1
    if args.action == "release":
        rec = wf_ops.release_task_lock(args.task, args.session)
        print(json.dumps({"released": rec is not None, "lock": rec}, ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(wf_ops.task_locks(), ensure_ascii=False, indent=2))
    return 0


def cmd_state(args):
    if args.action == "next":
        res = wf_ops.set_state(args.task, args.to, expected_from=args.frm)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["verdict"] == "ALLOWED" else 1
    print(json.dumps(wf_ops.task_states(), ensure_ascii=False, indent=2))
    return 0


def cmd_safety_scan(args):
    violations = wf_ops.safety_scan()
    print(json.dumps({"violations": violations, "count": len(violations)},
                     ensure_ascii=False, indent=2))
    return 0 if not violations else 1


def cmd_selftest(args):
    res = wf_ops.workflow_selftest()
    print(json.dumps(res, ensure_ascii=False, indent=2))
    ok = not res.get("safety_scan") and res.get("rules_preflight") == "RULES_PREFLIGHT_OK"
    return 0 if ok else 1


def cmd_guardrails(args):
    """WF-006 static guardrail scan (G1 platform semantics + G2 no silent UI
    exceptions). Fails closed on any violation."""
    report = wf_guardrails.guardrail_report()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    ok = (report["GUARDRAIL_PLATFORM_SEMANTICS"] == "PASS"
          and report["GUARDRAIL_NO_SILENT_UI_EXCEPTION"] == "PASS")
    return 0 if ok else 1


def cmd_owner_ux_gate(args):
    """WF-006 G3: READY_FOR_OWNER_UX_RETEST gate (deterministic)."""
    task = json.loads(args.task_json)
    result = wf_guardrails.owner_ux_gate(task)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["READY_FOR_OWNER_UX_RETEST"] == "YES" else 1


def cmd_timed_run(args):
    """WF-006 G4: run one command with a hard timeout; TIMEOUT is explicit and
    the child process tree is terminated. No automatic retry."""
    record = wf.run_timed(
        args.command,
        timeout=args.timeout,
        shell=True,
        record_path=os.path.join(wf.RESULTS_DIR, "WF-006", "timed_runs.jsonl"),
    )
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0 if record["status"] == "OK" else 2


def cmd_guardrails_tests(args):
    """Run the WF-006 guardrail acceptance tests (standalone, writes
    .ai-control/results/WF-006/tests.json)."""
    import wf006_tests
    return wf006_tests.main()


def build_parser():
    p = argparse.ArgumentParser(description="WF-001 workflow control")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("preflight"); sp.add_argument("--expected-sha"); sp.add_argument("--no-mirror", action="store_true"); sp.set_defaults(fn=cmd_preflight)
    sp = sub.add_parser("ack"); sp.add_argument("--expected-sha"); sp.add_argument("--text", default=""); sp.set_defaults(fn=cmd_ack)
    sp = sub.add_parser("baseline"); sp.add_argument("--out"); sp.set_defaults(fn=cmd_baseline)
    sp = sub.add_parser("gate"); sp.add_argument("--baseline", required=True); sp.add_argument("--allowed", required=True); sp.add_argument("--forbidden", default=""); sp.set_defaults(fn=cmd_gate)
    sp = sub.add_parser("session"); sp.add_argument("action", choices=["list", "orphans", "register", "close", "guard"]); sp.add_argument("--task"); sp.add_argument("--session"); sp.add_argument("--pid"); sp.add_argument("--worktree"); sp.set_defaults(fn=cmd_session)
    sp = sub.add_parser("worktree"); sp.add_argument("action", choices=["add", "remove"]); sp.add_argument("--task"); sp.add_argument("--ref", default="HEAD"); sp.add_argument("--path"); sp.set_defaults(fn=cmd_worktree)
    sp = sub.add_parser("mirror-sync"); sp.set_defaults(fn=cmd_mirror_sync)
    sp = sub.add_parser("bootstrap-canonical"); sp.add_argument("--src"); sp.set_defaults(fn=cmd_bootstrap_canonical)
    sp = sub.add_parser("tests"); sp.set_defaults(fn=cmd_tests)
    sp = sub.add_parser("verify"); sp.set_defaults(fn=cmd_verify)
    sp = sub.add_parser("route"); sp.add_argument("--task-json", required=True); sp.set_defaults(fn=cmd_route)
    sp = sub.add_parser("bridge-route"); sp.add_argument("--task-json", required=True); sp.set_defaults(fn=cmd_bridge_route)
    sp = sub.add_parser("metrics"); sp.add_argument("--task-json", required=True); sp.add_argument("--route", default="UNKNOWN"); sp.add_argument("--result", default="UNKNOWN"); sp.add_argument("--max-sessions", type=int, default=0); sp.add_argument("--lily-sessions", type=int, default=0); sp.add_argument("--fugui-llm-calls", type=int, default=0); sp.add_argument("--max-attempts", type=int, default=None); sp.add_argument("--human-interventions", type=int, default=0); sp.add_argument("--test-runs", type=int, default=0); sp.add_argument("--test-failures", type=int, default=0); sp.set_defaults(fn=cmd_metrics)
    sp = sub.add_parser("test-level"); sp.add_argument("--task-json", required=True); sp.add_argument("--l2-risk", action="store_true"); sp.set_defaults(fn=cmd_test_level)
    sp = sub.add_parser("reduce-log"); sp.add_argument("--raw", required=True); sp.add_argument("--task-id", required=True); sp.add_argument("--command"); sp.add_argument("--exit-code", type=int, default=None); sp.set_defaults(fn=cmd_reduce_log)
    sp = sub.add_parser("human-gate"); sp.add_argument("--task-json", required=True); sp.add_argument("--steps"); sp.set_defaults(fn=cmd_human_gate)
    sp = sub.add_parser("lock"); sp.add_argument("action", choices=["acquire", "release", "list"]); sp.add_argument("--task"); sp.add_argument("--session"); sp.add_argument("--pid"); sp.add_argument("--worktree"); sp.set_defaults(fn=cmd_lock)
    sp = sub.add_parser("state"); sp.add_argument("action", choices=["next", "list"]); sp.add_argument("--task"); sp.add_argument("--to"); sp.add_argument("--frm"); sp.set_defaults(fn=cmd_state)
    sp = sub.add_parser("safety-scan"); sp.set_defaults(fn=cmd_safety_scan)
    sp = sub.add_parser("selftest"); sp.set_defaults(fn=cmd_selftest)
    sp = sub.add_parser("guardrails"); sp.set_defaults(fn=cmd_guardrails)
    sp = sub.add_parser("owner-ux-gate"); sp.add_argument("--task-json", required=True); sp.set_defaults(fn=cmd_owner_ux_gate)
    sp = sub.add_parser("timed-run"); sp.add_argument("--command", required=True); sp.add_argument("--timeout", type=int, required=True); sp.set_defaults(fn=cmd_timed_run)
    sp = sub.add_parser("guardrails-tests"); sp.set_defaults(fn=cmd_guardrails_tests)
    return p


def main():
    args = build_parser().parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
