"""PASAY-OPENDESIGN-AUTO-DISPATCH-001 trigger contract.

Program First, LLM Last. Pure stdlib. Every check below is deterministic:
static scan over the issue_comment event payload, plus an in-memory
idempotency record. No LLM is called, no network is required.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROUTE_DESIGN_DEV = "route:design-dev"
APPROVAL_MARKER = "OWNER_APPROVED_FOR_OPENDESIGN"

STATE_APPROVED_NOT_DISPATCHED = "APPROVED_NOT_DISPATCHED"
STATE_DISPATCHED = "DISPATCHED"
STATE_DISPATCH_FAILED = "DISPATCH_FAILED"
STATE_DESIGN_ACCEPTED = "DESIGN_ACCEPTED"
STATE_BLOCKED_FOR_PRODUCT_DECISION = "BLOCKED_FOR_PRODUCT_DECISION"
STATE_NO_DISPATCH = "NO_DISPATCH"

ALL_STATES = (
    STATE_APPROVED_NOT_DISPATCHED,
    STATE_DISPATCHED,
    STATE_DISPATCH_FAILED,
    STATE_DESIGN_ACCEPTED,
    STATE_BLOCKED_FOR_PRODUCT_DECISION,
    STATE_NO_DISPATCH,
)

VERDICT_DISPATCH = "DISPATCH"
VERDICT_NO_DISPATCH = "NO_DISPATCH"
VERDICT_BLOCKED = "BLOCKED"

MAX_TITLE_LEN = 256
MAX_BODY_LEN = 64000
MAX_COMMENT_LEN = 16000

SHA_PREFIX_LEN = 12

IDEMPOTENCY_WINDOW_SECONDS = 24 * 60 * 60


def extract_repo(event):
    repo = event.get("repository") or {}
    name = repo.get("name") or ""
    owner = (repo.get("owner") or {}).get("login") or ""
    return owner, name


def extract_issue(event):
    issue = event.get("issue") or {}
    return {
        "number": int(issue.get("number") or 0),
        "state": str(issue.get("state") or ""),
        "title": str(issue.get("title") or ""),
        "body": str(issue.get("body") or ""),
        "labels": [str(l.get("name") or "") for l in (issue.get("labels") or [])],
    }


def extract_comment(event):
    comment = event.get("comment") or {}
    return {
        "id": int(comment.get("id") or 0),
        "body": str(comment.get("body") or ""),
        "created_at": str(comment.get("created_at") or ""),
    }


def extract_actor(event):
    sender = event.get("sender") or {}
    return {
        "login": str(sender.get("login") or ""),
        "id": int(sender.get("id") or 0),
        "type": str(sender.get("type") or "User"),
    }


def extract_event(event):
    owner, name = extract_repo(event)
    issue = extract_issue(event)
    comment = extract_comment(event)
    actor = extract_actor(event)
    return {
        "repository": {"owner": owner, "name": name, "full_name": owner + "/" + name},
        "issue": issue,
        "comment": comment,
        "actor": actor,
        "delivery_id": str(event.get("delivery") or event.get("_delivery_id") or ""),
    }


def check_route(labels):
    if not labels:
        return False, "no labels on issue"
    if ROUTE_DESIGN_DEV in labels:
        return True, "route:design-dev present"
    present = ",".join(sorted(l for l in labels if l.startswith("route:")))
    return False, "route mismatch; present route labels: " + repr(present)


def check_approval(comment_body):
    if not comment_body:
        return False, "empty comment body"
    if APPROVAL_MARKER in comment_body:
        return True, "approval marker " + repr(APPROVAL_MARKER) + " found"
    return False, "approval marker not found in comment"


def check_actor(actor_login, allowlist):
    if not actor_login:
        return False, "actor login empty"
    if not allowlist:
        return False, "allowlist empty"
    norm = {a.strip().lower() for a in allowlist if a and a.strip()}
    if actor_login.lower() in norm:
        return True, "actor " + repr(actor_login) + " in allowlist"
    return False, "actor " + repr(actor_login) + " not in allowlist"


def check_issue_state(state):
    if state == "open":
        return True, "issue is open"
    return False, "issue state is " + repr(state) + ", not open"


def _short_sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:SHA_PREFIX_LEN]


def _clip(text, limit):
    if len(text) <= limit:
        return text
    return text[: limit - 17] + "\n[... truncated ...]"


def build_dispatch_input(
    *,
    repository_full_name,
    issue_number,
    issue_title,
    issue_body,
    route,
    approval_actor,
    approval_comment_id,
    run_id,
    dispatch_id,
):
    return {
        "schema": "pasay.opendesign.dispatch/1",
        "dispatch_id": dispatch_id,
        "run_id": run_id,
        "repository": repository_full_name,
        "issue": {
            "number": int(issue_number),
            "title": _clip(issue_title, MAX_TITLE_LEN),
            "body": _clip(issue_body, MAX_BODY_LEN),
        },
        "route": route,
        "approval": {
            "marker": APPROVAL_MARKER,
            "actor": approval_actor,
            "comment_id": int(approval_comment_id),
        },
        "frozen_rules_path": "AI_WORKFLOW_RULES.md",
    }


def compute_dispatch_id(dispatch_input):
    blob = json.dumps(
        {
            "repository": dispatch_input["repository"],
            "issue_number": dispatch_input["issue"]["number"],
            "approval_actor": dispatch_input["approval"]["actor"],
            "approval_comment_id": dispatch_input["approval"]["comment_id"],
            "route": dispatch_input["route"],
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return _short_sha(blob)


def idempotency_decision(dispatch_id, records):
    if not records:
        return "FRESH", "no prior record"
    cutoff = _dt.datetime.now(_dt.timezone.utc).timestamp() - IDEMPOTENCY_WINDOW_SECONDS
    last_dispatched = None
    last_failed = None
    for r in records:
        if r.get("dispatch_id") != dispatch_id:
            continue
        state = r.get("state")
        ts = r.get("ts")
        try:
            ts_f = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except Exception:
            ts_f = 0.0
        if ts_f < cutoff:
            continue
        if state == STATE_DISPATCHED:
            last_dispatched = ts
        elif state == STATE_DISPATCH_FAILED:
            last_failed = ts
    if last_dispatched:
        return "DUPLICATE", "already DISPATCHED at " + str(last_dispatched)
    if last_failed:
        return "RETRY", "previous DISPATCH_FAILED at " + str(last_failed)
    return "FRESH", "only stale records"


def validate_issue_comment(
    *,
    event,
    owner_allowlist,
    idempotency_records,
    run_id,
    expected_repo_full_name=None,
):
    extracted = extract_event(event)
    repo_full = extracted["repository"]["full_name"]
    issue = extracted["issue"]
    comment = extracted["comment"]
    actor = extracted["actor"]["login"]

    trace = []
    verdict = VERDICT_NO_DISPATCH
    state = STATE_NO_DISPATCH
    reason = ""
    dispatch_input = None
    dispatch_id = ""

    action = str(event.get("action") or "")
    trace.append({"gate": "action", "ok": action == "created", "detail": "action=" + repr(action)})
    if action != "created":
        return _result(VERDICT_NO_DISPATCH, STATE_NO_DISPATCH, "not a 'created' issue_comment", trace, None, "")

    if expected_repo_full_name and repo_full.lower() != expected_repo_full_name.lower():
        trace.append({"gate": "repo", "ok": False, "detail": "got " + repr(repo_full)})
        return _result(VERDICT_NO_DISPATCH, STATE_NO_DISPATCH, "repository mismatch (" + repo_full + ")", trace, None, "")

    route_ok, route_detail = check_route(issue["labels"])
    trace.append({"gate": "route", "ok": route_ok, "detail": route_detail})
    if not route_ok:
        return _result(VERDICT_NO_DISPATCH, STATE_NO_DISPATCH, "route gate: " + route_detail, trace, None, "")

    approval_ok, approval_detail = check_approval(comment["body"])
    trace.append({"gate": "approval", "ok": approval_ok, "detail": approval_detail})
    if not approval_ok:
        return _result(VERDICT_NO_DISPATCH, STATE_APPROVED_NOT_DISPATCHED, "approval gate: " + approval_detail, trace, None, "")

    actor_ok, actor_detail = check_actor(actor, owner_allowlist)
    trace.append({"gate": "actor", "ok": actor_ok, "detail": actor_detail})
    if not actor_ok:
        return _result(VERDICT_BLOCKED, STATE_BLOCKED_FOR_PRODUCT_DECISION, "actor gate: " + actor_detail, trace, None, "")

    state_ok, state_detail = check_issue_state(issue["state"])
    trace.append({"gate": "issue_state", "ok": state_ok, "detail": state_detail})
    if not state_ok:
        return _result(VERDICT_BLOCKED, STATE_BLOCKED_FOR_PRODUCT_DECISION, "issue_state gate: " + state_detail, trace, None, "")

    placeholder_dispatch_id = "pending"
    dispatch_input = build_dispatch_input(
        repository_full_name=repo_full,
        issue_number=issue["number"],
        issue_title=issue["title"],
        issue_body=issue["body"],
        route=ROUTE_DESIGN_DEV,
        approval_actor=actor,
        approval_comment_id=comment["id"],
        run_id=run_id,
        dispatch_id=placeholder_dispatch_id,
    )
    dispatch_id = compute_dispatch_id(dispatch_input)
    dispatch_input["dispatch_id"] = dispatch_id

    decision, decision_detail = idempotency_decision(dispatch_id, idempotency_records)
    trace.append({"gate": "idempotency", "ok": decision != "DUPLICATE", "detail": decision_detail})
    if decision == "DUPLICATE":
        return _result(VERDICT_NO_DISPATCH, STATE_NO_DISPATCH, "idempotency: " + decision_detail, trace, None, dispatch_id)

    return _result(
        VERDICT_DISPATCH,
        STATE_APPROVED_NOT_DISPATCHED,
        "all gates passed; awaiting OpenDesign dispatch",
        trace,
        dispatch_input,
        dispatch_id,
    )


def _result(verdict, state, reason, trace, dispatch_input, dispatch_id):
    return {
        "verdict": verdict,
        "state": state,
        "reason": reason,
        "gate_trace": trace,
        "dispatch_input": dispatch_input,
        "dispatch_id": dispatch_id,
    }


def format_status_comment(
    *,
    state,
    issue_number,
    repository_full_name,
    trigger_actor,
    trigger_timestamp,
    run_id,
    dispatch_id,
    workflow_run_url="",
    open_design_target="",
    design_commit_sha="",
    changed_files=(),
    design_gate_result="",
    extra_lines=(),
):
    files = "\n".join("- `" + f + "`" for f in changed_files) or "- (none reported)"
    extras = "\n".join(extra_lines)
    return (
        "<!-- pasay-opendesign-dispatch:status -->\n"
        "```json\n"
        + json.dumps(
            {
                "schema": "pasay.opendesign.status/1",
                "state": state,
                "issue_number": int(issue_number),
                "repository": repository_full_name,
                "trigger_actor": trigger_actor,
                "trigger_timestamp": trigger_timestamp,
                "run_id": run_id,
                "dispatch_id": dispatch_id,
                "workflow_run_url": workflow_run_url,
                "open_design_target": open_design_target,
                "design_commit_sha": design_commit_sha,
                "design_gate_result": design_gate_result,
                "changed_files": list(changed_files),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n```\n"
        + "**OpenDesign Dispatch - " + state + "**\n\n"
        + "- Issue: `" + repository_full_name + "#" + str(issue_number) + "`\n"
        + "- Trigger actor: `" + trigger_actor + "`\n"
        + "- Triggered at: `" + trigger_timestamp + "`\n"
        + "- Workflow run: `" + run_id + "`\n"
        + ("- Workflow URL: " + workflow_run_url + "\n" if workflow_run_url else "")
        + ("- OpenDesign target: `" + open_design_target + "`\n" if open_design_target else "")
        + ("- Design commit SHA: `" + design_commit_sha + "`\n" if design_commit_sha else "")
        + ("- Design Gate result: `" + design_gate_result + "`\n" if design_gate_result else "")
        + "- Changed files:\n" + files + "\n"
        + ("\n" + extras + "\n" if extras else "")
        + "\n<!-- /pasay-opendesign-dispatch:status -->\n"
    )


def parse_status_comment(body):
    if "pasay-opendesign-dispatch:status" not in body:
        return None
    m = re.search(r"```json\n(.*?)\n```", body, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def _selftest():
    failures = []

    ok, _ = check_route(["route:dev"])
    if ok:
        failures.append("route:dev should not pass check_route")
    ok, _ = check_route(["route:design-dev"])
    if not ok:
        failures.append("route:design-dev must pass check_route")
    ok, _ = check_route([])
    if ok:
        failures.append("empty labels must not pass check_route")

    ok, _ = check_approval("hello\n" + APPROVAL_MARKER + "\nworld")
    if not ok:
        failures.append("approval marker must be detected inside multiline")
    ok, _ = check_approval(APPROVAL_MARKER.lower())
    if ok:
        failures.append("lowercase marker must NOT match (case-sensitive)")
    ok, _ = check_approval("")
    if ok:
        failures.append("empty comment must NOT match")

    ok, _ = check_actor("jhackuy", ["jhackuy", "OtherOwner"])
    if not ok:
        failures.append("actor in allowlist must pass")
    ok, _ = check_actor("Jhackuy", ["jhackuy"])
    if not ok:
        failures.append("actor allowlist must be case-insensitive")
    ok, _ = check_actor("evil", ["jhackuy"])
    if ok:
        failures.append("actor not in allowlist must NOT pass")
    ok, _ = check_actor("evil", [])
    if ok:
        failures.append("empty allowlist must deny everyone")

    ok, _ = check_issue_state("open")
    if not ok:
        failures.append("open issue must pass")
    ok, _ = check_issue_state("closed")
    if ok:
        failures.append("closed issue must NOT pass")

    event = {
        "action": "created",
        "delivery": "abc-1",
        "repository": {"name": "pasay-pm", "owner": {"login": "jhackuy"}},
        "issue": {
            "number": 4,
            "state": "open",
            "title": "PASAY-EXPENSE-VNEXT-001",
            "body": "Expense redesign",
            "labels": [{"name": "route:design-dev"}],
        },
        "comment": {
            "id": 999,
            "body": "Approved.\n" + APPROVAL_MARKER,
            "created_at": "2026-08-20T00:00:00Z",
        },
        "sender": {"login": "jhackuy", "id": 1, "type": "User"},
    }
    res = validate_issue_comment(
        event=event,
        owner_allowlist=["jhackuy"],
        idempotency_records=[],
        run_id="run-1",
        expected_repo_full_name="jhackuy/pasay-pm",
    )
    if res["verdict"] != VERDICT_DISPATCH:
        failures.append("happy path verdict: got " + repr(res["verdict"]))
    if not res["dispatch_id"]:
        failures.append("happy path dispatch_id must be set")

    event2 = dict(event)
    event2["issue"] = dict(event["issue"], labels=[{"name": "route:dev"}])
    res2 = validate_issue_comment(
        event=event2,
        owner_allowlist=["jhackuy"],
        idempotency_records=[],
        run_id="run-1",
        expected_repo_full_name="jhackuy/pasay-pm",
    )
    if res2["verdict"] != VERDICT_NO_DISPATCH:
        failures.append("route:dev must not dispatch, got " + repr(res2["verdict"]))

    event3 = dict(event)
    event3["sender"] = {"login": "evil", "id": 2, "type": "User"}
    res3 = validate_issue_comment(
        event=event3,
        owner_allowlist=["jhackuy"],
        idempotency_records=[],
        run_id="run-1",
        expected_repo_full_name="jhackuy/pasay-pm",
    )
    if res3["verdict"] != VERDICT_BLOCKED:
        failures.append("evil actor must block, got " + repr(res3["verdict"]))

    res_replay = validate_issue_comment(
        event=event,
        owner_allowlist=["jhackuy"],
        idempotency_records=[
            {
                "dispatch_id": res["dispatch_id"],
                "state": STATE_DISPATCHED,
                "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            }
        ],
        run_id="run-2",
        expected_repo_full_name="jhackuy/pasay-pm",
    )
    if res_replay["verdict"] != VERDICT_NO_DISPATCH:
        failures.append("replay must NOT dispatch, got " + repr(res_replay["verdict"]))

    bad_body = '"; rm -rf / ; echo "pwned"; `cat /etc/passwd`\n$(whoami)\n' + APPROVAL_MARKER
    event4 = dict(event)
    event4["comment"] = dict(event["comment"], body=bad_body)
    res4 = validate_issue_comment(
        event=event4,
        owner_allowlist=["jhackuy"],
        idempotency_records=[],
        run_id="run-3",
        expected_repo_full_name="jhackuy/pasay-pm",
    )
    if res4["verdict"] != VERDICT_DISPATCH:
        failures.append("malicious comment body must NOT prevent approval (marker present)")
    di = res4["dispatch_input"] or {}
    if "rm -rf" in json.dumps(di.get("approval", {})):
        failures.append("shell-injection chars must NOT appear in approval block")

    event5 = dict(event)
    event5["comment"] = dict(event["comment"], body="LGTM, please proceed.")
    event5["issue"] = dict(
        event["issue"],
        body='"; drop table users; --\n```bash\nrm -rf $HOME\n```\n' + APPROVAL_MARKER,
    )
    res5 = validate_issue_comment(
        event=event5,
        owner_allowlist=["jhackuy"],
        idempotency_records=[],
        run_id="run-4",
        expected_repo_full_name="jhackuy/pasay-pm",
    )
    if res5["verdict"] != VERDICT_NO_DISPATCH:
        failures.append("marker in issue body (not comment) must NOT trigger approval")

    sc = format_status_comment(
        state=STATE_DISPATCHED,
        issue_number=4,
        repository_full_name="jhackuy/pasay-pm",
        trigger_actor="jhackuy",
        trigger_timestamp="2026-08-20T00:00:00Z",
        run_id="run-1",
        dispatch_id=res["dispatch_id"],
        workflow_run_url="https://example/runs/1",
        open_design_target="D:\\AI-DESIGN\\projects\\expense",
        design_commit_sha="deadbeef1234",
        changed_files=["apps/web/src/components/X.tsx"],
        design_gate_result="PASS",
    )
    parsed = parse_status_comment(sc)
    if not parsed or parsed.get("state") != STATE_DISPATCHED:
        failures.append("status comment must round-trip")

    if failures:
        print("FAIL:")
        for f in failures:
            print("  - " + f)
        return 1
    print("OK: contract.selftest passed")
    return 0


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) > 1 and _sys.argv[1] == "selftest":
        _sys.exit(_selftest())
    else:
        print("usage: python -m scripts.opendesign.contract selftest")
        _sys.exit(2)
