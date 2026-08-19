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
   ready-for-dev         OpenDesign
          |             approved handoff
   Owner 在 TRAE IDE        |
        输入 /nd            |
          |                  |
   TRAE 领取 ready-for-dev   |
          |                  |
   targeted tests           |
   commit / push             |
          |                  |
          '-----.   TRAE  .---'
                |   实现
                v
               PR
               |  HANDOFF_COMPLETE
         .-----'-----.
         |           |
    CodeRabbit   pasay-gate
         '-----.___.-'
               |
        Gate satisfied
               |
     ChatGPT 总控审核
          /      \
   RETURN（返修）  ready-for-owner
                   |
                 Owner
```

TRAE IDE `/nd`（Next Dev）是当前 `dev route` 的标准执行入口：
`ready-for-dev Issue → TRAE IDE /nd → targeted tests → commit/push → PR → HANDOFF_COMPLETE → STOP`

`design-dev route` 必须在 OpenDesign approved handoff 后进入 TRAE 实现：
`GitHub Issue → OpenDesign approved handoff → TRAE → targeted tests → commit/push → PR`

两个需要开发实现的 route 最终都必须经过 TRAE 后才能进入 PR。

`HANDOFF_COMPLETE` 仅表示 PR handoff 完成：
- ≠ Code Review PASS
- ≠ CI PASS
- ≠ ready-for-owner
- ≠ 可以 merge

`/nd` 的边界：
- 不等待 GitHub Actions
- 不等待 CodeRabbit
- 不 Review 自己的代码
- 不设置 `ready-for-owner`
- 永不 merge PR

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

After a PR is opened by TRAE `/nd` (HANDOFF_COMPLETE), CodeRabbit and `pasay-gate` run in parallel. Their results are independent evidence for the same PR.

Then **ChatGPT 总控审核** is performed. Only the 总控 may issue RETURN (返修) or advance to `ready-for-owner`.

`ready-for-owner` label may be set only when the task-specific gate (if any), CodeRabbit, `pasay-gate`, and ChatGPT 总控审核 are all present and pass for the scope of the task. No PR may be auto-merged in this phase. Owner keeps final business acceptance authority.

`/nd` itself shall never set `ready-for-owner`.
