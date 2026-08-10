# V1.2.2 Ops Copilot — Phase A + B Engineering Brief (Codex Max)

> Author: Hermes (orchestrator) · Principal engineer: Codex Max

## 0. Working agreement

- You are the **principal engineer**. Implement Phase A (Reminder Safety Foundation) and
  Phase B (Deterministic Copilot Context) for the PASay-PM backend.
- **Do NOT begin Phase C (LLM integration)** — it is explicitly out of scope until the user
  accepts Phase A+B.
- **Financial safety is non-negotiable**: V1.1 income/expense/settlement state machines
  are the ONLY writers of financial tables. Your code must never create a second financial
  write path.
- Prefer KISS. No event bus / vector DB / RAG / complex AI infrastructure.
- DB source of truth. LLM (when it arrives in Phase C) must never write to DB directly.

## 1. Repo & baseline (verified by Hermes, 2026-08-10)

- Repo: `/Users/jhackuy/Documents/Codex/pasay-pm`
- HEAD `4290c148` tagged **v1.2.0** (PRODUCTION SEALED), branch `feature/telegram-ui-v2`, clean tree.
- Regression baseline: `pytest tests/ -q` → **154 passed** against real PostgreSQL
  (`pasay_pm_test` DB on postgres 16 at 127.0.0.1:5432).
- Production runtime mirrors source at `/opt/pasay-pm` (launchd: `ai.pasay.api`,
  `ai.pasay.operations-worker` running, `ai.pasay.postgres`, `ai.pasay.telegram-bot`).
  You work in the **dev tree**; Hermes handles deploy.
- Deploy uses `bin/deploy-v12.sh` (`rsync -a --delete`) which ALREADY excludes the canonical
  prod wrapper `bin/start-native-api.sh` (the dev tree holds only a shim). **Never re-introduce
  a path that overwrites /opt's canonical wrapper.** Keep that exclusion intact.

### Current core chain (already exists, do not break)
```
business data
→ operations worker (scheduler pass + notifier pass, independent DB transactions)
→ operational_tasks  (partial unique index uq_operational_tasks_active_dedupe: one PENDING per dedupe_key)
→ notification_outbox (uq_notification_outbox_dedupe unique; SKIP LOCKED claim; PENDING/SENT/FAILED/DROPPED; exp backoff)
→ Telegram (sendMessage via httpx; telegram_message_id stored)
→ complete / snooze / cancel (V1.2 router) + auto-complete / auto-cancel (reconcile)
```

### Key existing files you will build on
- `app/models/operations.py` — OperationalTask (has `remind_at`, `snoozed_until` cols already),
  RecurringRule, NotificationOutbox.
- `app/services/operations/scheduler.py` — `run_scheduler_once(db, now)` one transaction.
- `app/services/operations/worker.py` — `run_worker_once()`, loop, `--once` flag.
- `app/services/operations/outbox.py` — `enqueue_notification()` same-tx insert w/ ON CONFLICT.
- `app/services/operations/notifier.py` — `process_notifications_once()`, SKIP LOCKED claim,
  `TelegramSender.send()` → sendMessage.
- `app/services/operations/generation.py` — business-source task generation.
- `app/services/operations/reconcile.py` — auto-complete/cancel PENDING tasks by source state.
- `app/api/routers/operations.py` — `/operations/...` router, RBAC (`manager_or_admin`, agent scalar).
- `app/api/deps.py` — `get_current_user` (Bearer API key → User), `require_roles`, roles: admin/manager/agent.
- `app/services/audit.py` — `record_audit(db, table_name, record_id, action, actor_id, changed_fields, old_value, new_value)`.
- `tests/conftest.py` — real-PG fixtures on `pasay_pm_test`, per-test schema rebuild, admin/manager/agent fixtures.
- Alembic at `alembic/versions/` — head is `3c9a2f7b1e4d` (revises `1f1955f798cb`).

## 2. Phase A — Reminder Safety Foundation

**Gap today:** snooze only stores `snoozed_until`; the operations summary skips snoozed tasks,
but **nothing proactively redelivers a notification when a snoozed task's `snoozed_until` (or
`remind_at`) is reached**. Build the reliable delayed-reminder redelivery loop.

**Desired flow:**
```
task snoozed (DB stores snoozed_until)
→ worker pass detects snoozed_until reached (and task still PENDING, not superseded)
→ atomically create an idempotent notification_outbox row (same transaction)
→ notifier pass claims + sends via Telegram (existing retry/outbox path)
→ re-run worker = no duplicate
```

**Hard requirements (all must be satisfied, with real-PG tests):**
1. **DB-level dedupe**, never Python-memory. Reuse/strengthen the existing outbox
   `uq_notification_outbox_dedupe` unique constraint — a snooze-redelivery dedupe key must be
   unique so overlapping scheduler passes can only enqueue once.
2. **Worker restart-safe** — nothing in memory; next pass picks up state from DB.
3. **Multi-instance safe** — enqueue guarded by the unique index (not a SELECT-then-INSERT race).
4. **Repeated scheduler passes → no duplicate notification** (prove by running the pass twice).
5. **Telegram failure → existing retry/outbox** (PENDING → backoff → retry → FAILED at max). Do NOT
   bypass outbox.
6. **Repeated snoozes** — a re-snooze (new snoozed_until) must NOT allow an old reminder to fire at
   the wrong time. Design the dedupe key / state so only the **latest** snooze window produces a
   notification, and any previously-enqueued-but-unsent reminder for an older window is suppressed.
7. **complete / cancel / reconcile → no further reminder.** A task that is COMPLETED/CANCELLED, or
   that reconcile auto-transitions because its source no longer warrants a reminder, must not send.
8. **Every state change audited** via existing `record_audit` (actor_id=None for system).
9. **Real PostgreSQL concurrency tests** required (no SQLite as final proof).
10. **Must use the existing notification_outbox** — do not create a parallel delivery path.

Hints (you decide the cleanest KISS design within these constraints):
- Add a scheduler step "snooze redelivery scan" that selects PENDING tasks where
  `snoozed_until IS NOT NULL AND snoozed_until <= now(:utc)` (predictably ordered, bounded batch),
  and for each, atomically enqueues an outbox row with a dedupe_key that is **only valid for the
  current snooze window** (e.g. embed the specific `snoozed_until` value or snooze-generation counter),
  then clears `snoozed_until`. The unique index makes concurrent enqueues idempotent.
- Deciding whether/how to clear `snoozed_until` on the task vs. leave it must be reconciled with
  `operations_summary` (which skips tasks with future `snoozed_until`). Keep semantics consistent.
- Suppression on complete/cancel: because enqueue is only meaningful while the task is PENDING, gate
  the redelivery scan on `status == PENDING`, and let reconcile (already same-pass in the scheduler)
  settle the task before redelivery is considered; or add a guard so a task already COMPLETED/CANCELLED
  is never selected. Verify ordering to prevent a same-pass redelivery racing a reconcile.

### Phase A real Telegram E2E (Hermes runs this; you make it possible via `--once` + `--snooze-redeliver`
-able, or a test seam — `now` injection already exists on `run_scheduler_once`/`process_notifications_once`)
```
snooze a safe business/demo task (snoozed_until in the near past w/ test `now`)
→ worker pass with `now` past snoozed_until
→ real Telegram sendMessage HTTP 200
→ outbox row SENT
→ re-run worker pass with same `now` → 0 duplicate send
```
You need the test-seam `now` param threaded through the scheduler so Hermes can drive a
deterministic snooze-redelivery without waiting minutes.

## 3. Phase B — Deterministic Copilot Context (NO LLM DECISIONS YET)

Read-only endpoint, context builder only. No Copilot action may be executed in Phase A+B.

**Endpoint:** `GET /api/v1/operations/copilot/context` (under existing admin/manager RBAC — see req 4).
Builds structured JSON scoped to the current RBAC user, including at minimum:
- `current_time` + timezone
- user id / role
- pending operational tasks (scoped)
- overdue rents
- leases expiring
- pending expense approvals
- pending settlements
- maintenance / recurring tasks
- operational counts / totals (reuse `/operations/summary` semantics)
- relevant property + tenant summaries for the above
- source entity references

**Requirements:**
1. **Reuse existing service/query logic** (generation/reconcile/report queries, `rent_math`,
   `/operations/summary`, reports router). Do not duplicate business definitions.
2. Every fact must come from the DB (query live).
3. Context builder is **read-only** (only SELECTs; no writes outside optional audit of the `copilot_runs` row).
4. **RBAC at context-build time**: an agent sees only their own tasks; non-privileged users cannot
   enumerate properties/expenses/settlements they cannot access — enforced in code, NOT delegated to LLM.
5. Agent cannot see properties/tasks it lacks permission for via Copilot.
6. **Deterministic size cap + ordering rules** — cap list sizes (e.g. top-N per section by due_at/priority),
   stable ordering. Document the cap.
7. Do not pass API keys / tokens / internal secrets / unrelated PII.
8. Free text in data fields is **DATA**, never instruction (never inject into an LLM prompt as code —
   mark fields as data; this is a contract for Phase C).
9. Entity allowlist references for grounding:
   `property:{id}`, `lease:{id}`, `task:{id}`, `expense:{id}`, `income:{id}`, `settlement:{id}`.
10. Stable schema/version: include `context_schema_version = "1.0"`.

### New DB schema (KISS) — add one migration
Tables (create now, but **do not wire any execution** in Phase A+B):

`copilot_runs`
- id, actor_user_id, intent, context_snapshot JSONB, status, timestamps, created_at/updated_at
- used to log each context build (audit) — keep minimal.

`copilot_action_proposals` (required columns):
- `id`
- `actor_user_id` (FK users.id)
- `action_type` (string/enum)
- `target_type` (string: property|lease|task|expense|income|settlement|...)
- `target_id` (bigint)
- `payload_json` (JSONB)
- `status` — CHECK constraint over
  `PENDING | CONFIRMED | EXECUTED | CANCELLED | EXPIRED`
- `idempotency_key` — **UNIQUE** (DB-level dedupe; concurrent/duplicate submissions can only land once)
- `expires_at` (nullable timestamptz)
- `confirmed_at`, `executed_at` (nullable timestamptz)
- `created_at`, `updated_at`
- FK to users; audit mixin if it fits (or record_audit on transitions).

State transitions enforced (CHECK or app-layer + tests): only legitimate transitions allowed
(e.g. PENDING→CONFIRMED, PENDING→CANCELLED, CONFIRMED→EXECUTED, PENDING→EXPIRED). **Nothing may
transition to EXECUTED in Phase A+B** (execution is Phase C). Add a guard/flag so `executed_at`
cannot be set by any Phase A+B code path.

`copilot_runs` + `copilot_action_proposals` are the ONLY new business tables. No event bus.

### Action Safety Matrix (design targets; enforce structure now even before Phase C)
- **READ** (analyze/summarize/explain/risk-scan) → Copilot may eventually auto-execute.
- **OPERATIONAL** (create task / assign / snooze / follow-up) → proposal + explicit user confirm.
- **FINANCIAL** (income confirm/reverse, expense approve/reject/pay/reverse, settlement confirm,
  any financial write) → **V1.2.2 Copilot must never execute**; only route user into the existing
  V1.1 safe flow. Never a second financial write path.
Encode `action_type` allowlist + a safe/unsafe classifier table (or constants) so Phase C is
structurally prevented from financial mutation.

## 4. Tests — Codex Max must add (real-PG, no SQLite as proof)

Phase A:
1. concurrent reminder dispatch (multiple worker passes/instances → exactly one outbox row)
2. repeated scheduler pass → no duplicate
3. completed task reminder suppression
4. cancelled task reminder suppression
5. reconcile-suppressed reminder
6. repeated snooze → old window suppressed, only latest fires
7. Telegram failure → outbox retries then FAILED (mock sender raising), existing path intact

Phase B:
8. RBAC leakage — agent cannot see another agent's/manager's tasks/properties/settlements via context
9. hallucinated entity id in a proposal target → rejected by validation
10. invalid/missing target id → 422/validation
11. unknown action_type → rejected
12. duplicate confirmation (same idempotency_key twice) → only one CONFIRMED
13. expired proposal → cannot confirm, marked/treated EXPIRED
14. malformed JSON payload → rejected
15. `payload_json` must not introduce raw SQL / financial mutation bypass attempts
16. prompt-injection-like text stored in a task's notes/description must be treated as data
     (context builder returns it as data, never as instructions; no injection vector)
17. V1.2 operations regression (existing tests still green)
18. V1.1 financial regression (existing financial tests still green)

Plus a full migration **upgrade AND downgrade** test (follow existing
`test_alembic_migration_upgrade_downgrade` pattern in `tests/test_operations.py`).

Adversarial security review the specific vectors above explicitly.

## 5. Code style / conventions
- Match existing code: SQLAlchemy 2.0 `Mapped`/`mapped_column`, VARCHAR+CHECK enums via
  `pg_enum`, JSONB for payload/details, `record_audit` for every state change, Decimal for money,
  `datetime.now(timezone.utc)`. Timezone-aware timestamptz everywhere.
- Add a `context_schema_version = "1.0"` constant and document size caps.
- Docstrings like the existing modules.

## 6. Delivery format to Hermes
When done, provide in your final summary:
- Files added/changed
- New alembic revision id + up/down summary
- The `context_schema_version` + the exact size-cap / ordering rules you chose
- Full list of new tests (names) + `pytest tests/ -q` tail (expect all green, incl. migration)
- Any design decision you made that differs from the hints above and why
- Known remaining risks
Do the field work yourself: **run the tests** (real-PG), fix until green. Do not stop at "code written".

## 7. Explicitly OUT of scope (do not touch)
- Phase C LLM integration, any LLM call, any RAG/vector store.
- Any execution of Copilot actions (no EXECUTED transition, no `executed_at` set).
- Financial write logic beyond what exists.
- The canonical prod wrapper `/opt/pasay-pm/bin/start-native-api.sh` and `bin/deploy-v12.sh` behavior.
- Do not modify `bin/start-native-api.sh` in dev to do anything beyond forwarding.
