# PASAY-OPENDESIGN-AUTO-DISPATCH-001 — GitHub to OpenDesign Dispatcher

## What it does

Event-driven handoff from a GitHub Issue (`route:design-dev`) to the
local OpenDesign daemon. The dispatcher refuses to call OpenDesign
unless every gate below passes. It does NOT use scheduled polling, a
second database, UI automation, or a separate Issue system.

## Verified OpenDesign 0.19.2 capability proof

The capability proof was done on the actual Windows / OpenDesign
machine that runs this repository:

| Probe | Result |
| --- | --- |
| `node apps/daemon/bin/od.mjs --help` | Full help printed; subcommands include `automation`, `mcp`, `tools`, `connectors`, `plugin`, `share`, `chat`, `export`, `media`. |
| `GET http://127.0.0.1:7456/api/version` | `200 {"version":{"version":"0.19.2","channel":"development", ...}}` |
| `GET http://127.0.0.1:7456/api/health` | `200 {"ok":true,"version":"0.19.2"}` |
| `GET http://127.0.0.1:7456/api/ready` | `200 {"ok":true,"ready":true,"version":"0.19.2"}` |
| `od automation source ingest --source-kind upload --title ... --body-file - --json` | `200` ingest returns `{packet:{id: "packet_..."}}` against the running daemon. |
| `od automation create --name ... --prompt ... --schedule hourly:0 --target new-project --json` | Returns `{routine:{id: "routine_..."}}`. |
| `od automation run <id>` | Returns tab-separated `projectId / conversationId / agentRunId`. |
| `od automation runs <id> --limit 1` | Returns run history including status. |

There is NO documented `/api/opendesign/dispatch` HTTP endpoint in
OpenDesign 0.19.2. The `od` CLI is the only verified non-UI entry
point.

## Trigger contract

A single `issue_comment` event triggers dispatch **only when ALL** of
the following hold:

| Gate | Source | Required |
| --- | --- | --- |
| Action | `event.action` | `created` |
| Repository | `event.repository.full_name` | matches `$OD_EXPECTED_REPO` (default `jhackuy/pasay-pm`) |
| Is-issue | `event.issue.pull_request` | FALSY (PR conversation comments rejected) |
| Issue state | `event.issue.state` | `open` |
| Route label | `event.issue.labels` | contains `route:design-dev` |
| Approval marker | `event.comment.body` | contains exact marker `OWNER_APPROVED_FOR_OPENDESIGN` (case-sensitive) |
| Actor allowlist | `event.sender.login` | case-insensitive match against `$OD_OWNER_ALLOWLIST` |
| Idempotency | persisted Issue comments | no `DISPATCHED` record for the same `dispatch_id` within 24h, AND every persisted record parses cleanly (no FAIL_CLOSED) |

Any failed gate produces a structured verdict and status:
- Non-fatal gate failure (action, repo, is-issue, route, approval,
  idempotency DUPLICATE) -> `NO_DISPATCH`
- Actor or issue_state failure -> `BLOCKED_FOR_PRODUCT_DECISION`
- Persisted history unavailable or unparseable -> `BLOCKED_FOR_PRODUCT_DECISION`
- `APPROVED_NOT_DISPATCHED` is reserved for the brief moment after
  all gates pass but before the transport has returned; it is the
  intermediate writeback entry, not the final state.

## Status state machine

```
   APPROVED_NOT_DISPATCHED   (all gates passed; transport running)
            |
            v
       DISPATCHED             (OpenDesign accepted; ack returned)
            |
            v
       DESIGN_ACCEPTED         (Owner accepts; can flip to ready-for-dev)

  DISPATCH_FAILED             (transport rejected or raised)
  BLOCKED_FOR_PRODUCT_DECISION (Owner must act)
  NO_DISPATCH                 (non-fatal gate failed)
```

## Real production transport: `od` CLI via Windows self-hosted runner

OpenDesign 0.19.2 only exposes design execution through the `od` CLI
against the local daemon. The dispatcher therefore requires:

- A Windows GitHub self-hosted runner registered to `jhackuy/pasay-pm`.
  (Owner action: repo Settings -> Actions -> Runners -> New self-hosted
  runner. Register with the label `opendesign`.)
- The runner machine has `node` on PATH and the OpenDesign `od` CLI on
  PATH (or `OD_BIN` env pointing at `apps/daemon/bin/od.mjs`).
- The runner machine has the daemon reachable (default
  `OD_DAEMON_URL=http://127.0.0.1:7456`).

If both are present, the issue_comment step on the workflow uses
`OdCliTransport`, which executes:

```
od automation source ingest   (Issue body -> source packet)
od automation create          (manual-only routine)
od automation run <id>        (returns projectId/conversationId/agentRunId)
od automation runs <id>       (history; status)
```

Issue body is written to a temp file and passed as `--body-file -`,
so shell escaping is never involved.

### Reserved HTTP transport (disabled by default)

If Owner sets `OD_DISPATCH_URL` AND a documented OpenDesign webhook
endpoint is verified to exist, the dispatcher switches to
`HttpTransport`. Today no such endpoint exists in 0.19.2, so this
branch fails fast with a clear "scheme not allowed" or "endpoint
unreachable" reason.

### Why there is no StubTransport fallback in production

The previous slice used a `StubTransport(mode='accept')` fallback when
no transport was configured. That wrote `DISPATCHED` to the Issue
without ever contacting OpenDesign, which was a fake-success. This
slice REMOVES that fallback. Production `issue_comment` path with no
real transport writes `BLOCKED_FOR_PRODUCT_DECISION` and the exact
Owner-action message.

## Idempotency: cross-run persistence

Idempotency records are NOT held in an in-memory list. They are loaded
from prior machine-readable status comments on the same Issue (via
`gh api repos/.../issues/.../comments`). The runner combines those
records with the current run's writeback entries. The contract:

- Prior `DISPATCHED` for the same `dispatch_id` within 24h -> reject.
- Prior `DISPATCH_FAILED` for the same `dispatch_id` within 24h -> allow retry.
- Any record with an unparseable `trigger_timestamp` -> FAIL_CLOSED,
  refuse dispatch (do NOT treat unknown history as stale).
- Persisted history unavailable (gh api failure) -> BLOCKED_FOR_PRODUCT_DECISION.

This is verified end-to-end by
`tests/opendesign/test_cross_run_idempotency.py`, which spawns two
INDEPENDENT Python subprocesses and asserts the second sees the
first's persisted DISPATCHED record.

## PR-stage fixture validation

The same workflow can be exercised in PR review via
`workflow_dispatch`. The fixture path runs on `ubuntu-latest` with
the in-process StubTransport; no real OpenDesign is contacted.
`set -o pipefail` is enabled so a fixture assertion failure fails
the workflow step.

Fixture coverage:

| File | Scenario | `accept` verdict / state | `reject` verdict / state |
| --- | --- | --- | --- |
| `01_no_approval.json` | route:design-dev + no approval marker | `NO_DISPATCH` / `NO_DISPATCH` | same |
| `02_wrong_route.json` | approval + non design-dev route | `NO_DISPATCH` / `NO_DISPATCH` | same |
| `03_non_whitelisted_actor.json` | non-allowlist actor + marker | `BLOCKED` / `BLOCKED_FOR_PRODUCT_DECISION` | same |
| `04_happy_path.json` | valid route + valid approval | `DISPATCH` / `DISPATCHED` | `DISPATCH` / `DISPATCH_FAILED` |
| `05_duplicate_event.json` | replay of `04_happy_path` | `NO_DISPATCH` / `NO_DISPATCH` | same |
| `07_special_chars_in_body.json` | shell-meta in issue body | `DISPATCH` / `DISPATCHED` | `DISPATCH` / `DISPATCH_FAILED` |
| `08_malicious_comment.json` | unrelated comment w/o marker | `NO_DISPATCH` / `NO_DISPATCH` | same |

To prove `--strict` actually fails the workflow, intentionally
break a fixture and re-run `run_pr_fixture.py --strict`; the process
exits with status 3.

## Security notes

* Issue body / comment body are never interpolated into shell. They are
  stored as JSON strings inside `dispatch_input.issue.*`.
* The approval marker is matched case-sensitively as an exact
  substring. A lowercase copy or partial match is rejected.
* The `OWNER_APPROVED_FOR_OPENDESIGN` marker only matters inside the
  comment body. The same string inside the issue body is ignored.
* Secrets are not echoed to logs; the runner reads them only via
  process environment.
* `HttpTransport` validates the URL scheme (only `http`/`https`) at
  construction time.
* The workflow declares the minimum permissions
  (`contents: read`, `issues: write`).
* PR conversation comments are rejected by both the workflow `if:`
  and the contract `check_is_issue` gate.

## Files

* `.github/workflows/opendesign-dispatch.yml`
* `scripts/opendesign/contract.py` — gate logic.
* `scripts/opendesign/runner.py` — orchestrator + writeback helper.
* `scripts/opendesign/persisted_history.py` — cross-run idempotency loader.
* `scripts/opendesign/dispatch_stub.py` — in-process stub (fixture only).
* `scripts/opendesign/dispatch_http.py` — HTTP transport (reserved).
* `scripts/opendesign/dispatch_odcli.py` — `od` CLI transport (real).
* `scripts/opendesign/run_pr_fixture.py` — fixture runner CLI.
* `tests/opendesign/test_contract.py` — 21 contract tests.
* `tests/opendesign/test_runner.py` — 8 runner tests.
* `tests/opendesign/test_transports.py` — 11 transport tests.
* `tests/opendesign/test_fixture_runner.py` — 2 fixture runner tests.
* `tests/opendesign/test_odcli_transport.py` — 2 real-dispatcher tests
  (skipped if `od` CLI or daemon unavailable).
* `tests/opendesign/test_cross_run_idempotency.py` — 1 cross-process
  regression (two subprocesses).
* `.github/fixtures/opendesign-dispatch/*.json` — fixture events.
