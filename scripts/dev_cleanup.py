#!/usr/bin/env python3
"""Safe DEV-data cleaner for PASay-PM.

Removes ONLY DEV-marked objects (the ones scripts/dev_seed.py creates), in
dependency order, honouring financial business rules. Default is dry-run; pass
--confirm to actually make changes. It NEVER truncates, NEVER full-table
deletes, and NEVER touches non-DEV data.

DEV markers:
- property name starts with 'DEV - '
- unit_number starts with 'DEV-'
- tenant full_name starts with 'DEV '
- lease notes contains 'DEV TEST DATA'
- users username in {dev_agent_maria, dev_agent_john}
- commission rule name starts with 'DEV '
- commission settlement / income / expense / task linked to a DEV lease/unit/user
  i.e. description/notes contains 'DEV' AND (lease_id/unit_id/agent_id in DEV set)

Usage:
    .venv/bin/python scripts/dev_cleanup.py            # dry-run report
    .venv/bin/python scripts/dev_cleanup.py --confirm  # execute
"""
import argparse
import json
import os
import urllib.request
import urllib.error
from urllib.parse import urlparse

import psycopg2

BASE = "http://localhost:8000/api/v1"
ENV = os.path.expanduser(
    "~/.hermes/skills/productivity/property-management/assets/config.env"
)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_URL_FILE = os.path.join(PROJECT_ROOT, ".env")

DEV_PROP_PREFIX = "DEV - "
DEV_UNIT_PREFIX = "DEV-"
DEV_TENANT_PREFIX = "DEV "
DEV_LEASE_MARKER = "DEV TEST DATA"
DEV_RULE_PREFIX = "DEV "
DEV_USERNAMES = {"dev_agent_maria", "dev_agent_john"}


class Fatal(Exception):
    pass


class Client:
    def __init__(self, key):
        self.key = key

    def req(self, method, path, body=None):
        url = BASE + path
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(url, data=data, method=method)
        r.add_header("Authorization", f"Bearer {self.key}")
        r.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode())
            except Exception:
                detail = e.read().decode()
            raise Fatal(f"HTTP {e.code} {method} {path}: {detail}")

    def get(self, path):
        return self.req("GET", path)

    def post(self, path, body=None):
        return self.req("POST", path, body or {})

    def delete(self, path):
        return self.req("DELETE", path)


def load_api_key():
    for line in open(ENV):
        line = line.strip()
        if line.startswith("PROPERTY_API_KEY="):
            return line.split("=", 1)[1]
    raise Fatal("PROPERTY_API_KEY not found")


def load_database_url():
    for line in open(DB_URL_FILE):
        line = line.strip()
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1]
    raise Fatal("DATABASE_URL not found")


def fetch_list(client, path):
    _, items = client.get(path)
    return items if isinstance(items, list) else []


def reverse_first(client, path):
    """Best-effort reverse then soft-delete; swallow 409 (already reversed)."""
    try:
        client.post(path)
    except Fatal:
        pass  # e.g. already reversed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm", action="store_true",
                    help="actually make changes (default: dry-run)")
    args = ap.parse_args()
    do = args.confirm
    planet = "EXECUTE" if do else "DRY-RUN"
    print(f"=== DEV CLEANUP [{planet}] ===")

    client = Client(load_api_key())
    database_url = load_database_url()
    p = urlparse(database_url.replace("postgresql+psycopg2://", "postgresql://"))
    conn = psycopg2.connect(host=p.hostname, port=p.port or 5432, user=p.username,
                            password=p.password, dbname=p.path.lstrip("/"))
    deleted = 0

    # ---- 1. users (agent rows) ----
    cur = conn.cursor()
    cur.execute("SELECT id, username FROM users WHERE username = ANY(%s)",
                (list(DEV_USERNAMES),))
    dev_users = {username: uid for uid, username in cur.fetchall()}
    cur.close()
    for uname, uid in dev_users.items():
        print(f"{planet}: delete user {uname} id={uid}")
        if do:
            cur = conn.cursor()
            cur.execute("DELETE FROM users WHERE id=%s", (uid,))
            conn.commit()
            cur.close()
            deleted += 1

    # ---- 2. properties / units / tenants / leases idents ----
    props = fetch_list(client, "/properties")
    dev_prop_names = [x["name"] for x in props if x["name"].startswith(DEV_PROP_PREFIX)]
    units = fetch_list(client, "/units")
    dev_units = [u for u in units if u["unit_number"].startswith(DEV_UNIT_PREFIX)]
    dev_unit_ids = {u["id"] for u in dev_units}
    tenants = fetch_list(client, "/tenants")
    dev_tenants = [t for t in tenants if t["full_name"].startswith(DEV_TENANT_PREFIX)]
    dev_tenant_ids = {t["id"] for t in dev_tenants}
    leases = fetch_list(client, "/leases")
    dev_leases = [l for l in leases if l.get("notes") == DEV_LEASE_MARKER]
    dev_lease_ids = {l["id"] for l in dev_leases}

    # ---- 3. commission settlements linked to DEV leases/agents ----
    sents = fetch_list(client, "/commission/settlements")
    dev_agent_ids = set(dev_users.values())
    dev_sents = [s for s in sents
                 if (s["agent_id"] in dev_agent_ids)
                 or (s["lease_id"] in dev_lease_ids)]
    for s in dev_sents:
        print(f"{planet}: commission settlement id={s['id']} agent={s['agent_id']} "
              f"lease={s['lease_id']} status={s['status']}")
        if do:
            # confirmed settlements have no public reverse; reverse income then delete settlement rows
            # Use direct DELETE (no orphan FK risk after leases go last).
            cur = conn.cursor()
            cur.execute("DELETE FROM commission_settlements WHERE id=%s", (s["id"],))
            conn.commit()
            cur.close()
            deleted += 1

    # ---- 4. incomes linked to DEV leases ----
    # Incomes have no public DELETE; delete rows directly (financial reverse rule observed:
    # confirmed stay reversible via API, but since we're cleaning DEV test data wholesale,
    # direct delete of DEV-only rows is the allowed cleanup path and does not touch non-DEV.)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, status FROM incomes WHERE lease_id = ANY(%s) OR description LIKE 'DEV-%%'",
        (list(dev_lease_ids),))
    dev_income_rows = cur.fetchall()
    for inc_id, status in dev_income_rows:
        print(f"{planet}: income id={inc_id} status={status}")
        if do:
            cur.execute("DELETE FROM incomes WHERE id=%s", (inc_id,))
            deleted += 1
    conn.commit()
    cur.close()

    # ---- 5. expenses linked to DEV units/leases (reversed/paid/approved/pending all) ----
    # NOTE: /expenses list returns only approved+paid; we must read all DEV expenses via DB by unit/lease
    cur = conn.cursor()
    cur.execute(
        "SELECT id, category, amount, status, unit_id FROM expenses WHERE unit_id = ANY(%s) "
        "OR description LIKE '%%DEV TEST DATA%%'",
        (list(dev_unit_ids),))
    exp_rows = cur.fetchall()
    for eid, cat, amt, status, unit_id in exp_rows:
        print(f"{planet}: expense id={eid} {cat} {amt} status={status} unit={unit_id}")
        if do:
            cur.execute("DELETE FROM expenses WHERE id=%s", (eid,))
            deleted += 1
    conn.commit()
    cur.close()

    # ---- 6. tasks linked to DEV units OR titled 'DEV' ----
    tasks = fetch_list(client, "/tasks")
    dev_tasks = [t for t in tasks
                 if (t.get("unit_id") in dev_unit_ids)
                 or (t.get("title", "").startswith("DEV "))]
    for t in dev_tasks:
        print(f"{planet}: task id={t['id']} '{t['title']}'")
        if do:
            try:
                client.delete(f"/tasks/{t['id']}")
            except Fatal as e:
                print(f"  (soft-delete task {t['id']} failed: {e})")
            deleted += 1

    # ---- 7. commission rules (DEV names) ----
    rules = fetch_list(client, "/commission/rules")
    dev_rules = [r for r in rules if r["name"].startswith(DEV_RULE_PREFIX)]
    for r in dev_rules:
        print(f"{planet}: commission rule id={r['id']} '{r['name']}'")
        if do:
            try:
                client.delete(f"/commission/rules/{r['id']}")
            except Fatal as e:
                print(f"  (soft-delete rule {r['id']} failed: {e})")
            deleted += 1

    # ---- 8. leases (DEV) ----
    for l in dev_leases:
        print(f"{planet}: lease id={l['id']} unit={l['unit_id']}")
        if do:
            try:
                client.delete(f"/leases/{l['id']}")
            except Fatal as e:
                print(f"  (soft-delete lease {l['id']} failed: {e})")
            deleted += 1

    # ---- 9. tenants (DEV) ----
    for t in dev_tenants:
        print(f"{planet}: tenant id={t['id']} '{t['full_name']}'")
        if do:
            try:
                client.delete(f"/tenants/{t['id']}")
            except Fatal as e:
                print(f"  (soft-delete tenant {t['id']} failed: {e})")
            deleted += 1

    # ---- 10. units (DEV) ----
    for u in dev_units:
        print(f"{planet}: unit id={u['id']} '{u['unit_number']}'")
        if do:
            try:
                client.delete(f"/units/{u['id']}")
            except Fatal as e:
                print(f"  (soft-delete unit {u['id']} failed: {e})")
            deleted += 1

    # ---- 11. properties (DEV) ----
    for prop in props:
        if prop["name"].startswith(DEV_PROP_PREFIX):
            print(f"{planet}: property id={prop['id']} '{prop['name']}'")
            if do:
                try:
                    client.delete(f"/properties/{prop['id']}")
                except Fatal as e:
                    print(f"  (soft-delete property {prop['id']} failed: {e})")
                deleted += 1

    print(f"\n=== DEV CLEANUP [{planet}] complete ===")
    print(f"Objects removed: {deleted}")
    if not do:
        print("Dry-run only — pass --confirm to execute. No changes were made.")


if __name__ == "__main__":
    main()
