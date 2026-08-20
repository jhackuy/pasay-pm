"""PASAY-OPENDESIGN-AUTO-DISPATCH-001 stub transport.

Used ONLY by:
  1. PR-stage fixture validation (`run_pr_fixture.py`, `workflow_dispatch`).
  2. Unit tests for the runner.

This transport NEVER participates in the production issue_comment path.
The production path must either:
  - call the real `od` CLI via a Windows self-hosted runner, OR
  - refuse to dispatch with BLOCKED_FOR_PRODUCT_DECISION.

Modes:
  accept  - dispatch always succeeds (default for tests).
  reject  - dispatch always fails with a fake "endpoint unreachable".
  fail    - transport.submit() raises (network simulation).

Unknown modes raise ValueError at construction time so they cannot
silently fall back to `accept`.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import socket
from typing import Any, Dict, List

STUB_LOG_DEFAULT = ".ai-control/results/opendesign-dispatch/stub.jsonl"

_VALID_MODES = ("accept", "reject", "fail")


class StubTransport:
    """In-process OpenDesign transport stub (test/fixture only)."""

    def __init__(self, mode=None, log_path=None, target=None):
        resolved = (mode or os.environ.get("OD_STUB_MODE", "accept")).lower()
        if resolved not in _VALID_MODES:
            raise ValueError(
                "StubTransport: unsupported mode " + repr(resolved)
                + " (valid: " + ",".join(_VALID_MODES) + ")"
            )
        self.mode = resolved
        self.log_path = log_path or os.environ.get("OD_STUB_LOG", STUB_LOG_DEFAULT)
        self.target = target or os.environ.get(
            "OD_STUB_TARGET", "D:\\AI-DESIGN\\projects\\pasay-stub"
        )
        self.attempts = []
        try:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        except Exception:
            pass

    def submit(self, dispatch_input):
        ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
        attempt = {
            "ts": ts,
            "mode": self.mode,
            "target": self.target,
            "dispatch_id": dispatch_input.get("dispatch_id", ""),
            "issue_number": (dispatch_input.get("issue") or {}).get("number", 0),
            "repository": dispatch_input.get("repository", ""),
            "route": dispatch_input.get("route", ""),
            "approval_actor": (dispatch_input.get("approval") or {}).get("actor", ""),
        }
        self.attempts.append(attempt)
        self._append_log(attempt)

        if self.mode == "fail":
            raise RuntimeError(
                "StubTransport: simulated network failure (host=" + repr(socket.gethostname()) + ")"
            )
        if self.mode == "reject":
            return {
                "ok": False,
                "target": self.target,
                "run_id": "",
                "error": "stub mode=reject: simulated endpoint unreachable",
            }
        # accept (default)
        return {
            "ok": True,
            "target": self.target,
            "run_id": "stub-run-" + attempt["dispatch_id"],
            "design_commit_sha": "",
            "changed_files": [],
            "design_gate_result": "",
        }

    def _append_log(self, attempt):
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(attempt, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def reset(self):
        self.attempts = []
