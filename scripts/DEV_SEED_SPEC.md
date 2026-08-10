# DEV Seed & Cleanup — Specification

Backend: `~/Documents/Codex/pasay-pm` (FastAPI + PostgreSQL + Docker, running).
API base: `http://localhost:8000`, prefix `/api/v1`, auth `Authorization: Bearer <admin_key>`.
Admin key: read from `~/.hermes/skills/productivity/property-management/assets/config.env` (PROPERTY_API_KEY) — do not hardcode, do not print to stdout.

**Today = 2026-08-10. Current month = 2026-08.**

All DEV objects MUST be clearly marked so cleanup can match them safely:
- Property names MUST start with `DEV - ` (e.g. `DEV - Bayshore`).
- Unit numbers MUST start with `DEV-`.
- Tenant full names MUST start with `DEV ` (e.g. `DEV Juan Dela Cruz`).
- Lease notes MUST contain `DEV TEST DATA`.
- Income/expense/task/commission descriptions/notes MUST contain `DEV TEST DATA` or `DEV-`.
- Agent usernames MUST be `dev_agent_maria` and `dev_agent_john`.

Create the two scripts `scripts/dev_seed.py` and `scripts/dev_cleanup.py` in this repo.

## scripts/dev_seed.py

Idempotent, re-runnable. Use the public API (HTTP) only — NO direct SQL.
On each run:
1. Look up existing objects via GET. Reuse (do not duplicate) any DEV object that already **exactly** matches the seed definition (same name/unit/tenant/rent/etc).
2. Create only the DEV objects that are missing or differ.
3. Print a summary of created vs reused per entity.

### Seed dataset

**Properties**
- `DEV - Bayshore`  (city Pasay, address "DEV Bayshore St")
- `DEV - Solemare`  (city Pasay, address "DEV Solemare Ave")

**Units** (status=occupied except the vacant one)
| property | unit_number | monthly_rent | status |
|---|---|---|---|
| DEV - Bayshore | DEV-BAY-1203 | 65000 | occupied |
| DEV - Bayshore | DEV-BAY-1608 | 70000 | occupied |
| DEV - Bayshore | DEV-BAY-2208 | 55000 | occupied |
| DEV - Solemare | DEV-SOL-1103 | 60000 | occupied |
| DEV - Solemare | DEV-SOL-1805 | 48000 | occupied |
| DEV - Solemare | DEV-SOL-2308 | 52000 | vacant |

**Tenants** (full_name starts with `DEV `)
- DEV Juan Dela Cruz
- DEV Maria Santos
- DEV Carlo Reyes
- DEV Anna Lim
- DEV Paolo Cruz

**Leases** (notes always contain `DEV TEST DATA`). `accounting_start_date` nullable.
| unit | tenant | start_date | end_date | monthly_rent | accounting_start_date | scenario |
|---|---|---|---|---|---|---|
| DEV-BAY-1203 | DEV Juan Dela Cruz | 2026-01-01 | 2026-12-31 | 65000 | **2026-08-01** | F accounting_start late + A normal |
| DEV-BAY-1608 | DEV Maria Santos | 2026-05-15 | **2026-09-05** | 70000 | null | B current-month unpaid + D ends in 30d |
| DEV-BAY-2208 | DEV Carlo Reyes | 2026-03-20 | **2026-10-05** | 55000 | null | C 2m arrears + E ends in 60d |
| DEV-SOL-1103 | DEV Anna Lim | 2026-07-01 | 2027-06-30 | 60000 | null | A normal on-time |
| DEV-SOL-1805 | DEV Paolo Cruz | 2026-06-01 | 2027-05-31 | 48000 | null | G future payment + H late + B |

vacant unit DEV-SOL-2308 has NO lease.

For occupied units set due_day from start_date day (default). Do NOT set due_day explicitly unless noted.

**income stress scenarios.** Each income description must contain the exact `YYYY-MM` rent period (`DEV-YYYY-MM rent`). Use received_date = 2026-08-10 unless noted. Status confirmed unless noted.
- DEV-BAY-1203: `DEV-2026-08 rent` confirmed (65,000). Proves accounting_start=2026-08-01 means Feb–Jul are NOT overdue despite no historical income.
- DEV-BAY-1608: `DEV-2026-05 rent`,`DEV-2026-06 rent`,`DEV-2026-07 rent` confirmed (May/Jul paid; start 2026-05-15). **NO Aug income** → Aug 2026 overdue (current-month unpaid).
- DEV-BAY-2208: `DEV-2026-03 rent`,`DEV-2026-04 rent`,`DEV-2026-05 rent` confirmed. **NO Jun, NO Jul** → 2 months arrears (Jun+Jul). Do NOT add Aug (Aug due after today, not yet overdue).
- DEV-SOL-1103: `DEV-2026-07 rent`,`DEV-2026-08 rent` confirmed. Current month paid → no overdue.
- DEV-SOL-1805: 
  - `DEV-2026-06 rent` confirmed, **received_date=2026-08-10** (late payment, covers only its own 2026-06 period — NOT July/Aug).
  - `DEV-2026-07 rent` **pending** (received 2026-08-10) → pending does NOT count as paid → Jul overdue.
  - `DEV-2026-08 rent` confirmed, **received_date=2026-08-10** but description period = future-month? NO — this month's rent. Actually add a **future** rent: `DEV-2026-09 rent` confirmed received 2026-08-10 (future period 2026-09) — must NOT clear Jul arrears or affect Aug.
  - Net overdue for this lease = exactly [2026-07] = 1 month.

**Agents (users)** — there is NO POST /users API. dev_seed.py may create the two agent rows using a small direct SQLAlchemy insert (psycopg2, using the DB URL from .env's DATABASE_URL — but replace `@db:` host with `@localhost:` since we're outside Docker; the db port is 5432 exposed). Insert `users` rows: username `dev_agent_maria` and `dev_agent_john`, role=agent, is_active=True, and `api_key_hash = sha256(hexdigest)` of a constant DEV-only token string like `dev-agent-maria-key` / `dev-agent-john-key`. These usernames are the DEV marker for cleanup. Do NOT touch the existing `admin` / `maria` users.

**Commission rules** (via POST /commission/rules, admin only)
- name `DEV Agent Maria 5%`, rule_type percentage, value 5.00, agent_role 出租
- name `DEV Agent John 10%`, rule_type percentage, value 10.00, agent_role 出租

**Commission settlements** (via POST /commission/settlements; amount is computed SERVER-SIDE, ignore any client value)
- dev_agent_maria ← rule 5% ← lease DEV-BAY-1203 (65,000 → 3,250). Confirm it.
- dev_agent_maria ← rule 5% ← lease DEV-SOL-1103 (60,000 → 3,000). Confirm.
- dev_agent_john ← rule 10% ← lease DEV-BAY-1608 (70,000 → 7,000). Leave **pending** (pending not counted as settled).
- dev_agent_john ← rule 10% ← lease DEV-BAY-2208 (55,000 → 5,500). Confirm.
- Also create ONE settlement with a forged client-side computed_amount attempt: POST body with extra field computed_amount=12345.00 — must be IGNORED and the stored amount is the server-computed one. (Verify stored amount == lease.monthly_rent*value/100, NOT 12345.)

**Expenses** (via POST /expenses). Distinct amounts. status is part of create; then transition via approve/pay/reject/reverse endpoints.
| category | amount | unit | status chain |
|---|---|---|---|
| condo_dues | 8650.00 | DEV-BAY-1203 | pending → approved → **paid** |
| aircon_maintenance | 2500.00 | DEV-BAY-1608 | pending → approved → **paid** |
| plumbing | 1800.00 | DEV-SOL-1103 | pending → approved → paid → **reversed** (reversed must NOT count as net expense) |
| electricity | 3200.00 | DEV-SOL-1805 | pending → **rejected** (rejected not an actual expense) |
| agent_commission | 3250.00 | DEV-BAY-1203 (unit) | **pending** (not yet paid → not in paid expenses) |
| maintenance | 6000.00 | DEV-SOL-2308 (vacant, unit exists) | pending → approved → **paid** |

Payee: `DEV <something>`. description contains `DEV TEST DATA`.

**Tasks** (via POST /tasks)
1. Aircon maintenance (DEV-BAY-1203) — recurring, interval_months=3, due 2026-09-05, one completed via /complete → next_due derives +3m.
   Plus create a second recurring aircon (DEV-SOL-1805) recurring 3m that stays pending (overdue: due 2026-07-01 < today).
2. Condo dues reminder (DEV-BAY-2208) — recurring, interval_months=1, due 2026-08-20 (pending).
3. Lease expiry reminder (DEV-BAY-1608) — one-time pending, due 2026-09-05.
4. Plumbing inspection (DEV-SOL-2308) — one-time completed (via /complete).
5. Overdue rent follow-up — one-time, due 2026-07-25 (overdue, pending).
All titles/descriptions contain `DEV TEST DATA` or `DEV-`.

## scripts/dev_cleanup.py

Safe reverse of the seed. Requirements:
- **Default dry-run**: prints what WOULD be reversed/deleted, changes nothing. Only `--confirm` executes.
- Only matches DEV markers: property name starts `DEV - `, unit_number starts `DEV-`, tenant full_name starts `DEV `, lease notes contain `DEV TEST DATA`, users username in {dev_agent_maria,dev_agent_john}, commission rule name starts `DEV `, commission/income/expense/task description-or-notes contains `DEV` AND linked to a DEV lease/unit/user.
- **NO truncate. NO full-table DELETE.** 
- Financial (income/expense/commission) that are confirmed/paid/pending: follow existing business rules — reverse confirmed incomes first (POST /incomes/{id}/reverse), reverse paid expenses (POST /expenses/{id}/reverse), reverse confirmed settlements (if reverse endpoint exists, else note). Reversed/pending objects may be deleted directly only if the API supports it; otherwise soft-delete via the DELETE endpoints where present.
- Delete order (dependency-first): tasks → commission settlements → commission rules → incomes → expenses → leases → tenants → units → properties → (optionally) DEV agent users.
- Must NOT touch the pre-existing non-DEV objects (property "PASay Premier Residences" id=5, unit 1203/lease 3, users admin/maria).
- Print a clear dry-run diff. After run, verify no DEV rows remain and non-DEV rows intact.

## Verification (run AFTER seed, before cleanup)
Confirm via /reports/* endpoints:
1. /reports/overdue-rents shows: BAY-1608 [2026-08], BAY-2208 [2026-06,2026-07], SOL-1805 [2026-07]. BAY-1203 and SOL-1103 NOT overdue.
2. /reports/financial-summary?month=2026-08 : total confirmed income in Aug = sum of confirmed incomes with received_date in 2026-08.
3. /reports/commission?month=2026-08 shows Maria total=6250 (3250+3000), John total=5500 (only confirmed/pending? report sums all settlements created that month — note it includes pending too; verify).
4. /reports/expenses month=2026-08 shows paid+approved only (excludes rejected 3200, reversed 1800, pending 3250).
5. /reports/tasks status=pending, overdue=true returns the overdue tasks.

## Deliverables
- scripts/dev_seed.py
- scripts/dev_cleanup.py
- Run dev_seed.py — it must succeed idempotently.
- Leave DEV data in place (do NOT clean up this round — next round is Secretary Acceptance).
- Add/keep tests only if a real bug is found.
