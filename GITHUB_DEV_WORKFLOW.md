# Pasay GitHub Dev Workflow

GitHub Issue is the single task ID and the single task-source of truth for each
delivery slice.

## Flow

```text
Owner -> ChatGPT
          |
          v
      GitHub Issue
                    |
          .---------'--------.
          |                  |
       dev route      design-dev route
          |                  |
          |              OpenDesign
          |                  |
          '-------------.----'
                        |
                       TRAE
                        |
                        PR
                  .-----'-----.
                  |           |
             CodeRabbit   pasay-gate
                  '-----.___.-'
                        |
                 Gate satisfied
          |
                READY_FOR_OWNER
                      |
                    Owner
```

## Core Contract

- GitHub Issue is the only task ID for ChatGPT, OpenDesign, TRAE, CodeRabbit,
  and Owner handoff.
- Owner should not copy long task briefs across tools once the GitHub Issue is
  created.
- OpenDesign remains the design source of truth; production code remains in this
  repository and ships only through PRs.
- CodeRabbit review and GitHub Actions checks are independent evidence and
  cannot be replaced by the implementation agent self-report.
- Owner keeps final business acceptance authority and does not act as a manual
  message bus between tools.

## Supported Routes

- `dev`: no UX or product-design change is required; TRAE can implement
  directly from the Issue.
- `design-only`: only OpenDesign source-of-truth updates are required; no
  production code change is allowed.
- `design-dev`: OpenDesign completes design and a structured handoff first;
  TRAE implements only after approval.

## Workflow Labels

Use only the minimal metadata that GitHub native state does not already cover.

- Route labels: `route:dev`, `route:design-dev`, `route:design-only`
- Workflow labels: `ready-for-dev`, `ready-for-owner`, `blocked`

## Design To Dev Handoff

For `design-dev`, OpenDesign must leave a structured handoff in the same
GitHub Issue or an approved linked artifact. The handoff must include:

- GitHub Issue number
- Design status
- Changed design files
- Frozen business rules
- UX states, permissions, and copy changes
- Acceptance gates
- Explicit out-of-scope

TRAE must read the same GitHub Issue plus the approved handoff before coding.
TRAE must not reinterpret frozen design rules on its own. Phase one remains
serial-write only: OpenDesign and TRAE do not write to the same physical repo
workspace in parallel.

## READY_FOR_OWNER

After a PR is opened, CodeRabbit and `pasay-gate` run in parallel. Their
results are independent evidence for the same PR.

`READY_FOR_OWNER` may be set to `YES` only when the task-specific design gate
(if any), CodeRabbit, and `pasay-gate` evidence are all present and pass for
the scope of the task. No PR may be auto-merged in this phase.

## OpenDesign Auto-Dispatch (PASAY-OPENDESIGN-AUTO-DISPATCH-001)

The `opendesign-dispatch` workflow handles the GitHub to OpenDesign handoff
for `route:design-dev` issues. Trigger contract, status states, and PR-stage
fixture validation are documented in `docs/opendesign-dispatch.md`. The
dispatcher is event-driven; it does NOT poll, does NOT run on a schedule,
and does NOT use a second database. Until Owner configures either a
self-hosted runner or an `OD_DISPATCH_URL` secret, every approval event
records a `BLOCKED_FOR_PRODUCT_DECISION` status comment so the missing
step is auditable.

