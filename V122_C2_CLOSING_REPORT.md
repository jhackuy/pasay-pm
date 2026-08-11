# V1.2.2 Phase C2 — CLOSING REPORT: CONFIRMED ACTION COPILOT

> Date: 2026-08-11 · Author: Hermes (orchestrator + operator) · Principal engineer: Codex Max
> Commit: `ce1e528` (HEAD) · Tag: `v1.2.2-c2`
> Prior phase: C1.1 FAST UX ACCEPTED. This report closes C2 per the STOP CONDITION.

---

## Summary

C2 converts Copilot **recommendations** into **user-confirmed operational actions** for exactly three
allowlisted actions (`CREATE_FOLLOWUP_TASK`, `ASSIGN_TASK`, `SNOOZE_TASK`). It is **NOT**
autonomous: every mutation requires an explicit owner tap on a confirmation card. The finance wall is
absolute — no financial mutation is structurally reachable from the Copilot executor. Verified real:
a golden follow-up E2E (apt-due case → English secretary card delivered over real Telegram), a snooze
E2E, idempotent replay, and adversarial financial-wall rejection, all on the production DB.

**Test baselines (independent):** backend **279 passed / 4 deselected** (243 baseline + 36 new C2);
bot **167 passed** (150 baseline + 17 new C2). No regressions.

---

## A. Execution Architecture

```
LLM (WHY/ASK enrichment only)     backend NEVER lets an LLM write business state
   │  emits recommendation {code, refs, reason}   (no critical fields as free text)
   ▼
POST /operations/copilot/recommend  ── deterministic canonical builder (proposals.py) ──
   resolves assignee / due(Manila) / scope / target / idempotency-key backend-side
   ▼
CopilotActionProposal (PENDING) ── inline confirmation card (bot, owner zh)
   │  [✅ 确认安排][✏️ 修改][暂不处理]
   ▼
POST /proposals/{id}/confirm ── PENDING→CONFIRMED (revalidate, fail-closed)
   ▼
POST /proposals/{id}/execute ── execute.py ── EXECUTE-TIME revalidation (RBAC/TOCTOU/allowlist/
   payload/target stale/assignee eligibility) ── routes ONLY through existing operations service
   layer (create_operational_task → audit+outbox; assign → task update+outbox; snooze →
   snoozed_until + existing redelivery) ── status→EXECUTED, executed_at set
   ▼
notification_outbox ── notifier ── telegram (real send, retry/dedupe/audit)
   ▼
reconcile / scheduler continue tracking (same engine)
```

New backend modules: `app/services/copilot/execute.py`, `app/services/operations/proposals.py`;
public seam `generation.create_operational_task` (single write path); `FOLLOWUP` task_type;
env kill-switch `COPILOT_EXECUTION_ENABLED` (default false). The executor imports **no** financial
model/service (`execute.py` imports only operations models, generation, outbox, redelivery, audit).

## B. Action Allowlist

`app/services/operations/copilot.py::EXECUTABLE_ACTIONS = {"create_followup_task","assign_task","snooze_task"}`
— enforced at service + DB CHECK (`ck_copilot_action_proposals_action_type` now includes
`create_followup_task`). Legacy `create_task`/`follow_up` keep READ-side semantics and are **no-op
non-executable** (`_execute_noop`). Absent/confusable/financial/any-other codes are REJECTED
(fail closed, stable error_code, audit `copilot_proposal_rejected`). `COMPLETE_TASK`, `CANCEL_TASK`,
and all financial verbs are NOT executable (proven §J).

## C. Proposal Schema / Lifecycle

States: `PENDING → CONFIRMED → EXECUTED` (+ CANCELLED / EXPIRED). `EXECUTED ⟹ executed_at AND
confirmed_at set` (`assert_executed_invariant`). Schema (canonical, backend-resolved):
```
{ action, source_type, source_id|task_ref, reason_code(enum), assignee_user_id(backend-resolved),
  due_at(Manila ISO), display_context (render-only) , note (free text = DATA only) }
```
`display_context` is presentational; authority is in DB fields. `CopilotRecommendOut`/`CopilotExecuteOut`
never require the bot to display raw proposal id / internal enums / JSON / DB status.

## D. Execute-Time RBAC / TOCTOU

`execute_proposal` runs in ONE transaction: locks the proposal row (`FOR UPDATE`), revalidates
actor exists+active+manager/admin+owns proposal, revalidates action×target allowlist + payload
schema (current constants), re-resolves target (exists + in scope + business not stale), then
action-specific checks (assignee active/eligible, snooze window valid). Confirm-time state is
**never** authority — every check re-reads current DB state. Any failure → fail closed, stable
`error_code`, audit, no mutation. (Tests: `test_execute_rejections_return_structured_error_code`,
`test_revoked_rbac_demoted_actor_rejected`, `test_target_stale_*`, `test_target_deleted_*`,
`test_assignee_inactive_*`.)

## E. Idempotency / Concurrency Evidence

- **DB-level dedupe** on create (`uq_copilot_action_proposals_actor_idempotency`), on task
  (`uq_operational_tasks_active_dedupe` partial on PENDING), on outbox (`uq_notification_outbox_dedupe`).
- Conditional UPDATEs (`status=PENDING…`) make confirm/execute atomic w.r.t. concurrent confirm/cancel.
- **Production proof (real DB):** duplicate `recommend` on lease 3 → `created:false` (same
  proposal). Re-execute proposal → `replay:true`, `executed_at` unchanged, **no second task**.
  Exactly **1** FOLLOWUP task and **1** proposal exist for lease 3 despite multiple callbacks.
- Tests: `test_double_confirm_then_execute_once_single_effect`, `test_parallel_execute_single_logical_effect`,
  `test_callback_replay_bot_retry_at_most_one_task`.
- Honest caveat: network-layer exactly-once is impossible in distributed systems; we deliver
  DB deterministic idempotency + safe retry + audit reconciliation (§11).

## F. Multilingual UX (role-aware, not translation)

- **Owner (zh):** suggestion → confirmation card (房产/事项/负责人/截止 + "秘书将收到英文任务通知")
  → success; failure strings human (§I).
- **Secretary (en):** receives a **reorganized English card** via the outbox:
  ```
  📋 Follow-up Required
  Unit: 1203
  Issue: Rent overdue
  Note: Contact tenant… confirm expected payment date.
  Action: Contact tenant and confirm payment date.
  Due: 2026-08-12 01:00
  Tenant: Juan Dela Cruz
  ```
  (`execute._secretary_followup_message`, mirror for assign). The scheduler's real Chinese business
  tasks stay Chinese — the English override is **opt-in** (test:
  `test_scheduler_business_task_notification_stays_chinese`).

## G. Telegram Transcripts (real golden E2E)

Driven against production `@pasayhousebot` API token. Documented in §H/I. Key captured outcomes:
- `POST /recommend` followup on the real overdue lease 3 → proposal PENDING, card with resolved
  assignee/due.
- Confirm → CONFIRMED; Execute → EXECUTED, `task_id=3780` (FOLLOWUP, PENDING, due tomorrow 09:00
  Manila, inherits property/tenant/lease context).
- Outbox row → English secretary card(§F).
- **Real send to owner chat 5177241442 via `@pasayhousebot` → `SENT_OK message_id=53`.**

## H. Real Secretary Delivery

- Outbox row for follow-up was targeted at chat `1083657401` (secretary). First send returned
  Telegram **403 "bot can't initiate conversation with a user"** → the notifier retried (attempts=1,
  PENDING, backoff), proving §12/§18 "任务已建立,通知暂时失败,系统会自动重试" with real evidence.
  Root cause: `@pasayhousebot` cannot send an unsolicited DM until the secretary presses Start
  (Telegram policy) — a one-time onboarding prerequisite, **not** a code defect.
- **After the user confirmed the secretary pressed `/start`**, the real secretary delivery was
  re-verified end-to-end: fresh follow-up proposal → confirm → execute (task 6145) → outbox row → real
  notifier pass → `NOTIFIER_PASS {claimed:1, sent:1, retried:0, failed:0}`, outbox `SENT` with
  `telegram_message_id=68` to chat `1083657401`. The English secretary card (§F) reached the real
  secretary over `@pasayhousebot`. (Early hand-sent verification also returned `SECRETARY_SENT_OK
  message_id=67`.) Test artifacts cleaned; financial tables untouched.
- An earlier real send of the same English card to the owner chat (`5177241442`) returned
  `SENT_OK message_id=53`, confirming the delivery pipeline independently.

## I. Failure UX (human, never raw 409)

`_copilot_failure_text` maps error_code/detail → human strings: notify-retry → "任务已建立,通知暂时失败,
系统会自动重试"; already-executed/replay → "这个操作已经执行过了" (no second mutation); expired →
"操作已过期"; stale/target → "这个事项刚刚已经发生变化,我没有执行旧操作" + refresh. Verified in code
(callback.py) + test.

## J. Financial-Wall Adversarial Results (production)

| Attempt | Result |
|---|---|
| `action_type="confirm_income"` | **REJECTED** 422 "unknown action_type" (never a proposal) |
| `action_type="approve_expense"` | **REJECTED** 422 "unknown action_type" |
| `create_followup_task` → target_type=`income` | **REJECTED** 422 "may not target financial entity 'income'" |
| Generic-action smuggling (`note:"approve expense 123"`) | Only creates a **text task**; expense count unchanged (8→8, verified in DB) |
| Financial verb via any code | Structurally excluded (no financial service import in executor; allowlist at service+DB) |

Tests: `test_financial_action_verbs_rejected_no_creation`, `test_generic_action_financial_smuggling_text_task_only`,
`test_non_executable_action_rejected_at_execute`.

## K. Tests / Regression

- Backend `pytest tests/ -o addopts="-m 'not eval'"`: **279 passed / 4 deselected** (baseline 243
  all still green + 36 new C2). Bot: **167 passed** (150 baseline + 17 C2).
- C2 suite covers every §16 item: followup/assign/snooze success, outbox English payload, ambiguous
  assignee clarification, double/parallel confirm, expired, revoked RBAC, stale/deleted target,
  inactive/out-of-scope assignee, invalid snooze window, unknown/confusable/malformed payload,
  prompt injection, financial bypass + generic-action smuggling, callback replay, Telegram failure,
  **LLM-down → existing scheduler/reconcile/outbox unaffected** (`test_llm_down_scheduler_operations_unaffected`),
  kill-switch, endpoints. Alembic upgrade/downgrade verified on scratch DB.

## L. Audit Evidence (production)

- Proposal lifecycle: `copilot_proposal_created → confirmed → executing → executed` (actor_id=1,
  timestamps). Task: `task_created` (actor=confirming human). Snooze: `task_snoozed`. All in
  `audit_logs`. §15 satisfied (no CoT/secrets/raw credentials recorded).

## M. Mutation Evidence (production)

Real mutations produced and verified in DB then cleaned up: FOLLOWUP task 3780 (PENDING, FOLLOWUP,
lease 3, assignee 14, dedupe `followup:lease:3:RENT_OVERDUE`); snooze set `snoozed_until` +
`reminder_generation=1`; outbox row(s). **Exactly one logical effect per logical intent.** Test data
fully removed after (task 3780, proposals 3/6, outbox 16); **financial tables untouched**
(expenses stayed 8, incomes unchanged).

## N. Commit / Tag

- commits: `0f0bbd4` brief, `a531284` UX design, `d1cae87` backend executor, `62b4354` English
  secretary fix, `ce1e528` bot UX. Tag **`v1.2.2-c2`**.
- Deployed to `/opt/pasay-pm` (deploy-v12.sh), alembic `→c2a1b2c3d4e5` applied to prod PG, services
  restarted (api/bot/worker), `COPILOT_EXECUTION_ENABLED=true` set in prod `.env`. Live endpoints
  confirmed via `/openapi.json`: `/copilot/recommend`, `/copilot/proposals/{id}/execute`.

## O. Remaining Risks

1. ~~Secretary Start-on-bot onboarding~~ **RESOLVED.** The secretary pressed `/start` on
   `@pasayhousebot`; the real notification was delivered (outbox SENT, telegram_message_id=68).
   If the secretary ever starts a different notification bot, re-onboard them on that one.
2. `maria` (legacy referral agent, id 7) is **deactivated** (`is_active=false`) and is **NOT** a
   secretary — do not bind a telegram chat to it or treat it as the human secretary. The human
   secretary/operator identity is `pasay_bot_manager` (id 14, bound to `1083657401`). Monitor that
   `maria`'s ₱3,250 legacy settlement stays intact (it is a financial record, never deleted).
3. Execution kill-switch is now **on** in production (required for E2E). It remains a single-env
   flip to disable; monitor no autopilot regressions (C3 is out of scope until explicitly approved).
4. Snooze due-time default (`tomorrow_morning` → 17:00 +08 observed) is displayed exactly to the
   user in the confirmation card; confirm this matches product intent (vs. 09:00 +08) with the owner.
5. Financial-wall relies on the allowlist + executor not importing financial services; defense in
   depth only (schema + denylist are cosmetic). Any future executor change must keep this structural
   boundary.

---

## P. Identity & Routing Cleanup (final closeout, 2026-08-11)

Executed after the C2 sign-off per the canonical identity model. Commit `9d238d3`; tag `v1.2.2-c2-idcleanup`.

### Final identity model (verified in production DB)
| id | username        | role    | active | telegram | role |
|----|-----------------|---------|--------|----------|------|
| 1  | `admin`         | admin   | ✓ | `5177241442` | **Owner** |
| 14 | `pasay_bot_manager` | manager | ✓ | `1083657401` | **Secretary / Operator** (human channel) |
| 7  | `maria`         | agent   | **✗ deactivated** | — | legacy referral/commission identity |
| 12/13 | `dev_agent_maria`/`dev_agent_john` | agent | **✗ deactivated** | — | dev leftovers |

### What changed
- Deactivated `maria`(7), `dev_agent_maria`(12), `dev_agent_john`(13) via `is_active=false` (schema's
  clean inactivity mechanism). **No rows deleted** → financial/history preserved.
- Added `SECRETARY_ASSIGNEE_ID` (default 14, env `OPERATIONS_SECRETARY_ASSIGNEE`) and a deterministic
  fallback in `resolve_assignee`: with **zero** active agent candidates, C2 "安排秘书跟进" followup
  auto-resolves to the designated Secretary/Operator (14). Stale maria/dev no longer create
  artificial "multiple assignee candidates" ambiguity.

### maria legacy preservation evidence
- `commission_settlements` id 7 (agent 7, lease 3, **₱3,250 confirmed**) verified **unchanged**.
- User row kept (only `is_active` flipped). No financial row touched. **No financial mutation.**
- `expenses` count stayed **8**.

### DEV-user handling
- Deactivated (not hard-deleted) to preserve audit/commission references (2 settlements each).
- Historical terminal FAILED outbox rows (17/18, recipients `user:13`) left as-is — they are
  terminal (`FAILED`, max_attempts reached) and cause no runtime behavior; cleaned only via the DEV
  cleanup script if desired, not destructively now.

### Assignee candidate behavior
- **Before**: 3 active agents (maria + 2 dev) → "安排秘书跟进" returned `multiple assignee
  candidates; choose one` (artificial ambiguity).
- **After**: 0 active agents → deterministic fallback to Secretary/Operator (14). Verified on live
  production API: `POST /copilot/recommend` (followup, no assignee) → `assignee_user_id: 14`.

### Routing decision (requirement #4 — NO engine built)
- **Kept current safe behavior** for general proactive ops: `OPERATIONS_DEFAULT_ASSIGNEE` unset →
  business reminders still route to Owner (`admin`, id 1). No category-aware engine implemented.
- **C2 followup/assignment** uses the deterministic Secretary/Operator default (14) — this is the
  C2 UX's Secretary channel, distinct from general proactive routing.
- **Required C3 / V1.3 improvement (documented, NOT built):** a small deterministic routing map —
  operational/execution-oriented → Secretary(14); Owner-decision/financial-sensitive/approval →
  Owner(1); secretary-task overdue/escalated → secretary first then owner escalation (via the
  existing snooze/reconcile semantics). Financial mutation stays entirely in V1.1 safe flows.

### Regression
- Backend `pytest tests/ -o addopts="-m 'not eval'"`: **281 passed / 4 deselected** (279 + 2 new
  identity tests).
- Bot: **167 passed**. `hermes verify` Recipe: **OK**.
- Owner mapping (`admin`/5177241442) and secretary mapping (`pasay_bot_manager`/1083657401)
  **unchanged**. C2 confirmed-action flow re-verified live (proposal→confirm→execute→task→outbox,
  deterministic secretary assignee) then cleaned.

---

## Final Verdict

✅ **V1.2.2 PHASE C2 CONFIRMED ACTION COPILOT READY**

The three allowlisted actions execute only after explicit owner confirmation, route exclusively
through the existing operations service layer (single write path + audit + outbox), are idempotent
under concurrency/replay, deliver role-aware multilingual UX (owner zh / secretary en), and are
structurally incapable of any financial mutation — verified by 446 passing tests and real production
Telegram/DB evidence.

**STOP condition honored:** no autonomous (no-confirm) execution path, no financial automation, no
unattended business decisions. C3 is not entered.
