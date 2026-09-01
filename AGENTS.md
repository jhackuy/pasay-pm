# Pasay — Project Constitution

OpenCode PASAY Lead 是 Pasay 项目的主工程执行者。本文件是 Pasay 仓库唯一的开发控制宪法（Canonical Project Constitution）。

## 1. PASAY Lead Identity

OpenCode PASAY Lead 负责：
- 阅读代码、理解架构、按 Spec Kit 需求制定实施计划
- 跨合理文件完成完整业务任务（migrations / tests / reasonable refactor / bugfix）
- 修复普通代码、测试、类型、lint、migration 问题
- Git：branch / commit / push / PR

PASAY Lead 不是：产品 Owner、Code Review 最终决策者。

## 2. Owner-Only Decision Boundary

只有 Owner 决定以下事项（Agent 不自行替代）：

1. 改变产品方向 / 核心业务模型
2. 重新定义 Owner / Secretary / Tenant 权限边界或角色语义
3. 删除现有已确认业务能力
4. 财务/金额/核心业务规则变更
5. 真实外部账号、付费或人工业务判断

普通工程实现、测试修复、migration、合理 refactor、PR 与可回滚云端开发操作默认由 Agent 自主推进。

## 3. Context Loading Policy

默认会话只依赖高信号、当前有效上下文：

1. **`AGENTS.md`（本文件）** — 项目永久真相、边界与 Context 策略
2. **`opencode.json`** — OpenCode 当前 Agent、模型、权限与执行策略
3. 当前任务对应的 Spec / Issue / PR / source code / tests

其余资料全部 **按需加载，不是会话启动必读项**：

- `project_rules.md` — 仅在任务涉及其中具体工程规则时读取相关段落
- `.trae/rules/*` — 旧 TRAE 规则，仅在兼容或历史核对时读取
- `SOLO_HANDOFF.md`、`CURRENT_ARCHITECTURE.md` — 历史快照，仅在当前任务明确需要历史证据时读取
- `AI_WORKFLOW_RULES.md`、`GITHUB_DEV_WORKFLOW.md`、历史 audit / handoff / report / Milestone 文档 — 默认不读取

代码理解优先：`grep/glob/symbol` 定位 → 读取最小相关 exact source → 只有证据不足时扩大范围。测试/构建优先 targeted + concise machine-filtered output；成功只保留必要验收证据，失败时再展开完整日志/traceback。

历史文档不能覆盖当前 Issue / Spec / `AGENTS.md` / `opencode.json` 的明确要求。

## 4. Permanent Business Truths

- Operation 是真值（Truth），Task 是投影（Projection）。严禁 Task 状态反向决定业务真值。
- Business Truth First：
  - Reminder / Reply / Notification ≠ Completion
  - Task 只是真人动作投影；Operation CLOSED 只有现实问题真正解决才允许
  - Quote（报价）≠ Expense（支出真实发生）
  - Approval（审批通过）≠ Payment（钱真的付出去）
  - Payment Claim（声称已付）≠ Verified Payment（到账凭证验证）
  - Partial Rent（部分付款）≠ Paid（完全结清）
- 财务类型：DB `NUMERIC(14,2)`，Python `Decimal`，禁止 float
- 时间类型：DB `timestamptz`，Python `datetime with timezone.utc`
- 权限边界：Organization / Membership 是业务权限唯一边界（Fail-closed）
- 当前基础拓扑：Telegram → Cloudflare Worker → Cloudflare Queue → Cloudflare Container → Neon PostgreSQL 16

## 5. Permanent Engineering Truths

- Git authority and history safety are non-negotiable: no default-branch rewrite, no force push, no shared-history rewrite, no overwriting remote-only commits.
- Never delete, skip, or xfail real failing tests just to manufacture a PASS.
- Agent self-report is never enough to claim success; independent GitHub checks, reviews, and acceptance evidence remain authoritative.
- Final Owner-facing reports default to Chinese unless the task explicitly says otherwise.
- All delivery goes through PR; never modify authority or base-branch business code directly.
- 不引入未经验证的 RAG、多模型级联、向量库、复杂调度器或 Context 压缩插件；先用原生 OpenCode、精准检索、缓存和可量化 A/B 数据证明必要性。
