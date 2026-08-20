"""PASAY-OPENDESIGN-AUTO-DISPATCH-001 cross-run idempotency history loader.

Loads the dispatcher's machine-readable status comments from a GitHub
Issue via `gh api` and converts them into idempotency records. The
dispatcher then combines these with the current run's log to suppress
duplicate dispatches across workflow runs.

Pure stdlib. Uses `subprocess` to invoke `gh api` so we never echo the
issue number / repo into a shell command string beyond the documented
positional args (the gh CLI never sees user-controlled text).
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Dict, List, Optional

from . import contract as C


def _gh_json(cmd, env, timeout):
    """Run a gh command and parse its JSON output.

    Returns None on failure (missing token, 404, rate limit, malformed).
    The dispatcher treats None as "history unavailable" and the contract
    decides based on the records list; in fail-closed mode any unknown
    history blocks dispatch.
    """
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            env=env, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    raw = (proc.stdout or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def fetch_issue_comments(
    *,
    repository_full_name,
    issue_number,
    gh_token=None,
    gh_host="github.com",
    timeout=15,
):
    """Fetch all Issue comments for the given issue.

    Returns the JSON array of comment objects, or None on failure.
    """
    if not repository_full_name or not issue_number:
        return None
    env = os.environ.copy()
    if gh_token:
        env["GH_TOKEN"] = gh_token
    cmd = [
        "gh", "api",
        "-H", "Accept: application/vnd.github+json",
        "--paginate",
        "repos/" + repository_full_name + "/issues/" + str(issue_number) + "/comments",
    ]
    return _gh_json(cmd, env, timeout)


def parse_history(comments):
    """Extract idempotency records from a list of comment objects.

    Each record must contain: dispatch_id, state, trigger_timestamp.
    Records missing any of these are dropped (logged via debug? no -
    pure stdlib, no logger here; the caller decides).
    """
    if not comments:
        return []
    records = []
    for c in comments:
        if not isinstance(c, dict):
            continue
        body = c.get("body")
        if not body:
            continue
        parsed = C.parse_status_comment(body)
        rec = C.status_to_idempotency_record(parsed)
        if rec:
            records.append(rec)
    return records


def load_history(
    *,
    repository_full_name,
    issue_number,
    gh_token=None,
):
    """End-to-end: fetch issue comments and return idempotency records.

    Returns None on network / gh failure so the caller can decide
    whether to fail closed.
    """
    comments = fetch_issue_comments(
        repository_full_name=repository_full_name,
        issue_number=issue_number,
        gh_token=gh_token,
    )
    if comments is None:
        return None
    return parse_history(comments)
