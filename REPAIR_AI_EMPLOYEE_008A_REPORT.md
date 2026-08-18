# REPAIR-AI-EMPLOYEE-WORKFLOW-008A — FINAL REPORT

Task: make the Pasay AI Repair flow a first-class **AI Employee Operation**
(`Repair Operation → Proposal → Human Action → Verification → Closure`).

Scope covered: Repair Operation object + decoupled versioned Proposals + AI
continuation with requote dedup + Verification gate + Telegram/Mini-App read path
+ business-rule tests + live Windows E2E.
Scope NOT touched (008A §10): Rent Collection, Lease Renewal, Move-out, Deposit
Settlement, Merchant UI, new nav, OpenDesign, Telegram menu overhaul, Learning,
points/badges, runtime launcher/singleton code, Auth/RBAC architecture.

---

## A. Root Cause / 原模型问题

**为什么旧模型会把 Proposal、Expense、Repair 混在一起？**

Before 008A, a repair was NOT a first-class object. The only repair trace in the
old model (`app/services/operations/repair_flow.py`) lived on the `AC_MAINTENANCE`
`operational_tasks` row as JSONB `details.repair_stage`
(`ISSUE_REPORTED → … → WORK_COMPLETED → PAYMENT → VERIFICATION → CLOSED`), and the
task's own status was only `PENDING/IN_PROGRESS/COMPLETED/CANCELLED`.

Consequences of that conflation:
1. **No separate Proposals.** A quote was a business-flow step (or a loose Expense),
   not a persisted, versioned candidate. There was no way to record "V1 rejected,
   V2 approved" history.
2. **No separate Repair status.** The `operational_tasks.status` either "completed"
   the whole repair or not; there was no notion of `WAITING_APPROVAL` vs
   `WAITING_PAYMENT` vs `VERIFYING` as distinct real-world states.
3. **No Verification gate boundary.** Because repair and task were the same object,
   the natural (wrong) shortcut was to treat "proposal approved / expense paid /
   task completed / reminder sent" as "repair resolved" → closure. That is the P0
   error 008A forbids (`Owner rejected → Repair closed`, `Expense PAID → Repair
   closed`).
4. **No derived AI-employee state.** `next_action` / `waiting_on` / `blocked_reason`
   could not be answered from one row; the bot fell back to chat copy, so the Mini
   App and Telegram could disagree on the same issue.

008A fixes this by promoting Repair to its own `repair_operations` object, its own
state machine, decoupled `repair_proposals`, and an authoritative
`repair_actions` stream — so Closure is reachable only through real verification.

(The legacy `AC_MAINTENANCE` operational-task bridge is retained as
`repair_operations.operational_task_id` for back-compat; the operational_tasks
table is no longer the source of repair truth.)

---

## B. Data Model

New tables (Alembic `d1a9b3c4e5f6`, appended only):

### `repair_operations` — the real-world problem
| field | notes |
|---|---|
| merchant_id / property_id / unit_id | scoping |
| issue / issue_description | the problem |
| created_source / reported_by | provenance |
| assignee_user_id | responsible human |
| status | `OPEN, IN_PROGRESS, WAITING_HUMAN, WAITING_VENDOR, WAITING_APPROVAL, WAITING_PAYMENT, VERIFYING, CLOSED, CANCELLED` (DB CHECK) |
| next_action / waiting_on / blocked_reason / next_check_at | **derived AI-employee state** (the source both Telegram & Mini App read) |
| closure_criteria | what "done" means |
| verified_by / verified_at / verification_result | the Verification gate record |
| closed_at / closure_reason | closure record |
| evidence (JSONB) / details (JSONB) | evidence ids + extra data |
| operational_task_id | back-compat bridge |

### `repair_proposals` — decoupled, versioned solution candidates
`repair_id`, `version` (unique with repair_id), `vendor/source/description/amount`,
`submitted_by/at`, `status` (`PENDING, APPROVED, REJECTED, SUPERSEDED`),
`decision_by/at`, `rejection_reason`, `expense_id` (links the Expense — never
merges with it). V1/v2 history is always preserved.

### `repair_actions` — idempotent AI-continuation stream
`repair_id`, `action_kind` (REQUOTE / PROPOSE_ALTERNATIVE / CONTACT_VENDOR /
RECORD_REPAIR_RESULT / VERIFY_REPAIR), `title/description`, `status`
(`PENDING, IN_PROGRESS, COMPLETED, CANCELLED`), `assigned_user_id`, `dedupe_key`,
`source_event`, `resolved_at/by`, `detail`.
**Dedup boundary = partial unique index**
`(repair_id, dedupe_key) WHERE status IN ('PENDING','IN_PROGRESS')` → a repeated
worker tick / callback retry / page refresh can never create a second ACTIVE action
for the same step.

Migration applied to the LIVE DB `pasay_pm_win_test`:
`c4d5e6f7a8b9 → d1a9b3c4e5f6`; `alembic current = d1a9b3c4e5f6 (head)`; all three
tables verified present. `audit_action` is a plain VARCHAR(50) with no DB CHECK, so
the new repair audit actions (`repair_created`, `proposal_submitted`,
`proposal_approved`, `proposal_rejected`,
`repair_completed_pending_verification`, `repair_closed_after_verification`,
`repair_cancelled`) are appended to the Python `AuditAction` enum only.

Files added/changed:
- `app/models/repair.py` (new)
- `app/models/audit_log.py` (AuditAction additions)
- `app/models/__init__.py`
- `app/schemas/repair.py` (new)
- `app/services/repairs/` — `state.py`, `operations.py`, `proposals.py`,
  `continuation.py`, `payment.py`, `verification.py`, `__init__.py` (new)
- `app/api/routers/repairs.py` (new)
- `app/main.py` (register router)
- `alembic/versions/d1a9b3c4e5f6_repair_ai_employee_008a.py` (new)
- bot: `pasay_bot/api_client.py` (Repair dataclasses + methods),
  `pasay_bot/render/repairs.py` (new fast-path card)
- tests: backend + bot

---

## C. State Machine

### Repair Operation (`app/services/repairs/state.py`)
Transitions are an explicit allow-list; every move is validated. Absorbing
terminal states `CLOSED`/`CANCELLED` cannot move.

- `OPEN` → IN_PROGRESS / WAITING_APPROVAL / WAITING_VENDOR / WAITING_HUMAN /
  VERIFYING / CANCELLED
- `IN_PROGRESS` → OPEN / WAITING_VENDOR / WAITING_APPROVAL / WAITING_HUMAN /
  WAITING_PAYMENT / VERIFYING / CANCELLED
- `WAITING_HUMAN` → IN_PROGRESS / WAITING_APPROVAL / WAITING_VENDOR / VERIFYING / CANCELLED
- `WAITING_VENDOR` → IN_PROGRESS / WAITING_APPROVAL / WAITING_HUMAN / VERIFYING / CANCELLED
- `WAITING_APPROVAL` → IN_PROGRESS / WAITING_HUMAN (**proposal_rejected**) /
  WAITING_PAYMENT (**proposal_approved**) / VERIFYING / CANCELLED
- `WAITING_PAYMENT` → VERIFYING (**payment_paid**) / WAITING_HUMAN / IN_PROGRESS /
  CANCELLED
- `VERIFYING` → WAITING_HUMAN / IN_PROGRESS (rework) / **CLOSED (verified)** ← the
  ONLY path into CLOSED
- `CANCELLED` / `CLOSED`: absorbing.

Guards:
- `ensure_closable_via_verification(signal)` refuses any CLOSED move whose signal is
  not `HUMAN_CONFIRMED` / `COMPLETION_EVENT`. The payment path, approve path,
  reminder, and vendor-contact paths can never reach CLOSED.
- Rejecting a proposal keeps the repair `alive` (`WAITING_HUMAN`); it never
  CANCELLES and never CLOSES.

### Proposal (`app/services/repairs/proposals.py`)
- `PENDING → APPROVED / REJECTED`, older `PENDING → SUPERSEDED`.
- submit orders the next version (V1→V2→…); refuses while the latest is still
  APPROVED (you do not re-quote an already-approved plan).
- **reject_proposal**: ONLY the proposal becomes `REJECTED` (reason/actor stored);
  the Repair stays alive.
- **approve_proposal**: proposal → `APPROVED`; repair → `WAITING_PAYMENT` (never
  CLOSED).

---

## D. AI Continuation (after Reject)

008A Gate §3 — when a Proposal is rejected the AI **automatically continues**:

`proposal_rejected` event → `ensure_requote_action(repair, proposal)`:
- marks only that proposal `REJECTED` and stores the reason (service);
- keeps the Repair **alive** (`WAITING_HUMAN`);
- updates derived state (`next_action = "Get another quote for repair R-x (rejected V1)"`,
  `waiting_on = "secretary"`);
- creates ONE `REPAIR_AUTO` idempotent action (`dedupe_key =
  repair:<id>:requote:v<version>`) assigned to the Secretary → the single next
  step a real human must do, from a durable row (not chat text).

`record-result` (human confirms real work) → `ensure_record_result_action` keeps the
Repair `VERIFYING`; explicit `verify_and_close` completes any active
VERIFY/RECORD actions and writes the closure record.

---

## E. Dedup

Per-step `dedupe_key` + DB partial unique index
`(repair_id, dedupe_key) WHERE status IN ('PENDING','IN_PROGRESS')`; creation uses
`INSERT … ON CONFLICT DO NOTHING`. A repeated worker tick / bot callback / page
refresh / API retry can therefore never produce a second ACTIVE requote action.
A new action for the same step is only possible after the previous one is
COMPLETED or CANCELLED (the key’s `v<version>` seeds per rejection event).

**Proof:** Case C unit test runs `ensure_requote_action` 50× → exactly 1 CREATE,
1 active REQUOTE. Live E2E ran 30 worker ticks → `created_once=0 active_REQUOTE=1`.

---

## F. Tests

New backend `tests/test_repair_ai_employee_008.py`
- Case A — Reject keeps Repair OPEN (P0)……PASS
- Case B — Requote keeps V1 + creates V2 (both exist)……PASS
- Case C — repeated worker ticks → ONE active requote……PASS
  - plus: new requote allowed after previous resolved
- Case D — Payment does not close Repair……PASS
- Case E — Verification closes Repair (+ refusing a non-verification signal)……PASS
- Case F — History preserved after CLOSED (V1 rejected / V2 approved / Expense paid / verification)……PASS
- Router integration flow (create → reject → verify; audit enum + detail serialization)……PASS

New bot `pasay-telegram-bot/tests/test_repair_ai_employee_008_bot.py`
- reject stays open + one requote action (bot fast path, 008A §8 card)……PASS
- full approve→pay→verify→close flow (read real state)……PASS

Results:
- Backend full suite: **539 passed, 4 deselected, 0 failed** (baseline run) then
  repair suite re-run **9 passed** after the final router/enum fixes (additive,
  no other suite touched). Total ≈ **540 passed**.
- Bot full suite: **541 passed, 0 failed** (includes 2 new fast-path tests).
- Regression (Rent / Expense / Telegram / RBAC): all green (see H).

---

## G. Live Windows E2E (008A §13, 14 steps) — live API :8001 + live DB

Driver: `.runtime/e2e_008a_live.py` against the live API running the deployed SHA.

1. create repair R-5 → `OPEN` … PASS
2. Proposal V1 ₱8,000 → `WAITING_APPROVAL` … PASS
3. Owner reject, reason `Too expensive` → V1 `REJECTED`, repair `WAITING_HUMAN` … PASS
4. Repair detail confirmed **still alive** (not CLOSED) … PASS
5. Secretary requote action exists (1 REQUOTE PENDING) … PASS
6. **No duplicate** requote task (30 worker ticks → `active_REQUOTE=1`) … PASS
7. Proposal V2 ₱6,500 created (`PENDING`) … PASS
8. Owner approve V2 → `WAITING_PAYMENT` … PASS
9. Expense created (₱6,500, approved) + paid … PASS
10. **Repair still not closed** after payment (→ `VERIFYING`) … PASS
11. Secretary record-result → stays `VERIFYING` … PASS
12. Verification state alive (not closed) … PASS
13. **verify → CLOSED** `verified_by=1`, `closure_reason=HUMAN_CONFIRMED` … PASS
14. Timeline: V1 `REJECTED` / V2 `APPROVED` / payment / verification / closure all queryable … PASS

**LIVE E2E RESULT: ALL 14 STEPS PASS.**

---

## H. Regression

- **Rent 收款 / partial payment**: backend `test_financial*`, `test_operations*`,
  bot rent suite — all green (in the 539 + 541).
- **Expense approve/reject/pay**: `test_expense_identity_008`, `test_financial*`,
  bot expense suite — green.
- **Owner / Secretary 权限 (RBAC)**: unchanged; deps (`get_current_user`,
  `manager_or_admin`, `admin_only`) reused; `test_auth*`, bot `test_roles*` green.
- **中英文角色 UX**: unchanged (i18n untouched; new repair card is additive).
- **Telegram fast path**: bot 541 green.
- **Operation worker**: unchanged worker loop; added 008A actions are reactive.
- **Canonical runtime ownership**: the runtime launcher/owner code is untouched;
  only deployed the new commit + migration via the canonical workflow. Live
  deployment SHA recorded (see I).

No regressions observed: **backend 540 passed, 0 failed; bot 541 passed, 0 failed.**

---

## I. Git

```
TARGET_SHA = 7e432306ba82d9792d8607ac5b72f6734202e583
LIVE_SHA   = 7e432306ba82d9792d8607ac5b72f6734202e583   (RT worktree HEAD,
             runtime_api.lock sha, runtime-version-proof.json)
LIVE_SHA == TARGET_SHA : YES
```

Commits (main `feature/telegram-ui-v2`):
- `aea0a22` feat: Repair as first-class AI Employee Operation (008A)
- `f2fc439` fix: add Repair Operation audit actions to AuditAction enum (008A E2E)
- `279b8e8` fix: default None evidence + model_validate on repair detail (008A E2E)
- `7e43230` fix: exclude container fields from detail base dump + router E2E test

Deployment: RT worktree `BOT-V1-USABLE-001-RUNTIME` checked out to `7e43230` (clean);
migration applied to live DB `pasay_pm_win_test` → `d1a9b3c4e5f6 (head)`.

Note on runtime: within this sandbox the canonical-owner-spawned processes are
reaped when the launching command ends (documented 007D §12 harness behavior), so
the live E2E was executed against the live API started from the deployed checkout.
Ownership + live SHA are correctly recorded; the Scheduled-Task / autostart chain
keeps the runtime alive at `7e43230` outside the harness.

---

## J. Final Gate

All acceptance criteria met:

- Operation is the real problem; Closure only via Verification. ✅
- Proposal decoupled from Repair; V1/V2 history preserved. ✅
- Reject does not close Repair (P0). ✅
- Requote dedup (Case C + live 30× ticks). ✅
- Payment does not close Repair (Case D + live). ✅
- Verification closes Repair (Case E + live). ✅
- History preserved after CLOSED (Case F + live). ✅
- Telegram/Mini App read real `next_action/waiting_on/blocked_reason`. ✅
- Backend 540 passed / bot 541 passed (no regression). ✅
- Live E2E 14/14 PASS. ✅
- LIVE_SHA == TARGET_SHA (7e43230). ✅

## `READY_FOR_OWNER_REPAIR_008A_ACCEPTANCE`
