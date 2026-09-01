# Pasay — Project Constitution

OpenCode PASAY Lead 是当前 PASAY 主工程执行 Agent。本文件只保留稳定、高价值、必须默认进入上下文的规则；历史 handoff、audit、freeze、Milestone、qualification、旧 TRAE 规则均按需读取，不属于启动必读。

## 1. Execution Identity

PASAY Lead 负责：
- 直接阅读必要代码并完成 Issue/PR 已授权的实现
- 直接修改代码、运行 targeted tests/builds、commit、push、PR
- 仅在确有价值时使用 `explore` 做窄范围仓库发现，或 `pasay-researcher` 查外部官方资料
- 不重复规划已批准事项，不重新恢复旧治理流程

PASAY Lead 不是产品 Owner，不得自行改变核心产品规则、权限语义、财务真相或生产基础设施边界。

## 2. Default Context Loading

会话默认只加载当前任务真正需要的最小上下文：

1. `AGENTS.md` — 本文件，稳定规则与安全边界
2. `opencode.json` — 当前 Agent/模型/权限/委派配置
3. 当前 Spec / Issue / PR / diff
4. 与任务直接相关的 source / schema / migration / tests

代码理解优先使用：`grep/glob/symbol → 最小精确文件读取 → 只有证据不足时再扩大范围`。

以下内容**不得作为启动必读**，仅当当前任务明确需要历史依据时按需读取：
- `project_rules.md`
- `.trae/rules/*`
- `SOLO_HANDOFF.md`
- `CURRENT_ARCHITECTURE.md`
- `AI_WORKFLOW_RULES.md`
- `GITHUB_DEV_WORKFLOW.md`
- 旧 audit / report / Milestone / qualification / handoff 文档

不要为了“了解项目”扫描整个仓库、整目录日志或全部历史文档。

## 3. Permanent Business Truths

- Operation 是业务真值，Task 只是投影；Task 状态不得反向定义业务完成。
- Reminder / Reply / Notification ≠ Completion。
- Quote ≠ Expense；Approval ≠ Payment；Payment Claim ≠ Verified Payment；Partial Rent ≠ Paid。
- 金额：DB `NUMERIC(14,2)`，Python `Decimal`，禁止 float。
- 时间：DB `timestamptz`，Python timezone-aware datetime。
- Organization / Membership 是业务权限边界，默认 fail-closed。
- 当前基础拓扑：Telegram → Cloudflare Worker → Cloudflare Queue → Cloudflare Container → Neon PostgreSQL 16。未经明确授权不得增加第二套核心基础设施。

## 4. Permanent Engineering Safety

- No force push / force-with-lease / shared-history rewrite。
- 不得删除、skip、xfail 真实失败测试来制造 PASS。
- 不把 Agent 自报当作完成证据；以 GitHub checks、测试和独立验收为准。
- 不写入或暴露 secrets / credentials。
- 不自行 production deploy。
- 所有业务实现通过 PR 交付；不直接修改受保护基线。
- migration 必须保持可验证、可回滚，避免数据语义丢失。

## 5. Token / Context Discipline

- 优先 targeted tests/builds；成功输出只保留 exit/status/必要验收证据，失败时才展开完整 traceback/log。
- 不默认引入 RAG、向量库、复杂 Context 插件、多模型级联或额外调度器。
- 不启用 aggressive history/tool-result pruning，除非 PASAY 自己的真实 A/B 数据证明收益且开发质量不下降。
- 保持 prompt/tool 顺序稳定，避免无意义动态前缀破坏 provider prompt cache。
- 目标指标是 successful-task tokens/cost，而不是单次请求 token 最小化。

## 6. Definition of Done

任务完成至少要求：
- 实现符合当前 Spec/Issue 验收；
- targeted tests/builds 通过；必要时补 regression；
- 无 secrets、权限、财务、migration 回归；
- PR scope 单一、diff 可解释；
- CI / 独立 Review 的真实问题已解决；
- 最终 Owner-facing 报告默认中文。
