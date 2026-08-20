---
Name: nd
Description: Next Dev - 一键执行下一个开发任务或返修任务。支持显式 Issue 编号：/ND 或 /ND {ISSUE_NUMBER}
---

# /ND — Next Dev

## 核心原则

Owner 在 TRAE IDE 中输入：

- `\ND` → 自动发现唯一任务（现有行为，零变更）
- `\ND {ISSUE_NUMBER}` → 显式选择 GitHub Issue（PASAY-TASK-005 新增）

例如：
- `\ND 20` → 精准选择 `jhackuy/pasay-pm` 的 GitHub Issue #20
- `\ND 22` → 精准选择 Issue #22

治理边界：

- 不要求 Owner 复制 Issue/PR 正文；显式 Issue 编号只做任务路由，不绕过审批门禁。
- 不使用 `status:approved` / `status:in-progress` / `status:review`；只使用现有 contract：`route:dev`、`ready-for-dev`、`ready-for-owner`、`blocked`。
- TRAE 只负责开发实现、targeted tests、commit、push、PR handoff；不做运维、部署值守、监控平台、OpenDesign/GitHub 同步守护。
- 不 merge，不设置 `ready-for-owner`，不等待 CI / CodeRabbit，不 Review 自己。
- 达到任务 Stop Condition 后立即停止，禁止顺手重构。

## 任务优先级

### A. 无参数模式（`\ND`）

每次启动按以下顺序寻找唯一任务：

1. **RETURN 返修任务优先**
2. 没有返修任务时，才领取新的 `ready-for-dev` Issue

如果同一优先级存在多个候选，必须输出 `AMBIGUOUS_DEV_TASK` 并停止，不得自行猜测。

### B. 显式 Issue 模式（`\ND {ISSUE_NUMBER}`，PASAY-TASK-005 NEW）

仅当用户输入**恰好一个裸正整数**参数时进入本模式。例如 `\ND 20`。

执行顺序：

1. **跳过 1A/1B 全局候选扫描**。显式 Issue 编号是 Owner 的精准路由指令，不再和其他 Issue 一起做"全局唯一候选"判断。这绝不等于跳过审批门禁——下一步立即单独核验。
2. 通过 GitHub MCP 直接读取 `jhackuy/pasay-pm` 的该 Issue，强制检查下列门禁，任一不满足立即 STOP：
   - Issue 必须 OPEN。
   - 必须含 `route:dev`。
   - 必须含 `ready-for-dev`。
   - 不得含 `blocked`。
3. 上述门禁通过后，再判断 Repair / New Dev：
   - 若该 Issue 已关联**恰好一个 OPEN PR**，且该 PR 中存在最新的总控返修合同标记 `ND_RETURN` → **Repair Mode**（沿用 Repair Mode 完整合同）。
   - 若该 Issue**没有关联 OPEN PR** → **New Dev Mode**（沿用 New Dev Mode 完整合同）。
   - 若关联了**多个 OPEN PR** → `AMBIGUOUS_DEV_TASK` → STOP。
4. Claim（移除 Issue `ready-for-dev`）、开发、tests、commit、push、PR handoff 全部沿用 `/ND` 的既有合同，没有任何权限放松。

### C. 非法参数（PASAY-TASK-005 — fail closed）

以下输入**一律拒绝**，输出最终状态 `INVALID_ND_ARGUMENT` 并 STOP，不得猜测为"自动模式"、"截断后选前一个"或"去掉符号后重试"：

| 示例 | 拒绝原因 |
|---|---|
| `\ND abc` | 参数不是正整数 |
| `\ND 20 21` | 参数个数 = 2，只允许 0 或 1 个 |
| `\ND #20` | 含有 `#` 前缀，必须是裸数字 |
| `\ND -1` | 负数，Issue number 非负 |
| `\ND 0` | 0，GitHub Issue number 最小 = 1 |
| `\ND ` 尾部有空格以外的额外字符 | 只能有 `<SP>` + `<正整数>`，无别名/无 flags |

**唯一合法形式**：

```text
/ND                 → 模式 A（自动）
/ND <正整数>        → 模式 B（显式 Issue）
```

**`INVALID_ND_ARGUMENT` 报告必须提示**：唯一合法形式就是上述两种。

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
- `INVALID_ND_ARGUMENT`

当 Final Status = `INVALID_ND_ARGUMENT` 时，报告中必须额外列出：
- 实际接收到的用户命令（原始字符串，精确到大小写和前后空格）
- 唯一合法形式提示：`/ND` 或 `/ND <正整数>`
- 本此无效原因（非正整数 / 多参数 / `#` 前缀 / 负数或零 / 含多余字符）

最终报告必须中文、简短，只报告：

- 模式：Repair / New Dev / Auto(模式A) / Explicit(模式B) / Invalid(模式C)
- Issue number
- PR number（如有）
- branch
- commit SHA
- 修改文件
- targeted tests
- blocker（如有）
- Final Status
