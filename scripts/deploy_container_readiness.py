"""PASAY RETURN1 STEP 5.5: bounded 180s readiness TRUTH gate for
Cloudflare Container singleton rollout after
`npx wrangler deploy --containers-rollout=immediate`.

Exit codes:
   0 → readiness TRUTH gate PASS (new Container version actually serving)
  87 → 180s deadline exceeded (rollout not progressed)
  99 → argument / env error (fail-closed)

Environment inputs:
  RETURN1_APP_ID          required — a033e8e1-c52f-4298-8a46-fff6ead430be
  RETURN1_PRE_VERSION     required — output of predeploy script (or string NULL_NO_PRE_INSTANCE)
  RETURN1_GITHUB_SHA      optional — future; not used yet
  RETURN1_POLL_SEC        optional — integer seconds, default 5
  RETURN1_DEADLINE_SEC    optional — integer seconds, default 180
  RETURN1_HEALTHZ_URL     optional — if present, GET and print result (for future git_sha match; not required for gate closure now)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
import ssl
from typing import Any

APP_ID_DEFAULT = "a033e8e1-c52f-4298-8a46-fff6ead430be"
ACCEPTABLE_STATES = ("running", "active", "ready", "serving", "started")


def run_instances(app_id: str, timeout: int = 90) -> tuple[int, str, str]:
    cmd = ["npx", "wrangler", "containers", "instances", app_id, "--json"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def run_containers_list(timeout: int = 90) -> tuple[int, str, str]:
    cmd = ["npx", "wrangler", "containers", "list", "--json"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def parse_items(raw_text: str) -> list[dict]:
    try:
        data = json.loads(raw_text)
    except Exception:
        return []
    if isinstance(data, list):
        return [it for it in data if isinstance(it, dict)]
    if isinstance(data, dict):
        for k in ("instances", "result", "data"):
            v = data.get(k)
            if isinstance(v, list):
                return [it for it in v if isinstance(it, dict)]
    return []


def find_singleton(items: list[dict]) -> dict | None:
    if not items:
        return None
    exact = [it for it in items if str(it.get("name","")).strip().lower() == "pasay-singleton"]
    if exact:
        return exact[0]
    fuzzy = [it for it in items
             if "pasay-singleton" in (str(it.get("id","")) + str(it.get("name",""))).lower()]
    if fuzzy:
        return fuzzy[0]
    ok = [it for it in items if (str(it.get("state","")).lower() or "") in ACCEPTABLE_STATES]
    if len(ok) == 1:
        return ok[0]
    return items[0]


def get_version(it: dict) -> str:
    for k in ("version", "image", "build_sha", "image_sha", "revision"):
        v = it.get(k)
        if v:
            return str(v).strip()
    return ""


def healthz_probe(url: str, timeout: int = 15) -> str:
    if not url:
        return ""
    try:
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "User-Agent": "pasay-return1-readiness/1.0",
        })
        with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="replace")
        return body[:3000]
    except Exception as e:
        return f"probe_err: {type(e).__name__}: {str(e)[:300]}"


def main() -> int:
    app_id = os.environ.get("RETURN1_APP_ID") or APP_ID_DEFAULT
    pre_ver_raw = (os.environ.get("RETURN1_PRE_VERSION") or "").strip()
    if not pre_ver_raw:
        pre_ver_raw = "NULL_NO_PRE_INSTANCE"
    try:
        poll_s = int(os.environ.get("RETURN1_POLL_SEC") or "5")
        deadline_s = int(os.environ.get("RETURN1_DEADLINE_SEC") or "180")
    except ValueError:
        print("[step5.5] FAIL: RETURN1_POLL_SEC / RETURN1_DEADLINE_SEC env must be integers", file=sys.stderr)
        return 99
    healthz_url = (os.environ.get("RETURN1_HEALTHZ_URL") or "").strip()

    deadline = time.time() + deadline_s
    last_new = None
    consecutive = 0
    iter_n = 0

    print(f"[step5.5] PRE_DEPLOY_CONTAINER_VERSION = {pre_ver_raw}", flush=True)
    print(f"[step5.5] bounded poll deadline={deadline_s}s every {poll_s}s; app_id={app_id}", flush=True)

    final_singleton: dict | None = None
    final_post_ver: str | None = None
    while time.time() < deadline:
        iter_n += 1
        remaining = int(deadline - time.time())
        rc, out, err = run_instances(app_id)
        if rc != 0:
            head = lambda s, n: (s or "")[-n:]
            print(f"[step5.5][i={iter_n}] wrangler instances rc={rc} stderr(800)={head(err,800)} stdout(800)={head(out,800)} remaining={remaining}s", flush=True)
            time.sleep(poll_s)
            continue
        items = parse_items(out)
        if not items:
            print(f"[step5.5][i={iter_n}] instances EMPTY (not yet scheduled). remaining={remaining}s", flush=True)
            time.sleep(poll_s)
            continue
        print(f"[step5.5][i={iter_n}] instances total_count={len(items)} remaining={remaining}s", flush=True)
        for j, it in enumerate(items):
            i_id = str(it.get("id",""))[:20]
            i_name = str(it.get("name",""))[:40]
            i_st = str(it.get("state",""))
            i_ver = get_version(it)[:48]
            i_created = str(it.get("created", it.get("created_at", "")))[:32]
            print(f"  [{j}] id={i_id} name={i_name} state={i_st} version={i_ver} created={i_created}", flush=True)
        sing = find_singleton(items)
        if sing is None:
            print("  → pasay-singleton unidentifiable → continue poll", flush=True)
            time.sleep(poll_s)
            continue
        s_id = str(sing.get("id",""))
        s_name = str(sing.get("name",""))
        s_state = str(sing.get("state","")).lower()
        s_ver = get_version(sing)
        s_created = str(sing.get("created", sing.get("created_at","")))
        print(f"  → FOUND pasay-singleton id={s_id[:24]} name={s_name} state={s_state} version={s_ver[:64]} created={s_created}", flush=True)
        if s_state not in ACCEPTABLE_STATES:
            print(f"  → state={s_state!r} NOT serviceable (expect one of {ACCEPTABLE_STATES}). continue.", flush=True)
            time.sleep(poll_s)
            continue
        if pre_ver_raw and pre_ver_raw != "NULL_NO_PRE_INSTANCE" and s_ver == pre_ver_raw:
            print(f"  → FAIL-FAST STALE: version still PRE={pre_ver_raw[:64]}. rollout not progressed (yet?). continue poll.", flush=True)
            time.sleep(poll_s)
            continue
        # new version candidate
        if s_ver == last_new:
            consecutive += 1
        else:
            consecutive = 1
            last_new = s_ver
        print(f"  → NEW version candidate. stable_count={consecutive}/2 required", flush=True)
        final_singleton = sing
        final_post_ver = s_ver
        if consecutive >= 2:
            # ───────── TRUTH GATE PASS ─────────
            print("", flush=True)
            print("[step5.5] ╔══════════════════════════════════════════════════════════════════╗", flush=True)
            print("[step5.5] ║  RETURN1 CONTAINER ROLLOUT READINESS TRUTH GATE = PASS       ║", flush=True)
            print("[step5.5] ╚══════════════════════════════════════════════════════════════════╝", flush=True)
            version_changed = (pre_ver_raw in ("","NULL_NO_PRE_INSTANCE")) or (s_ver != pre_ver_raw)
            print(f"   PRE_DEPLOY_CONTAINER_VERSION    = {pre_ver_raw}", flush=True)
            print(f"   POST_DEPLOY_CONTAINER_VERSION   = {s_ver}", flush=True)
            print(f"   version_changed (POST≠PRE)      = {str(bool(version_changed)).upper()}", flush=True)
            print(f"   pasay-singleton id              = {s_id}", flush=True)
            print(f"   pasay-singleton name            = {s_name}", flush=True)
            print(f"   pasay-singleton state           = {s_state} (serviceable)", flush=True)
            print(f"   pasay-singleton created         = {s_created}", flush=True)
            print(f"   consecutive_stable_same_version = {consecutive}", flush=True)
            if healthz_url:
                h_res = healthz_probe(healthz_url)
                safe = h_res if len(h_res) < 3000 else h_res[:3000] + "…[TRUNC]"
                print(f"   /healthz probe result           = {safe}", flush=True)
            print(" ──── STEP A: wrangler containers list --json ────", flush=True)
            try:
                crc, cout, cerr = run_containers_list()
                print(f" containers-list rc={crc}", flush=True)
                if cout: print((cout or "")[:3000], flush=True)
                if cerr: print(f" STDERR: {(cerr or '')[:1000]}", flush=True)
            except Exception as ee:
                print(f" containers-list exception: {type(ee).__name__}: {ee}", flush=True)
            print(" ──── STEP A: wrangler containers instances --json (raw) ────", flush=True)
            print((out or "")[:4500], flush=True)
            print(" ──── END STEP A diagnostics ────", flush=True)
            return 0
        time.sleep(poll_s)
    # deadline exceeded
    print("", file=sys.stderr)
    print("[step5.5] FAIL: 180s DEADLINE EXCEEDED. readiness TRUTH gate NOT closed.", file=sys.stderr)
    print(f"  PRE_DEPLOY_CONTAINER_VERSION     = {pre_ver_raw}", file=sys.stderr)
    print(f"  last_new_version (closest match) = {(last_new or 'NONE')}", file=sys.stderr)
    print(f"  last_singleton_id                = {(str(final_singleton.get('id','')) if final_singleton else 'NONE')}", file=sys.stderr)
    print(f"  last_singleton_state             = {(str(final_singleton.get('state','')) if final_singleton else 'NONE')}", file=sys.stderr)
    print(f"  last_singleton_version           = {(final_post_ver or 'NONE')}", file=sys.stderr)
    print("  Suspicion: wrangler deploy returned SUCCESS but Container instances API still PRE version / not pasay-singleton ready.", file=sys.stderr)
    return 87


if __name__ == "__main__":
    sys.exit(main())
