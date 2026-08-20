#!/usr/bin/env python3
"""TRAE Auto Wake Bridge — local companion for PASAY-TASK-010.

Two modes (NEVER a long-running daemon in polling mode):

  A. HTTP LISTENER  (one-shot or server-mode) — receives webhook forwarded
     from GitHub Actions. Writes `event.json` then exits if server-mode=False.
     Owner exposes this port via any local tunnel (optional) OR configures
     TRAE_LOCAL_BRIDGE_URL as a loopback address.

  B. PULL MODE      (single-shot, NO LOOP) — queries GitHub REST API once for
     OPEN issues with {route:dev, ready-for-dev, no blocked}. Takes the
     newest ONE matching issue, records claim idempotency marker, prints
     the canonical `/ND <N>` command line, then EXITS.
     Designed to be invoked by Windows Scheduled Task "AtLogon + every 15 min
     if idle" OR Owner manually — the key invariant is ONE SHOT PER INVOCATION.

Both modes emit the same handshake file so launchers are decoupled.

Hard invariants (see Issue #29 Authoritative Design Decisions §1-6):
  * No GitHub API polling daemon or while-True sleep loop.
  * One process per invocation; PID lock prevents concurrent bridge instances.
  * Exactly one issue selected per invocation; never auto-chains to the next.
  * Never copies Issue body locally; always passes issue_number to /ND.
  * Never modifies issue labels; /ND performs its own Claim.
  * Never launches TRAE UI automation (no mouse, no AHK).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

REPO_OWNER = "jhackuy"
REPO_NAME = "pasay-pm"
LABEL_ROUTE_DEV = "route:dev"
LABEL_READY_FOR_DEV = "ready-for-dev"
LABEL_BLOCKED = "blocked"

CONTROL_DIR = Path(os.environ.get("TRAE_AUTO_WAKE_CONTROL", ".ai-control/trae-auto-wake"))
PID_FILE = CONTROL_DIR / "bridge.pid"
LAST_EVENT = CONTROL_DIR / "last_event.json"
CLAIMED_DIR = CONTROL_DIR / "claimed"
LOG_DIR = CONTROL_DIR / "logs"

GITHUB_API = "https://api.github.com"
GITHUB_TOKEN_ENV = "GITHUB_TOKEN"


# ---------------------------------------------------------------------------
# Singleton / filesystem helpers
# ---------------------------------------------------------------------------

def ensure_dirs() -> None:
    CLAIMED_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def acquire_singleton() -> bool:
    """Return True iff this process now owns the PID lock.

    Same pattern as bin/pasay_runtime.py and opendesign-sync watcher.
    Never blocks; returns False if another process is alive.
    """
    ensure_dirs()
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            os.kill(old_pid, 0)
            return False
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            PID_FILE.unlink(missing_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    return True


def release_singleton() -> None:
    try:
        if PID_FILE.exists() and int(PID_FILE.read_text().strip()) == os.getpid():
            PID_FILE.unlink(missing_ok=True)
    except (ValueError, OSError):
        pass


def already_claimed(issue_number: int) -> bool:
    marker = CLAIMED_DIR / f"issue-{issue_number}.claimed"
    return marker.exists()


def mark_claimed(issue_number: int, payload: dict) -> None:
    marker = CLAIMED_DIR / f"issue-{issue_number}.claimed"
    marker.write_text(json.dumps(payload, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# Output handshake
# ---------------------------------------------------------------------------

@dataclass
class WakeResult:
    mode: str
    status: str
    repository: Optional[str]
    issue_number: Optional[int]
    issue_title: Optional[str]
    issue_url: Optional[str]
    nd_command: Optional[str]
    detail: str

    def write_last_event(self) -> None:
        LAST_EVENT.write_text(json.dumps(self.__dict__, ensure_ascii=False, indent=2))

    def print_handoff(self) -> None:
        print("=== TRAE_AUTO_WAKE_RESULT ===")
        for k, v in self.__dict__.items():
            print(f"{k}: {v}")
        if self.status == "ISSUED" and self.nd_command:
            print(f"CANONICAL_ND_CMD: {self.nd_command}")
        print("============================")


def mk_result(mode: str, status: str, detail: str, issue: Optional[dict] = None) -> WakeResult:
    if issue is None:
        return WakeResult(mode, status, None, None, None, None, None, detail)
    repo = issue.get("repository_full") or f"{REPO_OWNER}/{REPO_NAME}"
    num = issue["number"]
    return WakeResult(
        mode=mode,
        status=status,
        repository=repo,
        issue_number=num,
        issue_title=issue.get("title"),
        issue_url=issue.get("html_url"),
        nd_command=f"/ND {num}",
        detail=detail,
    )


# ---------------------------------------------------------------------------
# GitHub helpers (stdlib only — no PyGithub / httpx / requests dep)
# ---------------------------------------------------------------------------

def _gh_request(path: str, token: str, params: Optional[dict] = None) -> dict | list:
    url = f"{GITHUB_API}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "pasay-trae-auto-wake/0.1",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return json.loads(body)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GitHub HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:400]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"GitHub network error: {e.reason}") from e


def fetch_ready_issues(token: str) -> list[dict]:
    """Return OPEN issues with labels route:dev + ready-for-dev, no blocked.

    Sorted by updated desc so we pick the most recently authorised one
    when the launcher is configured as one-shot per invocation.
    """
    q = (
        f"repo:{REPO_OWNER}/{REPO_NAME} is:issue is:open "
        f"label:{LABEL_ROUTE_DEV} label:{LABEL_READY_FOR_DEV} -label:{LABEL_BLOCKED}"
    )
    data = _gh_request("/search/issues", token, params={"q": q, "sort": "updated", "order": "desc", "per_page": 20})
    assert isinstance(data, dict), data
    items = data.get("items", [])
    return items


def fetch_single_issue(token: str, number: int) -> Optional[dict]:
    try:
        data = _gh_request(f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{number}", token)
    except RuntimeError:
        return None
    assert isinstance(data, dict)
    return data


# ---------------------------------------------------------------------------
# Mode B — pull once, print /ND, exit
# ---------------------------------------------------------------------------

def run_pull_mode(target_issue: Optional[int] = None) -> WakeResult:
    token = os.environ.get(GITHUB_TOKEN_ENV)
    if not token:
        return mk_result("pull", "BLOCKED_GITHUB_TOKEN_MISSING",
                         f"Set ${GITHUB_TOKEN_ENV} for pull-mode fetch.")

    try:
        if target_issue is not None:
            issue = fetch_single_issue(token, target_issue)
            if not issue:
                return mk_result("pull", "BLOCKED_ISSUE_NOT_FOUND",
                                 f"Issue #{target_issue} not found via GitHub API.")
            labels = {l["name"] for l in issue.get("labels", [])}
            if issue.get("state") != "open" or LABEL_ROUTE_DEV not in labels \
                    or LABEL_READY_FOR_DEV not in labels or LABEL_BLOCKED in labels:
                return mk_result("pull", "BLOCKED_LABEL_GATE_FAIL",
                                 f"Issue #{target_issue} failed gate (open+route:dev+ready-for-dev, no blocked).",
                                 issue=issue)
            candidate = issue
        else:
            issues = fetch_ready_issues(token)
            if not issues:
                return mk_result("pull", "NO_PENDING_DEV_TASK",
                                 "No OPEN issue with {route:dev, ready-for-dev, -blocked} found.")
            candidate = issues[0]
    except RuntimeError as e:
        return mk_result("pull", "BLOCKED_GITHUB_API_ERROR", f"{e}")

    num = candidate["number"]
    if already_claimed(num):
        return mk_result("pull", "ALREADY_CLAIMED_IDEMPOTENT",
                         f"Issue #{num} marker already exists; refusing to re-emit (idempotent).",
                         issue=candidate)

    mark_claimed(num, {"mode": "pull", "ts": time.time(), "source": candidate.get("html_url")})
    issue_norm = dict(candidate)
    issue_norm["repository_full"] = f"{REPO_OWNER}/{REPO_NAME}"
    return mk_result("pull", "ISSUED", f"Hand-off one-shot /ND {num}.", issue=issue_norm)


# ---------------------------------------------------------------------------
# Mode A — HTTP listener (single event then stop, OR server mode)
# ---------------------------------------------------------------------------

def validate_push_payload(data: dict) -> Optional[str]:
    for key in ("repository", "issue_number", "issue_url"):
        if not isinstance(data.get(key), str) and key != "issue_number":
            pass
    if not isinstance(data.get("issue_number"), int) or data["issue_number"] < 1:
        return "missing/invalid issue_number"
    if not isinstance(data.get("repository"), str) \
            or not data["repository"].lower().endswith(f"{REPO_OWNER.lower()}/{REPO_NAME.lower()}"):
        return f"repository mismatch; expected *{REPO_OWNER}/{REPO_NAME}"
    return None


def run_http_mode(host: str, port: int, token: Optional[str], server_mode: bool) -> WakeResult:
    accepted: list[WakeResult] = []
    import http.server

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 (stdlib API)
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                self.send_response(400); self.end_headers()
                self.wfile.write(b"invalid json")
                return
            auth = self.headers.get("Authorization", "")
            expect = f"Bearer {token}" if token else None
            if expect and auth != expect:
                self.send_response(401); self.end_headers()
                self.wfile.write(b"bad token")
                return
            err = validate_push_payload(body)
            if err:
                self.send_response(400); self.end_headers()
                self.wfile.write(err.encode("utf-8", errors="replace"))
                return
            num = int(body["issue_number"])
            if already_claimed(num):
                self.send_response(208); self.end_headers()
                self.wfile.write(b"ALREADY_CLAIMED_IDEMPOTENT")
                accepted.append(mk_result(
                    "http", "ALREADY_CLAIMED_IDEMPOTENT",
                    f"Issue #{num} already claimed; idempotent no-op.",
                    issue={"number": num, "title": body.get("issue_title"),
                           "html_url": body.get("issue_url"),
                           "repository_full": body.get("repository")}))
                return
            mark_claimed(num, {"mode": "http", "ts": time.time(), "payload": body})
            issue = {"number": num, "title": body.get("issue_title"),
                     "html_url": body.get("issue_url"),
                     "repository_full": body.get("repository")}
            res = mk_result("http", "ISSUED", f"Received webhook event for #{num}.", issue=issue)
            res.write_last_event()
            accepted.append(res)
            self.send_response(202)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "nd": res.nd_command}).encode("utf-8"))

        def log_message(self, fmt, *args):  # silence noisy default access log
            return

    httpd = http.server.HTTPServer((host, port), Handler)
    httpd.timeout = 0.5
    start_ts = time.time()
    try:
        while True:
            httpd.handle_request()
            if accepted:
                if not server_mode:
                    return accepted[0]
            if not server_mode and (time.time() - start_ts) > 30:
                return mk_result("http", "NO_EVENT_TIMEOUT",
                                 "HTTP listener timed out after 30s without accepted event.")
    except KeyboardInterrupt:
        return mk_result("http", "BLOCKED_INTERRUPTED", "KeyboardInterrupt.")
    finally:
        try:
            httpd.server_close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="trae_auto_wake_bridge",
        description="Local one-shot companion that hands `issue_number` to /ND.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp_pull = sub.add_parser("pull", help="Fetch GitHub once, pick 1 issue, print /ND, exit.")
    sp_pull.add_argument("--issue", type=int, default=None,
                         help="Target a specific issue number instead of auto-pick.")

    sp_http = sub.add_parser("http", help="HTTP listener (default single-event then stop).")
    sp_http.add_argument("--host", default="127.0.0.1")
    sp_http.add_argument("--port", type=int, default=8765)
    sp_http.add_argument("--token", default=None,
                         help="Bearer token for Authorization header; defaults to $TRAE_AUTO_WAKE_HTTP_TOKEN.")
    sp_http.add_argument("--server-mode", action="store_true",
                         help="Keep running across events (ONLY use behind a loopback-only firewall).")

    sub.add_parser("check", help="Print singleton state and most recent event, no network.")
    sub.add_parser("reset-claim", help="Delete claimed marker for --issue to allow retry.") \
       .add_argument("--issue", type=int, required=True)

    return p


def cmd_check(_args) -> WakeResult:
    ensure_dirs()
    status_lines = []
    status_lines.append(f"control_dir: {CONTROL_DIR.resolve()}")
    if PID_FILE.exists():
        status_lines.append(f"pid_file: {PID_FILE} -> {PID_FILE.read_text().strip()}")
    else:
        status_lines.append("pid_file: NONE")
    if LAST_EVENT.exists():
        status_lines.append(f"last_event: {LAST_EVENT}")
        status_lines.append(LAST_EVENT.read_text())
    else:
        status_lines.append("last_event: NONE")
    claimed = sorted(p.name for p in CLAIMED_DIR.glob("issue-*.claimed"))
    status_lines.append(f"claimed_count: {len(claimed)}")
    for name in claimed[-10:]:
        status_lines.append(f"  - {name}")
    return WakeResult("check", "OK", "\n".join(status_lines),
                      None, None, None, None, "\n".join(status_lines))


def cmd_reset_claim(args) -> WakeResult:
    ensure_dirs()
    marker = CLAIMED_DIR / f"issue-{args.issue}.claimed"
    if marker.exists():
        marker.unlink()
        return mk_result("reset-claim", "OK", f"Deleted marker {marker}")
    return mk_result("reset-claim", "NO_MARKER", f"No marker for issue #{args.issue}")


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.cmd == "check":
        res = cmd_check(args)
        res.write_last_event()
        res.print_handoff()
        return 0
    if args.cmd == "reset-claim":
        res = cmd_reset_claim(args)
        res.write_last_event()
        res.print_handoff()
        return 0

    if not acquire_singleton():
        res = mk_result(args.cmd, "BLOCKED_SINGLETON_CONFLICT",
                        "Another bridge instance holds PID file; rejecting concurrent run.")
        res.print_handoff()
        return 2
    try:
        if args.cmd == "pull":
            res = run_pull_mode(target_issue=args.issue)
        elif args.cmd == "http":
            token = args.token or os.environ.get("TRAE_AUTO_WAKE_HTTP_TOKEN")
            res = run_http_mode(args.host, args.port, token, args.server_mode)
        else:  # pragma: no cover
            res = mk_result(args.cmd, "BLOCKED_UNKNOWN_CMD", "unknown subcommand")
        res.write_last_event()
        res.print_handoff()
        return 0 if res.status in ("ISSUED", "OK", "NO_EVENT_TIMEOUT",
                                    "NO_PENDING_DEV_TASK", "ALREADY_CLAIMED_IDEMPOTENT") else 1
    finally:
        release_singleton()


if __name__ == "__main__":
    sys.exit(main())
