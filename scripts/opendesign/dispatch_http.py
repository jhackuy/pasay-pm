"""PASAY-OPENDESIGN-AUTO-DISPATCH-001 real HTTP transport.

Calls OpenDesign daemon at OD_DISPATCH_URL using OD_TOOL_TOKEN as bearer
auth. The URL/token are configured by Owner via GitHub Actions secrets.

NOTE: OpenDesign 0.19.2 does not expose a generic
`/api/opendesign/dispatch` endpoint. The real non-UI handoff uses the
`od` CLI (`od automation source ingest` + `od automation run`) against
the local daemon; this HTTP transport is reserved for a future official
webhook surface and is NOT enabled by default. To enable it, Owner must
explicitly set OD_DISPATCH_URL with a documented endpoint that they have
verified via the daemon source / a release note.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict


_ALLOWED_SCHEMES = ("http", "https")


def _validate_url(base_url):
    """Validate the base URL: scheme + host must be present.

    Returns the URL on success. Raises ValueError on rejection.
    """
    if not base_url:
        raise ValueError("OD_DISPATCH_URL is empty")
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(
            "OD_DISPATCH_URL scheme not allowed: " + repr(parsed.scheme)
            + " (allowed: " + ",".join(_ALLOWED_SCHEMES) + ")"
        )
    if not parsed.netloc:
        raise ValueError("OD_DISPATCH_URL missing host: " + base_url)
    return base_url


class HttpTransport:
    """HTTP transport for a remote OpenDesign daemon.

    Currently disabled by default; the dispatcher refuses to construct
    one unless Owner has set OD_DISPATCH_URL AND verified it points at a
    real documented OpenDesign endpoint (see notes above).
    """

    def __init__(self, base_url=None, token=None, timeout=15):
        self.base_url = (base_url or os.environ.get("OD_DISPATCH_URL", "")).rstrip("/")
        self.token = token or os.environ.get("OD_TOOL_TOKEN", "")
        self.timeout = int(os.environ.get("OD_DISPATCH_TIMEOUT", str(timeout)))
        # Eagerly validate so the runner can refuse fast.
        _validate_url(self.base_url)

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
        url = self.base_url + "/api/opendesign/dispatch"
        req = urllib.request.Request(
            url, data=body, method="POST", headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                try:
                    ack = json.loads(raw)
                except Exception:
                    ack = {"ok": False, "error": "non-JSON response", "raw": raw[:1024]}
        except urllib.error.HTTPError as exc:
            # HTTP-level error (4xx/5xx). Treat as DISPATCH_FAILED rather
            # than transport failure, since the endpoint actually replied.
            return {
                "ok": False,
                "target": self.base_url,
                "run_id": "",
                "error": "endpoint returned HTTP " + str(exc.code) + ": "
                          + (exc.reason or "unknown"),
            }
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
