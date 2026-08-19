---
Name: nd
Description: Next Dev - 一键执行下一个已批准的开发任务
---

# /nd — Next Dev 命令（PASAY-TRAE-ND-001）

## 定位

`/nd` = Next Dev

**唯一职责：从 GitHub 获取唯一已批准开发任务 → 实现 → targeted tests → commit → push → 创建 PR → 进入 review 状态 → STOP。**

`/nd` 不承担 Review，不等待 CI / CodeRabbit，不 merge。
后续 Review 由现有体系负责：
`PR → GitHub Actions + CodeRabbit → ChatGPT 总控审核 → PASS / RETURN`
返修由总控另外发出明确修复任务。

---

## 触发后固定执行流程（仅针对 jhackuy/pasay-pm 仓库）

### 阶段 1：候选 Issue 发现

1. 通过 GitHub MCP 搜索所有 OPEN 状态的 Issue。
2. **候选必须同时拥有标签**：`route:dev` **AND** `status:approved`。
   - 如果仓库缺少 `status:approved` 标签 → `BLOCKED_MISSING_LABEL` → STOP。
3. **排除以下状态的 Issue**：
   - 已有对应 OPEN PR 的 Issue。
   - 含 `status:in-progress` 标签。
   - 含 `status:review` 标签。
   - 含 `blocked` 标签。
4. **候选数量判断**：
   - **0 个候选**：输出 `NO_APPROVED_DEV_TASK` → STOP，不改任何东西。
   - **≥2 个候选**：列出所有候选（Issue number + title），输出 `AMBIGUOUS_DEV_TASK` → STOP，禁止自行选择。
   - **恰好 1 个候选**：进入阶段 2。

### 阶段 2：读取任务合同

5. 通过 GitHub MCP 读取：
   - 该 Issue 完整正文
   - 该 Issue 所有 comments
6. 严格以：
   - **Scope**
   - **Acceptance Criteria**
   - **Stop Condition**
   作为开发合同。
   - 如果执行合同不明确 → `BLOCKED_UNCLEAR_CONTRACT` → STOP。
   - 禁止要求 Owner 复制 Issue 内容。

### 阶段 3：执行前检查 + 状态切换 + 分支

7. 执行前安全状态检查：
   - 确认正确 repo：`jhackuy/pasay-pm`。
   - 确认 authority / 远端 base（默认 main）本地同步。
   - 确认工作区安全：不覆盖其他人的 branch / worktree / 未提交修改。
   - 不满足 → `BLOCKED` → STOP。
8. 将 Issue 标签从 `status:approved` 切换为 `status:in-progress`：
   - 如果 `status:in-progress` 标签不存在 → `BLOCKED_MISSING_LABEL` → STOP。不要擅自创建治理体系外标签。
9. 创建独立任务分支/worktree：
   - Issue 若明确指定 branch 名则优先使用。
   - 未指定时使用格式：`issue/<number>-<short-slug>`（short-slug 由 title 生成，小写短横线，20 字符以内）。

### 阶段 4：开发

10. **严格限定 Scope**：只实现 Issue 明确要求。
    - 禁止顺手重构、全仓审计、扩大范围、修改无关代码。
    - 禁止修改 Secrets、禁止修改 branch protection。
    - 禁止 force push / force-with-lease。
11. 运行测试：
    - Issue 指定测试 → 严格执行。
    - Issue 未指定 → 仅运行 changed files 直接相关的最小 targeted tests。
12. 任何测试失败：先基于失败证据修复；修复范围不得超出 Issue 合同。

### 阶段 5：提交 + 创建 PR

13. Commit + Push：
    - commit message 引用 Issue（如 `feat: xxx (#123)`）。
    - **禁止 force push / force-with-lease**。
14. 创建 PR：
    - PR 标题和 body 必须引用 Issue（`Closes #<number>` 或 `Refs #<number>`）。
    - **禁止 merge**。

### 阶段 6：交给 Review 系统

15. 将 Issue 标签从 `status:in-progress` 切换为 `status:review`：
    - 如果 `status:review` 标签不存在 → 记录 WARNING 但不回滚 PR。
16. **立即 STOP**。
    - 不等待 GitHub Actions 完成。
    - 不等待 CodeRabbit Review。
    - 不 Review 自己的代码。
    - 不判断最终 PASS / FAIL。
    - 不自动修复 CodeRabbit 意见。
    - 永远不 merge PR。
17. 输出最终结构化摘要（中文）。

---

## 安全与禁止事项

- `/nd` 永远不能 merge PR。
- 禁止 force push / force-with-lease。
- 禁止修改 GitHub Secrets、仓库权限、branch protection。
- 禁止删除/覆盖其他人的 branch/worktree/未提交工作。
- 禁止自动执行没有 `status:approved` 的 Issue。
- 禁止同时执行多个 Issue（执行前检查 worktree/session 状态）。
- 禁止把 GitHub MCP token/PAT 写入 repo。
- 不修改现有 OpenDesign MCP / GitHub MCP 配置。
- **不承担 Review：** 不 Review 自己的代码、不判断最终 PASS/FAIL。
- **不等待 Review 系统：** PR 创建后不等 GitHub Actions / CodeRabbit，不自动修复它们的意见。

---

## 最终报告字段（中文输出）

完成/停止后，必须输出以下结构化摘要：

```
/nd 执行摘要
===========
候选数量: <0|1|N>
选定 Issue: <#number + title 或 N/A>
执行阶段: <候选发现|读取合同|执行前检查|开发|提交|交给Review系统|BLOCKED>
修改文件数: <N>
是否创建 PR: <YES #pr_number | NO>
是否切换 status:in-progress: <YES | NO>
是否切换 status:review: <YES | NO>
------
Blocker: <具体 blocker 或 NONE>
Final Status: PASS / BLOCKED / NO_APPROVED_DEV_TASK / AMBIGUOUS_DEV_TASK
```
