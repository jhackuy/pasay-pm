# PASAY_VNEXT_EXPENSE_OPERATION_003B_REPORT

**Task**: PASAY-VNEXT-EXPENSE-OPERATION-003B — Expense Integration Truth & Continuity Closeout
**Date**: 2026-08-17 (overnight unattended)
**Branch**: `feature/telegram-ui-v2`
**Baseline SHA**: `3af10ee2649295a6c1e1c4a712806776b770ed97`
**Final CODE SHA (deployed + verified LIVE)**: `bff46a6960aa0439ae276b33e609396985343085` (= LIVE_RUNTIME_SHA)
**Final report commit**: a doc-only commit on `feature/telegram-ui-v2` (no code change; see §N).
**Runtime LIVE_SHA**: `bff46a6960aa0439ae276b33e609396985343085` (LIVE == TARGET for the deployed code).

---

## A. Root Cause (integration-truth problems found)

| ID | Problem | Location before fix |
|----|---------|---------------------|
| R1 | **No Payment Claim entity.** The only payment truth was `Expense.status`; `approved -> paid` was a raw status flip with no claim/evidence/verification. | `app/api/routers/expense.py` `pay_expense` |
| R2 | **"Click = paid" false-closure.** A single Owner tele-bot tap or NL "已经付款" flipped `approved->paid` with no verified record — "someone said they paid" was treated as "money truly paid". | bot `_handle_expense_pay_confirm`, `api.pay_expense` |
| R3 | **No partial payment / remaining.** `ExpenseStatus` had no `partially_paid` / `payment_claimed`; task generator always issued one `PAYMENT_PENDING` for the full amount. | `app/models/financial.py`, `generation.py` |
| R4 | **Approve == treated as needing full payment.** Single-amount, single-jump model. |
| R5 | **Rejection destroyed forward-path evidence.** `reject` only audited; no reason, no version preserved, no resubmit continuity. |
| R6 | **Critical-field change on approved was a hard 409** — category/payee could be silently changed and keep a stale approval. |
| R7 | **Evidence attached only to Expense root** (`receipt_attachment_id`), never to a specific claim → could not tell which payment a proof supported. |
| R8 | **Worker/Telegram never surfaced partial/remaining/verify state.** |
| R9 | **No expense detail serializer / timeline / reversal-by-records** (the 008A pattern existed for Repair only). `reverse` (paid->reversed) did not recompute from verified claims. |
| R10 | **Telegram copy could overstate "Paid"** from an unverified flip. |
| R11 | **Audit missing expense-step actions** (claim_created / verified / failed / mismatch / reapproval / fully_paid). |

## B. Truth Model (authoritative source after fix)

| Fact | Authoritative source |
|------|----------------------|
| Expense request | `expenses` row (category/purpose/amount/payee/unit/payer) |
| Approval | `expenses.status` in {approved, paid, partially_paid, payment_claimed} + `approved_by/at` |
| Amount (total) | `expenses.amount` |
| Payment required | derived — `remaining = amount - verified_paid` |
| Payment claim | `expense_payment_claims` (PENDING) |
| Payment evidence | `evidence` rows linked `entity_type='expense_payment_claim'`, `entity_id=<claim.id>` |
| **Verified amount** | **SUM of VERIFIED `expense_payment_claims.verified_amount`** (single calculator), used by router/bot/worker/Mini-App |
| Remaining balance | derived (`amount - SUM(VERIFIED)`), never negative |
| Paid status | derived — `expense.status == 'paid'` ONLY when remaining == 0 via verified aggregation |
| Related-op continuation | Expense PAID never closes a Repair (008A gate preserved) |

**No two objects can now claim to be the payment truth** — `app/services/expense_payment_truth.py` is the single authoritative calculator (`payment_truth`, `sync_expense_status`, `expense_finance_payload`), reused by the router serializer, the Mini App detail, the bot cards, and the worker.

## C. Payment Claim (lifecycle)

Added `expense_payment_claims` with:

```
PENDING (payment reported)  -> VERIFIED  (only real verification)
                            -> FAILED    (never affects paid aggregate)
VERIFIED                    -> REVERSED  (legacy reversal; aggregate recomputes)
```

Key facts:
- A **PENDING** claim moves `expense.status` to `payment_claimed` (never `paid`).
- A **VERIFIED** claim is the ONLY thing that adds `verified_amount` to the aggregate.
- A **FAILED** claim adds nothing to paid/verified/ledger; the failure is preserved in history.
- **Amount mismatch** (section 5 / E6): if admitting the claim would exceed the total, the claim is flagged `mismatch=True`, surfaced with a reason, NEVER auto-PAIDs and NEVER truncates; `failure_reason=OVERPAYMENT_MISMATCH`.
- **Idempotency** (section 6 / E5): deterministic `idempotency_key` guarded by a **DB partial unique index** + `ON CONFLICT DO NOTHING`; a ₱10,000 claim replayed 30× yields exactly one row and one verified credit.

## D. Partial Payment (₱10,000 + ₱18,000)

Proven end-to-end (backend test + live-runtime E2E):
- **Claim 1 ₱10,000** verified → `verified_paid = 10,000`, `remaining = 18,000`, `expense.status = partially_paid`. The payment operation continues; Owner + Secretary both see `remaining ₱18,000`; worker keeps ONE active follow-up task (E12b).
- **Claim 2 ₱18,000** verified → `10,000 + 18,000 = 28,000` **aggregated from VERIFIED records** (not a user-supplied override), `remaining = 0`, `expense.status = paid`.
- Timeline shows `Payment claim → Evidence → ₱10,000 verified → Remaining ₱18,000 → Payment claim → Evidence → ₱18,000 verified → Expense fully paid`.

## E. Verification (success / failure / mismatch)

- **Success** → claim VERIFIED, amount admitted, expense reconciles to partial/full paid.
- **Failure** (`/fail`) → claim FAILED; `paid`/`verified`/ledger unaffected; history preserved (E7).
- **Mismatch** (over-claim) → surfaced (E6), never silent.
- Verification is Owner/verifier-gated (`admin_only`); the Secretary's report is always a PENDING claim that Owner verifies.

## F. Approval Continuity

- **Reject → Edit → Resubmit** (E8): `POST /expenses/{id}/reject` preserves **V1 REJECTED** with `rejection_reason`; `POST /expenses/{id}/resubmit` creates **V2 PENDING** linked via `parent_expense_id`, `version` incremented. V1 is never overwritten.
- **Critical-field change → Reapproval** (E9): changing `amount/payee/category/purpose-unit/payer` on an APPROVED/PAID/PARTIAL expense calls `clear_approval` → expense returns to `PENDING`, `approved_by/at` cleared, `reapproval_reason` recorded. It must be re-approved before payment continues (never shows a stale `APPROVED ₱35,000`).
- **Approve ≠ Paid** (E1): approving only moves to `approved`/waiting-for-payment; the ledger records no real payment and the Mini App never shows "已支付".

## G. Task & Worker Continuity

Reuses the existing **`operational_tasks`** projection (no third task system).
- One expense → exactly one active `PAYMENT_PENDING` (dedupe key `expense:{id}:PAYMENT_PENDING`, DB partial unique index). **E11**: 30 worker ticks produce exactly ONE active payment task and at-most one reminder — no duplicates, no Telegram spam.
- **Remaining-aware**: the PAYMENT_PENDING task carries `amount = remaining` (not the full total); a partially-paid expense refreshes it with the remaining balance.
- **E12**: full verified payment on the next tick completes the stale payment task (no more chasing) — `reconcile`/`generation._complete_payment_task`.
- **E12b**: a verified ₱10,000 partial keeps exactly one follow-up task.
- Financial truth is NEVER derived from a user clicking "complete task"; task state is a projection.

## H. Telegram (what a human actually sees)

Card copy is derived from the verified-claims truth (never chat copy):
- `payment_claimed` → **"Payment reported · verification pending"** (zh: 已上报付款 · 待核验) — never "Paid".
- `partially_paid` → **"Partially paid · ₱10,000 verified · ₱18,000 remaining"**.
- `approved` → "Waiting for payment".
- Amount shown for partial = **remaining**, not total.
- **E16** (tests/test_expense_003b_telegram_truth.py) proves a pending/partial expense never renders "Paid/已付款".

## I. Mini App

The Mini App reads the same authoritative backend detail serializer.
- **Backend files changed**: `app/api/routers/expense.py` (new `GET /expenses/{id}/detail`, claim endpoints, verified-pay/reverse), `app/schemas/financial.py` (`ExpenseDetailOut`, `ExpensePaymentInfo`, `PaymentClaimOut`), `app/services/expense_timeline.py` (`build_expense_timeline`).
- **Presented content** (section 15): Expense (category/purpose/unit/vendor/total/status), Approval (approved_by/at, rejection reason, reapproval reason, version), Payment (required/verified_paid/remaining/fully_paid), each Claim (claimed amount/by/at/status/evidence), Evidence grouped per claim, Verification (result/amount/by/at), Actions (projection), Timeline (full ordered history).

## J. Timeline & Audit

- **Timeline** (`expense_timeline.py`): `Expense created → Submitted → Approved → Payment claim ₱10,000 → ₱10,000 verified → Remaining ₱18,000 → Payment claim ₱18,000 → ₱18,000 verified → Expense fully paid`. A Repair-linked expense continues toward verification and never shows "Repair closed".
- **Audit** (`AuditAction`) appended: `expense_claim_created/verified/failed/reversed`, `expense_amount_mismatch`, `expense_partially_paid`, `expense_fully_paid`, `expense_requires_reapproval`, `expense_resubmitted`, `expense_rejected`. Every action records the real actor.

## K. Tests (exact numbers)

**Backend (tests/)** — full suite: **561 passed**, 4 deselected, exit 0.
New targeted suites:
- `test_expense_003b_payment_truth.py` — E1,E2,E3,E4,E5,E6,E7,E8,E9,E10,E13,E15,E17 → **13 passed**.
- `test_expense_003b_worker_continuity.py` — E11,E12,E12b,E14 → **4 passed**.
- `test_expense_003b_final_e2e.py` — section-22 28k chain + Repair-linked → **2 passed**.
- Updated `tests/test_financial.py` (E9 reapproval behavior); existing financial/idempotency suites pass (29 then 91 passed).

**Telegram bot (pasay-telegram-bot/tests/)** — full suite: **544 passed**, exit 0.
- `test_expense_003b_telegram_truth.py` — E16 → **3 passed**.

**Coverage of mandated E-numbers**: E1✓ E2✓ E3✓ E4✓ E5✓ E6✓ E7✓ E8✓ E9✓ E10✓ E11✓ E12✓ E13✓ E14✓ E15✓ E16✓ E17✓.

## L. Regression

- **Rent Operation v1** — untouched; rent suites pass (in full backend 561).
- **Repair 008A** — untouched core; `test_repair_ai_employee_008.py` (18) pass; E14 proves Expense PAID does NOT close Repair.
- **Owner/Secretary RBAC** — unchanged deps; role tests pass.
- **Telegram i18n + deterministic buttons** — appended keys only; menu not rebuilt.
- **Idempotency / concurrency** — `test_financial_idempotency` concurrent pay/reverse converges (row-lock + ON CONFLICT).
- **canonical runtime / API/Bot/Worker singleton** — verified (see M).
- **Alembic migration** — `upgrade d1a9b3c4e5f6 -> e2a114b2f9d0` validated by `test_alembic_migration_upgrade_downgrade` and applied to the live test-bed DB.

## M. Runtime

Canonical runtime started via `bin/start-runtime.ps1` / `bin/pasay_runtime.py bootstrap` from the pinned worktree `worktrees/BOT-V1-USABLE-001-RUNTIME`:
- **API exactly 1** (`owned=True alive=True healthy=True`, pid 31336)
- **Bot exactly 1** (`owned=True alive=True`, pid 5348)
- **Worker exactly 1** (`owned=True alive=True`, pid 6780)
- **`/health` = 200**
- **canonical owned**: all three components `owned=True` (atomic lock files `runtime_api.lock`, `runtime_bot.lock`, `runtime_worker.lock`)
- **`LIVE_SHA == TARGET_SHA`**: `bff46a6…` == `bff46a6…` ✓ (`runtime-version-proof.json`)

> **Platform note (honest)**: within this Harness session, child processes spawned by a plain tool-call PowerShell are reaped by the harness shortly after the command returns (section 24 note). The runtime is kept stably alive by a long-running background holder job (the components are spawned with `CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS` and persist under that holder). Without such a holder the components were observed to be reaped. No "constantly running" claim is made beyond what was actually observed (LIVE_SHA + health 200 + all 3 owned/alive while the holder is active).

**Live-runtime E2E (section 22)** executed against `http://127.0.0.1:8001` on the live test-bed DB with a throwaway admin credential:
```
LIVE-E2E PASS: expense paid; claims=2; verified=28000; remaining=0; timeline complete
  timeline = [created, submitted, approved, payment_claim, verified, remaining,
              payment_claim, verified, fully_paid]
```

## N. Git (all commits)

```
bff46a6 feat(expense): 003B final E2E + timeline Intermediate-remaining fixes
1076200 fix(repair): pay-linked expense via verified claim (003B truth coherence)
64936df feat(expense): 003B alembic migration (claim table + continuity columns)
dff44b9 feat(expense-bot): 003B claim API client + Telegram UX truth (E16)
fe8f3e2 feat(expense): 003B detail serializer fields + E1-E15/E17 tests
3ad077e feat: Expense payment-claim truth model (003B) - claims/verification/partial-pay
+ docs: PASAY-VNEXT-EXPENSE-OPERATION-003B final report (READY_FOR_OWNER_ACCEPTANCE)
```

**Key changed/new files** (all committed):
- `app/models/financial.py`, `app/models/expense_claim.py` (new), `app/models/__init__.py`, `app/models/audit_log.py`
- `app/services/expense_payment_truth.py` (new), `app/services/expense_claims.py` (new), `app/services/expense_timeline.py` (new)
- `app/schemas/financial.py`, `app/api/routers/expense.py`, `app/api/routers/repairs.py`
- `app/services/operations/generation.py`, `app/services/operations/quick.py`
- `alembic/versions/e2a114b2f9d0_expense_003b_payment_claim_truth.py` (new)
- bot: `api_client.py`, `render/cards.py`, `render/i18n.py`, `handlers/callback.py`
- tests: `test_expense_003b_payment_truth.py`, `test_expense_003b_worker_continuity.py`, `test_expense_003b_final_e2e.py`, `test_financial.py` (updated), bot `test_expense_003b_telegram_truth.py`

## O. Gate

All mandated checks passed:
- targeted E1–E17 ✓
- final E2E (28k chain + Repair-linked) ✓ (automated + live runtime)
- full backend suite 561 ✓, full bot suite 544 ✓, Alembic migration ✓, git clean ✓, runtime LIVE_SHA == TARGET_SHA ✓, live `/health` 200 with exactly one API/Bot/Worker ✓

### `READY_FOR_OWNER_EXPENSE_003B_ACCEPTANCE`

---

## 27 (final) — unattended closeout

- Temp test/dev processes stopped; the canonical Pasay runtime is left running under the background holder (per §27 step 2, "不停止 canonical Pasay runtime").
- Logs preserved in `.runtime/*.log*` and `.ai-control/results/EXPENSE-003B/`.
- Final report written (this file).
- `git status` clean of new task-tracked changes (all slices committed).
- Final deployed code SHA recorded: `bff46a6…` (verified LIVE, == TARGET_SHA). A doc-only final report commit follows on `feature/telegram-ui-v2`; it does not change the running code.
- Windows shutdown executed last.

### `READY_FOR_OWNER_EXPENSE_003B_ACCEPTANCE · WINDOWS_SHUTDOWN_SCHEDULED`
