# Pasay GitHub Dev Workflow — SOLO Milestone Mode

> **生效时间：** PASAY-SOLO-TRANSITION-001 (Issue #34) 完成后
> **旧工作流：** `ready-for-dev` → `/ND` 微任务模式已退役，仅作历史记录。

## 核心原则

TRAE SOLO 是 Pasay 的主工程执行者。开发不再机械按 Issue 一片一 PR。SOLO 先理解全局、形成 Milestone，再跨合理文件完成完整业务目标。GitHub Issue / PR 仍然保留，但作为历史输入、需求记录与审核单元，不再机械驱动开发节奏。

## Flow

```
Owner Product Direction / GitHub Issues / OpenDesign
                    │
                    ▼
            TRAE SOLO（Milestone 规划）
                    │
     Milestone = 一组相关 Issue / 功能切片的集合
                    │
                    ▼
         合理分支 (issue/{id}-desc 或 milestone/{name})
                    │
                    ▼
         跨文件开发 + migrations + tests + 合理 refactor
                    │
                    ▼
         targeted tests → commit → push → PR
                    │
                    ▼
   CodeRabbit + pasay-gate + ChatGPT Milestone Review
                    │
                    ▼
                 Owner 最终验收 → merge（Owner 执行）
```

## GitHub Issue / Label 说明

| 元数据 | 说明 |
|---|---|
| `route:dev` / `route:design-dev` / `route:design-only` | 历史标签，仅作参考；SOLO 不再机械等待 `ready-for-dev` |
| `ready-for-dev` / `ready-for-owner` / `blocked` | 历史 workflow 状态标签；SOLO 不再根据这些标签启动或停止 |
| Issue body / comments | 保留：作为需求、Bug、历史决策输入 |

**重要：** 旧 Issue 的 `blocked` / `ready-for-dev` 等状态不再控制 SOLO 执行。SOLO 基于产品语义与 Milestone 自行判断是否实施。

## Core Contract

- GitHub Issue 是需求与 Bug 的重要输入载体；SOLO 可合并多个相关 Issue 到一个 Milestone。
- OpenDesign 仍然是 UX/UI Source of Truth。
- 生产代码只通过 PR 交付；一个 PR 可对应一个 Milestone 或多个相关 Issue 的集合。
- CodeRabbit review 和 GitHub Actions 检查是独立证据，不能被实现者自证替代。
- Owner 保留最终业务验收权限，不 merge 前 TRAE SOLO 不得自行执行 merge。

## Milestone 模式（推荐）

1. **理解**：SOLO 阅读代码 + GitHub Issues + OpenDesign + `SOLO_HANDOFF.md`
2. **规划**：形成 Milestone 计划（目标、范围、验收、边界）
3. **实施**：单分支完成 migrations / 代码 / 测试 / 合理 refactor
4. **验证**：targeted tests → 必要 regression
5. **交付**：commit & push → PR → ChatGPT Review → Owner 验收

### 何时需要请求 Owner 产品决策

只有以下情况 SOLO 应停下请求 Owner：
- 产品规则存在真实歧义
- 必须改变核心业务模型
- 必须改变冻结架构（`CURRENT_ARCHITECTURE.md` ARCHITECTURE_FROZEN=YES）
- 必须删除现有已确认能力
- 存在无法自行解决的真实 blocker

其余普通代码实现、模块拆分、测试修复、migration 细节、合理 refactor、接口实现等工程问题由 SOLO 自己解决。

## Definition of Done

1. 代码实现完整且符合产品规则与业务真相优先原则
2. migrations 安全（upgrade / downgrade 均通过审计；downgrade 有 sa.inspect 列属性审计门控防止语义丢失）
3. targeted tests 通过；真实失败不得通过 skip/xfail 换 PASS
4. 无 secrets 写入；无权限越界
5. PR 打开且 CodeRabbit / pasay-gate 证据存在（如适用）
6. 业务代码未破坏 Owner / Secretary / Tenant 角色边界
7. 状态机与金额字段保持正确（Decimal / NUMERIC / timestamptz）

## Git Rules

- No force push / force-with-lease
- No history rewrite of shared branches
- No bypass PR（不直接修改 authority 分支业务代码）
- Branch 命名：延续现有 `issue/{id}-{desc}` 或新增 `milestone/{name}-{desc}`；无强制格式
- commit 信息：Conventional Commits 推荐（feat/fix/chore/docs 等），中文或英文皆可
- Push 后不等待 CI 或 CodeRabbit 完成；独立证据自行产生
