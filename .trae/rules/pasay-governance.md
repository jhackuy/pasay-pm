---
alwaysApply: true
---

# Pasay SOLO Governance

> 生效：PASAY-SOLO-TRANSITION-001 (Issue #34)
> 取代：历史 ND 微任务治理（已退役）

## Git Governance

- Use one dedicated task branch per Milestone or Issue slice (worktree 可选，不强制).
- Never force push, force-with-lease, rewrite shared history, overwrite remote-only commits, delete shared remote branches, bypass PR, or auto-merge.
- Never modify authority or base-branch business code directly; all delivery goes through PR.
- Treat Git CLI and GitHub results as authority, not IDE UI guesses.

## Task Discipline — SOLO Milestone Mode

- SOLO 先形成 Milestone 理解，合理组织交付节奏；不再机械 `1 Issue = 1 small branch = 1 PR = STOP`.
- 合理跨文件完成完整业务 Milestone（migrations / tests / reasonable refactor / bugfix）。
- 合理 Targeted tests 优先；不跑不必要的全量；真实失败不 skip/xfail 换绿。
- 如果 blocked、scope 存在真实歧义、或安全无法判定：停下并报告，不无休止探索或猜测 Owner 意图。
- 旧 Issue 的 `blocked` / `ready-for-dev` 等历史标签不驱动执行。

## Validation

- After changes, run only targeted tests and existing relevant gates for the touched scope.
- Distinguish real regression, stale test, and uncertain result explicitly.
- Never change confirmed business facts, delete tests, skip tests, or weaken behavior just to get green.

## Reporting

- Final owner-facing reports are in Chinese.
- Keep code, commands, paths, SHAs, branch names, PR URLs, and field names in English.
- Milestone / Handoff 报告给出变更摘要、Targeted tests 结果、影响文件、已知风险。

## Owner-Only Decisions

只有 Owner 决定以下事项（SOLO 不自行替代）：

- 改变产品方向 / 核心业务模型
- 重新定义 Owner / Secretary / Tenant 权限边界
- 推翻冻结架构（`ARCHITECTURE_FROZEN=YES`）
- 删除现有已确认业务能力
- Merge PR、production deploy、Secrets 写入
