"""PASAY-OPENDESIGN-AUTO-DISPATCH-001 runner.

Orchestrates: validate event -> compute dispatch -> call transport ->
record result. Pure stdlib. No LLM. No second database.

The runner is transport-agnostic. The transport layer is provided by:
  - `dispatch_stub.StubTransport` (PR fixture / unit tests ONLY).
  - `dispatch_odcli.OdCliTransport` (production path via Windows
    self-hosted runner; invokes `od` CLI against the local daemon).
  - `dispatch_http.HttpTransport` (reserved for a future documented
    OpenDesign webhook surface; disabled by default).

The runner never builds a shell command from user-controlled text.
The only shell command it ever runs is `gh api` for writeback, and even
that is mediated by `gh_writeback()` which encodes the JSON safely.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
from typing import Any, Dict, Optional, Sequence

from . import contract as C


def now_iso():
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _emit(writeback_fn, record, error_key):
    """Call writeback_fn with a COPY of record so subsequent mutation
    cannot pollute history. Append the error to the original record.
    """
    if writeback_fn is None:
        return
    try:
        writeback_fn(dict(record))
    except Exception as exc:
        record[error_key] = repr(exc)


def run(
    *,
    event,
    owner_allowlist,
    idempotency_records,
    run_id,
    expected_repo_full_name,
    transport,
    writeback_fn,
):
    """Run the dispatcher for one issue_comment event."""
    verdict = C.validate_issue_comment(
        event=event,
        owner_allowlist=owner_allowlist,
        idempotency_records=idempotency_records,
        run_id=run_id,
        expected_repo_full_name=expected_repo_full_name,
    )
    extracted = C.extract_event(event)
    repo_full = extracted["repository"]["full_name"]
    issue_n = extracted["issue"]["number"]
    actor = extracted["actor"]["login"]

    base_record = {
        "issue_number": issue_n,
        "repository": repo_full,
        "trigger_actor": actor,
        "trigger_timestamp": now_iso(),
        "ts": now_iso(),
        "run_id": run_id,
        "dispatch_id": verdict["dispatch_id"],
    }

    if verdict["verdict"] != C.VERDICT_DISPATCH:
        state = verdict["state"]
        record = dict(base_record)
        record["state"] = state
        record["reason"] = verdict["reason"]
        record["verdict"] = verdict["verdict"]
        _emit(writeback_fn, record, "writeback_error")
        return record

    dispatch_input = verdict["dispatch_input"]
    record = dict(base_record)
    record["state"] = C.STATE_APPROVED_NOT_DISPATCHED
    record["reason"] = "all gates passed; awaiting OpenDesign dispatch"
    record["verdict"] = verdict["verdict"]
    record["dispatch_input"] = dispatch_input
    _emit(writeback_fn, record, "writeback_error")

    if transport is None:
        # No transport at all -> refuse. The caller is expected to use
        # BLOCKED_FOR_PRODUCT_DECISION only when Owner needs to act (e.g.
        # self-hosted runner install). For other no-transport cases the
        # caller may override this default by providing a transport.
        record["state"] = C.STATE_BLOCKED_FOR_PRODUCT_DECISION
        record["reason"] = (
            "no transport configured; Owner must set OD_DISPATCH_URL "
            "or install a self-hosted runner with `od` on PATH"
        )
        _emit(writeback_fn, record, "writeback_error_2")
        return record

    try:
        ack = transport.submit(dispatch_input)
    except Exception as exc:
        record["state"] = C.STATE_DISPATCH_FAILED
        record["reason"] = "transport raised: " + repr(exc)
        _emit(writeback_fn, record, "writeback_error_3")
        return record

    if not ack.get("ok"):
        record["state"] = C.STATE_DISPATCH_FAILED
        record["reason"] = "OpenDesign rejected: " + repr(ack.get("error"))
        record["open_design_ack"] = ack
        _emit(writeback_fn, record, "writeback_error_4")
        return record

    record["state"] = C.STATE_DISPATCHED
    record["reason"] = "OpenDesign accepted dispatch"
    record["open_design_ack"] = ack
    record["open_design_target"] = ack.get("target", "")
    record["design_commit_sha"] = ack.get("design_commit_sha", "")
    record["changed_files"] = list(ack.get("changed_files", []))
    record["design_gate_result"] = ack.get("design_gate_result", "")
    _emit(writeback_fn, record, "writeback_error_5")
    return record


def gh_writeback(
    *,
    state,
    issue_number,
    repository_full_name,
    trigger_actor,
    trigger_timestamp,
    run_id,
    dispatch_id,
    workflow_run_url,
    open_design_target="",
    design_commit_sha="",
    changed_files=(),
    design_gate_result="",
    extra_lines=(),
    gh_token=None,
    dry_run=False,
):
    """Post a status comment to GitHub via `gh api`."""
    body = C.format_status_comment(
        state=state,
        issue_number=issue_number,
        repository_full_name=repository_full_name,
        trigger_actor=trigger_actor,
        trigger_timestamp=trigger_timestamp,
        run_id=run_id,
        dispatch_id=dispatch_id,
        workflow_run_url=workflow_run_url,
        open_design_target=open_design_target,
        design_commit_sha=design_commit_sha,
        changed_files=changed_files,
        design_gate_result=design_gate_result,
        extra_lines=extra_lines,
    )
    if dry_run:
        return {"ok": True, "dry_run": True, "body": body}

    env = os.environ.copy()
    if gh_token:
        env["GH_TOKEN"] = gh_token
    cmd = [
        "gh",
        "api",
        "--method",
        "POST",
        "repos/" + repository_full_name + "/issues/" + str(issue_number) + "/comments",
        "-f",
        "body=" + body,
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", env=env, timeout=30
    )
    ok = proc.returncode == 0
    return {
        "ok": ok,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "body": body,
    }
