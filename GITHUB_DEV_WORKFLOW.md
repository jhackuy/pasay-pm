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
     route decision
       /       \
OpenDesign    TRAE
       \       /
          PR
          |
      CodeRabbit
          |
   GitHub Actions
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

- `dev-only`: no UX or product-design change is required; TRAE can implement
  directly from the Issue.
- `design-only`: only OpenDesign source-of-truth updates are required; no
  production code change is allowed.
- `design-then-dev`: OpenDesign completes design and a structured handoff first;
  TRAE implements only after approval.
- `parallel-approved`: reserved for frozen-contract work only; defined here but
  blocked by default in phase one.

## Workflow Labels

Use labels as the workflow status contract.

### Route labels

- `route:dev-only`
- `route:design-only`
- `route:design-then-dev`
- `route:parallel-approved`

### Status labels

- `status:needs-design`
- `status:design-running`
- `status:design-review`
- `status:design-approved`
- `status:ready-for-dev`
- `status:dev-running`
- `status:review`
- `status:changes-requested`
- `status:ci-running`
- `status:ready-for-owner`
- `status:owner-rejected`
- `status:blocked`
- `status:done`

## Design To Dev Handoff

For `design-then-dev`, OpenDesign must leave a structured handoff in the same
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

`READY_FOR_OWNER` may be set to `YES` only when the task-specific design gate
(if any), CodeRabbit, and GitHub Actions evidence are all present and pass for
the scope of the task. No PR may be auto-merged in this phase.
