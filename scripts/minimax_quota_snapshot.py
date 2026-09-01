#!/usr/bin/env python3
"""Print a compact, secret-free MiniMax Token Plan quota snapshot.

Input is the JSON returned by GET /v1/token_plan/remains. This script is
observability-only: unknown fields are ignored and it never prints credentials.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SAFE_FIELDS = (
    "model_name",
    "current_interval_total_count",
    "current_interval_usage_count",
    "current_interval_used_count",
    "current_interval_remaining_percent",
    "current_interval_status",
    "current_weekly_total_count",
    "current_weekly_usage_count",
    "current_weekly_used_count",
    "current_weekly_remaining_percent",
    "current_weekly_status",
    "remains_time",
)


def compact_record(record: dict) -> dict:
    return {key: record[key] for key in SAFE_FIELDS if key in record}


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: minimax_quota_snapshot.py <before|after> <json-file>", file=sys.stderr)
        return 2

    label, path = sys.argv[1], Path(sys.argv[2])
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"MINIMAX_QUOTA {label}: unavailable ({exc})")
        return 0

    status = payload.get("base_resp") if isinstance(payload, dict) else None
    rows = payload.get("model_remains", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        rows = []

    snapshot = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "phase": label,
        "base_resp": status if isinstance(status, dict) else None,
        "model_remains": [compact_record(row) for row in rows if isinstance(row, dict)],
    }
    print("MINIMAX_QUOTA " + json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
