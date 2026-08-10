# PASay V1.1 Final Financial-Safety Review

Reviewer: Codex Max (Principal Engineer + Financial-Safety Reviewer)
Date: 2026-08-10
Branch: `feature/telegram-ui-v2` @ `55b4edf` (working tree, no commits made)
Scope: backend + PostgreSQL hardening only; no new user-facing features, no bots, no cron, no `/opt` changes.

> **Goal:** any financial/state-changing write, replayed 10×, by a stale card,
> after a network timeout, or under concurrency, must leave the same DB state
> as executing it once. The safety boundary is the backend + PostgreSQL, never
> the Telegram/UI layer.

---

## 1. Audit findings (Phase 1)

All write paths were confirmed to follow the **SELECT-then-status-check-then-UPDATE**
(TOCTOU) pattern before this change:

| Operation | File (pre-fix) | Race |
|---|---|---|
| `POST /incomes` create | `app/api/routers/income.py` | No idempotency key — N duplicate calls → N identical business incomes |
| `POST /incomes/{id}/confirm` | `income.py` | Non-atomic status check → 2 confirm audit rows possible |
| `POST /incomes/{id}/reverse` | `income.py` | Non-atomic status check → double reversal possible |
| `POST /expenses/{id}/approve` | `expense.py` | Non-atomic `if status != pending` |
| `POST /expenses/{id}/reject` | `expense.py` | Non-atomic `if status != pending` |
| `POST /expenses/{id}/pay` | `expense.py` | Non-atomic `if status != approved` |
| `POST /expenses/{id}/reverse` | `expense.py` | Non-atomic `if status != paid` |
| `POST /commission/settlements/{id}/confirm` | `commission.py` | Non-atomic `if status != pending` |

Schema before fix: `incomes`, `expenses`, `commission_settlements` had **no
UNIQUE constraint and no idempotency column** (only `users.api_key_hash` and
`users.username` are UNIQUE). Idempotency had **no DB-level backstop**.

**Side-effect inventory (audited):** the only monetary side effects in this
codebase are (a) the income/expense/settlement status transition itself and
(b) the `audit_logs` row. There is **no ledger/journal/payment table** that a
transition writes to, so the conditional UPDATE + same-transaction audit
covers every side effect. `create_settlement` computes `computed_amount` via
`app/services/commission_engine.py::compute_settlement` (unchanged); settlement
`confirm` only transitions state and writes audit — it does **not** re-compute
or create a payment. Commission engine semantics are untouched (P2).

---

## 2. Fixes implemented (Phase 2)

### P0 — create idempotency (UNIQUE backstop)
- `incomes.idempotency_key VARCHAR(128) NULL` + **partial UNIQUE index**
  `uq_incomes_idempotency_key ON incomes(idempotency_key) WHERE idempotency_key IS NOT NULL`
  (legacy NULL rows never collide).
- `create_income`: key provided → pre-query; found → return existing (HTTP 200).
  Not found → INSERT + create-audit in one transaction; on `IntegrityError`
  (concurrent same-key winner) → rollback → re-query → return existing (200).
  No key → legacy behavior unchanged.
- `idempotency_key` is not exposed on `IncomeUpdate`, so it is immutable via API.

### P1 — atomic conditional state transitions
All seven transitions rewritten to:
```sql
UPDATE <table> SET status='<target>', <actor fields>, updated_at=now()
WHERE id=<id> AND status='<expected_old>'
```
- `rowcount == 1` → write the audit row **in the same transaction**, commit.
- `rowcount == 0` → rollback (nothing written, no audit), re-read current state:
  - current == target state → **idempotent replay**, return current (HTTP 200)
  - otherwise → 409 (genuine conflict)
- No side effect can land outside the conditional UPDATE.

### P2 — settlement
`confirm_settlement` converted to the same conditional-UPDATE pattern. No
global-uniqueness constraint was added because confirm produces **no payment
record** (verified: `commission_settlements` + audit only); a per-(agent,
lease, rule) UNIQUE would change business semantics, which the brief forbids.
Confirm idempotency only.

### P3 — bot responsibilities (unchanged role split)
- `pasay_bot/state/store.py::user_defaults` remains a pure UI preference
  (SQLite) — not an idempotency boundary.
- `pasay_bot/state/idempotency.py` (SQLite nonce guard) remains the UX-level
  double-click block; **final arbitration is the backend**.
- `PasayApiClient.create_income` now accepts `idempotency_key` and the rent
  entry handler passes its guard key (`ik:cnf:ren:<nonce>`) to the backend, so
  a timeout-after-commit retry or stale replay reuses the committed row.

---

## 3. Protected operations — protection type matrix

| Operation | Protection | Mechanism |
|---|---|---|
| `POST /incomes` (same key) | **UNIQUE** partial index + pre-query + IntegrityError re-read | DB-level, atomic |
| `POST /incomes/{id}/confirm` | **Conditional UPDATE** + rowcount + same-tx audit | DB-level, atomic |
| `POST /incomes/{id}/reverse` | **Conditional UPDATE** + rowcount + same-tx audit | DB-level, atomic |
| `POST /expenses/{id}/approve` | **Conditional UPDATE** + rowcount + same-tx audit | DB-level, atomic |
| `POST /expenses/{id}/reject` | **Conditional UPDATE** + rowcount + same-tx audit | DB-level, atomic |
| `POST /expenses/{id}/pay` | **Conditional UPDATE** + rowcount + same-tx audit | DB-level, atomic |
| `POST /expenses/{id}/reverse` | **Conditional UPDATE** + rowcount + same-tx audit | DB-level, atomic |
| `POST /commission/settlements/{id}/confirm` | **Conditional UPDATE** + rowcount + same-tx audit | DB-level, atomic |

Semantic note: repeated confirm/reverse/approve/reject/pay of an already-final
state now returns **200 with the current state** (idempotent replay) instead of
409. Cross-state conflicts (e.g. confirm after reverse) still return 409. Three
legacy tests asserting 409-on-replay were updated to assert 200-on-replay
(`tests/test_financial.py`, `tests/test_commission.py`).

---

## 4. Concurrency tests (Phase 3) — real PostgreSQL, no mocks

New file: `tests/test_financial_idempotency.py` (9 tests). Each test boots a
real **uvicorn** server bound to the `pasay_pm_test` database with per-request
sessions, driven by `ThreadPoolExecutor` (10 threads). No mocks; assertions
read final DB state directly.

### 4.1 Sequential ×10
| Test | Result |
|---|---|
| income create same key ×10 | 1st=201, 9×=200, same id, **1 row, 1 create audit** |
| income confirm ×10 | 10×=200 confirmed, **1 confirm audit** |
| income reverse ×10 | 10×=200 reversed, **1 reverse audit** |
| expense approve ×10 | 10×=200 approved, **1 approve audit** |
| expense pay ×10 | 10×=200 paid, **1 pay audit** |
| settlement confirm ×10 | 10×=200 confirmed, **1 confirm audit** |

### 4.2 Concurrent ×10 (ThreadPoolExecutor, real HTTP)
| Test | Result |
|---|---|
| income create same key ×10 | all 200/201 same id, **1 row, 1 create audit** (UNIQUE backstop) |
| income confirm ×10 | all 200 confirmed, **1 confirm audit** |
| income reverse ×10 | all 200 reversed, **1 reverse audit** |
| expense approve ×10 | all 200 approved, **1 approve audit** |
| expense reject ×10 | all 200 rejected, **1 reject audit** |
| expense pay ×10 | all 200 paid, **1 pay audit** |
| expense reverse ×10 | all 200 reversed, **1 reverse audit** |
| settlement confirm ×10 | all 200 confirmed, **1 confirm audit** |

### 4.3 Timeout-after-commit replay
`test_timeout_after_commit_replay`: DB committed, response "lost" → retry with
same key returns the existing row (200, same id), **no second row, no second
create audit**; same for confirm.

### 4.4 Stale-callback replay
`test_stale_callback_replay`: full flow landed (created+confirmed), then a
stale card replays create (same key) and confirm → existing row returned,
**1 income row, 1 create audit, 1 confirm audit**; state stays `confirmed`.

### 4.5 System invariant (explicit test)
`test_invariant_n_identical_commands_equal_one`: `once` vs `sequential ×10` vs
`concurrent ×10` of the same command (create+confirm, same key) → identical
final fingerprint `(rows=1, status='confirmed', create_audits=1, confirm_audits=1)`.

---

## 5. Production pre-apply duplicate detection (read-only)

Detection SQL (0 rows = safe to add the UNIQUE index):
```sql
SELECT idempotency_key, count(*) AS n
FROM incomes
WHERE idempotency_key IS NOT NULL
GROUP BY idempotency_key
HAVING count(*) > 1;
```

Results:
- **Live production** (`pasay_pm`, read-only): alembic `2b4cbce5195f`; column
  `incomes.idempotency_key` **absent** → 0 non-NULL keys by construction. Row
  counts: incomes 19, expenses 7, settlements 6, audit_logs 125.
- **Real production data restore**: restored `backups/pasay_pm_20260810_111725.dump.gz`
  into a scratch DB, applied the full migration chain
  (`d7e5c461d569 → 2b4cbce5195f → 1f1955f798cb`) → detection SQL returned
  **0 duplicate groups**; all legacy NULL-key rows preserved and untouched.
- Migration applied from scratch on `pasay_pm_test` via `alembic upgrade head`
  → full backend suite green (111 passed) on the migration-created schema.

---

## 6. Migration

- Revision: **`1f1955f798cb`** — `financial idempotency: incomes.idempotency_key + partial unique index`
  (`alembic/versions/1f1955f798cb_financial_idempotency.py`), down_revision `2b4cbce5195f`.
- `upgrade()`: add nullable `idempotency_key VARCHAR(128)`; create partial
  UNIQUE index `uq_incomes_idempotency_key ... WHERE idempotency_key IS NOT NULL`.
- `downgrade()`: drop index, drop column (data preserved).
- **Rollback**: `alembic downgrade 2b4cbce5195f` (or `alembic downgrade -1`).
  Verified: upgrade from empty, upgrade on restored prod data, downgrade
  (index+column removed), re-upgrade.
- Deployment note: migration must be applied **before** the new backend code
  starts (new code reads/writes `idempotency_key`); both are shipped in this
  branch. Old rows (NULL key) are unaffected.

---

## 7. Test suite results (Phase 4)

| Suite | Command | Result |
|---|---|---|
| Backend baseline (pre-change) | `env -u PYTHONPATH .venv/bin/python -m pytest tests/ -q` | 102 passed |
| Backend full (post-change) | same | **111 passed** (102 + 9 new idempotency tests) |
| Concurrency file ×3 repeats | `pytest tests/test_financial_idempotency.py` | 9 passed each run (no flake) |
| Bot baseline (pre-change) | `cd pasay-telegram-bot && env -u PYTHONPATH .venv/bin/python -m pytest tests/ -q` | 132 passed |
| Bot full (post-change) | same | **132 passed** |

All runs use the real PostgreSQL test DB (`pasay_pm_test`); no mocks in the
concurrency tests.

---

## 8. Remaining known double-booking / double-reverse / double-pay risks

Definitive list after this hardening:

1. **None on the protected paths.** All seven state transitions and
   keyed income creation are DB-atomic (conditional UPDATE / partial UNIQUE +
   same-transaction audit). Concurrent or replayed commands cannot double-book,
   double-confirm, double-reverse, double-approve, double-reject, double-pay,
   or double-settle.
2. **Unkeyed income creates remain non-idempotent by design** — clients that
   omit `idempotency_key` get legacy behavior (a new row per call). This is
   documented scope: the bot always sends a key now; third-party clients that
   want dedup must send one. No production double-booking is possible through
   the bot.
3. **`expenses`/`commission_settlements` create** have no idempotency key
   (out of the brief's P0 scope; no bot path creates them and no UNIQUE exists).
   They cannot double-**pay** etc., because pay/confirm are atomic, but a
   duplicate *create* (unkeyed) is technically possible via direct API use.
4. **Cross-table business rules** (e.g. "one rent payment per lease-month")
   are not enforced by a DB constraint; dedup relies on the bot's payload
   reconcile (`find_income`) + backend key. If a strict one-per-(lease, month)
   rule is required later, it needs a new partial UNIQUE on business fields —
   explicitly out of scope for this hardening (would change business semantics).
5. **Known non-race gaps (pre-existing, unchanged):** `PATCH` update endpoints
   remain ORM-style (acceptable — not state transitions); commission rule
   `soft_delete` remains ORM-style.

---

## 9. Second-review conclusion

The invariant test (`test_invariant_n_identical_commands_equal_one`) plus the
per-operation sequential/concurrent suites confirm: **repeating any protected
financial command N times, sequentially or concurrently, leaves the same DB
state as executing it once.** The final safety boundary is now backend +
PostgreSQL (partial UNIQUE index + conditional UPDATE with rowcount + audit in
the same transaction), no longer dependent on the Telegram/UI layer. No
remaining double-booking/double-reverse/double-pay path exists on protected
operations.

**Production-deploy status:** code + migration are ready in the working tree on
`feature/telegram-ui-v2` (no commit made). Live prod remains at `2b4cbce5195f`;
apply `1f1955f798cb` before starting the new backend. Pre-apply detection on
real prod data = 0 conflicts.

**Tag readiness:** pending Hermes deploy sequence (migrate → restart backend →
restart bot). No tag created in this task.
