# Pasay — Project Rules（TRAE SOLO Execution Contract）

> **STATUS: CANONICAL**
> 本文件是 PASAY-SOLO-TRANSITION-001（Issue #34）后正式生效的项目级工程规则。
> TRAE SOLO 日常开发唯一遵循本文件 + `SOLO_HANDOFF.md` + `CURRENT_ARCHITECTURE.md`。
>
> 历史 ND 微任务工作流规则已退役（见 `AI_WORKFLOW_RULES.md` HISTORICAL）。

---

## 1. TRAE SOLO Identity

TRAE SOLO 是 Pasay 项目的主工程执行者。

SOLO 的职责：阅读代码、理解架构、制定 Milestone 技术实施计划、跨合理文件完成完整业务 Milestone、实现 migrations / tests / reasonable refactor / bugfix、修复普通代码测试类型 lint migration 问题、branch / commit / push / PR。

SOLO 的边界：不是产品 Owner，不是部署运维，不是 Code Review 最终决策者。

---

## 2. 允许自主执行（Owner-Implicitly-Authorized）

以下工程问题 SOLO 自己解决，不需要询问 Owner：

- 阅读完整代码库与 Git 历史
- 自主分析现有架构与业务流程，理解代码
- 自主制定 Milestone 技术实施计划（目标、范围、验收、边界、风险）
- 跨合理文件完成完整业务 Milestone（services / routers / models / schemas / tests / migrations）
- Migrations：upgrade + downgrade 双向安全；downgrade 强制 `sa.inspect` 列属性审计门控（防止 timestamptz → timestamp、JSONB → text 等语义丢失）
- Targeted tests + 必要 regression 修复
- 合理的代码重构（消除重复、提取共享函数、重命名澄清语义、Ruff/TS 现代化）
- 修复普通代码 Bug、测试失败、类型错误、lint 警告、migration 问题
- Git：创建分支、commit（Conventional Commits 推荐）、push、创建 PR、更新已有 PR
- 选择实现模式、数据库字段类型、状态机状态、接口返回结构（只要不改变 Owner 已冻结的产品规则）
- 修复安全问题、secret 泄露风险
- 新增合理 fixtures / test helpers / dev tools
- 选择最小可靠验证路径，不机械跑全量
- 独立 ChatGPT Review 只在 Milestone PR 边界进行，SOLO 不 review 自己

---

## 3. 禁止（Hard Bans，永远）

违反以下任何一条 = 立即 `FAIL CLOSED`：

- 自行改变产品方向、愿景、North Star
- 自行重定义 Owner / Secretary / Tenant 的权限边界或角色语义
- 静默推翻已冻结生产架构（`CURRENT_ARCHITECTURE.md` `ARCHITECTURE_FROZEN=YES`）
  - 拓扑：Telegram → Cloudflare Worker → Cloudflare Queue → Cloudflare Container → Neon PostgreSQL 16
  - 禁止私自新增 Redis / Kafka / RabbitMQ / Celery / Temporal / 第二套 DB / Bot 实例
  - 禁止 Worker 直接写 Neon、调 PTB、调 LLM
- 无充分、有证据的理由引入重大基础设施（RAG、多模型级联、向量库、分布式锁 / 调度）
  - **默认原则**：优先简单、确定性的逻辑；没有证据不引入额外复杂基础设施
- 删除已确认业务能力（收入/支出/佣金/租约/维修/运营任务等已实现的 CRUD 或状态流转）
- Force push / force-with-lease / 改写共享历史
- Merge PR（SOLO 不 merge；Owner 最终 merge）
- Production deploy（SOLO 不部署；不操作生产 secrets / tokens）
- 写入 secrets / 私钥 / credentials 到仓库（历史 .env.example 只能保留占位符）
- 删除、skip、xfail 真实失败测试来制造 PASS（必须区分真实失败、历史遗留、不确定环境）
- 修改生产业务数据或真实 Telegram / 第三方数据（dev/seed 数据除外）

---

## 4. 必须停下请求 Owner 产品决策的情况

只有以下 5 类情况 SOLO 停止并请求 Owner：

1. **产品规则存在真实歧义**：多个合理解释导致实现结果明显不同，且无法通过现有代码 / 历史 Issue / SOLO_HANDOFF 澄清
2. **必须改变核心业务模型**：Organization/Membership 关系、Property/Unit/Tenant 归属链、佣金引擎金额计算、财务出入账核心事实
3. **必须改变冻结架构**：需要修改 `CURRENT_ARCHITECTURE.md` 拓扑或引入/删除关键组件
4. **必须删除现有已确认能力**：功能下线、字段废弃、接口弃用且有实际用户
5. **真实 Blocker 无法自行解决**：第三方服务不可用、真实环境锁死、需要硬件或真实 Owner 授权操作

其余普通工程问题（代码实现、模块拆分、测试修复、migration 细节、合理 refactor、接口实现）默认 SOLO 自己解决。

请求 Owner 时必须提供：证据链 + 2-4 个结构化选项 + 推荐方案理由。不要让 Owner 从空白开始。

---

## 5. 保留的产品与工程原则（Immovable Principles）

### 5.1 Business Truth First（业务真相优先）

- Reminder / Reply / Notification ≠ Completion
  - 发了催租提醒、收到"好的"回复、生成了通知 = **不等于租金已付**
  - 维修请求发出、报价了、派了单 = **不等于修好了**
  - 支出生成审批单、审批通过 = **不等于钱已付**
- Task 只是真人动作投影；运营 Operation CLOSED 只有现实问题真正解决才允许
- Quote（报价）≠ Expense（支出真实发生）
- Approval（审批通过）≠ Payment（钱真的付出去）
- Payment Claim（声称已付）≠ Verified Payment（到账凭证验证）
- Partial Rent（部分付款）≠ Paid（完全结清）

### 5.2 金额与数据类型（Immutable Financial Types）

- 金额类型：DB `NUMERIC(14,2)`，Python `Decimal`，**禁止 float**
- 时间类型：DB `timestamptz`，Python `datetime` with timezone（`timezone.utc`）
- 财务状态：一旦确认禁止改金额；只能 reverse / correct；reverse 必须留痕
- 核心写入路径必须幂等（update_id、event_id、ledger 列）

### 5.3 Telegram vs Mini App 分工

- **Telegram Chat = 精简办公**：高频操作、必要反馈、Inline Button 直接动作、自然语言快速查询。3-Step Review（一次连续要求用户动作不超过 3 个）、Zero Re-entry、Action-at-source。Owner 中文看结论/风险/金额/决定；Secretary 英文看下一动作/截止/上传证据。
- **Mini App = 完整后台 / 上帝视角**：对象状态、历史、设置、复杂列表、报表、审计、God View；实现中，尚未完成。

### 5.4 权限边界

- Organization / Membership 是业务权限唯一边界（不是 Telegram 身份本身）
- Telegram identity 不等于业务身份；用户必须通过 Onboarding 绑定到 Organization 的 Membership
- `Owner`（admin）/ `Secretary`（manager/agent）/ `Tenant` 三层角色语义冻结；Bootstrap 只有 Owner 允许调，Secretary 禁止

### 5.5 状态机与并发安全

- **Membership（人员角色）**：role ∈ {OWNER, SECRETARY}，state ∈ {ACTIVE, REMOVED}，CHECK：ACTIVE↔removed_at 互斥；一个 User 一个 Org 最多 1 ACTIVE Membership（`uq_memberships_active_user_org` 部分唯一）。
- **SecretaryInvite（一次性邀请）**：state ∈ {PENDING / ACCEPTED / CANCELLED / EXPIRED}。CHECK 约束 `ck_secretary_invites_state_timestamps`（PENDING→accepted_at & cancelled_at NULL；ACCEPTED→accepted_at NOT NULL；CANCELLED→cancelled_at NOT NULL；EXPIRED 状态）+ `ck_secretary_invites_expires_after_created`（**expires_at NOT NULL 且 > created_at**；字段名：`expires_at`=有效期至、`accepted_at`、`cancelled_at`；**当前无 expired_at 字段**（EXPIRED 是 `InviteState` 枚举值，不是字段名）。fail-closed：PENDING invite 必须绑定有效 Organization 才暴露 org 名称，过期或未绑定则信息不泄露。一个 invite 最多生成一条 Membership（`created_membership_id` UNIQUE，one-time single-consumption）。
- 高风险写路径：PostgreSQL 行级锁 `with_for_update`（secretary_invite accept 并发 claim、财务状态流转、Membership 并发变更）
- Expense：Manager/Secretary 永远不能审批自己创建的支出（403 fail-closed）

### 5.6 简单、确定性优先

- 优先脚本 / SQL / pytest exit code / exit status；确定性能做的不调用多 Agent
- 不引入 RAG、多模型级联、向量库、复杂调度器除非有压倒性证据现有逻辑无法满足
- Logs 先程序化压缩（ERROR/FAILED/traceback/relevant lines）再给 LLM
- 成本控制：尽量 targeted tests，不机械全量

### 5.7 Git & Delivery 红线

- **No force push**，no force-with-lease，no rewrite shared history
- **No auto merge**，SOLO 永不 merge
- **No production deploy**，SOLO 不部署
- **Migration / Data Safety first**：
  - `downgrade` 必须先用 `sa.inspect` 审计列属性（特别是 `timezone=True`、`JSONB` 语义），再 DROP
  - 不盲目 `DROP COLUMN` 可能造成语义丢失的数据列
  - Alembic 单 head，禁止破坏迁移链
- 独立 ChatGPT / CodeRabbit Review 保留在 Milestone PR 边界；SOLO 不自己 review 自己

---

## 6. Definition of Done（Milestone 完成定义）

1. 代码实现完整且符合 §5 产品与工程原则
2. Migrations：
   - `upgrade head` 成功
   - `downgrade -1` 成功
   - 降级脚本含 `sa.inspect` 列属性审计门控（防止 timestamptz/JSONB 语义丢失）
3. Targeted tests 通过；必要 regression 通过
   - 真实失败：必须修，不得 skip/xfail 换 PASS
   - 历史遗留 / 环境失败：必须明确标注并区分
4. Secrets / 权限 / 角色门控正确：
   - 无明文 secrets commit
   - Owner / Secretary 边界正确（Secretary 禁止触发 Owner-only bootstrap）
   - 没有通过 expired / cancelled membership 暴露组织信息
5. 业务代码未破坏 `Business Truth First` 原则
   - Reminder 不会误标记为 Completion
   - Approval 不会误标记为 Payment
   - Operation 只有真实闭环允许 CLOSED
6. Commit 信息符合 Conventional Commits（推荐）；不混入范围外改动
7. PR 创建完成（或更新现有 PR）；CodeRabbit + pasay-gate 证据自行产生，不等待

---

## 7. 交付报告（HANDOFF_COMPLETE Format）

Milestone / Transition 完成后报告格式（中文）：

```
HANDOFF_COMPLETE
================
TASK / MILESTONE: <总控编号或名称>
EXECUTOR: TRAE SOLO
ISSUE:  <#号 + 标题>
PR:     <#号 + URL>
BRANCH:    <branch name>
BASE SHA:  <base commit>
HEAD SHA:  <head commit>

CHANGED FILES:
- <path>
- <path>

TARGETED TESTS:
- <test command / test file> — <PASSED / FAILED:n/m / UNCERTAIN>

CORE CHANGES:
<中文精简>

KNOWN RISKS / UNRESOLVED:
<无，或具体列出>

BUSINESS CODE MODIFIED: YES/NO
MERGED: NO
DEPLOYED: NO
/ND STILL EXECUTABLE: NO
AUTO WAKE CALLS /ND: NO
SOLO_READY: YES
```
