# PASAY Rewrite Project Status

> Last verified: 2026-08-31 UTC.  
> Live work is tracked by GitHub Issues, pull requests, checks and GitHub Projects. This visual snapshot follows the rewrite authority in Issue #99 and excludes retired Milestone gates, 17-door freeze, qualification probes, duplicate dispatchers and complex reviewer governance.

## Current snapshot

| Item | Verified status |
| --- | --- |
| Authoritative branch | `feature/telegram-ui-v2` |
| Rewrite authority | [Issue #99 — clean rewrite + Mini App + simple cloud delivery](https://github.com/jhackuy/pasay-pm/issues/99), open |
| Active delivery | [PR #100 — complete application + Mini App + simple cloud delivery](https://github.com/jhackuy/pasay-pm/pull/100), open, 51 commits / 151 changed files |
| Current head | `751531f2c1830101cd61bedae3277d794e7ae1cb` |
| CI | `rewrite-ci` **in progress** at verification time |
| Review | **CHANGES_REQUESTED** remains; merge is blocked until findings are cleared and re-verified |
| Spec Kit artifacts | `spec.md`, `plan.md`, `tasks.md` and coverage matrix exist on PR #100 head, not yet merged |
| Progress counting | `tasks.md` checkboxes are stale and therefore are **not** used to claim a percentage |
| Merge state | **Forbidden until current CI, review, product coverage and ChatGPT acceptance pass** |

## Development workflow

```mermaid
flowchart TD
    I["Authoritative Issue 99"] --> S["Spec Kit: spec"]
    S --> P["Spec Kit: plan"]
    P --> T["Spec Kit: tasks"]
    T --> O["OpenCode implementation"]
    O --> PR["Pull request 100"]
    PR --> CI["Minimal CI: tests, fresh DB, build and smoke"]
    CI --> A["ChatGPT acceptance"]
    A -->|PASS| M["Merge"]
    A -->|RETURN| O
    M --> DB["Alembic upgrade"]
    DB --> D["Cloudflare deploy"]
    D --> H["Health and Telegram webhook smoke"]
```

## Project progress

```mermaid
flowchart TD
    A["✅ Rewrite authority and retained product truth — Issue 99"] --> B["🟡 Spec Kit artifacts — present on unmerged PR"]
    B --> C["🔵 Application and Mini App implementation — PR 100"]
    C --> D["🔴 CI, review and coverage corrections — CHANGES_REQUESTED"]
    D --> E["⚪ Final acceptance and merge"]
    E --> F["⚪ Alembic upgrade and cloud deploy"]
    F --> G["⚪ Health and Telegram webhook smoke"]
    G --> H["⚪ Rewrite release accepted"]

    classDef done fill:#1f883d,stroke:#116329,color:#fff
    classDef active fill:#0969da,stroke:#0550ae,color:#fff
    classDef attention fill:#bf8700,stroke:#9a6700,color:#fff
    classDef blocked fill:#cf222e,stroke:#a40e26,color:#fff
    classDef pending fill:#d0d7de,stroke:#8c959f,color:#24292f

    class A done
    class B attention
    class C active
    class D blocked
    class E,F,G,H pending
```

## Status meaning

- **Done** requires accepted evidence; file presence, commit volume or a green sub-check alone is not completion.
- **Active** means OpenCode is advancing the authoritative rewrite.
- **Blocked** means the PR cannot merge, not that development must stop.
- **Pending** means no accepted evidence exists for that stage.
- Update this snapshot only when Issue, PR, CI, review, acceptance or deployment state materially changes.
