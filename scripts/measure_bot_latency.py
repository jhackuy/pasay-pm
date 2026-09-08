#!/usr/bin/env python3
"""Issue #119 P0 LATENCY — production-side latency measurement for the six
frozen menu routes on @sellandrentbot.

This script is the OWNER-VISIBLE before/after measurement helper used
during Issue #119 P0. It is **read-only** on production:
  * never writes to the production DB;
  * never provisions/rotates Worker secrets;
  * never pushes changes to the codebase;
  * never sends a real Telegram message to anyone (the Owner/Secretary
    Telegram ids are NEVER used; the synthetic update is rejected by
    every backend endpoint without a valid ``Authorization`` header).

What it does:
  1. Hits ``https://<worker>/health`` to probe the Worker / Container
     reachability (produces no DB write, no auth, no side effect).
  2. Hits the SAME nine V1 endpoints every frozen menu button calls
     (``/operations/quick/{properties,rent,expense}``,
     ``/operations/digest``, ``/reports/{financial-summary,overdue-rents}``,
     ``/units``, and the negative control ``/api/v1/__probe_not_a_route``).
  3. Fires each as an unauthenticated request; the V1 endpoints reply 401
     *after* the request has traversed the full Worker -> Container -> V1
     path, so the wall-clock time is the real bot-side per-tap cost.
  4. Reports per-endpoint ``median / p95 / max`` plus a cold-vs-warm split:
        * the FIRST request per endpoint = cold-Container-wake cost
          (when at least one warm-up tap has NOT landed in the previous
          ``sleepAfter`` window — currently ``2h``);
        * the 2nd-Nth request per endpoint = warm cost.
        The watchdog probing ``/health`` does NOT keep the Container
        warm (``/health`` is Worker-only and never calls
        ``getContainer``/``container.fetch``); the warm window is
        governed entirely by ``PasayContainer.sleepAfter``.
  5. Prints a Markdown summary table suitable for pasting into Issue #119
     as the before/after evidence.

Example:

    PYTHONUNBUFFERED=1 python3 scripts/measure_bot_latency.py \\
        --worker https://pasay-cloudflare-worker.d07b0d2fdbce1d820bd215cc9c5f361c.workers.dev \\
        --repeats 5

To re-run AFTER the PR ships (HTTP/2 multiplexed transport + Rent
gathering fix), the Operator runs the same command on the same
production HEAD and the numbers will line up.

This script is intentionally dependency-free (stdlib only) so it can run
on ANY operator workstation without a Python venv.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterable


# Endpoints the six frozen menu routes hit on production. These match
# ``pasay_telegram_bot/pasay_bot/handlers/commands.py::show_*`` exactly.
FROZEN_MENU_ENDPOINTS: list[tuple[str, str]] = [
    ("properties", "/api/v1/operations/quick/properties"),
    ("tasks",       "/api/v1/operations/digest"),
    ("rent",        "/api/v1/operations/quick/rent"),
    ("expense",     "/api/v1/operations/quick/expense"),
    # Home (compiles 7 endpoints; we sample the two largest variance ones):
    ("home_fin",    "/api/v1/reports/financial-summary"),
    ("home_ovd",    "/api/v1/reports/overdue-rents"),
    ("home_units",  "/api/v1/units"),
    ("archive",     "/api/v1/operations/copilot/today"),  # Archive uses 0 V1 calls; negative control
    # Negative control confirms we still get a real 404 from the Container
    # (FastAPI shape) — proves the Worker hop is actually answering, not
    # Worker-cache shortcircuiting.
    ("NEG__404",    "/api/v1/__probe_not_a_route"),
]

# Operations the six menu buttons actually perform, mapped to a route name.
MENU_BUTTON_TO_ENDPOINTS: dict[str, list[str]] = {
    "首页 (home)": ["/api/v1/reports/financial-summary",
                  "/api/v1/reports/overdue-rents",
                  "/api/v1/units",
                  "/api/v1/operations/digest",
                  "/api/v1/operations/quick/expense",
                  "/api/v1/operations/quick/rent"],
    "房源 (properties)": ["/api/v1/operations/quick/properties"],
    "待办 (tasks)":       ["/api/v1/operations/digest"],
    "租金 (rent)":        ["/api/v1/operations/quick/rent",     # after PR: also units/leases/tasks in same gather
                          "/api/v1/units",                     # same gather (post-fix)
                          "/api/v1/leases",                    # same gather (post-fix)
                          "/api/v1/operations/tasks"],        # same gather (post-fix)
    "支出 (expense)":     ["/api/v1/operations/quick/expense"],
    "档案 (archive)":     [],   # zero V1 calls (Telegram channel link only)
}


def _measure_once(worker: str, path: str, timeout: float) -> tuple[float, int, str]:
    url = worker.rstrip("/") + path
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                # Issue #119 P0 latency: Cloudflare's bot-detection blocks
                # the default urllib User-Agent ("Python-urllib/x.y") on
                # the Worker URL; mirror a curl-like UA so the /health
                # path remains reachable from an operator workstation.
                "User-Agent": "pasay-issue-119-p0-latency/1.0 (+https://github.com/jhackuy/pasay-pm)",
                # Mirror what the bot does (no Authorization -> 401 after
                # the full Worker->Container->V1 traversal).
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status = r.status
            body = r.read(2048).decode("utf-8", errors="replace")
            body_snip = body[:120]
    except urllib.error.HTTPError as e:
        status = e.code
        body_snip = e.read(2048).decode("utf-8", errors="replace")[:120]
    except Exception as e:
        status = -1
        body_snip = repr(e)[:120]
    elapsed_ms = (time.monotonic() - t0) * 1000
    return elapsed_ms, status, body_snip


def _fmt_table(rows: list[dict]) -> str:
    headers = ["route", "endpoint", "cold_ms", "warm_p50_ms", "warm_p95_ms", "warm_max_ms", "status"]
    widths = [max(len(h), *(len(str(r.get(h, ""))) for r in rows)) for h in headers]
    line = lambda cells: " | ".join(str(c).ljust(w) for c, w in zip(cells, widths))
    bar = "-+-".join("-" * w for w in widths)
    lines = [line(headers), bar]
    for r in rows:
        lines.append(line([
            r["route"],
            r["endpoint"],
            f"{r['cold_ms']:.1f}",
            f"{r['warm_p50_ms']:.1f}",
            f"{r['warm_p95_ms']:.1f}",
            f"{r['warm_max_ms']:.1f}",
            r["status"],
        ]))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    default_worker = (
        "https://pasay-cloudflare-worker."
        "d07b0d2fdbce1d820bd215cc9c5f361c.workers.dev"
    )
    p.add_argument("--worker", default=os.environ.get("PASAY_WORKER_URL", default_worker),
                   help="Cloudflare Worker URL (production pasay-cloudflare-worker).")
    p.add_argument("--repeats", type=int, default=5,
                   help="Number of warm-path samples per endpoint (default 5).")
    p.add_argument("--timeout", type=float, default=10.0,
                   help="Per-request timeout in seconds (default 10).")
    args = p.parse_args(argv)

    print(f"# Issue #119 P0 LATENCY measurement (production-side)")
    print(f"Worker = {args.worker}")
    print(f"Repeats per endpoint = {args.repeats} (1 cold + N-1 warm)")
    print()

    # 0) Worker / health baseline
    health_ms, health_status, _ = _measure_once(args.worker, "/health", args.timeout)
    print(f"Worker /health (no Container hop): {health_ms:.1f}ms status={health_status}")
    if health_status != 200:
        print("Worker /health did not return 200; aborting measurement.")
        return 1
    print()

    rows: list[dict] = []
    print("# Per-endpoint latency table")
    print()
    for route_label, path in FROZEN_MENU_ENDPOINTS:
        if path.endswith("__probe_not_a_route"):
            # Only need 1 sample for negative control.
            cold_ms, status, _ = _measure_once(args.worker, path, args.timeout)
            rows.append(dict(
                route=route_label, endpoint=path,
                cold_ms=cold_ms,
                warm_p50_ms=cold_ms, warm_p95_ms=cold_ms, warm_max_ms=cold_ms,
                status=str(status),
            ))
            continue
        samples: list[float] = []
        last_status = -1
        for i in range(args.repeats):
            ms, status, _ = _measure_once(args.worker, path, args.timeout)
            samples.append(ms)
            last_status = status
        cold_ms = samples[0]
        warm_samples = samples[1:] if len(samples) > 1 else samples
        p50 = statistics.median(warm_samples)
        p95 = (
            warm_samples[-1]
            if len(warm_samples) >= 1
            else cold_ms
        )
        # small-N p95 fallback: max
        if len(warm_samples) < 20:
            p95 = max(warm_samples)
        rows.append(dict(
            route=route_label, endpoint=path,
            cold_ms=cold_ms,
            warm_p50_ms=p50,
            warm_p95_ms=p95,
            warm_max_ms=max(warm_samples),
            status=str(last_status),
        ))

    print(_fmt_table(rows))
    print()

    # 1) Per-MENU-button projected total (worst case; serial)
    print("# Per-button projected total (current behaviour, " ""
          "purely the dominant V1 fetch phase)")
    print()
    serial_total = 0.0
    for button, paths in MENU_BUTTON_TO_ENDPOINTS.items():
        per_path_ms = []
        for path in paths:
            # pick the warm p50 for the match
            for r in rows:
                if r["endpoint"] == path:
                    per_path_ms.append(r["warm_p50_ms"])
                    break
        if not per_path_ms:
            button_total = "0.0 (no V1 calls — link only)"
        else:
            # BEFORE the gather fix, Rent summed 2 sequential batches; for
            # the worst-case projection use the sum of warm p50s
            button_total = f"{sum(per_path_ms):.1f}"
            serial_total += sum(per_path_ms)
        print(f"  {button:20s}  {button_total:>12s} ms (sum of warm p50 V1 reads, sequential baseline)")
    print()
    print(f"# Total wall-clock if Owner taps all 6 buttons sequentially: {serial_total:.1f}ms "
          f"({serial_total/1000:.2f}s)")
    print()
    print("# Notes")
    print("- The /api/v1/* path traverses Worker -> Container -> V1 -> Neon -> back; "
          "this is the dominant per-tap cost the Owner observes on a WARM Container.")
    print("- Cold-Container wake (after PASAY_CONTAINER's sleepAfter elapses without "
          "traffic) is typically several seconds higher than the first 'cold_ms' shown above, "
          "because the very first Container wake after sleep takes the same path PLUS a fresh "
          "boot (image start + PTB build_application + DB pool connect + FastAPI listen). "
          "WATCHDOG NOTE: the production watchdog probes Worker /health, but /health is "
          "Worker-only — it inspects Env shape and binding function presence, it does NOT call "
          "getContainer() or container.fetch(). That means probing /health does NOT keep the "
          "Container warm; the warm window is governed ENTIRELY by PasayContainer.sleepAfter "
          "(see cloudflare-worker/src/index.ts). Currently '2h' to match the official "
          "Cloudflare Containers latency-sensitive example.")
    print("- All numbers are pure wall-clock httpx-equivalent GETs with NO telegram sendMessage "
          "or answerCallbackQuery — those add a separate ~100-200ms WARM to a tap, but they "
          "are NOT on the latency-critical path because PTB's HTTPXRequest transport pools "
          "those keepalive connections the same way the V1 fix above pools them.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
