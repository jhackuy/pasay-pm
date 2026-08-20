"""PASAY-OPENDESIGN-AUTO-DISPATCH-001 real HTTP transport.

Calls OpenDesign daemon at OD_DISPATCH_URL using OD_TOOL_TOKEN as bearer
auth. The URL/token are configured by Owner via GitHub Actions secrets
(OD_DISPATCH_URL, OD_TOOL_TOKEN). Until those secrets are set, the
runner refuses to dispatch (BLOCKED_FOR_PRODUCT_DECISION).
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from typing import Any, Dict


class HttpTransport:
    """HTTP transport for a remote OpenDesign daemon."""

    def __init__(self, base_url=None, token=None, timeout=15):
        self.base_url = (base_url or os.environ.get("OD_DISPATCH_URL", "")).rstrip("/")
        self.token = token or os.environ.get("OD_TOOL_TOKEN", "")
        self.timeout = int(os.environ.get("OD_DISPATCH_TIMEOUT", str(timeout)))

    def submit(self, dispatch_input):
        if not self.base_url:
            raise RuntimeError(
                "OD_DISPATCH_URL is not configured; Owner must set this secret "
                "or install a self-hosted runner with `od` on PATH"
            )
        body = json.dumps(dispatch_input, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "pasay-opendesign-dispatch/1",
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(
            self.base_url + "/api/opendesign/dispatch",
            data=body,
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                try:
                    ack = json.loads(raw)
                except Exception:
                    ack = {"ok": False, "error": "non-JSON response", "raw": raw[:1024]}
        except urllib.error.URLError as exc:
            return {
                "ok": False,
                "target": self.base_url,
                "run_id": "",
                "error": "endpoint unreachable: " + repr(exc) + " host=" + socket.gethostname(),
            }
        except Exception as exc:
            return {
                "ok": False,
                "target": self.base_url,
                "run_id": "",
                "error": "transport error: " + repr(exc),
            }
        ack.setdefault("target", self.base_url)
        ack.setdefault("ok", False)
        return ack
