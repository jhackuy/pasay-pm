# Pasay SOLO — AI Development Control

TRAE SOLO 是 Pasay 的主工程执行者。本文件是 Pasay 仓库级开发控制入口。

## Canonical Rules Location

- Pasay 项目级工程规则：`project_rules.md`
- Pasay 项目完整上下文与接手合同：`SOLO_HANDOFF.md`
- 历史工作流（已退役，仅作记录）：`AI_WORKFLOW_RULES.md`（HISTORICAL）、`GITHUB_DEV_WORKFLOW.md`

## Long-Term Engineering Rules（继续有效）

- Git authority and history safety are non-negotiable: no default-branch rewrite,
  no force push, no shared-history rewrite, no overwriting remote-only commits.
- Never delete, skip, or xfail real failing tests just to manufacture a PASS.
- Agent self-report is never enough to claim success; independent GitHub checks,
  reviews, and human acceptance remain authoritative.
- Final Owner-facing reports default to Chinese unless the task explicitly says
  otherwise.

## Hard Bans（永远有效）

- No force push / force-with-lease
- No merge PR (TRAE SOLO 不 merge)
- No production deploy (TRAE SOLO 不负责部署)
- No secret write or credential commit
- No weakening confirmed business facts or core product rules
