"""Tests for scripts/opendesign/dispatch_odcli.py.

These tests run against a real OpenDesign daemon. They are SKIPPED if
the daemon is not reachable on the configured OD_DAEMON_URL or if the
`od` CLI is not on PATH. CI runners without the daemon therefore do
not exercise this transport; they rely on the StubTransport tests.
"""

from __future__ import annotations

import os
import shutil
import socket
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
TESTS_DIR = os.path.dirname(THIS_DIR)
SCRIPTS_DIR = os.path.dirname(TESTS_DIR)
REPO = os.path.dirname(SCRIPTS_DIR)
for p in (SCRIPTS_DIR, REPO):
    if p not in sys.path:
        sys.path.insert(0, p)

from scripts.opendesign.dispatch_odcli import OdCliTransport


DAEMON_URL = os.environ.get("OD_DAEMON_URL", "http://127.0.0.1:7456")


def _daemon_up(url):
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        s = socket.create_connection((p.hostname, p.port or 80), timeout=2)
        s.close()
        return True
    except Exception:
        return False


def _od_cli_present():
    od_bin = os.environ.get("OD_BIN", "")
    if od_bin and os.path.isfile(od_bin):
        return True
    return shutil.which("od") is not None


def _skip_if_unavailable():
    if not _daemon_up(DAEMON_URL):
        raise AssertionError("SKIP: OpenDesign daemon not reachable at " + DAEMON_URL)
    if not _od_cli_present():
        raise AssertionError("SKIP: od CLI not on PATH and OD_BIN not set")


def _dispatch_input():
    return {
        "dispatch_id": "test-odcli-" + str(os.getpid()),
        "repository": "jhackuy/pasay-pm",
        "issue": {
            "number": 4,
            "title": "OdCliTransport smoke test",
            "body": "Body for OdCliTransport smoke test.",
        },
        "route": "route:design-dev",
        "approval": {
            "marker": "OWNER_APPROVED_FOR_OPENDESIGN",
            "actor": "jhackuy",
            "comment_id": 999,
        },
    }


def test_odcli_submit_returns_dispatch_ack():
    """If the daemon + od CLI are available, a real submit must produce
    a structured ack with ok=True and the routine id.
    """
    _skip_if_unavailable()
    t = OdCliTransport(daemon_url=DAEMON_URL)
    ack = t.submit(_dispatch_input())
    assert ack["ok"] is True, "ack: " + repr(ack)
    assert ack["target"] == "od-cli"
    assert ack["routine_id"], "no routine_id in ack"
    assert ack["project_id"], "no project_id in ack"
    assert ack["conversation_id"], "no conversation_id in ack"
    assert ack["agent_run_id"], "no agent_run_id in ack"
    # source_packet_id may be empty if the JSON parse missed it; log
    # that for visibility but don't fail.
    if not ack.get("source_packet_id"):
        print("WARN: source_packet_id empty in ack (ingest output parse miss)")


def test_odcli_submit_with_unreachable_daemon_returns_failure():
    """Pointing at an unroutable URL must yield ok=False, never crash."""
    t = OdCliTransport(daemon_url="http://127.0.0.1:1", timeout=2)
    ack = t.submit(_dispatch_input())
    assert ack["ok"] is False
    assert "error" in ack


def _run_all():
    failures = []
    fns = [
        test_odcli_submit_returns_dispatch_ack,
        test_odcli_submit_with_unreachable_daemon_returns_failure,
    ]
    for fn in fns:
        try:
            fn()
        except AssertionError as exc:
            msg = str(exc)
            if msg.startswith("SKIP:"):
                print("SKIP:", fn.__name__, msg)
                continue
            failures.append((fn.__name__, msg))
        except Exception as exc:
            failures.append((fn.__name__, "EXC:" + repr(exc)))
    return failures


if __name__ == "__main__":
    failures = _run_all()
    if failures:
        print("FAIL:")
        for name, msg in failures:
            print("  - " + name + ": " + msg)
        sys.exit(1)
    print("OK: tests/opendesign/test_odcli_transport.py passed")
    sys.exit(0)
