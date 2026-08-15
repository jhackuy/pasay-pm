#!/usr/bin/env python3
"""Deterministic read-only suspicious-data manifest (P1-...-008 B2).

Classifies live-looking records in the Pasay PM database that carry strong,
explicit DEV/test signals, and emits a cleanup-candidate manifest. The script
ONLY reads: it never deletes, never updates, never hides anything from the UI.

Signals used (strong, explicit — backed by repository evidence):
  * properties named "DEV - ..." / addresses starting "DEV " (scripts/dev_seed.py)
  * units whose number starts with "DEV-" (dev_seed unit_specs)
  * tenants whose full_name starts with "DEV " (dev_seed tenant_names)
  * incomes whose description starts with "DEV-" / "DEV " (dev_seed income_specs)
  * expenses with description == "DEV TEST DATA" or payee starting "DEV "
    (dev_seed expense_specs)
  * leases with notes == "DEV TEST DATA" (dev_seed lease notes)
  * tasks with description "DEV TEST DATA" or title starting "DEV "
  * users with username like "dev_agent_%" (dev_seed upsert_dev_agents)
  * expenses payee == "Fix-It Co" (bot test FakeBackend default payee
    signature) — multiple same-day reversed rows make it HIGH
  * expense category == "??" (explicit placeholder sentinel defined by the
    read-path cleaners; an incomplete record, not proof of test data)

Never classifies on amount/date/category equality alone.

Usage:
    .venv/bin/python scripts/suspect_data_manifest.py [--url DATABASE_URL] [--out PATH]
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

import psycopg2

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(PROJECT_ROOT, ".runtime", "suspect-data-manifest-008.json")


def load_database_url(explicit: str | None) -> str:
    if explicit:
        return explicit
    env_file = os.path.join(PROJECT_ROOT, ".env")
    for line in open(env_file, encoding="utf-8"):
        line = line.strip()
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1]
    raise SystemExit("DATABASE_URL not found in .env; pass --url explicitly")


def connect(url: str):
    p = urlparse(url.replace("postgresql+psycopg2://", "postgresql://"))
    return psycopg2.connect(
        host=p.hostname, port=p.port or 5432, user=p.username,
        password=p.password, dbname=p.path.lstrip("/"),
    )


def add(candidates: list[dict], record_type: str, record_id, reason: str, confidence: str) -> None:
    candidates.append(
        {
            "record_type": record_type,
            "record_id": record_id,
            "reason": reason,
            "confidence": confidence,
        }
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=None, help="postgres DATABASE_URL (default: repo .env)")
    ap.add_argument("--out", default=DEFAULT_OUT, help="manifest JSON output path")
    args = ap.parse_args()

    url = load_database_url(args.url)
    conn = connect(url)
    candidates: list[dict] = []
    try:
        cur = conn.cursor()
        dbname = urlparse(url.replace("postgresql+psycopg2://", "postgresql://")).path.lstrip("/")

        cur.execute(
            "SELECT id, name, address FROM properties ORDER BY id"
        )
        for pid, name, address in cur.fetchall():
            if (name or "").startswith("DEV ") or (address or "").startswith("DEV"):
                add(candidates, "property", pid,
                    f"DEV-prefixed name/address: {name!r}", "HIGH")

        cur.execute(
            "SELECT id, unit_number, is_active, deleted_at FROM units ORDER BY id"
        )
        for uid, num, active, deleted in cur.fetchall():
            if (num or "").upper().startswith("DEV-"):
                add(candidates, "unit", uid,
                    f"DEV-prefixed unit number {num!r} (dev_seed unit_specs)", "HIGH")

        cur.execute(
            "SELECT id, full_name FROM tenants ORDER BY id"
        )
        for tid, name in cur.fetchall():
            if (name or "").startswith("DEV "):
                add(candidates, "tenant", tid,
                    f"DEV-prefixed tenant name {name!r} (dev_seed tenant_names)", "HIGH")

        cur.execute(
            "SELECT id, lease_id, amount, received_date, status, description "
            "FROM incomes ORDER BY id"
        )
        for iid, lease_id, amount, rdate, status, desc in cur.fetchall():
            if (desc or "").startswith("DEV-") or (desc or "").startswith("DEV "):
                add(candidates, "income", iid,
                    f"DEV-prefixed description {desc!r} (dev_seed income_specs)", "HIGH")

        cur.execute(
            "SELECT id, expense_date, category, amount, payee, description, status "
            "FROM expenses ORDER BY id"
        )
        for eid, edate, cat, amount, payee, desc, status in cur.fetchall():
            if (desc or "") == "DEV TEST DATA":
                add(candidates, "expense", eid,
                    f"description == 'DEV TEST DATA' (dev_seed expense_specs)", "HIGH")
            elif (payee or "").startswith("DEV "):
                add(candidates, "expense", eid,
                    f"DEV-prefixed payee {payee!r} (dev_seed expense_specs)", "HIGH")
            elif (payee or "") == "Fix-It Co" and status == "reversed":
                add(candidates, "expense", eid,
                    "'Fix-It Co' + reversed (bot test FakeBackend payee signature)", "HIGH")
            elif (cat or "") == "??":
                add(candidates, "expense", eid,
                    "placeholder category '??' (explicit sentinel; incomplete record)", "MEDIUM")

        cur.execute("SELECT id, unit_id, notes, start_date FROM leases ORDER BY id")
        for lid, unit_id, notes, start in cur.fetchall():
            if (notes or "") == "DEV TEST DATA":
                add(candidates, "lease", lid,
                    "notes == 'DEV TEST DATA' (dev_seed lease notes)", "HIGH")

        cur.execute(
            "SELECT id, title, description FROM tasks ORDER BY id"
        )
        for tid, title, desc in cur.fetchall():
            if (desc or "") == "DEV TEST DATA" or (title or "").startswith("DEV "):
                add(candidates, "task", tid,
                    f"DEV TEST DATA description / DEV title {title!r}", "HIGH")

        cur.execute(
            "SELECT id, title, description FROM operational_tasks ORDER BY id"
        )
        for tid, title, desc in cur.fetchall():
            if (desc or "") == "DEV TEST DATA" or (title or "").startswith("DEV "):
                add(candidates, "operational_task", tid,
                    f"DEV TEST DATA description / DEV title {title!r}", "HIGH")

        cur.execute("SELECT id, username FROM users ORDER BY id")
        for uid, username in cur.fetchall():
            if (username or "").startswith("dev_agent_"):
                add(candidates, "user", uid,
                    f"dev_agent_* username {username!r} (dev_seed dev agents)", "HIGH")

        cur.execute("SELECT id, name FROM commission_rules ORDER BY id")
        for rid, name in cur.fetchall():
            if (name or "").startswith("DEV "):
                add(candidates, "commission_rule", rid,
                    f"DEV-prefixed rule name {name!r}", "HIGH")

        cur.execute("SELECT id, notes FROM commission_settlements ORDER BY id")
        for sid, notes in cur.fetchall():
            if (notes or "") == "DEV TEST DATA" or (notes or "") == "FORGED TEST":
                add(candidates, "commission_settlement", sid,
                    f"notes == {notes!r} (dev_seed)", "HIGH")
    finally:
        conn.close()

    by_type: dict[str, int] = {}
    for c in candidates:
        by_type[c["record_type"]] = by_type.get(c["record_type"], 0) + 1
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database": dbname,
        "candidate_count": len(candidates),
        "by_record_type": by_type,
        "confidence_counts": {
            "HIGH": sum(1 for c in candidates if c["confidence"] == "HIGH"),
            "MEDIUM": sum(1 for c in candidates if c["confidence"] == "MEDIUM"),
            "UNCERTAIN": sum(1 for c in candidates if c["confidence"] == "UNCERTAIN"),
        },
    }
    manifest = {"summary": summary, "candidates": candidates}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    out = io.StringIO()
    out.write(f"database={dbname} candidates={len(candidates)}\n")
    for c in candidates:
        out.write(
            f"  {c['confidence']:<8} {c['record_type']:<20} id={c['record_id']:<6} {c['reason']}\n"
        )
    print(out.getvalue())
    print(f"manifest written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
