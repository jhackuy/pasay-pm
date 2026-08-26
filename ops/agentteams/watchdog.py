#!/usr/bin/env python3
"""Pause a PASAY AgentTeams project after repeated no-progress snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys
from typing import Any


TEAM_NAME = os.environ.get("PASAY_WATCHDOG_TEAM", "pasay-engineering")
STALL_POLLS = int(os.environ.get("PASAY_WATCHDOG_STALL_POLLS", "6"))
DRY_RUN = os.environ.get("PASAY_WATCHDOG_DRY_RUN", "1") != "0"
CONTAINER_CMD = os.environ.get("PASAY_WATCHDOG_CONTAINER_CMD", "docker")
STATE_PATH = pathlib.Path(
    os.environ.get(
        "PASAY_WATCHDOG_STATE",
        "~/.local/state/pasay-agentteams/watchdog.json",
    )
).expanduser()


def parse_json_output(raw: str) -> Any:
    decoder = json.JSONDecoder()
    for index, char in enumerate(raw):
        if char not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(raw[index:])
            return value
        except json.JSONDecodeError:
            continue
    raise ValueError("AgentTeams CLI returned no JSON payload")


def run_agt(arguments: list[str]) -> Any:
    command = [CONTAINER_CMD, "exec", "agentteams-manager", "agt", *arguments]
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=90,
    )
    return parse_json_output(completed.stdout)


def project_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("projects", "items", "result"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = project_list(value)
            if nested:
                return nested
    return []


def progress_fingerprint(detail: dict[str, Any]) -> str:
    nodes = []
    for node in detail.get("nodes", []):
        if isinstance(node, dict):
            nodes.append(
                {
                    "id": node.get("id"),
                    "status": node.get("status"),
                    "assignee": node.get("assignee"),
                }
            )
    stable = {
        "status": detail.get("status"),
        "nodes": sorted(nodes, key=lambda item: str(item.get("id"))),
        "next": detail.get("next", []),
        "interrupts": detail.get("interrupts", []),
        "loop": detail.get("loop"),
    }
    encoded = json.dumps(stable, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def next_stall_count(previous: dict[str, Any] | None, fingerprint: str) -> int:
    if previous and previous.get("fingerprint") == fingerprint:
        return int(previous.get("stall_count", 0)) + 1
    return 1


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(STATE_PATH)


def pause_project(project_id: str, reason: str) -> None:
    if DRY_RUN:
        print(f"WATCHDOG_DRY_RUN project={project_id} reason={reason}")
        return
    run_agt(["project", "pause", project_id, "--reason", reason])
    print(f"WATCHDOG_PAUSED project={project_id} reason={reason}")


def main() -> int:
    if STALL_POLLS < 2:
        raise ValueError("PASAY_WATCHDOG_STALL_POLLS must be >= 2")
    state = load_state()
    active_keys: set[str] = set()
    projects = project_list(run_agt(["get", "projects", "-o", "json"]))

    for project in projects:
        project_id = str(project.get("project_id") or project.get("id") or "")
        team = str(project.get("team_id") or project.get("team") or "")
        status = str(project.get("status") or "")
        if not project_id or team != TEAM_NAME or status != "active":
            continue
        key = f"{team}:{project_id}"
        active_keys.add(key)
        detail = run_agt(["get", "projects", project_id, "-o", "json"])
        if not isinstance(detail, dict):
            print(f"WATCHDOG_SKIPPED project={project_id} reason=invalid-detail")
            continue
        fingerprint = progress_fingerprint(detail)
        count = next_stall_count(state.get(key), fingerprint)
        state[key] = {"fingerprint": fingerprint, "stall_count": count}
        if count >= STALL_POLLS:
            reason = f"PASAY circuit breaker: no workflow state change for {count} checks"
            pause_project(project_id, reason)
            state[key]["paused_by_watchdog"] = True
            state[key]["stall_count"] = 0

    for key in list(state):
        if key.startswith(f"{TEAM_NAME}:") and key not in active_keys:
            state.pop(key, None)
    save_state(state)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        print(f"WATCHDOG_ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
