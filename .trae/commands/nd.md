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

### Final Status 枚举

/nd 只负责到 handoff，不判断最终 Review 结果。Final Status 只能是以下之一：

- `HANDOFF_COMPLETE` — PR handoff 已成功完成。**HANDOFF_COMPLETE ≠ Code Review PASS；HANDOFF_COMPLETE ≠ CI PASS；HANDOFF_COMPLETE ≠ 可以 merge。**
- `BLOCKED` — 发生通用不可继续 blocker。
- `NO_APPROVED_DEV_TASK` — 当前没有满足条件的已批准开发任务。
- `AMBIGUOUS_DEV_TASK` — 存在多个候选，无法唯一确定。
- `DEV_TASK_ALREADY_RUNNING` — 已经存在 `status:in-progress` 的开发 Issue，拒绝并发。

---

## 触发后固定执行流程（仅针对 jhackuy/pasay-pm 仓库）

### 阶段 0：Single-Runner 并发保护（v1）

0. 在候选发现之前，先通过 GitHub MCP 检查是否存在任何 OPEN Issue 带有 `status:in-progress` 标签。
   - 如果存在：列出所有 `status:in-progress` Issue（Issue number + title），输出 `DEV_TASK_ALREADY_RUNNING` → STOP，不得继续领取新的开发 Issue。

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

### 阶段 2：读取任务合同 → 生成执行合同快照

5. 通过 GitHub MCP 读取：
   - 该 Issue 完整正文（body）
   - 该 Issue 所有 comments
6. 严格以：
   - **Scope**
   - **Acceptance Criteria**
   - **Stop Condition**
   作为开发合同，生成**本次 /nd 的执行合同快照**。
   - 如果执行合同不明确 → `BLOCKED_UNCLEAR_CONTRACT` → STOP。
   - 禁止要求 Owner 复制 Issue 内容。
7. 合同快照固定规则：
   - 本次执行只能按照启动时读取到的合同快照执行。
   - 执行期间不得动态吸收新的任务要求。
   - comments 可以作为合同补充，但不得静默扩大已经读取的 Scope。
   - 在 PR 创建前的任意阶段，如果发现：Issue body 变化 / Scope 变化 / Acceptance Criteria 变化 / Stop Condition 变化 / 新 comment 与当前合同存在冲突 → `BLOCKED_CONTRACT_CHANGED` → STOP，不得自行判断新旧要求哪个优先，等待重新批准后再执行 `/nd`。

### 阶段 3：执行前检查 + 状态切换 + Claim 复核 + 分支

8. 执行前安全状态检查：
   - 确认正确 repo：`jhackuy/pasay-pm`。
   - Git 查询必须使用非交互模式，禁止触发 pager 阻塞：所有可能触发 Git pager 的查询必须使用 `git --no-pager` 前缀，或等价设置 `GIT_PAGER=cat`。禁止因为 pager 等待人工按 q 导致无人值守流程卡死。
   - 确认 authority / 远端 base 本地同步：authority / remote base 必须根据仓库事实确认。优先依据：1) Issue 明确指定的 base；2) origin/HEAD；3) 当前仓库已确认的治理事实。禁止仅凭 main/master/分支名称猜测 authority。如果 authority 无法唯一确认 → `BLOCKED_UNCLEAR_AUTHORITY` → STOP。不得 checkout 或同步猜测出来的 base。
   - 确认工作区安全：不覆盖其他人的 branch / worktree / 未提交修改。
   - 不满足 → `BLOCKED` → STOP。
9. 将 Issue 标签从 `status:approved` 切换为 `status:in-progress`：
   - 如果 `status:in-progress` 标签不存在 → `BLOCKED_MISSING_LABEL` → STOP。不要擅自创建治理体系外标签。
10. Claim 复核（状态切换后立即执行）：
    - 重新通过 GitHub MCP 读取目标 Issue 并复核：
      1. `status:in-progress` 存在；
      2. `status:approved` 已不存在；
      3. Issue 不包含 `blocked` 标签；
      4. Issue 不包含 `status:review` 标签；
      5. 当前仓库不存在另一个 `status:in-progress` 开发 Issue。
    - 全部通过后，才允许进入开发阶段。
    - 如果复核失败 → `BLOCKED_CLAIM_CONFLICT` → 按「阶段 3.5 A. PR 尚未创建时的失败恢复」处理 → STOP。
11. 创建独立任务分支/worktree：
    - Issue 若明确指定 branch 名则优先使用。
    - 未指定时使用格式：`issue/<number>-<short-slug>`（short-slug 由 title 生成，小写短横线，20 字符以内）。

### 阶段 3.5：status:in-progress 失败恢复规则

A. **PR 尚未创建时**（发生在开发 / test / commit / push / PR 创建之前的任何不可继续 blocker）：
1. 立即停止继续开发。
2. 记录明确 BLOCKED 原因。
3. 尝试把 Issue 标签从 `status:in-progress` 恢复为 `status:approved`。
4. 如果无法安全恢复标签 → 输出 `BLOCKED_STATUS_RECOVERY`。
5. 最终报告必须记录：Issue number、当前标签、需要人工恢复的标签动作。
6. STOP。不得静默留下 `status:in-progress`。

B. **PR 已成功创建后**（无法把 `status:in-progress` 切换为 `status:review`）：
1. 不得只记录 WARNING；改为输出 `BLOCKED_HANDOFF_LABEL`。
2. 最终报告必须记录：PR number、Issue number、当前标签状态、需要人工执行的动作（`status:in-progress` → `status:review`）。
3. PR 保留，不得删除或回滚 PR。
4. STOP。

### 阶段 4：开发

12. **严格限定 Scope**：只实现 Issue 明确要求。
    - 禁止顺手重构、全仓审计、扩大范围、修改无关代码。
    - 禁止修改 Secrets、禁止修改 branch protection。
    - 禁止 force push / force-with-lease。
13. 运行测试：
    - Issue 指定测试 → 严格执行。
    - Issue 未指定 → 仅运行 changed files 直接相关的最小 targeted tests。
14. 测试失败处理：
    - Issue 合同范围内可修复的测试失败 → 做最小修复 → 重新运行 targeted test。
    - 如果测试失败属于以下任一情况：环境问题 / baseline 问题 / flaky / 与当前 Issue 无关 / 无法在 Issue Scope 内修复 → 输出 `BLOCKED_TEST_FAILURE` 并立即 STOP。
    - `BLOCKED_TEST_FAILURE` 最终报告必须记录：test command、failure evidence、blocker 原因。
    - `BLOCKED_TEST_FAILURE` 触发后，禁止继续 commit、禁止 push、禁止创建 PR。
    - 禁止为了让测试通过扩大 Issue Scope。

### 阶段 5：提交 + 创建 PR

15. Commit + Push：
    - commit message 引用 Issue（如 `feat: xxx (#123)`）。
    - **禁止 force push / force-with-lease**。
16. 创建 PR：
    - PR 标题和 body 必须引用 Issue（`Closes #<number>` 或 `Refs #<number>`）。
    - **禁止 merge**。

### 阶段 6：交给 Review 系统

17. 将 Issue 标签从 `status:in-progress` 切换为 `status:review`：
    - 如果 `status:review` 标签不存在或切换失败 → 按「阶段 3.5 B. PR 已成功创建后的失败恢复」处理 → STOP。
18. **立即 STOP**。
    - 不等待 GitHub Actions 完成。
    - 不等待 CodeRabbit Review。
    - 不 Review 自己的代码。
    - 不判断最终 PASS / FAIL。
    - 不自动修复 CodeRabbit 意见。
    - 永远不 merge PR。
19. 输出最终结构化摘要（中文）。

---

## 安全与禁止事项

- `/nd` 永远不能 merge PR。
- 禁止 force push / force-with-lease。
- 禁止修改 GitHub Secrets、仓库权限、branch protection。
- 禁止删除/覆盖其他人的 branch/worktree/未提交工作。
- 禁止自动执行没有 `status:approved` 的 Issue。
- 禁止同时执行多个 Issue（执行前 Single-Runner 并发保护 + Claim 复核双重检查）。
- 禁止把 GitHub MCP token/PAT 写入 repo。
- 不修改现有 OpenDesign MCP / GitHub MCP 配置。
- **不承担 Review：** 不 Review 自己的代码、不判断最终 PASS/FAIL。
- **不等待 Review 系统：** PR 创建后不等 GitHub Actions / CodeRabbit，不自动修复它们的意见。

---

## 最终报告字段（中文输出）

完成/停止后，必须输出以下结构化摘要：

```text
/nd 执行摘要
===========
候选数量: <0|1|N>
选定 Issue: <#number + title 或 N/A>
执行阶段: <并发保护|候选发现|读取合同|执行前检查|Claim复核|开发|提交|交给Review系统|BLOCKED>
修改文件数: <N>
是否创建 PR: <YES #pr_number | NO>
是否切换 status:in-progress: <YES | NO>
是否切换 status:review: <YES | NO>
------
Blocker: <具体 blocker 或 NONE>
Final Status: HANDOFF_COMPLETE / BLOCKED / NO_APPROVED_DEV_TASK / AMBIGUOUS_DEV_TASK / DEV_TASK_ALREADY_RUNNING
```
