"""PASAY RETURN1 STEP 4.9: read PRE_DEPLOY container version via
`npx wrangler containers instances <APP_ID> --json` and print the
pasay-singleton version string (or "" if none).

Exit 0 always; write stdout ONLY the version line (one line) for bash
to capture via $(...). Diagnostic stderr is fine.

Environment inputs (passed from workflow):
  RETURN1_APP_ID = "a033e8e1-c52f-4298-8a46-fff6ead430be"
  RETURN1_RAW_JSON_PATH = optional; if set read JSON from file instead of running npx
  RETURN1_DIAG=1 print diagnostics on stderr
"""
from __future__ import annotations

import json
import os
import subprocess
import sys


def _run_instances(app_id: str, timeout: int = 90):
    cmd = ["npx", "wrangler", "containers", "instances", app_id, "--json"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def _parse_items(raw_text: str):
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


def _find_singleton(items: list[dict]):
    if not items:
        return None
    exact = [it for it in items if str(it.get("name", "")).strip().lower() == "pasay-singleton"]
    if exact:
        return exact[0]
    fuzzy = [it for it in items
             if "pasay-singleton" in (str(it.get("id", "")) + str(it.get("name", ""))).lower()]
    if fuzzy:
        return fuzzy[0]
    runningish = {"running","active","ready","serving","started"}
    ok = [it for it in items if (str(it.get("state","")).lower() or "") in runningish]
    if len(ok) == 1:
        return ok[0]
    return items[0]


def main() -> int:
    app_id = os.environ.get("RETURN1_APP_ID") or "a033e8e1-c52f-4298-8a46-fff6ead430be"
    diag = bool(int(os.environ.get("RETURN1_DIAG","0") or "0"))
    raw_path = os.environ.get("RETURN1_RAW_JSON_PATH")
    if raw_path and os.path.isfile(raw_path):
        text = open(raw_path, encoding="utf-8").read()
        rc = 0
        err = ""
    else:
        rc, text, err = _run_instances(app_id)
    if diag:
        print(f"[step4.9-diag] instances rc={rc} stderr(400): {(err or '')[:400]}", file=sys.stderr, flush=True)
    if rc != 0 or not text:
        print("")
        return 0
    items = _parse_items(text)
    if diag:
        print(f"[step4.9-diag] parsed instances count={len(items)}", file=sys.stderr, flush=True)
    cand = _find_singleton(items)
    if cand is None:
        print("")
        return 0
    ver = (
        cand.get("version") or cand.get("image") or cand.get("build_sha")
        or cand.get("revision") or cand.get("image_sha") or ""
    )
    print(str(ver).strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
