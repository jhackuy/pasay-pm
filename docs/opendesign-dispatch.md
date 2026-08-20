# PASAY-OPENDESIGN-AUTO-DISPATCH-001 — GitHub to OpenDesign Dispatcher

## What it does

Event-driven handoff from a GitHub Issue (`route:design-dev`) to the local
OpenDesign daemon. The dispatcher refuses to call OpenDesign unless every
gate below passes. It does NOT use scheduled polling, a second database,
UI automation, or a separate Issue system.

## Trigger contract

A single `issue_comment` event triggers dispatch **only when all** of the
following hold:

| Gate | Source | Required |
| --- | --- | --- |
| Action | `event.action` | `created` |
| Repository | `event.repository.full_name` | matches `$OD_EXPECTED_REPO` (default `jhackuy/pasay-pm`) |
| Issue state | `event.issue.state` | `open` |
| Route label | `event.issue.labels` | contains `route:design-dev` |
| Approval marker | `event.comment.body` | contains exact marker `OWNER_APPROVED_FOR_OPENDESIGN` (case-sensitive) |
| Actor allowlist | `event.sender.login` | case-insensitive match against `$OD_OWNER_ALLOWLIST` |
| Idempotency | in-process record list | no prior `DISPATCHED` for the same `dispatch_id` within 24h |

Any failed gate produces a structured `NO_DISPATCH` or `BLOCKED` verdict
that is written back as a status comment. No second dispatch is ever
attempted for the same approval event.

## Status state machine

```
   APPROVED_NOT_DISPATCHED (gate passed, awaiting OpenDesign)
            |
            v
       DISPATCHED  (OpenDesign accepted)
            |
            v
       DESIGN_ACCEPTED  (Owner accepts the design; can flip to ready-for-dev)
       
   DISPATCH_FAILED         (endpoint unreachable / rejected)
   BLOCKED_FOR_PRODUCT_DECISION (Owner must act: e.g. install self-hosted runner)
   NO_DISPATCH             (any non-fatal gate failed)
```

## How Owner configures real dispatch

The dispatcher in this slice is shipped with a stub transport. Real
dispatch requires ONE of the following, both under Owner control:

1. **GitHub self-hosted runner on the Windows machine that runs the
   OpenDesign daemon.** The runner uses the `od` CLI directly. No HTTP
   bridge, no extra secrets needed.

2. **Public HTTP endpoint** for the OpenDesign daemon. Owner sets the
   repository secrets `OD_DISPATCH_URL` (base URL) and `OD_TOOL_TOKEN`
   (bearer). The dispatcher POSTs the dispatch input as JSON to
   `{OD_DISPATCH_URL}/api/opendesign/dispatch`.

Until either of these is set, the dispatcher will report
`BLOCKED_FOR_PRODUCT_DECISION` for every approval event.

## PR-stage fixture validation

The same workflow can be exercised in PR review by running the
`workflow_dispatch` entry point. It replays every fixture under
`.github/fixtures/opendesign-dispatch/*.json` through the runner and
asserts the verdict + state.

Fixture coverage:

| File | Scenario | Expected verdict | Expected state |
| --- | --- | --- | --- |
| `01_no_approval.json` | route:design-dev + no approval marker | `NO_DISPATCH` | `APPROVED_NOT_DISPATCHED` |
| `02_wrong_route.json` | approval + non design-dev route | `NO_DISPATCH` | `NO_DISPATCH` |
| `03_non_whitelisted_actor.json` | non-allowlist actor + marker | `BLOCKED` | `BLOCKED_FOR_PRODUCT_DECISION` |
| `04_happy_path.json` | valid route + valid approval | `DISPATCH` | `DISPATCHED` |
| `05_duplicate_event.json` | replay of `04_happy_path` | `NO_DISPATCH` | `NO_DISPATCH` |
| `07_special_chars_in_body.json` | shell-meta in issue body | `DISPATCH` | `DISPATCHED` (parameter-safe) |
| `08_malicious_comment.json` | unrelated comment w/o marker | `NO_DISPATCH` | `APPROVED_NOT_DISPATCHED` |

`DISPATCH_FAILED` (endpoint unreachable) is exercised by re-running the
same fixture runner with `--mode reject`.

## Security notes

* Issue body / comment body are never interpolated into shell. They are
  stored as JSON strings inside `dispatch_input.issue.*`.
* The approval marker is matched case-sensitively as an exact substring.
  A lowercase copy or partial match is rejected.
* The `OWNER_APPROVED_FOR_OPENDESIGN` marker only matters inside the
  comment body. The same string inside the issue body is ignored.
* Secrets are not echoed to logs; the runner reads them only via
  process environment.
* The workflow declares the minimum permissions
  (`contents: read`, `issues: write`).

## Files

* `.github/workflows/opendesign-dispatch.yml`
* `scripts/opendesign/contract.py` — gate logic (pure stdlib).
* `scripts/opendesign/runner.py` — orchestrator + writeback helper.
* `scripts/opendesign/dispatch_stub.py` — in-process stub for PR.
* `scripts/opendesign/dispatch_http.py` — HTTP transport for real dispatch.
* `scripts/opendesign/run_pr_fixture.py` — fixture runner CLI.
* `tests/opendesign/test_contract.py` — 14 contract tests.
* `tests/opendesign/test_runner.py` — 6 runner tests.
* `tests/opendesign/test_transports.py` — 6 transport tests.
* `tests/opendesign/test_fixture_runner.py` — fixture validation.
* `.github/fixtures/opendesign-dispatch/*.json` — fixture events.
