---
Name: nd
Description: Next Dev - 一键执行下一个开发任务或返修任务
---

# /ND — Next Dev

## 核心原则

Owner 在 TRAE IDE 中只输入：`/ND`

- 不要求 Owner 输入 Issue/PR 编号或复制任务正文。
- 不使用 `status:approved` / `status:in-progress` / `status:review`。
- 只使用现有 workflow contract：`route:dev`、`ready-for-dev`、`ready-for-owner`、`blocked`。
- TRAE 只负责开发实现、targeted tests、commit、push、PR handoff；不做运维、部署值守、监控平台、OpenDesign/GitHub 同步守护。
- 不 merge，不设置 `ready-for-owner`，不等待 CI / CodeRabbit，不 Review 自己。
- 达到任务 Stop Condition 后立即停止，禁止顺手重构。

## 任务优先级

`/ND` 每次启动按以下顺序寻找唯一任务：

1. **RETURN 返修任务优先**
2. 没有返修任务时，才领取新的 `ready-for-dev` Issue

如果同一优先级存在多个候选，必须输出 `AMBIGUOUS_DEV_TASK` 并停止，不得自行猜测。

---

## 阶段 0：Single Runner

检查当前 TRAE IDE / worktree 是否已有未完成的 `/ND` 开发任务。

- 有活跃任务 → `DEV_TASK_ALREADY_RUNNING` → STOP。
- 没有 → 继续。

禁止引入 lock service、daemon、Redis、dispatcher 等额外基础设施。

---

## 阶段 1A：优先发现 RETURN 返修任务

通过 GitHub MCP 查询 OPEN Issue 与其关联 OPEN PR。

返修候选必须同时满足：

- Issue 为 OPEN。
- Issue 含 `route:dev`。
- Issue 含 `ready-for-dev`。
- Issue 不含 `blocked`。
- Issue 已关联 **恰好一个 OPEN PR**。
- 该 PR 中存在最新的总控返修合同标记：`ND_RETURN`。

候选判断：

- 0 个 → 进入阶段 1B。
- 1 个 → 进入 **Repair Mode**。
- 多于 1 个 → `AMBIGUOUS_DEV_TASK` → STOP。

### Repair Mode 合同

1. 读取目标 Issue body + comments。
2. 读取关联 OPEN PR 的最新 `ND_RETURN` 评论，以及该 PR 当前 unresolved review threads。
3. **最新 `ND_RETURN` 评论是本轮唯一返修合同。**
   - CodeRabbit / CI 只作为证据。
   - 不得自动修复 `ND_RETURN` 未要求的格式、docstring、理论优化或低价值建议。
4. 再次确认 Issue 仍有 `route:dev + ready-for-dev` 且无 `blocked`。
5. 移除 Issue 的 `ready-for-dev`，作为领取动作。
6. 直接在 **现有 PR 的 head branch/worktree** 上做最小返修；禁止新建第二个 PR。
7. 只运行与返修项直接相关的 targeted tests。
8. commit + push 到现有 PR head branch。
9. 在现有 PR 写入简短 `HANDOFF_COMPLETE` 返修报告。
10. 不等待 CI / CodeRabbit，不 merge，不设置 `ready-for-owner` → STOP。

如果返修过程中出现无法在合同范围内解决的 blocker：

- 停止修改。
- 尝试恢复 Issue 的 `ready-for-dev`。
- 输出 `BLOCKED` 和明确原因。

---

## 阶段 1B：发现新的开发任务

仅当阶段 1A 没有返修候选时执行。

候选 Issue 必须同时满足：

- OPEN。
- 含 `route:dev`。
- 含 `ready-for-dev`。
- 不含 `blocked`。
- 没有关联 OPEN PR。

候选判断：

- 0 个 → `NO_APPROVED_DEV_TASK` → STOP。
- 1 个 → 进入 New Dev Mode。
- 多于 1 个 → `AMBIGUOUS_DEV_TASK` → STOP。

---

## New Dev Mode

### 读取合同

通过 GitHub MCP 读取 Issue body + comments。

只以以下内容作为开发合同：

- Scope
- Acceptance Criteria
- Targeted Tests（如有）
- Stop Condition

合同不明确 → `BLOCKED_UNCLEAR_CONTRACT` → STOP。

禁止要求 Owner 复制 Issue 内容。

### 执行前检查

- repo 必须是 `jhackuy/pasay-pm`。
- authority/base 必须根据仓库事实确认，禁止猜 main/master。
- Git 查询使用非交互模式，禁止 pager 卡住。
- 不覆盖其他 branch/worktree/未提交修改。

无法确认 authority → `BLOCKED_UNCLEAR_AUTHORITY` → STOP。

### Claim

再次确认目标 Issue 仍满足 `route:dev + ready-for-dev`、无 `blocked`、无 OPEN PR。

移除 `ready-for-dev` 作为领取动作，并重新读取确认。

冲突 → `BLOCKED_CLAIM_CONFLICT` → STOP。

### 开发

- 严格限定 Issue Scope。
- 禁止顺手重构、全仓审计、未来架构扩建。
- 禁止修改 Secrets / branch protection。
- 禁止 force push / force-with-lease。
- Issue 指定测试就按指定测试执行；未指定则只跑 changed files 直接相关的最小 targeted tests。

测试失败若不能在当前 Scope 内最小修复 → 恢复 `ready-for-dev` → `BLOCKED_TEST_FAILURE` → STOP。

### Handoff

完成后：

1. commit。
2. push 当前任务分支。
3. 创建一个 PR，并引用目标 Issue。
4. 不 merge。
5. 不设置 `ready-for-owner`。
6. 不等待 GitHub Actions / CodeRabbit。
7. 输出 `HANDOFF_COMPLETE` → STOP。

---

## Review 系统边界

PR 创建或返修 push 后：

`GitHub Actions + CodeRabbit → ChatGPT 总控审核 → READY / RETURN → Owner 最终验收`

只有 ChatGPT 总控审核通过后，Review 系统才允许进入 `ready-for-owner`。

`/ND` 永远不承担上述 Review 职责。

---

## 最终状态

只允许：

- `HANDOFF_COMPLETE`
- `BLOCKED`
- `NO_APPROVED_DEV_TASK`
- `AMBIGUOUS_DEV_TASK`
- `DEV_TASK_ALREADY_RUNNING`

最终报告必须中文、简短，只报告：

- 模式：Repair / New Dev
- Issue number
- PR number（如有）
- branch
- commit SHA
- 修改文件
- targeted tests
- blocker（如有）
- Final Status
