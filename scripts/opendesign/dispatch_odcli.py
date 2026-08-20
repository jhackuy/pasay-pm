"""PASAY-OPENDESIGN-AUTO-DISPATCH-001 OpenDesign CLI transport.

Real non-UI entrypoint for the dispatcher. Wraps the OpenDesign daemon
`od` CLI and drives:

    od automation source ingest  -> ingest Issue body as a source packet
    od automation create         -> create a manual-only routine
    od automation run <id>       -> trigger design execution
    od automation runs <id>      -> poll run history for status

All input is passed via positional CLI arguments. Issue body is written
to a temp file and passed as `--body-file -` (stdin) so shell escaping
is never involved. The transport returns a structured DispatchAck with
the routine ID, run ID, and projectId/conversationId/agentRunId.

Environment:
  OD_BIN             path to the od CLI script (default: bin/od or od)
  OD_NODE_BIN        path to node (default: node)
  OD_DAEMON_URL      daemon HTTP base (default: http://127.0.0.1:7456)
  OD_TOOL_TOKEN      bearer token for connector endpoints (optional)
  OD_RUN_TIMEOUT     per-call timeout in seconds (default: 60)
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from typing import Any, Dict, List

DEFAULT_NODE = "node"
DEFAULT_BIN = "od"


def _which(name, fallback=None):
    found = shutil.which(name)
    return found or fallback or name


class OdCliTransport:
    """Invokes the OpenDesign `od` CLI against a local daemon."""

    def __init__(
        self,
        bin_path=None,
        node_path=None,
        daemon_url=None,
        tool_token=None,
        timeout=None,
    ):
        self.node_path = node_path or os.environ.get("OD_NODE_BIN") or _which(DEFAULT_NODE)
        self.bin_path = (
            bin_path
            or os.environ.get("OD_BIN")
            or _which(DEFAULT_BIN, fallback="od")
        )
        self.daemon_url = daemon_url or os.environ.get(
            "OD_DAEMON_URL", "http://127.0.0.1:7456"
        )
        self.tool_token = tool_token or os.environ.get("OD_TOOL_TOKEN", "")
        self.timeout = int(os.environ.get("OD_RUN_TIMEOUT", str(timeout or 60)))

    def _run(self, args, stdin_text=None, timeout=None):
        env = os.environ.copy()
        env["OD_DAEMON_URL"] = self.daemon_url
        if self.tool_token:
            env["OD_TOOL_TOKEN"] = self.tool_token
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            input=stdin_text,
            timeout=timeout or self.timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def submit(self, dispatch_input):
        # 1) Ingest the Issue body as a source packet.
        body_md = (
            "# " + dispatch_input["issue"]["title"] + "\n\n"
            + "Repository: " + dispatch_input["repository"] + "\n"
            + "Issue: #" + str(dispatch_input["issue"]["number"]) + "\n"
            + "Route: " + dispatch_input["route"] + "\n"
            + "Approval actor: " + dispatch_input["approval"]["actor"] + "\n"
            + "Approval comment id: " + str(dispatch_input["approval"]["comment_id"]) + "\n"
            + "Dispatch id: " + dispatch_input["dispatch_id"] + "\n\n"
            + "## Body\n\n"
            + dispatch_input["issue"]["body"]
        )
        title = (
            "pasay-"
            + dispatch_input["repository"].split("/", 1)[-1]
            + "#"
            + str(dispatch_input["issue"]["number"])
            + " "
            + dispatch_input["issue"]["title"][:120]
        )

        try:
            rc, out, err = self._run(
                [
                    self.node_path,
                    self.bin_path,
                    "automation",
                    "source",
                    "ingest",
                    "--source-kind",
                    "upload",
                    "--title",
                    title,
                    "--source-ref",
                    "pasay:" + dispatch_input["dispatch_id"],
                    "--body-file",
                    "-",
                    "--json",
                ],
                stdin_text=body_md,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return {
                "ok": False,
                "target": "od-cli",
                "run_id": "",
                "error": "source ingest failed: " + repr(exc),
            }
        if rc != 0:
            return {
                "ok": False,
                "target": "od-cli",
                "run_id": "",
                "error": "source ingest non-zero exit: "
                + str(rc) + " stderr=" + (err or "").strip()[:400],
            }
        # Parse the single multi-line JSON object on stdout. The object
        # may begin with `{` and end with `}` on separate lines.
        packet_id = None
        try:
            text = out.strip()
            end_idx = None
            for i, ln in enumerate(text.splitlines()):
                stripped = ln.rstrip(",").rstrip()
                if stripped == "}":
                    end_idx = i
                    break
            if end_idx is not None:
                blob = "\n".join(text.splitlines()[: end_idx + 1])
                ingest_payload = json.loads(blob)
                if isinstance(ingest_payload, dict):
                    packet_id = (ingest_payload.get("packet") or {}).get("id")
        except Exception:
            packet_id = None

        # 2) Create a manual-only automation that references the source.
        routine_name = "pasay-dispatch-" + uuid.uuid5(
            uuid.NAMESPACE_URL, dispatch_input["dispatch_id"]
        ).hex[:12]
        prompt = (
            "Design work requested via PASAY dispatcher. "
            "Apply the ingested source packet "
            + str(packet_id or "?")
            + " to produce a design artifact for "
            + dispatch_input["repository"]
            + " issue #" + str(dispatch_input["issue"]["number"])
            + "."
        )
        try:
            rc, out, err = self._run(
                [
                    self.node_path,
                    self.bin_path,
                    "automation",
                    "create",
                    "--name",
                    routine_name,
                    "--prompt",
                    prompt,
                    "--schedule",
                    "hourly:0",
                    "--target",
                    "new-project",
                    "--json",
                ]
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return {
                "ok": False,
                "target": "od-cli",
                "run_id": "",
                "error": "automation create failed: " + repr(exc),
            }
        if rc != 0:
            return {
                "ok": False,
                "target": "od-cli",
                "run_id": "",
                "error": "automation create non-zero exit: "
                + str(rc) + " stderr=" + (err or "").strip()[:400],
            }
        # Parse the single multi-line JSON object on stdout. Find the
        # line index of the closing brace and parse the whole block.
        routine_id = ""
        try:
            text = out.strip()
            # find the last line that is exactly '}' (or '},' or ends with '}')
            end_idx = None
            for i, ln in enumerate(text.splitlines()):
                stripped = ln.rstrip(",").rstrip()
                if stripped.endswith("}"):
                    end_idx = i
            if end_idx is not None:
                lines = text.splitlines()[: end_idx + 1]
                blob = "\n".join(lines)
                routine_id = (json.loads(blob).get("routine") or {}).get("id", "")
        except Exception:
            routine_id = ""
        if not routine_id:
            return {
                "ok": False,
                "target": "od-cli",
                "run_id": "",
                "error": "could not parse routine id from: " + out[:200],
            }

        # 3) Trigger manual run.
        try:
            rc, out, err = self._run(
                [
                    self.node_path,
                    self.bin_path,
                    "automation",
                    "run",
                    routine_id,
                ]
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return {
                "ok": False,
                "target": "od-cli",
                "run_id": "",
                "error": "automation run failed: " + repr(exc),
            }
        if rc != 0:
            return {
                "ok": False,
                "target": "od-cli",
                "run_id": "",
                "error": "automation run non-zero exit: "
                + str(rc) + " stderr=" + (err or "").strip()[:400],
            }
        run_id = ""
        project_id = ""
        conversation_id = ""
        agent_run_id = ""
        for ln in out.strip().splitlines():
            ln = ln.strip()
            if ln.startswith("runId\t"):
                run_id = ln.split("\t", 1)[1]
            elif ln.startswith("projectId\t"):
                project_id = ln.split("\t", 1)[1]
            elif ln.startswith("conversationId\t"):
                conversation_id = ln.split("\t", 1)[1]
            elif ln.startswith("agentRunId\t"):
                agent_run_id = ln.split("\t", 1)[1]

        # 4) Query the run history to confirm a run record exists. A
        # non-zero exit here does NOT fail the dispatch (the run already
        # happened); we just log it.
        run_status = "unknown"
        try:
            rc2, out2, _ = self._run(
                [
                    self.node_path,
                    self.bin_path,
                    "automation",
                    "runs",
                    routine_id,
                    "--limit",
                    "1",
                ],
                timeout=10,
            )
            if rc2 == 0:
                # First row of tab-separated output is the header; the
                # latest run row is the second. Status is column 2.
                rows = [ln for ln in out2.strip().splitlines() if "\t" in ln]
                if len(rows) >= 2:
                    cells = rows[1].split("\t")
                    if len(cells) >= 2:
                        run_status = cells[1]
        except Exception:
            pass

        return {
            "ok": True,
            "target": "od-cli",
            "run_id": run_id or routine_id,
            "routine_id": routine_id,
            "source_packet_id": packet_id or "",
            "project_id": project_id,
            "conversation_id": conversation_id,
            "agent_run_id": agent_run_id,
            "run_status": run_status,
            "design_commit_sha": "",
            "changed_files": [],
            "design_gate_result": "",
        }
