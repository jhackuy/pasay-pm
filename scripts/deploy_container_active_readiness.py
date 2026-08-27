"""RETURN1 FIX step 5.5 — ACTIVE Container readiness probe (bounded poll, no fixed sleep).

Cloudflare Container instances are scale-to-zero (sleepAfter=15m by default).
When no traffic has hit a container the instances CLI may report:
  state=inactive  version=null
This is IDLE/EXPECTED and MUST NOT be used as a PASS/FAIL gate.

Owner's new readiness TRUTH GATE (Task A+B+C+D):

  GATE-1   Worker probe HTTP 200
           GET $WORKER_BASE/internal/container-probe
           Header X-Pasay-Ingest-Token = $PASAY_CONTAINER_INGEST_TOKEN

  GATE-2   Worker response.probe.worker_to_container_fetch_ok == true
           (Worker actually reached the running Container Docker instance via
            getContainer(env.PASAY_CONTAINER, "pasay-singleton").fetch(...))

  GATE-3   Response.container_health.status == "ok"
           (FastAPI DB SELECT 1 PASSED inside Container; uvicorn is running
            and initialized enough to handle real Telegram webhook POSTs.)

  GATE-4   Response.container_health.build_sha == EXPECTED_BUILD_SHA
           (Container actually serves NEW image built from current GITHUB_SHA.
            Old instance serving OLD /opt/pasay-pm path code will NOT match
            this SHA and will keep failing readiness until replaced.)

  GATE-5   TWO consecutive polls return (GATE-1..GATE-4) with IDENTICAL
           container_health.build_sha value.
           (Avoid false-positive flip at Cloudflare rollover boundary.)

Containers instances CLI JSON is ONLY dumped as DIAGNOSTIC EVIDENCE at:
  (a) First iteration  (PRE-WAKE — probably state=inactive + version=null)
  (b) After PASS        (POST-WAKE — will often show new instance started).

Exit codes:
   0 = READINESS TRUTH CLOSED (all 5 gates above)
  87 = bounded 180s deadline exceeded without closing
  99 = caller configuration error (missing env vars)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

APP_ID_DEFAULT = "a033e8e1-c52f-4298-8a46-fff6ead430be"

DEADLINE_SEC = max(30, int(os.environ.get("RETURN1_DEADLINE_SEC", "180")))
POLL_SEC = max(2, int(os.environ.get("RETURN1_POLL_SEC", "5")))
EXPECTED_SHA = os.environ.get("RETURN1_EXPECTED_BUILD_SHA", "").strip().lower()
WORKER_BASE = os.environ.get("RETURN1_WORKER_BASE", "").rstrip("/")
INGEST_TOKEN = os.environ.get("RETURN1_INGEST_TOKEN", "").strip()
APP_ID = os.environ.get("RETURN1_APP_ID", APP_ID_DEFAULT).strip()
WORKDIR = os.environ.get("RETURN1_WRANGLER_CWD", "/github/workspace/cloudflare-worker")
RAW_DIR = os.environ.get("RETURN1_ARTIFACT_DIR", "/tmp/return1_readiness")
PROBE_PATH = "/internal/container-readiness-probe"
AUTH_HEADER = "X-Pasay-Ingest-Token"


def log(msg: str) -> None:
    print(msg, flush=True)


def fail_conf(msg: str) -> None:
    log(f"[readiness][FATAL cfg] {msg}")
    sys.exit(99)


def dump_containers_instances(tag: str) -> dict[str, Any]:
    """Best-effort wrangler containers instances --json dump as diagnostics."""
    raw_path = Path(RAW_DIR) / f"instances_{tag}.json"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {"tag": tag, "collected_at": time.time()}
    try:
        proc = subprocess.run(
            ["npx", "wrangler", "containers", "instances", APP_ID, "--json"],
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            timeout=60,
        )
        raw_path.write_text(proc.stdout or "", encoding="utf-8")
        result["stdout_head"] = (proc.stdout or "")[:2000]
        result["stderr_head"] = (proc.stderr or "")[:1200]
        result["rc"] = proc.returncode
        try:
            parsed = json.loads(proc.stdout or "{}")
            if isinstance(parsed, list):
                instances = parsed
            elif isinstance(parsed, dict) and isinstance(parsed.get("instances"), list):
                instances = parsed["instances"]
            else:
                instances = []
            compact: list[dict[str, Any]] = []
            for inst in instances:
                if not isinstance(inst, dict):
                    continue
                compact.append({
                    "id": inst.get("id") or inst.get("instanceId") or inst.get("instance_id"),
                    "name": inst.get("name") or inst.get("instanceName") or inst.get("instance_name"),
                    "state": inst.get("state") or inst.get("status"),
                    "version": inst.get("version") or inst.get("build_sha") or inst.get("image_sha") or inst.get("revision"),
                    "created": inst.get("created_at") or inst.get("created") or inst.get("startedAt"),
                })
            result["parsed_instances_count"] = len(compact)
            result["parsed_instances"] = compact
            for row in compact:
                log(f"  [diag:{tag}] instance id={row['id']} name={row['name']} state={row['state']} version={row['version']} created={row['created']}")
        except Exception as exc:  # noqa: BLE001
            result["parse_error"] = type(exc).__name__
    except Exception as exc:  # noqa: BLE001
        result["subprocess_error"] = f"{type(exc).__name__}:{str(exc)[:200]}"
        log(f"  [diag:{tag}] instances CLI unavailable: {result['subprocess_error']}")
    return result


def dump_containers_list(tag: str) -> dict[str, Any]:
    raw_path = Path(RAW_DIR) / f"list_{tag}.json"
    result: dict[str, Any] = {"tag": tag}
    try:
        proc = subprocess.run(
            ["npx", "wrangler", "containers", "list", "--json"],
            cwd=WORKDIR, capture_output=True, text=True, timeout=60,
        )
        raw_path.write_text(proc.stdout or "", encoding="utf-8")
        result["rc"] = proc.returncode
        result["stdout_head"] = (proc.stdout or "")[:1600]
    except Exception as exc:  # noqa: BLE001
        result["subprocess_error"] = type(exc).__name__
    return result


def http_probe() -> tuple[int, dict[str, Any]]:
    url = f"{WORKER_BASE}{PROBE_PATH}"
    req = urllib.request.Request(
        url, method="GET",
        headers={AUTH_HEADER: INGEST_TOKEN, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", errors="replace")
            status = getattr(r, "status", 200)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        status = e.code
    except Exception as exc:  # noqa: BLE001
        return 0, {"ok": False, "transport_error": type(exc).__name__, "detail": str(exc)[:300]}
    try:
        parsed = json.loads(raw) if raw else {}
    except Exception:
        parsed = {"raw_head": raw[:400]}
    return status, parsed


def gates_closed(status: int, body: dict[str, Any]) -> tuple[bool, str, str]:
    probe = body.get("probe") if isinstance(body, dict) else None
    health = body.get("container_health") if isinstance(body, dict) else None
    reasons: list[str] = []
    if status != 200:
        reasons.append(f"http={status}")
    if not isinstance(probe, dict) or not bool(probe.get("worker_to_container_fetch_ok")):
        reasons.append("fetch_ok=false")
    if not isinstance(health, dict) or health.get("status") != "ok":
        reasons.append("container_status!=ok")
    build_sha = ""
    if isinstance(health, dict):
        build_sha = (health.get("build_sha") or "").strip().lower()
    if EXPECTED_SHA and build_sha and build_sha != EXPECTED_SHA:
        reasons.append(f"build_sha mismatch got={build_sha[:12]} exp={EXPECTED_SHA[:12]}")
    if EXPECTED_SHA and not build_sha:
        reasons.append("container build_sha absent (still old/init-in-progress?)")
    return (len(reasons) == 0), ",".join(reasons) if reasons else "-", build_sha


def main() -> None:
    if not WORKER_BASE:
        fail_conf("env RETURN1_WORKER_BASE is required (e.g. https://pasay-cloudflare-worker.<account>.workers.dev)")
    if not INGEST_TOKEN:
        fail_conf("env RETURN1_INGEST_TOKEN is required (=secret PASAY_CONTAINER_INGEST_TOKEN)")
    if not EXPECTED_SHA:
        log("[readiness][WARN] RETURN1_EXPECTED_BUILD_SHA not set; build_sha equality SKIPPED")
    Path(RAW_DIR).mkdir(parents=True, exist_ok=True)
    deadline = time.time() + DEADLINE_SEC
    consecutive = 0
    last_build_sha_seen = ""
    it = 0
    log(f"[readiness] START active readiness; deadline={DEADLINE_SEC}s  poll={POLL_SEC}s  expected_sha_head={EXPECTED_SHA[:12] if EXPECTED_SHA else '(none)'}")
    log(f"[readiness] worker probe URL = {WORKER_BASE}{PROBE_PATH}")
    # ── (STEP D-1) DIAGNOSTIC ONLY: instances BEFORE any wake-up traffic ──
    log("[readiness] DIAGNOSTIC (L1 pre-wake) containers instances (NOT used as gate):")
    dump_containers_instances("L1_prewake")
    dump_containers_list("L1_prewake")

    while time.time() < deadline:
        it += 1
        remaining = int(deadline - time.time())
        status, body = http_probe()
        ok, reason, sha = gates_closed(status, body)
        log(
            f"  [iter {it:03d} t-remain={remaining:>4d}s] http={status} ok={ok} reason={reason} sha_head={sha[:12] or 'N/A'}"
        )
        if ok:
            if sha == last_build_sha_seen and sha:
                consecutive += 1
            else:
                consecutive = 1
                last_build_sha_seen = sha
            if consecutive >= 2:
                # ── TRUTH CLOSED ──
                log(f"[readiness] PASS TRUTH CLOSED (2 consecutive stable new_sha={sha[:16]}…).")
                log("[readiness] DIAGNOSTIC (L2 post-wake PASS) containers instances (NOT gate):")
                dump_containers_instances("L2_postwake_pass")
                dump_containers_list("L2_postwake_pass")
                # Always dump final probe response as evidence
                final_path = Path(RAW_DIR) / "final_probe_response.json"
                final_path.write_text(json.dumps({"iter": it, "status": status, "body": body}, indent=2), encoding="utf-8")
                log(f"[readiness] evidence final_probe_response at {final_path}")
                log(f"[readiness] container_health.build_sha = {sha}")
                log(f"[readiness] worker side probe.worker_pasay_build_sha = {(body.get('probe') or {}).get('worker_pasay_build_sha') if isinstance(body, dict) else 'N/A'}")
                sys.exit(0)
        else:
            consecutive = 0
            last_build_sha_seen = ""
        sleep_for = min(POLL_SEC, max(1, int(deadline - time.time())))
        if sleep_for > 0:
            time.sleep(sleep_for)

    # DEADLINE exceeded
    log("[readiness] FAIL DEADLINE EXCEEDED (exit 87). Final state dump:")
    dump_containers_instances("L3_deadline_timeout")
    dump_containers_list("L3_deadline_timeout")
    last_probe_path = Path(RAW_DIR) / "deadline_last_probe.json"
    last_status, last_body = http_probe()
    last_probe_path.write_text(json.dumps({"status": last_status, "body": last_body}, indent=2), encoding="utf-8")
    log(f"[readiness] last probe http={last_status}; body written {last_probe_path}")
    sys.exit(87)


if __name__ == "__main__":
    main()
