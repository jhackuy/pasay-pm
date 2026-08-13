# WF Guardrails (UX-ACCEPTANCE-FREEZE-AND-GUARDRAILS-001)

Deterministic workflow guardrails, implemented in `wf_guardrails.py` and
covered by `wf006_tests.py`. Program First, LLM Last: every check below is a
static scan, an exit code, a process/log inspection or a workspace hash - no
LLM is called. These guardrails do not refactor the Telegram bot and do not
change business UX.

## G1 - Platform semantics enter the test double

Rule: when a real incident is caused by Telegram / PostgreSQL / HTTP API /
third-party platform semantics, the fix must also lock the semantics in through
at least one of:

- Fake / Stub semantic enforcement
- Contract test
- Deterministic integration test

Frozen Telegram rule (OWNER-UX-FAILURE-LIVE-TRACE-001): a message sent with a
non-inline `ReplyKeyboardMarkup` is **not editable** in real Telegram (400
`Message can't be edited`). FakeBot must keep mirroring that: `edit_message_text`
on such a message raises `BadRequest("Message can't be edited")`. Deleting this
semantic "to make tests pass" fails the `guardrails` scan and the bot test
`test_fakebot_mirrors_telegram_reply_keyboard_edit_semantics`.

Enforcement: `scan_platform_semantics()` (static) + bot unit test (behavior).

## G2 - No silent exception swallowing in Telegram user-visible paths

Scope: fixed menu, inline button, callback, approval/reject, message send/edit,
NL routing final reply. User-visible interaction paths must never silently
swallow exceptions; at minimum they need structured logging or a user-visible
fallback. `except: pass` and `except ...: pass` (including bare `except`) are
scan failures unless explicitly allowlisted with a written reason.

Enforcement: `scan_silent_exceptions()` over
`pasay-telegram-bot/pasay_bot/handlers/` with
`SILENT_EXCEPTION_ALLOWLIST` (each entry carries a reason; line numbers are
pinned, so moving code without re-review fails the scan).

## G3 - READY_FOR_OWNER_UX_RETEST gate

```text
READY_FOR_OWNER_UX_RETEST =
    TARGETED_TEST_PASS
    AND FAILURE_REGRESSION_PROVEN
    AND LIVE_VERSION_MATCH
    AND RUNTIME_HEALTHY
```

- `TARGETED_TEST_PASS`: only change-related tests run and pass (exit 0).
- `FAILURE_REGRESSION_PROVEN`: for a real bug, a regression test or
  deterministic reproducer that stably reproduces the old failure exists and
  passes.
- `LIVE_VERSION_MATCH`: the running Runtime loads the target acceptance code
  (live workspace SHA == target workspace SHA).
- `RUNTIME_HEALTHY`: single polling instance, `getMe OK`, no 409 Conflict, and
  backend health OK when the feature depends on the backend.

Any missing condition => `READY_FOR_OWNER_UX_RETEST=NO`, and the Owner must not
be invited to test.

Enforcement: `owner_ux_gate()` / `wf_ctl.py owner-ux-gate --task-json ...`.

## G4 - Command timeout enforcement

The workflow runner/command wrapper executes every command with a programmatic
timeout - never a prompt reminder:

- ordinary shell / PowerShell: 60s
- git / status / hash / process checks: 60s
- targeted tests: 180s (task may override explicitly)
- long-running tasks must declare an explicit `timeout_seconds`
- LLM workers keep their existing independent timeout policy

On timeout the wrapper returns an explicit `TIMEOUT`, records the command and
elapsed time, terminates the child process tree, never waits forever and never
auto-retries indefinitely.

Enforcement: `wf.run_timed()` (used by `wf.sh()` and `wf_ctl.py timed-run`);
proven by `wf006_tests.py` TEST-4/TEST-5 with a 1s deliberately-hanging command.
