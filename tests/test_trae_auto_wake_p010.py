# tests/test_trae_auto_wake_p010.py — targeted tests for PASAY-TASK-010.
#
# Scope strictly limited to the LOCAL bridge library. No real GitHub calls;
# no HTTP listener bind on CI; no TRAE external process spawn.
#
# Touchpoints:
#   scripts/trae_auto_wake/trae_auto_wake_bridge.py (unit smoke, CLI syntax)
#
# The bridge's real GitHub pull-mode / HTTP push-mode are validated by
# dedicated manual acceptance runs documented in PASAY-TASK-010 handoff.

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BRIDGE_PY = REPO_ROOT / "scripts" / "trae_auto_wake" / "trae_auto_wake_bridge.py"

sys.path.insert(0, str(BRIDGE_PY.parent))


def _load_bridge_module():
    spec = importlib.util.spec_from_file_location("trae_auto_wake_bridge", BRIDGE_PY)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    sys.modules["trae_auto_wake_bridge"] = m
    spec.loader.exec_module(m)
    return m


bridge = _load_bridge_module()


# ── T1: control dirs + singleton + claimed marker are fs-isolated (no shared state) ──

def test_t1_control_dirs_are_configurable_via_env(tmp_path, monkeypatch):
    ctrl = tmp_path / "ctrl"
    monkeypatch.setenv("TRAE_AUTO_WAKE_CONTROL", str(ctrl))
    # re-evaluate module-level Path defaults by resetting cached derived values:
    bridge.CONTROL_DIR = Path(os.environ["TRAE_AUTO_WAKE_CONTROL"])
    bridge.PID_FILE = bridge.CONTROL_DIR / "bridge.pid"
    bridge.LAST_EVENT = bridge.CONTROL_DIR / "last_event.json"
    bridge.CLAIMED_DIR = bridge.CONTROL_DIR / "claimed"
    bridge.LOG_DIR = bridge.CONTROL_DIR / "logs"

    bridge.ensure_dirs()
    assert bridge.CLAIMED_DIR.is_dir()
    assert bridge.LOG_DIR.is_dir()
    assert not bridge.PID_FILE.exists()
    assert bridge.already_claimed(99) is False

    bridge.mark_claimed(99, {"foo": "bar"})
    assert bridge.already_claimed(99) is True
    marker = bridge.CLAIMED_DIR / "issue-99.claimed"
    assert marker.exists()
    assert json.loads(marker.read_text()) == {"foo": "bar"}


def test_t2_singleton_lock_prevents_concurrent_instance(tmp_path, monkeypatch):
    ctrl = tmp_path / "ctrl"
    monkeypatch.setenv("TRAE_AUTO_WAKE_CONTROL", str(ctrl))
    bridge.CONTROL_DIR = ctrl
    bridge.PID_FILE = ctrl / "bridge.pid"
    bridge.LAST_EVENT = ctrl / "last_event.json"
    bridge.CLAIMED_DIR = ctrl / "claimed"
    bridge.LOG_DIR = ctrl / "logs"

    assert bridge.acquire_singleton() is True
    assert bridge.PID_FILE.exists()
    # Second acquire within the same live-pid must fail.
    assert bridge.acquire_singleton() is False
    bridge.release_singleton()
    assert not bridge.PID_FILE.exists()
    # After release, acquire should succeed again.
    assert bridge.acquire_singleton() is True
    bridge.release_singleton()


# ── T3: push payload validation matches Issue gates (Authoritative Design §2) ──

@pytest.mark.parametrize("payload,expected_err_substr", [
    ({}, "issue_number"),
    ({"issue_number": "x"}, "issue_number"),
    ({"issue_number": 0}, "issue_number"),
    ({"issue_number": 5}, "repository mismatch"),
    ({"issue_number": 5, "repository": "wrong/other"}, "repository mismatch"),
    ({"issue_number": 5, "repository": "jhackuy/pasay-pm", "issue_url": "u"}, None),
])
def test_t3_validate_push_payload(payload, expected_err_substr):
    err = bridge.validate_push_payload(payload)
    if expected_err_substr is None:
        assert err is None
    else:
        assert err and expected_err_substr in err


# ── T4: mk_result produces canonical /ND N command and serialises OK ──

def test_t4_wake_result_contracts():
    issue = {
        "number": 31,
        "title": "some title",
        "html_url": "https://github.com/jhackuy/pasay-pm/issues/31",
        "repository_full": "jhackuy/pasay-pm",
    }
    r = bridge.mk_result("pull", "ISSUED", "ok", issue=issue)
    assert r.repository == "jhackuy/pasay-pm"
    assert r.issue_number == 31
    assert r.nd_command == "/ND 31"
    # last_event serialisation round-trips.
    r.write_last_event()
    assert bridge.LAST_EVENT.exists()
    data = json.loads(bridge.LAST_EVENT.read_text())
    assert data["issue_number"] == 31
    assert data["nd_command"] == "/ND 31"
    # Handoff output to stdout includes the canonical CANONICAL_ND_CMD line:
    import io
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        r.print_handoff()
    finally:
        sys.stdout = old_stdout
    assert "CANONICAL_ND_CMD: /ND 31" in buf.getvalue()


# ── T5: CLI parser exposes exactly the 4 subcommands (pull/http/check/reset-claim) ──

def test_t5_cli_subcommands():
    parser = bridge.build_parser()
    subparsers_actions = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
    assert subparsers_actions
    cmds = set(subparsers_actions[0].choices.keys())
    assert cmds == {"pull", "http", "check", "reset-claim"}


# ── T6: No polling / no daemon; one run = one issue = one exit ──

def test_t6_pull_mode_no_github_token_fails_closed(tmp_path, monkeypatch, capsys):
    ctrl = tmp_path / "ctrl"
    monkeypatch.setenv("TRAE_AUTO_WAKE_CONTROL", str(ctrl))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    bridge.CONTROL_DIR = ctrl
    bridge.PID_FILE = ctrl / "bridge.pid"
    bridge.LAST_EVENT = ctrl / "last_event.json"
    bridge.CLAIMED_DIR = ctrl / "claimed"
    bridge.LOG_DIR = ctrl / "logs"

    code = bridge.main(["pull"])
    captured = capsys.readouterr().out
    assert "BLOCKED_GITHUB_TOKEN_MISSING" in captured
    assert code == 1
    # Must not have emitted a CANONICAL_ND_CMD.
    assert "CANONICAL_ND_CMD:" not in captured


# ── T7: Bridge source never imports requests/httpx/redis (stdlib-only contract). ──

def test_t7_stdlib_only_contract():
    src = BRIDGE_PY.read_text(encoding="utf-8")
    forbidden = ["import requests", "import httpx", "import redis", "import pydantic", "from flask", "from fastapi"]
    for f in forbidden:
        assert f not in src, f"Forbidden dependency import detected: {f}"
    # Stdlib urllib and http.server ARE allowed.
    assert "import urllib" in src


# ── T8: Idempotent claimed-marker path prevents duplicate /ND. ──

def test_t8_pull_idempotency_requires_github_token_but_rejects_when_marker_exists(
    tmp_path, monkeypatch, capsys
):
    """Unit-test the `already_claimed`-then-stop path via mocked fetch_ready_issues."""
    ctrl = tmp_path / "ctrl"
    monkeypatch.setenv("TRAE_AUTO_WAKE_CONTROL", str(ctrl))
    monkeypatch.setenv("GITHUB_TOKEN", "fake-not-used")
    bridge.CONTROL_DIR = ctrl
    bridge.PID_FILE = ctrl / "bridge.pid"
    bridge.LAST_EVENT = ctrl / "last_event.json"
    bridge.CLAIMED_DIR = ctrl / "claimed"
    bridge.LOG_DIR = ctrl / "logs"
    bridge.ensure_dirs()

    fake_issue = {
        "number": 42,
        "title": "t",
        "html_url": "https://github.com/jhackuy/pasay-pm/issues/42",
        "labels": [],
        "state": "open",
    }

    # Pre-place a claimed marker BEFORE the fetch.
    bridge.mark_claimed(42, {"simulated": True})

    def _fake_fetch_ready(_token):
        return [fake_issue]

    monkeypatch.setattr(bridge, "fetch_ready_issues", _fake_fetch_ready)
    code = bridge.main(["pull"])
    out = capsys.readouterr().out
    assert "ALREADY_CLAIMED_IDEMPOTENT" in out
    assert "CANONICAL_ND_CMD:" not in out
    # Status is success-ish per main() classification.
    assert code == 0


# ── T9: workflow file references route:dev + ready-for-dev gate labels. ──

def test_t9_github_workflow_gate_labels():
    wf = (REPO_ROOT / ".github" / "workflows" / "trae-auto-wake.yml").read_text(encoding="utf-8")
    assert "route:dev" in wf
    assert "ready-for-dev" in wf
    assert "labeled" in wf  # correct on: trigger
    # Never merges / never sets status labels.
    assert "ready-for-owner" not in wf
    assert "merge" not in wf.lower()


# ── T10: entry ps1 sink contract exposes CANONICAL_ND_CMD / nd_entry.txt. ──

def test_t10_entry_sink_contract_keywords():
    entry = (REPO_ROOT / "scripts" / "trae_auto_wake" / "trae-nd-entry.ps1").read_text(encoding="utf-8")
    assert "CANONICAL_ND_CMD" in entry
    assert "nd_entry.txt" in entry
    assert "TRAE_ND_SINK_CMD" in entry
    # Never auto-chains or loops.
    assert "while " not in entry
    assert "for (;;)" not in entry


# ── T11: install task declares IgnoreNew (single instance, not a daemon). ──

def test_t11_install_task_singleton_and_non_daemon():
    inst = (REPO_ROOT / "scripts" / "trae_auto_wake" / "install-scheduled-task.ps1").read_text(encoding="utf-8")
    assert "IgnoreNew" in inst
    assert "ExecutionTimeLimit" in inst
    assert "AtLogOn" in inst
    # No Start-Job / Start-Process without -Wait; single-shot.
    # (Our entry ps1 uses Start-Process -Wait for the python bridge.)
    entry = (REPO_ROOT / "scripts" / "trae_auto_wake" / "trae-nd-entry.ps1").read_text(encoding="utf-8")
    assert "Start-Process" in entry and "-Wait" in entry
