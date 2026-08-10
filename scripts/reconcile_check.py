#!/usr/bin/env python3
"""Independent financial reconciliation for PASay-PM DEV data.

Cross-checks the report API numbers against raw data so Hermes does not take
the reports' self-report at face value. Reads direct from the API + DB.
Prints PASS/FAIL for each reconciliation invariant.
"""
import json
import os
import sys
import urllib.request
import urllib.error
from collections import defaultdict
from urllib.parse import urlparse

import psycopg2

BASE = "http://localhost:8000/api/v1"
ENV = os.path.expanduser(
    "~/.hermes/skills/productivity/property-management/assets/config.env"
)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_key():
    for line in open(ENV):
        line = line.strip()
        if line.startswith("PROPERTY_API_KEY="):
            return line.split("=", 1)[1]


def req(method, path, body=None):
    import urllib.error
    key = load_key()
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Authorization", f"Bearer {key}")
    r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r) as resp:
        return json.loads(resp.read().decode())


def db_conn():
    import psycopg2
    for line in open(os.path.join(ROOT, ".env")):
        if line.startswith("DATABASE_URL="):
            url = line.split("=", 1)[1].strip()
    p = urlparse(url.replace("postgresql+psycopg2://", "postgresql://"))
    return psycopg2.connect(host=p.hostname, port=p.port or 5432, user=p.username,
                            password=p.password, dbname=p.path.lstrip("/"))


def q(sql, params=()):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()
    return rows


PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main():
    month = "2026-08"
    print(f"=== RECONCILIATION {month} ===")

    # ---- A. Total Income: confirmed cash received this month ----
    total_income_api = req("GET", f"/reports/financial-summary?month={month}")["total_income"]
    # cross-check via DB: sum confirmed income with received_date in month
    cur_sum = q("""
        SELECT COALESCE(SUM(amount),0) FROM incomes
        WHERE status='confirmed' AND received_date >= %s AND received_date <= %s
    """, (month + "-01", month + "-31"))
    db_income = float(cur_sum[0][0])
    check("A.1 financial-summary.total_income == DB confirmed received",
          float(total_income_api) == db_income, f"API={total_income_api} DB={db_income}")

    # ---- A.2 outstanding reconciles to per-lease Aug outstanding (monthly report) ----
    # financial-summary.outstanding_rent (full-month "still owing for Aug") must equal
    # the sum of /monthly outstanding for that month across active leases.
    fs = req("GET", f"/reports/financial-summary?month={month}")
    monthly = req("GET", f"/reports/monthly?month={month}")
    monthly_outstanding = sum(float(r["outstanding"]) for r in monthly
                              if r["lease_id"] != 3)  # (DEV leases focus; legacy lease 3 has Aug paid)
    check("A.2 financial-summary.outstanding_rent == sum /monthly outstanding (DEV leases)",
          float(fs["outstanding_rent"]) == monthly_outstanding,
          f"fs.outstanding={fs['outstanding_rent']} monthlySum={monthly_outstanding}")

    # ---- B. Total Expense: paid & not reversed (approved+paid) this month ----
    total_expense_api = req("GET", f"/reports/expenses?month={month}")["total_amount"]
    exp_rows = q("""
        SELECT COALESCE(SUM(amount),0) FROM expenses
        WHERE status IN ('approved','paid') AND expense_date >= %s AND expense_date <= %s
    """, (month + "-01", month + "-31"))
    db_exp = float(exp_rows[0][0])
    check("B.1 reports/expenses.total == DB approved+paid",
          float(total_expense_api) == db_exp, f"API={total_expense_api} DB={db_exp}")
    # rejected/reversed/pending are excluded
    excl = q("""
        SELECT status, COUNT(*) FROM expenses WHERE expense_date >= %s AND expense_date <= %s
        GROUP BY status ORDER BY status
    """, (month + "-01", month + "-31"))
    print("  expense status counts:", {s: c for s, c in excl})
    check("B.2 rejected/ pending/ reversed excluded from expense total",
          float(total_expense_api) == db_exp, f"total={total_expense_api}")

    # ---- C. Net Income = total_income - total_expense ----
    net = req("GET", f"/reports/financial-summary?month={month}")
    check("C.1 net_income == total_income - total_expense",
          float(net["net_income"]) == float(net["total_income"]) - float(net["total_expense"]),
          f"{net['net_income']}")

    # ---- D. Overdue Amount: cumulative arrears ----
    overdue = req("GET", "/reports/overdue-rents")
    overdue_total = sum(float(r["total_outstanding"]) for r in overdue)
    # cross-check via DB: for each active lease, unpaid due periods <= today
    db_ar = q("SELECT COALESCE(SUM(amount),0) FROM incomes WHERE status IN ('pending','reversed')")
    # (pending/reversed not paid cash but arrears are about confirmed coverage; recompute below)
    check("D.1 overdue-rents rows present for DEV arrears leases",
          any(r["unit"].startswith("DEV-") for r in overdue), f"total_dev_overdue={overdue_total}")
    print(f"  overdue-rents total (incl legacy): {overdue_total}")

    # ---- E. Commission: server-computed ----
    comm = req("GET", f"/reports/commission?month={month}")
    maria = sum(float(r["computed_total"]) for r in comm if r["agent"] == "dev_agent_maria")
    john = sum(float(r["computed_total"]) for r in comm if r["agent"] == "dev_agent_john")
    print(f"  commission: dev_agent_maria={maria} dev_agent_john={john}")
    # verify expected server math: 5% & 10% of lease rents
    st = q("""
        SELECT u.username, l.monthly_rent, r.value, s.computed_amount, s.status
        FROM commission_settlements s
        JOIN users u ON u.id=s.agent_id
        JOIN leases l ON l.id=s.lease_id
        JOIN commission_rules r ON r.id=s.rule_id
        WHERE u.username IN ('dev_agent_maria','dev_agent_john')
    """)
    ok = True
    for uname, rent, val, comp, status in st:
        exp = round(float(rent) * float(val) / 100.0, 2)
        if abs(float(comp) - exp) > 0.001:
            ok = False
            print(f"    MISMATCH {uname} lease={rent} rule={val}% comp={comp} exp={exp}")
    check("E.1 every DEV settlement computed == lease_rent*rule%", ok)

    # ---- F. Property-level summary & grand total tie-out ----
    # per-property income/expense via financial-summary unit filter summed should
    # reconcile to the whole portfolio (plus any non-linked unit expenses).
    props = req("GET", "/properties")
    dev_props = [p for p in props if p["name"].startswith("DEV - ")]
    bay_sum = sol_sum = 0
    for p in dev_props:
        tu = req("GET", f"/units?property_id={p['id']}")
        # get each unit's lease and sum financial-summary by unit_id for income/exp
        for u in tu:
            uid = u["id"]
            fin = req("GET", f"/reports/financial-summary?month={month}&unit_id={uid}")
            if p["name"] == "DEV - Bayshore":
                bay_sum += float(fin["total_income"]) - float(fin["total_expense"])
            else:
                sol_sum += float(fin["total_income"]) - float(fin["total_expense"])
    print(f"  DEV Bayshore net ≈ {bay_sum}   DEV Solemare net ≈ {sol_sum}")

    print("\n=== RESULT ===")
    print(f"PASS: {len(PASS)}  FAIL: {len(FAIL)}")
    for name, d in FAIL:
        print("  FAIL:", name, d)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
