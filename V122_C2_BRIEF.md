# V1.2.2 Phase C2 — CONFIRMED ACTION COPILOT Engineering Brief (Codex Max)

> Author: Hermes (orchestrator) · Principal engineer: Codex Max · Date: 2026-08-11
> Context: C1.1 FAST UX ACCEPTED. Baseline green (backend 243 passed / 4 deselected, bot 150 passed,
> PG alive on 127.0.0.1:5432). This phase converts Copilot *recommendations* into *user-confirmed
> operational actions*. **NOT autonomous — every mutation requires explicit human confirmation.**
>
> Read these first (do NOT re-derive from scratch): `app/services/operations/copilot.py` (proposal
> lifecycle + confirm-time revalidation already built), `app/services/operations/generation.py`
> (`_register_task` atomic create+audit+outbox), `app/services/operations/scheduler.py` /
> `redelivery.py` / `notifier.py` / `outbox.py`, `app/models/operations.py`, `app/models/copilot.py`,
> `app/api/routers/operations.py` (proposal + ops endpoints), `app/services/copilot/llm.py`
> (provider profile map), `pasay-telegram-bot/pasay_bot/keyboards.py` + `render/cards.py` +
> `render/i18n.py` + `handlers/callback.py` + `roles.py`.

---

## 0. HARD CONSTRAINTS (binding, reiterate from C1)

1. **HUMAN-CONFIRMED ONLY.** NO autopilot. NO AI-created/assigned/snoozed mutation without an
   explicit owner tap on a confirmation card. §19 in the task brief.
2. **FINANCIAL WALL is absolute.** The Copilot execution surface must be *structurally incapable* of
   any financial mutation (income confirm/reverse, expense approve/reject/pay/reverse, settlement
   confirm). This is enforced by architecture (allowlist + no financial service reachable from the
   executor), not by lexical denylist. See §14.
3. **Reuse the existing operations task service layer.** The Copilot executor MUST NOT write
   `operational_tasks` directly. It must call into the existing service/transactional helpers
   (task create + audit + outbox in ONE tx), the existing snooze redelivery, the existing outbox.
   **No second write path.**
4. **LLM proposes, backend resolves, backend validates at EXECUTE time.** proposal-creation-time state
   is NEVER trusted at execute time. §0 PREFLIGHT in the task brief.
5. **At most one logical business effect** per proposal/callback — DB deterministic idempotency +
   safe retry + audit reconciliation. §11.
6. **KISS.** ~14-unit portfolio. No distributed cache, no new infra. Reuse everything that exists.
7. **Don't break committed C1/A+B contracts** unless a real regression proves a bug.
8. **No raw LLM→DB.** Natural language intent → deterministic intent parse → backend canonical
   proposal → confirmation card → existing service executes. §13.
9. **Fail closed.** Any execute-time validation failure → stable `error_code`, no mutation, audit
   rejection. Never fabricate success.

---

## 1. C2 MVP ACTION ALLOWLIST (ONLY these three; everything else REJECT)

Service-layer `ACTION_SAFETY` in `app/services/operations/copilot.py` currently lists:
`summarize, analyze, explain, risk_scan, create_task, assign_task, snooze_task, follow_up`.
The DB CHECK (`app/models/copilot.py::COPILOT_ACTION_TYPES`) matches the same list.

**Required change:** Introduce the canonical C2 action code `create_followup_task` so the
executor allowlist is EXACTLY:

```
CREATE_FOLLOWUP_TASK   (alias in code: "create_followup_task")
ASSIGN_TASK            ("assign_task")
SNOOZE_TASK            ("snooze_task")
```

- `READ_ACTIONS` (summarize/analyze/explain/risk_scan) stay read-only, never executable.
- `follow_up` and `create_task` may keep existing READ-side semantics but must NOT be executable —
  the only mutable codes are the three above. Decide cleanly: either rename/alias so
  `create_followup_task` is the executable one and `create_task`/`follow_up` are read-only helpers,
  OR make `create_followup_task` an alias that maps into `follow_up`. **Keep the DB CHECK in sync**
  (it's a hard guard). Document the mapping in the module docstring.
- **ABSENT action codes (including any financial verb, COMPLETE_TASK, CANCEL_TASK, confirm/reverse,
  approve/reject/pay/settle, unknown strings, and confusable/Unicode variants) → REJECT** with a
  stable `error_code` and audit `copilot_proposal_rejected`. The existing `canonicalize()` (NFC +
  invisible-char strip) is the first gate.

---

## 2. CANONICAL PROPOSAL BUILDER (backend-resolves all critical fields)

The LLM only emits a **recommendation**: `{recommended_action_code, target_refs[], reason}` (no free
text for critical fields). A new **deterministic builder** constructs the canonical proposal.

New module: `app/services/operations/proposals.py` (or extend `copilot.py` — your call, but keep the
existing `create_proposal` contract). API — one builder per action:

- `build_followup_proposal(db, actor, *, source_ref, reason_code, assignee_user_id=None, due_at=None, ...)`
  → resolves current target (lease/task/property/etc.), **resolves assignee** (default secretary per
  property/group OR unique reasonable candidate OR None→ask), resolves due time (Manila-aware default),
  builds a **strict payload schema** (see §3), and calls `create_proposal` with a
  deterministic `idempotency_key` (e.g. `f"followup:{target_type}:{target_id}:{reason_code}:{actor.id}"`).
- `build_assign_proposal(db, actor, *, task_ref, assignee_user_id*)` — assignee fully resolved
  (see §8), payload carries `{assignee_user_id}` (backend-resolved, never LLM-free-text).
- `build_snooze_proposal(db, actor, *, task_ref, until*)` — `until` is a **Manila-aware target
  time**; if the user gave no time, use the product default but the UI MUST show the exact resolved
  value in the confirmation card (§9) — never hide a guessed time.

**Critical fields must NEVER come from LLM free text.** `actor_user_id`, `assignee_user_id`,
`target_id`, property scope, financial amount, status transition — all resolved by backend. If entity
resolution is ambiguous ("which unit?"), the builder returns a structured `NEEDS_CLARIFICATION` and
the UX presents buttons — never guesses.

**Property scope:** a follow-up on an overdue lease inherits `property_id`/`tenant_id`/`lease_id`
from the target lease deterministically. If a property/group has a default secretary (roll of an
`assignee` default), pre-fill it.

Payload schema (strict, enum-validated, POST-validated at create AND re-validated at confirm AND at
execute). Example canonical payload for FOLLOWUP (values in `assets` terms):
```
call "create_followup_task" payload = {
  "action": "create_followup_task",         # echoed, must match action_type
  "source_type": "lease",                    # from DB, not LLM
  "source_id": 123,
  "reason_code": "RENT_OVERDUE",             # enum
  "assignee_user_id": 8,                    # backend-resolved
  "due_at": "2026-08-11T17:00:00+08:00",    # Manila-aware, backend-resolved
  "display_context": { "unit": "1608", "tenant": "Ana P.", "amount": "85000.00", ... }  # for rendering only, never authoritative
}
```
`display_context` is presentational; all authority is in the DB fields. **A follow-up task must never
carry a financial mutation.** Only ever creates a text/tracking task.

---

## 3. PAYLOAD STRICT SCHEMA + DENYLIST (extend, keep defense-in-depth as SECONDARY)

Reuse `_validate_payload` in `copilot.py`. Extend `PAYLOAD_DENYLIST_KEYS` if needed so a key like
`approve`, `confirm_income`, `financial`, `reverse`, `settle`, `complete`, `cancel`, `status_transition`
cannot place a financial/irreversible verb into a FOLLOWUP payload. Remember: the denylist is the
cosmetic guard; the REAL boundary is the structured schema + enum allowlist + parameterized backend
service the executor calls (no raw DB, no financial service).

**Adversarial rule (§14):** a malicious `{action: "create_followup_task", note: "approve expense 123"}`
must only create a text task, NEVER mutate the expense. Wire a test proving this.

---

## 4. COPILOT EXECUTE SERVICE — THE CORE NEW PIECE

New module: `app/services/copilot/execute.py` (an executor for the 3 allowed actions). Contracts:

- `execute_proposal(db, *, actor, proposal_id, now=None) -> CopilotActionProposal` — **CONFIRMED -> EXECUTED**
  in ONE transaction, with EXECUTE-TIME revalidation (NOT trusting the confirm that happened earlier).
- Flip `COPILOT_EXECUTION_ENABLED` in `copilot.py` to `True` (C2 authorizes it) but **keep every
  execution path gated through it** as a belt-and-suspenders guard. Also keep the `_guard_execution_disabled`
  semantics (now it must assert EXECUTION IS ENABLED).
- Execute-time validation (must all pass in the SAME transaction, fail closed):
  1. Re-lock the proposal row `SELECT ... FOR UPDATE`; must be exactly `CONFIRMED` (idempotent replay
     if already EXECUTED → return existing, no second effect; reject CANCELLED/EXPIRED/PENDING).
  2. Re-validate actor exists + active + manager/admin + owns the proposal (reuse `_revalidate_proposal_for_confirm`
     logic or a focused execute variant — do not assume the confirm still holds).
  3. Re-validate action×target allowlist + payload schema (current constants).
  4. Re-resolve target; must still exist + still in-scope + business not stale (reuse `_target_in_actor_scope`
     + `_business_stale_reason`).
  5. For ASSIGN: current `task.status` still PENDING, assignee active + eligible (see §8).
  6. For SNOOZE: task still PENDING, snooze window still valid.
- **Finally route to the EXISTING service layer**:
  - `CREATE_FOLLOWUP_TASK` → `generation._register_task` (or a thin public wrapper
    `create_operational_task` that does create+audit+outbox atomically; do not silently call a private
    underscore function if you prefer a public seam — add one small public helper in `generation.py`)
    with `task_type` = one of the existing enum values appropriate to the source (e.g. `RENT_OVERDUE`
    follow-up → a NEW generic `FOLLOWUP` type is best if allowed, OR reuse `AC_MAINTENANCE`… no — add a
    **new `FOLLOWUP` task_type** to `OperationalTaskType` + the `ck_operational_tasks_task_type` CHECK +
    the `RecurringRule` CHECK if symmetric; keep the DB enum in sync). Resource: a manual follow-up task
    has `status=PENDING`, `assigned_user_id=resolved_assignee`, `due_at=resolved`, `details` from payload,
    `dedupe_key` maybe null (manual) or `followup:{source}:{source_id}:{reason}` if you want one-active-followup
    dedupe.
  - `ASSIGN_TASK` → update `task.assigned_user_id`, bump `reminder_generation` if needed, enqueue a
    notification-outbox row to the NEW assignee (secretary English card) — reuse `_enqueue_for_task` /
    `enqueue_notification`.
  - `SNOOZE_TASK` → reuse the existing snooze plumbing: set `task.snoozed_until` to the resolved
    Manila-aware `until`, bump `reminder_generation`, and rely on the existing `redeliver_due_snoozes`
    → outbox → notifier so the due reminder fires on schedule. Do NOT create a second reminder path.
- **Mark EXECUTED with `executed_at`** and record `copilot_proposal_executing` + `copilot_proposal_executed`
  audit rows. On business-service conflict (e.g. task already completed), keep the proposal in an
  explainable state, record `execution_error_code`, do NOT re-run. Keep `assert_executed_invariant`
  satisfied (EXECUTED ⟹ executed_at + confirmed_at set).
- **Concurrency/crash semantics (§11):** the DB unique indices (`uq_copilot_action_proposals_actor_idempotency`
  on create; partial `uq_operational_tasks_active_dedupe` on task; `uq_notification_outbox_dedupe` on
  outbox) plus conditional UPDATEs give deterministic at-most-once mutation. A service succeeds but the
  HTTP/Telegram response crashes → proposal is EXECUTED but owner may not have seen it; the outbox
  carries the secretary notification. "Task created / notification retrying" (§12, §18) — never claim
  all-failed.

---

## 5. EXECUTION ENABLE MODE (safe default + explicit on/off)

Add an env switch `COPILOT_EXECUTION_ENABLED` (default **false** in `.env.example`, gated in
`app/config.py`): the executor refuses (fail closed) unless enabled. C2 ships it **enabled in the
running deployment** after tests pass, but the flag stays a kill-switch. Keep `COPILOT_EXECUTION_ENABLED`
in `copilot.py` in sync with the env so there is one source of truth (env wins; module default matches
`.env.example`).

---

## 6. RBAC / TOCTOU (execute-time) — §D final report

- Reuse the existing `manager_or_admin` dependency on all execution endpoints.
- Every execute step re-reads CURRENT state within the SAME transaction (no stale reads). The
  confirm-time pass is NOT authority; execute revalidates. Any diff → fail closed with a stable code.
- Assignee eligibility: SECRETARY/manager/admin candidates only; must be active; must be in the
  resolvable allow-range (never a raw user id from the LLM). Use deterministic/default selection.

---

## 7. API ROUTER EXTENSIONS (`app/api/routers/operations.py`)

Keep existing `/copilot/context`, `/today`, `/why`, `/ask`, `/copilot/proposals`, `/confirm`, `/cancel`.
Add:

- `POST /copilot/proposals/{id}/execute` → calls `execute.py::execute_proposal`. Response returns the
  proposal (now EXECUTED) plus a **rendering-friendly result block** (`{action_type, target_type,
  target_id, task_id, assignee, due_at, executed_at, status, replay:bool, detail}`) — NO proposal id /
  internal enums leak to the bot UI, but the API returns them for the bot to render role-aware text.
  Structured 409 for fail-closed rejections (same `{message,error_code}` shape as confirm).
- `POST /copilot/recommend` (canonical proposal builder endpoint) — the bot posts an intent +
  resolved refs; backend returns the canonical PENDING proposal card data + `proposal_id` (used
  internally; bot must NOT display the raw id). This is where intent→canonical-proposal happens (§13).
- Snooze presets already exist (`operations.py::_resolve_snooze_until`); reuse for SNOOZE_TASK.

No financial endpoint touches the copilot executor.

---

## 8. ASSIGN_TASK resolution (§8)

- Current actor must have manager/admin (already the dependency).
- `task.status == PENDING` (revalidate at execute).
- Assignee candidates = active users with SECRETARY (agent) or manager/admin role, scoped to the
  task's property/group when a default secretary exists. LLM says "交给秘书" → backend picks the
  deterministic default (property default secretary first, else an unambiguous unique candidate, else
  `NEEDS_CLARIFICATION`). **Never let the LLM type a user id.**

---

## 9. SNOOZE_TASK (§9)

- Reuse the existing snooze presets + redelivery mechanism. Manila-aware resolution of "tomorrow" →
  product default time (e.g. 09:00) unless the user gave a time; the confirmation card MUST show the
  exact resolved value (e.g. "明天 9:00 AM"), never a hidden guess.

---

## 10. AUDIT (§15)

Record (reuse `record_audit`): `copilot_proposal_created *modified *confirmed *execution_started
*executed *rejected *cancelled *expired`. Include actor, action_type, target, result, idempotency,
timestamp. Never CoT/secrets/raw credentials. Safe structured reason only.

---

## 11. BOT / TELEGRAM UX (Hermes owns render + callbacks; but you provide the API shape the bot calls)

Task brief §3 UX is authoritative: suggestion card → [安排秘书跟进][明天再提醒][暂不处理] → confirm card
(inline keyboard, no form-filling) → [✅ 确认安排][✏️ 修改][取消] → success + role-aware messages.

Owner (zh): conclusion/risk/decision. Secretary (en): Action/Property/Tenant/Deadline/Done —
**reorganized per receiver role, never a literal translation** (§5).

Callbacks: add `ACTION_COPILOT_SUGGEST`, `ACTION_COPILOT_CONFIRM`, `ACTION_COPILOT_EDIT`,
`ACTION_COPILOT_DECLINE`, `ACTION_COPILOT_SNOOZE_PICK`, `ACTION_COPILOT_ASSIGNEE_PICK` in
`keyboards.py`; wire in `handlers/callback.py`. Reuse `roles.locale_for` for role-aware language.
Failure UX (§18): "这个事项刚刚已经发生变化" / "任务已建立，通知正在重试" / "这个操作已经执行过了" — human
readable, never `409 STALE_TARGET`.

You implement the API + backend shape; Hermes wires the bot render/callbacks. Coordinate the exact
request/response JSON of `/copilot/recommend` and `/copilot/proposals/{id}/execute` so the bot can
consume them. Put the response schema in a shared place both sides import (e.g. a small
`CopilotExecuteOut`/`CopilotRecommendOut` in `app/schemas/` + mirror dataclasses in the bot's `api_client.py`).

---

## 12. TESTS (real PostgreSQL — reuse `tests/conftest.py`)

Add `tests/test_copilot_c2.py` covering at minimum (task §16):

- create_followup_task success (task created exactly once logically; outbox row for secretary; audit rows)
- assign_task success (reassign + outbox to new assignee)
- snooze_task success (snoozed_until set; due reminder via existing redelivery)
- double confirm / parallel confirm → one logical effect (proposal CONFIRMED once; then execute once)
- expired proposal → fails closed
- revoked RBAC (actor demoted before execute → reject)
- target stale (task completed before execute → reject, no mutation)
- target deleted → reject
- assignee inactive / out-of-scope → reject
- unknown action / malformed payload → reject
- Unicode / confusable action (zero-width, NFC-bypass) → reject
- prompt injection in note → no mutation, text task only
- **financial action bypass**: income confirm / expense approve / reverse / settlement as action → REJECTED (no creation)
- **generic-action financial smuggling**: `{action: create_followup_task, note: "approve expense 123"}`
  → only creates text task; expense unchanged
- callback replay / bot retry → at most one logical task
- Telegram failure → task created, outbox PENDING (retry)
- LLM provider failure / LLM malformed recommendation → execute path unaffected; recommend path fails
  closed or falls back deterministically
- **LLM down → existing operations (scheduler/reconcile/outbox) unaffected** (§16 last line + prove it)

Also keep `test_copilot_phase_ab.py` green (don't regress the proposal lifecycle).

---

## 13. DELIVERABLES (files/commit) — N

1. `app/schemas/copilot.py` — add `CopilotExecuteOut`, `CopilotRecommendIn/Out` (render-safe).
2. `app/services/copilot/execute.py` — executor. NEW.
3. `app/services/operations/proposals.py` (or extend copilot.py) — canonical builder. NEW.
4. `app/services/operations/generation.py` — public `create_operational_task` seam if needed;
   add `FOLLOWUP` task_type to model + CHECKs.
5. `app/services/operations/copilot.py` — flip execution enabled (env-sync), tighten allowlist,
   keep all existing lifecycle; add execute-time helper that non-executors keep using.
6. `app/api/routers/operations.py` — add `/copilot/recommend`, `/copilot/proposals/{id}/execute`.
7. `app/main.py` / `app/config.py` — env `COPILOT_EXECUTION_ENABLED`.
8. `alembic/` — migration for any new task_type / new columns (e.g. `FOLLOWUP` enum member) +
   proposal columns if needed (`execution_error_code` — only if schema requires; else use audit per KISS).
9. `tests/test_copilot_c2.py`.
10. `docs: V122_C2_BRIEF.md` (this file kept).

Commit message style (repo uses): `feat(copilot): V1.2.2 Phase C2 — confirmed action executor ...`.

---

## 14. STOP CONDITION

Implement the backend so that after your work: `pytest tests/ -q -o addopts="-m 'not eval'"` is green,
the new C2 tests pass, the existing proposal lifecycle tests stay green, and the executor is
provably unable to touch financial state. Bot work (UX) is a follow-on I orchestrate; your brief ends
at a clean API + tests. Do NOT implement any autonomous (no-confirm) path.

**NO C3.** No autonomous actions / financial automation / unattended decisions after C2.
