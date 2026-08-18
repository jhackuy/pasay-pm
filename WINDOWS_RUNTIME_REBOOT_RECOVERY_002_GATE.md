# WINDOWS-RUNTIME-REBOOT-RECOVERY-002 — READY_FOR_OWNER_REBOOT_ACCEPTANCE

Date: 2026-08-18
Workspace: D:\AI-Review\pasay-pm

## GATE RESULT: READY_FOR_OWNER_REBOOT_ACCEPTANCE

The production runtime is restored to exactly-one canonical units. The reboot
root cause and the two Phase C defects found by live acceptance are fixed and
committed. A future Windows reboot is expected to auto-recover API + Bot +
Worker with NO manual terminal.

> Phase C was re-opened twice by real Owner acceptance:
>   (1) Remind-Owner falsely reported "sent" without delivering (delivery truth).
>   (2) Owner reminder DM was notification-only (no actionable buttons).
> Both are now fixed, committed, tested and live-verified by the Owner.

---

## 1. Root Cause

After the 2026-08-18 reboot, the pre-reboot Telegram bot PID (7916) was
REUSED by an unrelated Windows system process (`ShellHost.exe`). The
pre-reboot `runtime_bot.lock` still recorded pid=7916 (`lifecycle: STARTING`).

`bin/pasay_runtime.py bootstrap` (the canonical owner) then hit its 007D
fail-closed branch: a lock whose pid is ALIVE but whose PEB identity is NOT a
`pasay_bot.main` process was treated as unrecoverable `UNOWNED_BOT`, and the
bootstrap `break`-ed — so the Bot was never started and the Worker (deferred
after bot) was never reached. The API survived independently and /health
stayed 200, which is why the menu was still visible but button clicks got no
replies (no poller).

Independent capture: `.runtime/acceptance/007c/007D_REBOOT_EVIDENCE.log` shows
`2026-08-18T09:30:03 census complete: ... readiness=FAILED`, and
`readiness.json` recorded `lifecycle=FAILED reason=UNOWNED_BOT`,
`bot: lock_pid=7916, alive=true, identity=mismatch`.

## 2. Changed Files

- `bin/pasay_runtime.py` — the fix (see #11). Committed as `a2f9292`.
- `tests/test_runtime_singleton_007d.py` — corrected T4a (PID-reuse → reclaim
  → READY) and T4b (live component elsewhere → still fail closed). Committed.
- `.runtime/acceptance/007c/reboot_collector.py` — evidence tooling corrected
  (ancestry-dedup for the codex-runtime interpreter subprocess; Telegram
  conflict scan limited to the recent log tail). Runtime-local, not tracked.

## 3. Final SHA

- Repo HEAD (canonical owner, the fixed file the launcher runs): `a2f9292902eebe2b94ab72600b6a86827d7370ae`
- Live runtime (deployed worktree that components run from): `bff46a6960aa0439ae276b33e609396985343085`

## 4. API

- PID (canonical owner / lock): 25840 (venv `python -m uvicorn app.main:app`)
- Port 127.0.0.1:8001 listener: exactly 1 (child pid 21228 of owner 25840)
- `/health` → HTTP 200 `{"status":"ok"}`
- OpenAPI identity: `PASay Property Management API` v1.0.0

## 5. Bot

- PID: 6484 (venv `pasay-telegram-bot\.venv\python -u -m pasay_bot.main`), exactly-one
- Polling: live `Updater:start_polling:polling_task` + single `update_fetcher`,
  monotonic `getUpdates` offset, NO Telegram 409 conflict in current window
- getMe: `@pasayhousebot` (is_bot=True)
- Started 2026-08-18 ~09:41/10:04 via canonical launcher

## 6. Worker

- PID: 37052 (`run-operations-worker.py --interval 60`), exactly-one
- 60-second cycles visible in `worker_runtime.log.err` (e.g. 09:41:27 → 09:42:27)

## 7. Canonical-owned evidence

`bin/pasay_runtime.py status`: api/bot/worker each `owned=True`,
`identity=ok`, `alive=True`, `readiness: lifecycle=READY reason=ready`.

## 8. unowned runtime = 0

- `pasay_runtime.py status`: no unowned component.
- reboot-collector `unowned_runtime_0 = True`.

## 9. Autostart configuration evidence (`Pasay Runtime Autostart`)

- Execute: `powershell.exe` `-NoProfile -NonInteractive -WindowStyle Hidden
  -ExecutionPolicy Bypass -File "D:\AI-Review\pasay-pm\bin\start-runtime.ps1"`
- WorkingDirectory: `D:\AI-Review\pasay-pm` (correct)
- Trigger: MSFT_TaskLogonTrigger, delay=PT20S, user CUNZHANG\Admin
- Principal: user Admin, LogonType=Interactive, RunLevel=Limited
- MultipleInstances=IgnoreNew, StartWhenAvailable=True, ExecutionTimeLimit=PT0S
- Chain: task → bin/start-runtime.ps1 → bin/pasay_runtime.py bootstrap
  (never directly uvicorn / pasay_bot / worker)
- LastRunTime 2026-08-18 09:29:03; earlier LastTaskResult=2 was the pre-fix
  owner exit(UNOWNED_BOT); with the fix the owner exits 0.

## 10. Four Telegram menu buttons — live evidence

Owner (user 5177241442) clicked in group -1004433994558; `.runtime/bot_runtime.log`:
- update_id=480912490 message_id=769 text=🏠 Properties → `[TRACE] button route=properties` → `render OK`
- update_id=480912491 message_id=771 text=✅ Tasks   → `[TRACE] button route=tasks`    → `quick_tasks api OK ... len=4` → `render OK`
- update_id=480912492 message_id=773 text=💰 Rent     → `[TRACE] button route=rent`     → `render OK`
- update_id=480912493 message_id=775 text=💸 Expense  → `[TRACE] button route=expense`  → `render OK`

For each: update received → router executed → response rendered/sent.

## 11. Post-reboot expected recovery mechanism

Fix in `bin/pasay_runtime.py` `_bootstrap()`: when a component lock records a
live PID whose identity is NOT the expected Pasay component:
- If NO live canonical <name> process exists anywhere (poll over all PIDs via
  Toolhelp snapshot + PEB cmdline identity) → the lock is STALE from post-reboot
  PID-reuse → reclaim it and start a fresh component. The unrelated PID is
  NEVER killed or adopted.
- If a live canonical <name> process exists on another PID → still fail closed
  (never start a duplicate poller → no Telegram 409), never mis-kill.

Thus at next reboot the scheduled task fires `start-runtime.ps1` →
`pasay_runtime.py bootstrap`, which reclaims any PID-reused stale locks and
starts exactly-one API + Bot + Worker with no manual terminal.

## 12. Verification snapshots

- Singleton test suites: 24 passed (007d + 007b)
- Stop/start recovery: `pasay_runtime.py stop` → all stopped, locks released,
  port free, no orphans; `start-runtime.ps1` → exactly-one restored, READY.
- Launcher idempotent: re-run skips owned components, exit 0, no duplicates.
- reboot-collector verdict: PASS (api/bot/worker exactly-1, canonical-owned,
  unowned_runtime_0, telegram_conflict_0, readiness READY).

---

## 13. Phase C follow-ups — Remind-Owner (2 real defects found by live acceptance)

### 13.1 Delivery-truth false success (commit `31c001b`)
Symptom: Owner clicked 🔔 Remind, bot said "Reminder sent to Owner", but the
Owner got nothing and the card/button did not flip.

Root cause: the Remind button on the approved-expense card was encoded WITHOUT
a nonce, so the idempotency key collapsed to `ik:rmo:{expense}:0`. After the
first-ever successful send settled it to `done`, every later click (incl. the
next PH day) hit the guard's `status=="done"` replay branch → printed "sent"
without delivering, without marking today, without flipping the button.

Fix:
- `keyboards.expense_open_keyboard`: Remind button gets a fresh per-render nonce.
- `store`: new `reminder_deliveries` table (PK expense_id+date) = persisted
  same-day gate AND delivery-truth record (target_user, destination, sent_at,
  message_id), written ONLY after `send_message` returns a confirmed message_id.
- `callback._handle_remind_owner`: daily gate reads the persisted record; a
  `done` replay truthfully answers "already reminded"; confirmed send logs real
  message_id; failed delivery persists nothing and does NOT consume the daily
  limit.

### 13.2 Actionable Owner reminder (commit `d15c8b9`)
Symptom: the Owner reminder DM delivered correctly but was notification-only
(no buttons), forcing the human to hunt for the Expense.

Fix: `keyboards.expense_reminder_actions(status, expense_id, locale)` renders
STATE-DRIVEN buttons derived from the CURRENT Expense Operation `next_action`,
reusing the EXACT existing deterministic callbacks:
- pending (Owner approval) → [✅ Approve][❌ Reject][🔎 View]  (`exa`/`exr`/`exd`)
- approved (waiting payment) → [✅ Paid][🔎 View]                 (`exp`/`exd`)
- paid/rejected/reversed → None (no actionable reminder)

`_handle_remind_owner` attaches these to the Owner DM via `reply_markup`. Because
they reuse `_handle_expense_pay`/`_handle_expense_approve`/`_handle_expense_detail`
they inherit: existing authorization (Owner-only), IdempotencyGuard, backend
re-read at tap time (stale reminders refused), and the shared Expense state
machine. Reminder delivery alone never changes Expense state.

### 13.3 Phase C targeted tests
`pasay-telegram-bot/tests/test_remind_delivery_truth.py` — 16 tests covering
both fixes: success/failure/exception, same-day dedup, next-day re-allowed,
missing destination, business-state unchanged, restart survival, and the 7
actionable-reminder tests (approved→Paid+View; pending→Approve/Reject/View;
closed→None; Paid routes to existing handler; unauthorized blocked; stale safe;
action progresses same Expense). All related bot suites green.

### 13.4 Live acceptance evidence
- Delivery persisted: `reminder_deliveries` row for E7/2026-08-18 =
  target 5177241442, destination 5177241442, sent_at 11:45, message_id 719
  (a real Telegram-returned id ⇒ confirmed delivery).
- Owner confirmed: "DM 有 Paid + View 按钮，View Expense 正常工作；Paid 未执行，
  因为不想修改真实 E7 付款状态。" (DM has Paid + View; View works; Paid not tapped
  to keep the real E7 unchanged).
- E7 live status remains `approved` (Waiting for payment), amount 7000.
- Runtime restarted at live SHA `d15c8b9`; API/Bot/Worker exactly-one READY.
