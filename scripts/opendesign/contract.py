"""PASAY-OPENDESIGN-AUTO-DISPATCH-001 trigger contract.

Program First, LLM Last. Pure stdlib. Every check below is deterministic:
static scan over the issue_comment event payload, plus an in-memory
idempotency record. No LLM is called, no network is required.

This module is the single source of truth for what counts as a valid
OpenDesign dispatch event. The GitHub Actions workflow and the PR-stage
fixture runner both import from here.

Constants
---------
ROUTE_DESIGN_DEV    : label that must be present on the issue.
APPROVAL_MARKER     : exact marker that must appear in a comment body
                      to authorise dispatch.
STATES              : canonical status strings written back to GitHub.

Functions
---------
extract_event       : pull (repo, issue, actor, labels, comment) from an
                      issue_comment event payload.
check_route         : True iff `route:design-dev` is present.
check_approval      : True iff the comment body contains the exact marker.
check_actor         : True iff the actor is in the allowlist (case-insensitive,
                      exact-match on GitHub login).
check_issue_state   : True iff the issue is open.
validate_issue_comment
                    : combines all checks + idempotency. Returns a
                      Verdict object (verdict, reason, state, dispatch_input).
build_dispatch_input
                    : builds the JSON-safe, shell-safe payload handed to
                      OpenDesign. Strips / normalises fields, never echoes
                      the comment body into shell.
format_status_comment
                    : renders the markdown status comment that gets
                      written back to the issue.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ROUTE_DESIGN_DEV = "route:design-dev"
APPROVAL_MARKER = "OWNER_APPROVED_FOR_OPENDESIGN"
DESIGN_DEV_ROUTES = (ROUTE_DESIGN_DEV,)

# Canonical status values. These are the only strings the dispatcher is
# allowed to write back to the issue as a status comment / label.
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

# Verdict strings returned by validate_issue_comment.
VERDICT_DISPATCH = "DISPATCH"
VERDICT_NO_DISPATCH = "NO_DISPATCH"
VERDICT_BLOCKED = "BLOCKED"

# Issue body / comment field limits. Beyond these, we refuse to echo into
# dispatch payload (DoS / log-flooding guard).
MAX_TITLE_LEN = 256
MAX_BODY_LEN = 64_000
MAX_COMMENT_LEN = 16_000

# A short SHA-256 prefix is enough for human + machine correlation.
SHA_PREFIX_LEN = 12

# Idempotency: the same approval event must not produce more than one
# DISPATCH. We record the dispatch_id (sha256 of the dispatch input) and
# reject duplicate dispatches within the same run window.
IDEMPOTENCY_WINDOW_SECONDS = 24 * 60 * 60


# ---------------------------------------------------------------------------
# Event extraction
# ---------------------------------------------------------------------------


def extract_repo(event: Dict[str, Any]) -> Tuple[str, str]:
    repo = event.get("repository") or {}
    name = repo.get("name") or ""
    owner = (repo.get("owner") or {}).get("login") or ""
    return owner, name


def extract_issue(event: Dict[str, Any]) -> Dict[str, Any]:
    issue = event.get("issue") or {}
    return {
        "number": int(issue.get("number") or 0),
        "state": str(issue.get("state") or ""),
        "title": str(issue.get("title") or ""),
        "body": str(issue.get("body") or ""),
        "labels": [str(l.get("name") or "") for l in (issue.get("labels") or [])],
    }


def extract_comment(event: Dict[str, Any]) -> Dict[str, Any]:
    comment = event.get("comment") or {}
    return {
        "id": int(comment.get("id") or 0),
        "body": str(comment.get("body") or ""),
        "created_at": str(comment.get("created_at") or ""),
    }


def extract_actor(event: Dict[str, Any]) -> Dict[str, Any]:
    sender = event.get("sender") or {}
    return {
        "login": str(sender.get("login") or ""),
        "id": int(sender.get("id") or 0),
        "type": str(sender.get("type") or "User"),
    }


def extract_event(event: Dict[str, Any]) -> Dict[str, Any]:
    owner, name = extract_repo(event)
    issue = extract_issue(event)
    comment = extract_comment(event)
    actor = extract_actor(event)
    return {
        "repository": {"owner": owner, "name": name, "full_name": f"{owner}/{name}"},
        "issue": issue,
        "comment": comment,
        "actor": actor,
        "delivery_id": str(event.get("delivery") or event.get("_delivery_id") or ""),
    }


# ---------------------------------------------------------------------------
# Individual gates
# ---------------------------------------------------------------------------


def check_route(labels: Sequence[str]) -> Tuple[bool, str]:
    if not labels:
        return False, "no labels on issue"
    if ROUTE_DESIGN_DEV in labels:
        return True, "route:design-dev present"
    present = ",".join(sorted(l for l in labels if l.startswith("route:")))
    return False, f"route mismatch; present route labels: {present!r}"


def check_approval(comment_body: str) -> Tuple[bool, str]:
    if not comment_body:
        return False, "empty comment body"
    if APPROVAL_MARKER in comment_body:
        return True, f"approval marker {APPROVAL_MARKER!r} found"
    return False, "approval marker not found in comment"


def check_actor(actor_login: str, allowlist: Sequence[str]) -> Tuple[bool, str]:
    if not actor_login:
        return False, "actor login empty"
    if not allowlist:
        return False, "allowlist empty"
    norm = {a.strip().lower() for a in allowlist if a and a.strip()}
    if actor_login.lower() in norm:
        return True, f"actor {actor_login!r} in allowlist"
    return False, f"actor {actor_login!r} not in allowlist"


def check_issue_state(state: str) -> Tuple[bool, str]:
    if state == "open":
        return True, "issue is open"
    return False, f"issue state is {state!r}, not open"


# ---------------------------------------------------------------------------
# Dispatch input (sanitised, shell-safe)
# ---------------------------------------------------------------------------


def _short_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:SHA_PREFIX_LEN]


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 17] + "\n[... truncated ...]"


def build_dispatch_input(
    *,
    repository_full_name: str,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    route: str,
    approval_actor: str,
    approval_comment_id: int,
    run_id: str,
    dispatch_id: str,
) -> Dict[str, Any]:
    """Build the JSON-safe dispatch input.

    Every field is a plain string / int. No comment body is forwarded to
    shell or to OpenDesign prompt. The body is stored as a separate
    `issue_body` field, never concatenated into a command line. This is the
    single guarantee against comment-injection into the dispatcher.
    """
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


# ---------------------------------------------------------------------------
# Idempotency record
# ---------------------------------------------------------------------------


def compute_dispatch_id(dispatch_input: Dict[str, Any]) -> str:
    """Stable id for an approval event. Two events with the same input
    produce the same id, which the runner uses to suppress duplicates.
    """
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


def idempotency_decision(
    dispatch_id: str, records: Sequence[Dict[str, Any]]
) -> Tuple[str, str]:
    """Decide what to do given the in-memory idempotency record list.

    Returns (decision, reason) where decision is one of:
        - "FRESH"        : dispatch_id has never been seen; safe to dispatch.
        - "DUPLICATE"    : dispatch_id was already DISPATCHED in window; reject.
        - "RETRY"        : dispatch_id was DISPATCH_FAILED but within window
                           and no later DISPATCHED record exists; allow retry.
    """
    if not records:
        return "FRESH", "no prior record"
    cutoff = _dt.datetime.now(_dt.timezone.utc).timestamp() - IDEMPOTENCY_WINDOW_SECONDS
    last_dispatched: Optional[str] = None
    last_failed: Optional[str] = None
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
        return "DUPLICATE", f"already DISPATCHED at {last_dispatched}"
    if last_failed:
        return "RETRY", f"previous DISPATCH_FAILED at {last_failed}"
    return "FRESH", "only stale records"


# ---------------------------------------------------------------------------
# Top-level verdict
# ---------------------------------------------------------------------------


def validate_issue_comment(
    *,
    event: Dict[str, Any],
    owner_allowlist: Sequence[str],
    idempotency_records: Sequence[Dict[str, Any]],
    run_id: str,
    expected_repo_full_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate a single issue_comment event.

    Returns a dict with keys:
        verdict           : VERDICT_DISPATCH | VERDICT_NO_DISPATCH | VERDICT_BLOCKED
        state             : one of ALL_STATES
        reason            : human-readable string
        dispatch_input    : present iff verdict == VERDICT_DISPATCH
        dispatch_id       : stable id for the approval event
        gate_trace        : list of {gate, ok, detail}
    """
    extracted = extract_event(event)
    repo_full = extracted["repository"]["full_name"]
    issue = extracted["issue"]
    comment = extracted["comment"]
    actor = extracted["actor"]["login"]

    trace: List[Dict[str, Any]] = []
    verdict = VERDICT_NO_DISPATCH
    state = STATE_NO_DISPATCH
    reason = ""
    dispatch_input: Optional[Dict[str, Any]] = None
    dispatch_id = ""

    # Gate 0: event is an issue_comment with non-empty action.
    action = str(event.get("action") or "")
    trace.append({"gate": "action", "ok": action == "created", "detail": f"action={action!r}"})
    if action != "created":
        return _result(VERDICT_NO_DISPATCH, STATE_NO_DISPATCH, "not a 'created' issue_comment", trace, None, "")

    # Gate 1: repository matches.
    if expected_repo_full_name and repo_full.lower() != expected_repo_full_name.lower():
        trace.append({"gate": "repo", "ok": False, "detail": f"got {repo_full!r}"})
        return _result(VERDICT_NO_DISPATCH, STATE_NO_DISPATCH, f"repository mismatch ({repo_full})", trace, None, "")

    # Gate 2: route label.
    route_ok, route_detail = check_route(issue["labels"])
    trace.append({"gate": "route", "ok": route_ok, "detail": route_detail})
    if not route_ok:
        return _result(VERDICT_NO_DISPATCH, STATE_NO_DISPATCH, f"route gate: {route_detail}", trace, None, "")

    # Gate 3: approval marker.
    approval_ok, approval_detail = check_approval(comment["body"])
    trace.append({"gate": "approval", "ok": approval_ok, "detail": approval_detail})
    if not approval_ok:
        return _result(VERDICT_NO_DISPATCH, STATE_APPROVED_NOT_DISPATCHED, f"approval gate: {approval_detail}", trace, None, "")

    # Gate 4: actor allowlist.
    actor_ok, actor_detail = check_actor(actor, owner_allowlist)
    trace.append({"gate": "actor", "ok": actor_ok, "detail": actor_detail})
    if not actor_ok:
        return _result(VERDICT_BLOCKED, STATE_BLOCKED_FOR_PRODUCT_DECISION, f"actor gate: {actor_detail}", trace, None, "")

    # Gate 5: issue state.
    state_ok, state_detail = check_issue_state(issue["state"])
    trace.append({"gate": "issue_state", "ok": state_ok, "detail": state_detail})
    if not state_ok:
        return _result(VERDICT_BLOCKED, STATE_BLOCKED_FOR_PRODUCT_DECISION, f"issue_state gate: {state_detail}", trace, None, "")

    # Build dispatch input.
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

    # Gate 6: idempotency.
    decision, decision_detail = idempotency_decision(dispatch_id, idempotency_records)
    trace.append({"gate": "idempotency", "ok": decision != "DUPLICATE", "detail": decision_detail})
    if decision == "DUPLICATE":
        return _result(VERDICT_NO_DISPATCH, STATE_NO_DISPATCH, f"idempotency: {decision_detail}", trace, None, dispatch_id)

    return _result(
        VERDICT_DISPATCH,
        STATE_APPROVED_NOT_DISPATCHED,
        "all gates passed; awaiting OpenDesign dispatch",
        trace,
        dispatch_input,
        dispatch_id,
    )


def _result(
    verdict: str,
    state: str,
    reason: str,
    trace: List[Dict[str, Any]],
    dispatch_input: Optional[Dict[str, Any]],
    dispatch_id: str,
) -> Dict[str, Any]:
    return {
        "verdict": verdict,
        "state": state,
        "reason": reason,
        "gate_trace": trace,
        "dispatch_input": dispatch_input,
        "dispatch_id": dispatch_id,
    }


# ---------------------------------------------------------------------------
# Status comment rendering
# ---------------------------------------------------------------------------


def format_status_comment(
    *,
    state: str,
    issue_number: int,
    repository_full_name: str,
    trigger_actor: str,
    trigger_timestamp: str,
    run_id: str,
    dispatch_id: str,
    workflow_run_url: str = "",
    open_design_target: str = "",
    design_commit_sha: str = "",
    changed_files: Sequence[str] = (),
    design_gate_result: str = "",
    extra_lines: Sequence[str] = (),
) -> str:
    """Render the markdown status comment that the dispatcher writes back.

    The output is a fenced code block (machine-readable) followed by a
    short human-readable summary. The fence identifier is fixed so the
    writeback helper can detect duplicates.
    """
    files = "\n".join(f"- `{f}`" for f in changed_files) or "- (none reported)"
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
        + f"**OpenDesign Dispatch — {state}**\n\n"
        + f"- Issue: `{repository_full_name}#{issue_number}`\n"
        + f"- Trigger actor: `{trigger_actor}`\n"
        + f"- Triggered at: `{trigger_timestamp}`\n"
        + f"- Workflow run: `{run_id}`\n"
        + (f"- Workflow URL: {workflow_run_url}\n" if workflow_run_url else "")
        + (f"- OpenDesign target: `{open_design_target}`\n" if open_design_target else "")
        + (f"- Design commit SHA: `{design_commit_sha}`\n" if design_commit_sha else "")
        + (f"- Design Gate result: `{design_gate_result}`\n" if design_gate_result else "")
        + f"- Changed files:\n{files}\n"
        + (f"\n{extras}\n" if extras else "")
        + "\n<!-- /pasay-opendesign-dispatch:status -->\n"
    )


def parse_status_comment(body: str) -> Optional[Dict[str, Any]]:
    """Parse a previously-written status comment back into a dict.

    Used by the runner to detect duplicate writes (idempotency record).
    """
    if "pasay-opendesign-dispatch:status" not in body:
        return None
    m = re.search(r"```json\n(.*?)\n```", body, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Self-test (run via `python -m scripts.opendesign.contract selftest`)
# ---------------------------------------------------------------------------


def _selftest() -> int:
    """Tiny deterministic self-test. Run with `python -m scripts.opendesign.contract selftest`."""
    failures = []

    # Gate: route.
    ok, _ = check_route(["route:dev"])
    if ok:
        failures.append("route:dev should not pass check_route")
    ok, _ = check_route(["route:design-dev"])
    if not ok:
        failures.append("route:design-dev must pass check_route")
    ok, _ = check_route([])
    if ok:
        failures.append("empty labels must not pass check_route")

    # Gate: approval marker (case-sensitive, exact substring).
    ok, _ = check_approval(f"hello\n{APPROVAL_MARKER}\nworld")
    if not ok:
        failures.append("approval marker must be detected inside multiline")
    ok, _ = check_approval(f"{APPROVAL_MARKER.lower()}")  # wrong case
    if ok:
        failures.append("lowercase marker must NOT match (case-sensitive)")
    ok, _ = check_approval("")
    if ok:
        failures.append("empty comment must NOT match")

    # Gate: actor.
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

    # Gate: issue state.
    ok, _ = check_issue_state("open")
    if not ok:
        failures.append("open issue must pass")
    ok, _ = check_issue_state("closed")
    if ok:
        failures.append("closed issue must NOT pass")

    # Full validate: design-dev + approval + actor + open.
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
            "body": f"Approved.\n{APPROVAL_MARKER}",
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
        failures.append(f"happy path verdict: got {res['verdict']!r}")
    if not res["dispatch_id"]:
        failures.append("happy path dispatch_id must be set")

    # Non-design-dev must NOT dispatch even with approval + actor.
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
        failures.append(f"route:dev must not dispatch, got {res2['verdict']!r}")

    # Non-allowlist actor must BLOCK.
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
        failures.append(f"evil actor must block, got {res3['verdict']!r}")

    # Idempotency: replaying the same event must NOT re-dispatch.
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
        failures.append(f"replay must NOT dispatch, got {res_replay['verdict']!r}")

    # Comment body with shell-injection chars must not appear in dispatch_input.
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
    # Body lives in issue_body, NOT in approval.* nor concatenated.
    if "rm -rf" in json.dumps(di.get("approval", {})):
        failures.append("shell-injection chars must NOT appear in approval block")

    # Approval marker ONLY in issue body, NOT in comment. Must NOT trigger approval.
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

    # Status comment format: parse round-trip.
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
            print(f"  - {f}")
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
