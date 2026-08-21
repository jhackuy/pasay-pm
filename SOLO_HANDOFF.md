# Pasay — SOLO_HANDOFF（TRAE SOLO 完整接手合同）

> **生效：** PASAY-SOLO-TRANSITION-001 (Issue #34) 完成后
> **Canonical:** 本文件 + `project_rules.md` + `CURRENT_ARCHITECTURE.md` 构成全新 TRAE SOLO 会话的完整上下文
> **旧工作流状态：已退役**
>
> ⚠️ **旧 workflow Issue 的 `blocked` / `ready-for-dev` / `/ND` / `route:dev` 等状态不再控制 SOLO 执行。**
>
> ✅ **旧 Issue 内容可作为需求、Bug、历史决策输入。**
>
> 🚫 **SOLO 应先形成 Milestone，再实施。不要机械逐 Issue 一片一 PR 一 STOP。**

---

## 1. Pasay Product Mission / North Star

**North Star：Telegram 里的 AI Property Manager（真人优秀物业管家的 AI 化身）。**

目标用户：小规模房东（约 10 套出租房起步），Owner 一人决策 + 1–2 名 Secretary 协助日常运营。

产品不是「软件 + 聊天窗口」。产品 = Telegram 里一套 Button-first、AI-underneath 的完整物业运营系统：
- **Button-first**：高频动作 1 个按钮完成；四个固定底部按钮永不消失；不要求用户学系统结构
- **AI-underneath**：LLM 藏在下方做上下文理解、自然语言路由、跟进承诺、催办升级；用户不觉得是在「跟模型聊天」
- **Business Truth First**：通知/回复/报价/审批 ≠ 完成；只有钱真正到账、房真正修好、租客真正确认才算完成

四层系统职责（产品设计，00 页）：
1. **Telegram**（完成 ✅）= 精简办公 + 高频动作 + 决策入口
2. **Unit Channel Binding（PR #33 已合入 P0 最小绑定 ✅）/ Property Channel（未来设计目标，未实现 🔶）**
   - **当前实现（CONFIRMED BY `app/models/property_channel.py`）**：`unit_channel_bindings` 最小绑定模型（Unit ↔ Telegram Channel/Group/Topic 绑定 + org scoped + bind/replace/revoke/history/audit）。Docstring 明确："no archive article or render-publish scaffolding"。
   - **未来设计目标（未实现）**：每套房源一个动态档案文章（Property Channel Articles）、业务变化自动增量更新、双语详情、群内「精简结论 + 完整档案跳转」引用模式。
3. **PostgreSQL**（完成 ✅）= 唯一业务事实源，所有写路径幂等 + 审计
4. **Control Panel / Mini App**（完全未开始 ❌）= 完整后台 / 上帝视角 / 报表 / 设置 / 历史

---

## 2. Owner / Secretary 角色与语言边界

**核心：** Telegram ID ≠ 业务身份。完整权威链：
`TelegramIdentityBinding → HUMAN Principal → User → Membership(role, state) → Organization`

| 角色 | DB 表示 | 语言 | Telegram 菜单 | 权限边界 |
|---|---|---|---|---|
| Owner | `Membership.role='OWNER' state='ACTIVE'` | **中文**看结论/风险/金额/决定 | 底部四键中文：🏠房产 ✅待办 💰收租 💸支出 | 全部权限：审批/拒绝收支、支付、Reverse、Bootstrap、设置、佣金规则、查审计 |
| Secretary | `Membership.role='SECRETARY' state='ACTIVE'` | **English**看 next action / deadline / upload evidence | 底部四键英文：🏠 Properties ✅ Tasks 💰 Rent 💸 Expense | 业务 CRUD、确认收入、**不能审批自己创建的支出**、创建任务、查自己佣金；Owner bootstrap 绝对禁止（HTTP 403 fail-closed） |
| Tenant | 未来角色（当前通过 Telegram 身份直接接触，未 Membership 化） | 本地语言（未实现 i18n） | None（通过消息交互） | 看服务状态和简单选择（未完成） |

**冻结规则：**
- Onboarding Bootstrap 仅 Owner 允许调；Secretary 调用直接 fail-closed 403（CONFIRMED BY TESTS：`test_onboarding_p0_024.py`）
- PENDING invite 必须绑定有效 Organization 才暴露组织名；过期 PENDING invite = CANCELLED
- 一个 User 在一个 Organization 最多 1 个 ACTIVE Membership（`uq_memberships_active_user_org` 部分唯一索引）
- Secretary removal 软删除（`state='REMOVED' removed_at=now()`），可重新邀请；不硬删审计事实

---

## 3. Telegram 与 Mini App 分工

### Telegram Chat = 精简办公
- 主工作区：Telegram Chat 直接操作
- 主要交互：**Inline Keyboard Buttons**（reply/action 直接挂在事件消息下，Action-at-source）
- 固定底部 Reply Keyboard（Owner 中文 / Secretary 英文 / Group 英文）
  ```
  🏠 Properties | ✅ Tasks
  💰 Rent       | 💸 Expense
  ```
  Group 只发英文避免双语混乱；`More / ☰ 更多` 仅 legacy alias，不是 canonical 顶部入口
- 不设计成「菜单 → 子菜单 → 子菜单 → 表单」
- **3-Step Review 铁律：** 一次业务连续要求 Owner/Secretary 超过 3 个动作必须重新设计；高频任务人工操作 ≤ 1
- **Zero Re-entry：** 数据库已知道的数据绝不重复问用户
- **Role-aware UX：** Owner 中文看金额/风险/决策；Secretary 英文看下一动作/证据上传
- **Notification Budget：** 正常事件默认静默、完成事件汇总、需要决定即时通知、重大异常即时升级 Owner
- **Message Mutation：** 操作后优先更新原消息（editMessageText/ReplyMarkup），不制造大量垃圾消息

### Mini App = 完整后台 / 上帝视角（未实现 ❌）
- 完整控制台 / 对象状态历史和设置
- 报表、审计、佣金结算、God View
- 不与 Telegram Chat 抢高频动作；Mini App 只做 Telegram 做不了的复杂列表和历史查询

---

## 4. 已冻结生产架构（ARCHITECTURE_FROZEN = YES）

详细文件：`CURRENT_ARCHITECTURE.md`（禁止私自修改拓扑）。

```
Telegram api.telegram.org
      │ HTTPS POST /telegram/webhook
      │ + X-Telegram-Bot-Api-Secret-Token
      ▼
┌─ Cloudflare Worker (pasay-cloudflare-worker) ─┐
│  (A) fetch handler — Telegram ingress:        │
│       校验 method/content-type/secret         │
│       封装 PASAY-QUEUE-ENVELOPE-V1            │
│       PASAY_QUEUE.send(telegram_update)       │
│  (F) scheduled() handler — Cron ingress:      │
│       5-minute bucket 幂等 → 同一队列 send    │
│  (C) queue() consumer → Container binding:    │
│       每条消息 → POST /internal/ingest        │
└────────────────────────┬──────────────────────┘
                         │ Cloudflare Queue (pasay-events)
                         │ max_retries=5, DLQ=pasay-events-dlq
                         ▼
┌─ Cloudflare Container (pasay-container) ─────┐
│  Dockerfile: python:3.11-slim → uvicorn       │
│  PASAY_RUNTIME_MODE=cloudflare-container(HC)  │
│  entrypoint: alembic upgrade head             │
│  POST /internal/ingest (Container 私有):      │
│    telegram_update → services.telegram_webhook│
│    scheduled_job → scheduled_job_ledger 幂等  │
│  GET /health: 架构快照 + 冻结拓扑声明          │
└───────────────────────┬───────────────────────┘
                        ▼
              Neon PostgreSQL 16（唯一事实源）
                • 12+ 业务表 + migrations
                • alembic single-head（未破坏链）
```

**队列合同（PASAY-QUEUE-ENVELOPE-V1）：**
TypeScript 源：[envelope.ts](file:///d:/AI-Review/pasay-pm/cloudflare-worker/src/envelope.ts)
Python Pydantic：[envelope.py](file:///d:/AI-Review/pasay-pm/app/schemas/envelope.py)

| 字段 | 值 |
|---|---|
| `version` | `"1"` 字面量 |
| `kind` | `"telegram_update"` \| `"scheduled_job"` |
| `event_id` | `"tg:<update_id>"` / `"sched:<job>:<YYYY-MM-DDTHH-MM>"`（5-min 桶） |
| `occurred_at` | ISO-8601 UTC |
| `payload` | Telegram Update JSON 或 `{job_name, scheduled_at, params?}` |

**禁止：** Redis / Kafka / RabbitMQ / Celery / Temporal / 第二套 DB / 第二套 Bot token consumer。Worker 零解释（直接把 Telegram update 送队列），Worker 不写 Neon、不调 PTB、不调 LLM。

**Legacy Runtime（development-only，已明确退出生产）：**
1. Long Polling / Hermes 双 consumer：409 根因；仅保留 Hermes 作为独立 supervisor/AI capability，**不得再消费同一 bot token updates**
2. Native Windows PTB polling：`bin/pasay_runtime.py` / `start-native-bot.ps1` = development-only；Docker/Container 永远不引用
3. Webhook+polling 双主：已取消；生产只保留 Worker→Queue→Container 单链（CONFIRMED BY `test_prod_arch_closeout_p0_031.py`）

---

## 5. 当前核心数据模型

**PostgreSQL 16 / Alembic single-head。** 所有金额 `NUMERIC(14,2)`（Python `Decimal`，禁 float）；时间一律 `timestamptz`（Python `datetime + timezone.utc`）。

### Person / Identity 链
| 表 | 关键字段 | 说明 |
|---|---|---|
| `organizations` | `id`, `name` | 组织（房东实体），1..N Owner + 0..N Secretary |
| `users` | `id`, `email?`, `display_name?` | 自然人抽象 |
| `telegram_identity_bindings` | `user_id`, `telegram_id`, `is_primary`, `verified_at` | Telegram ↔ User 绑定；不是权限本身 |
| `memberships` | `org_id`, `user_id`, `role[OWNER/SECRETARY]`, `state[ACTIVE/REMOVED]`, `joined_at`, `removed_at`, `removed_by?`, `removal_reason?`, `invited_by?` | **权限真实边界**。CHECK 约束：ACTIVE↔removed_at 互斥；一个 User 一个 Org 最多 1 ACTIVE Membership（`uq_memberships_active_user_org` 部分唯一） |
| `secretary_invites` | `code UNIQUE`, `org_id NOT NULL`, `created_by_membership_id NOT NULL`, `invited_name_hint?`, `state[PENDING/ACCEPTED/CANCELLED/EXPIRED]`, `expires_at NOT NULL（有效期至，> created_at）`, `accepted_at?`, `accepted_by_user_id?`, `cancelled_at?`, `cancelled_by_membership_id?`, `created_membership_id? UNIQUE（最多生成一条 Membership）`, `note?` | 一次性、单消费、可过期邀请。CHECK：PENDING→accepted/cancelled NULL；ACCEPTED→accepted_at NOT NULL；CANCELLED→cancelled_at NOT NULL；EXPIRED 状态存在。**无 expired_at 字段**（EXPIRED 是 state 枚举值） |
| `api_keys` | `key_hash`, `user_id`, `role[admin/manager/agent]`, `revoked_at` | 旧 Bearer API Key 体系（向后兼容；逐渐迁移到 Membership） |

### 财产/租约链
| 表 | 关键字段 | 说明 |
|---|---|---|
| **`properties`** | `organization_id BIGINT NULL ⚠️ TECH DEBT`, `name`, `address`, `city`, `total_units` | Property 组织归属仍可 NULL（legacy 数据遗留），技术债见 §11 |
| `units` | `property_id NOT NULL`, `unit_number`, `floor`, `size_sqm`, `monthly_rent`, `unit_state VARCHAR(自由)`, `status[vacant/occupied/maintenance]` | 单元；unit_state 自由 VARCHAR 不是 enum（PARTIAL） |
| `tenants` | `primary_contact_name`, `phone`, `email`, `organization_id` | 租客 |
| `leases` | `unit_id`, `tenant_id`, `start_date`, `end_date`, `monthly_rent`, `deposit_amount`, `due_day`, `status`, `accounting_start_date` | 租约；佣金引擎基准；到期事件生成 scheduled |

### 财务链
| 表 | 关键字段 | 说明 |
|---|---|---|
| `incomes` (Rent) | `lease_id?`, `unit_id`, `amount NUMERIC`, `status[pending/confirmed/reversed]`, `payment_method`, `idempotency_key UNIQUE`, `confirmed_by/reversed_by` | 收入/租金；**confirmed 禁改金额、禁 DELETE；只能 reverse**。Amount alone is never a match. |
| `income_matchings` | `income_id`, `payment_amount`, `matched_at` | 部分付款匹配台账 |
| `expense_claims` | `unit_id?`, `amount NUMERIC`, `status[pending/approved/rejected/paid/reversed]`, `created_by`, `approved_by`, `paid_by`, `due_date`, `receipt_attachment_id`, `payment_claim_truth` | **Approval ≠ Payment**。APPROVED 未付仍在 Owner payable 队列；PAID 才关任务。审批自己创建的支出 Manager 403。 |
| `payment_matches` | `expense_id`, `payment_ref`, `matched_at`, `matched_amount`, `verification_status` | Payment Claim ≠ Verified Payment（见 §5.11 产品规则） |
| `commission_rules` | `org_id`, `rule_type[percentage/flat]`, `value NUMERIC`, `target_role[OWNER/SECRETARY]`, `effective_from` | 佣金规则 |
| `commission_settlements` | `org_id`, `user_id`, `period_start/end`, `computed_amount NUMERIC`, `status[draft/confirmed]` | 佣金结算。**computed_amount 只由引擎写入。API 传入无效。** |
| `financial_idempotency` | `idempotency_key UNIQUE` | 跨表写路径幂等 |
| `audit_logs` | `actor_user_id`, `table_name`, `record_id`, `action`, `old_values`, `new_values` | 所有关键写路径审计 |

### 业务运营链
| 表 | 关键字段 | 说明 |
|---|---|---|
| `operations_tasks` | `org_id`, `task_type[RENT_DUE/RENT_OVERDUE/APPROVAL_PENDING/PAYMENT_PENDING/REPAIR/AC_MAINTENANCE/...]`, `status[OPEN/CLOSED/BLOCKED]`, `severity`, `next_actor`, `next_action`, `deadline`, `unit_id?`, `lease_id?`, `expense_id?`, `requires_human_decision` | 真人动作投影（Task ≠ Completion）。Operation 只有现实问题真正解决才 CLOSED |
| `operation_promises` | `task_id`, `promised_by`, `promised_at`, `next_check_at`, `escalation_count`, `MAX_ESCALATION=3` | **承诺机制：** AI 说「会跟进」必须落地结构化 promise；到期自动提醒，超 N 次升级 Owner。否则就是口头承诺 |
| `outbox_notifications` | `task_id`, `recipient_telegram_id?`, `channel_id?`, `status[PENDING/SENT/FAILED]` | 通知投递 + redelivery |
| `scheduled_job_ledger` | `event_id (sched:<job>:<bucket>)`, `job_name`, `scheduled_at`, `consumed_at?`, `payload JSONB` | Cron + scheduled task 幂等写入。INSERT ON CONFLICT DO NOTHING → 200/208 |
| `repair_records` | `unit_id`, `task_id`, `repair_stage[ISSUE_REPORTED→QUOTE→APPROVED→IN_PROGRESS→EVIDENCE→COMPLETED→CLOSED]`, `contractor_info`, `invoice_amount`, `associated_expense_id` | REPORTED→…→COMPLETED→EXPENSE→PAID。完成后缺凭证 → 自动 FOLLOWUP Secretary |
| `unit_lifecycle_events` | `unit_id`, `event_type[NEW/VACANT/OCCUPIED]`, `event_at`, `notes` | 单元生命周期事件（PARTIAL；没有 SOLD/ARCHIVED 强制迁移） |

### Property Channel（当前实现 = Unit Channel Binding Foundation；未来动态档案 = 设计目标未实现）
| 分类 | 内容 | 状态 / 说明 |
|---|---|---|
| **当前实现（CONFIRMED BY CODE：`app/models/property_channel.py`）** | **`unit_channel_bindings` 表（`UnitChannelBinding` 类）** — 字段：`organization_id NOT NULL`, `unit_id NOT NULL`, `purpose enum archive\|business_group NOT NULL`, `channel_chat_id`, `thread_topic_id?`, `status ACTIVE\|REVOKED`, `revoked_at?`, `revoked_by_membership_id?`, `notes?`。DB guarantees：partial UNIQUE (unit_id, purpose) WHERE status='ACTIVE'（每 unit 每 purpose 最多 1 个 ACTIVE 绑定）；ACTIVE→channel_chat_id NOT NULL；REVOKED↔revoked_at 互斥；`ix_unit_bindings_org_unit_status` 索引。 | **PR #33 已合入的 P0 最小绑定**。Docstring 原文："exactly the Issue #25 P0 minimal binding, **no archive article or render-publish scaffolding**"。 |
| **未来设计目标（NOT implemented — 设计输入，不是 backend 已实现结构）** | `property_channel_articles`（每套房一个动态档案 message：publish_status DRAFT/PUBLISHED/ARCHIVED、last_edited_at、version、publish/unpublish/edit 幂等流程、业务变化自动增量更新、双语详情）；`property_channel_pins`（频道内引用索引 / pin 管理）；群内引用「精简结论 → 跳频道完整档案」交互；动态档案和 Evidences/Attachments 解耦（私有存档频道 ≠ 房产档案出版物）。 | **完全未实现，仅 OpenDesign / 旧 Issue body 设计残留**；不得当作现有表或已完成能力。 |

### AI / Copilot
| 表 | 关键字段 | 说明 |
|---|---|---|
| `copilot_sessions` / `copilot_context` | 自然语言路由上下文 | `services/copilot/` 下的 ask/today/why/execute/nl_parse 模块 |
| `copilot_proposals` | `proposal_type`, `status[PENDING/CONFIRMED/REJECTED]`, `proposed_by`, `confirmed_by`, `payload` | AI PREPARE + Owner CONFIRM 双步；Owner 不点 ✅ 不执行 |

### 附件/证据
| 表 | 关键字段 | 说明 |
|---|---|---|
| `attachments` | `file_path`, `mime_type`, `size_bytes`, `storage_provider[local/telegram_channel/s3]`, `uploaded_by` | 通用附件 |
| `evidences` | `task_id?`, `expense_id?`, `repair_id?`, `external_message_id（私有存档频道）`, `attachment_id` | 证据链。**私有 Telegram 存档频道是媒体存档，不是 Property Channel 房产档案出版物。** 别混淆。 |

---

## 6. 已完成能力（CONFIRMED BY CODE + TESTS）

来源：`PRODUCT_CONFORMANCE_AUDIT_001.md` (61% pass) + 2026-08-20 合入的 PR #33 + 最新 tests/ 与 pasay-telegram-bot/tests/ 结果。

✅ **四个固定按钮**：`keyboards.FIXED_MENU_ROUTES`，Owner 中文 / Secretary 英文 / Group 英文
✅ **房产上帝视角 Quick Card**：逾期>将到期>空置>正常排序
✅ **单房 Quick Status + Unit Timeline** + `unit_page_keyboard`
✅ **Owner Attention 过滤**：只把审批/付款/决策事项进 Owner 队列，不把 Secretary 任务倒垃圾给 Owner
✅ **Rent 完整闭环**：DUE→PARTIAL→PAID→OVERDUE→FOLLOWUP+Promise+Escalation。部分付款匹配台账 + idempotency_key 唯一键 + 审计 confirm/reverse + 409 防重
✅ **Repair 主动跟进闭环**：ISSUE_REPORTED→QUOTE→APPROVED→IN_PROGRESS→EVIDENCE→COMPLETED→Expense。完成后 24 小时无凭证 → `ensure_evidence_followup` → 秘书 FOLLOWUP
✅ **Expense PENDING→APPROVED→PAID** 分步行；Manager 禁批自己创建的支出；APPROVED 未付继续留 Owner payable 队列；Amount alone is never a match（强字段匹配才提示相似）
✅ **AI 主动运营闭环**：Scheduler 自动生成 RENT_DUE/RENT_OVERDUE/APPROVAL_PENDING/PAYMENT_PENDING 任务；Promises 机制 + Outbox 通知；Copilot Proposals (PENDING→CONFIRMED→EXECUTE) 双步
✅ **AI Persona / 中英策略**：i18n bi 双语 renderer；Owner 中文 / Secretary 英文双写
✅ **Button + Fast Path 绕开 LLM**：`conversation.handle_message` 先固定路由精确匹配，再 NL/LLM。`/operations/quick/*` 和 `/operations/copilot/today` 默认无 LLM。LatencyTracker 记录 wall-clock
✅ **Operations / Scheduled / Promises / Outbox 全栈**
✅ **Commission 纯函数引擎** `compute_settlement(settlement, rule, lease_amount) → Decimal`。引擎写 `computed_amount`；客户端传被忽略
✅ **Audit logs 全路径**
✅ **Telegram Webhook → CF Worker → CF Queue → CF Container → Neon** 生产链路（PR #31 收口，CONFIRMED BY `test_prod_arch_closeout_p0_031.py`）
✅ **Identity V13**（IdentityBinding → HUMAN → User → Membership）
✅ **Onboarding P0**（Owner Bootstrap + Secretary Invite + PENDING/ACCEPTED/CANCELLED/EXPIRED state-timestamps + fail-closed Secretary 防越权，CONFIRMED BY `test_onboarding_p0_024.py`）
✅ **Membership & Secretary Invite P0**（Membership: ACTIVE↔removed_at CHECK + `uq_memberships_active_user_org` partial unique；SecretaryInvite: `ck_secretary_invites_state_timestamps` state-timestamps + `expires_at NOT NULL > created_at` + one-time single-consumption；并发场景 `with_for_update` 行级锁）
✅ **Unit Channel Binding P0**（PR #33，`unit_channel_bindings` 最小绑定模型 + org/unit/status 索引 + partial unique (unit,purpose) for ACTIVE + ACTIVE/REVOKED 双时间戳 CHECK；**没有 publish/unpublish/edit 流程，没有 property_channel_articles 表**）
✅ **Cloudflare Worker 真实编译门（PR #32 FIX11）**：`tsconfig.json` + `tsconfig.tests.json` 双配置；engine-strict；Node 22 锁死

---

## 7. 当前部分完成能力（PARTIAL）

🔶 **Property Lifecycle（Partially Done）**：
- 有 `unit_state VARCHAR(自由)` + `unit_lifecycle_events` 表；但**没有 SOLD/ARCHIVED 枚举/强制合法迁移/归档筛选**
- 有 Unit add confirm；**没有「出售/归档」高风险确认流**
- 软删除存在但没有 archived/sold 过滤查询端点

🔶 **Latency & Reliability Observability**：
- 有 `LatencyTracker` 记录 wall-clock
- **无指标看板、无 SLO 可视化、无队列 backlog monitor**
- Promise/Escalation 决定重催 vs 升级但仅单点，**无统一通知预算配置**

🔶 **通知预算（Partially Done）**：
- 「低价值默认静默/中价值摘要/行动即时通知/高风险 @」只有局部实现
- **无统一 Notification Budget Engine**；各模块各自决定通知节奏

🔶 **Property Channel 动态档案功能（完全未实现）**：
- PR #33 只合入了 **Unit Channel Binding 最小绑定 foundation**（`unit_channel_bindings` 表，见 §5）
- **Property Channel 动态档案（property_channel_articles / property_channel_pins 结构、publish/unpublish/edit 幂等流程、频道双语详情、业务变化自动增量更新、群内精简结论 + 跳转档案引用模式）全部未实现**
- 私有 Telegram 存档频道（Evidences 证据链）≠ Property Channel 房产档案出版物（设计态），两者边界已在 §5 附件部分界定

🔶 **Legacy ↔ New API Key ↔ Membership 兼容**：
- 旧 `role=admin/manager/agent`（Bearer API Key）存在
- 新 Membership（OWNER/SECRETARY）存在
- 两者的完整等价映射与最终退役 API Keys 路线**未最终定案**

---

## 8. 当前未完成能力（MISSING）

❌ **Excel/照片/文件夹/ZIP/合同导入**：无 openpyxl、无 staging 表、无 conflict preview、无确认导入安全流
❌ **完整 Excel 导出**：现 `reports/` 仅服务器聚合 JSON；无 Spreadsheet/CSV 批量导出
❌ **Control Panel / Mini App**：完全未开始。仅 Telegram Chat 做精简办公
❌ **`merchant_id` 多租户边界**：全库 grep merchant = 0 命中。当前单 Organization assumption 工作，多房东（多 merchant）架构地基未打
❌ **Rent P0：Organization / Unit scoped 全量闭环**（Issue #26 标记 blocked）：基础 Rent 完成，但跨 Organization 严格过滤 + Unit scoped 权限严格切片 + Secretary 严格租约可见性范围未硬化到全接口
❌ **Repair P0：Organization / Unit scoped 全量闭环**（Issue #27 标记 blocked）：基础 Repair 完成，但同上 scoped 权限 + Repair 到 Payment Claim → Verified Payment 完整对账未硬化
❌ **Telegram → TRAE Auto Wake → `/ND` 链路（Issue #29 / PR #30）**：本 Transition 已明确**退役**，不应再继续投入
❌ **`/ND` 显式 Issue 参数（Issue #22 / PR #23）** 已退役
❌ **OpenDesign 自动派发（Issue #5 / PR #16）**：Auto dispatch 存在但 `/ND` 退役后应切换为 SOLO 输入
❌ **Property Channel 动态档案全部能力**：property_channel_articles / property_channel_pins 结构、publish/unpublish/edit 幂等、自动双语详情、业务变化自动增量更新、群内引用跳转；**当前仅有 `unit_channel_bindings` 最小绑定 foundation（PR #33）**，动态档案 0%
❌ **Tenant 端自助服务 Portal / Bot**：当前 Tenant 仅被动收通知；无主动查询
❌ **指标 & 看板 & KPI Dashboard**：完全无

---

## 9. 已知 Bug（2026-08-21 snapshot）

基于近期返修与测试：

1. **HTTP 测试时钟漂移**（已在 Onboarding P0 FIX3 中用 `timezone.utc` 对齐修复为策略，但要注意新 HTTP tests 继续沿用）：测试用例必须以 `timezone.utc` 与 API 服务端时钟对齐
2. **Ruff 规范红线**（已修复多处，但作为已知代码风格 Bug 模式）：
   - E741：避免单字母变量 `l`（与数字 1 混淆）
   - RUF059：解构未使用的变量加 `_` 前缀
   - UP007：用 `X | Y` 而非 `Union[X,Y]`（Python 3.10+）
   - UP031：用 f-string 而非 `%` 或 `.format()`
   - UP035：用 `collections.abc.Iterable` 而非 `typing.Iterable`
3. **FakeBot 语义固化**（WF_Guardrails 17.1）：带非 inline `ReplyKeyboardMarkup` 的消息在真实 Telegram 不可 `editMessageText`（400 Message can't be edited）。FakeBot 必须保留该语义，禁止未来为追 PASS 删除
4. **Alchemic downgrade 语义丢失**（Membership P0 FIX2 中首次实现门控）：任何 Alembic `downgrade` 前必须 `sa.inspect` 审计列属性，不能盲目 DROP：
   - `timezone=True` → 若变成 timestamp without tz = 语义损失（所有历史时刻错了 8 小时或任意）
   - `JSONB` → TEXT = 键值索引失效 + schema 可破坏
5. **`SecretaryInvite` 状态-时间戳合规性 CHECK**（Membership P0 FIX2 首次引入门控模式，Alembic downgrade 同原则）：`ck_secretary_invites_state_timestamps` 约束状态↔时间戳互斥一致（PENDING→accepted/cancelled NULL；ACCEPTED→accepted_at NOT NULL；CANCELLED→cancelled_at NOT NULL；EXPIRED 状态存在）+ `ck_secretary_invites_expires_after_created`（expires_at NOT NULL 且 > created_at）。**字段名：expires_at（有效期至，NOT NULL）/ accepted_at / cancelled_at；当前无 expired_at 列（EXPIRED 是 `InviteState` 枚举值，不是字段名）**。一个 invite 只允许生成最多一条 Membership（created_membership_id UNIQUE，one-time single-consumption）
6. **`ready-for-dev` / Auto Wake 残留 race**：本 Transition 全链路退役后此条自然失效

---

## 10. 技术债（可被 Milestone 分批清偿，但不阻塞业务）

| 技术债 | 说明 | 风险 |
|---|---|---|
| **Property.organization_id = NULL（详见 §11）** | `properties.organization_id` 允许 NULL（历史 legacy）。Organization scoped enforcement 在部分接口无法严格切分 | 多 Organization 时数据泄露；无法做行级权限 |
| **`unit_state` free VARCHAR vs legacy `UnitStatus` enum 并存** | `Unit.unit_state` 是 VARCHAR；另有 `UnitStatus(status)` enum。语义重复 | 未来状态机强制合法迁移困难 |
| **i18n `More / ☰ 更多` 中文 legacy alias** | 与 canonical 英文 2×3 菜单共存 | 长期 UX 维护成本 |
| **双任务系统**：`operations_tasks`（AI 运营任务投影）+ `tasks`（旧 CRUD Task）并存 | 两套独立的 status/recurring/assigned | 未来必须合并；否则 Owner 不知道真正待办在哪 |
| **双权限体系**：旧 `api_keys.role[admin/manager/agent]` vs 新 `membership.role[OWNER/SECRETARY]` | 两套角色边界在 router deps 里混合判断 | 边界漏洞；最终要把所有权限门全部迁到 Membership |
| **Cloudflare Worker `index.ts` Durable Object Branded types TS2589 兼容 workaround** | FIX11 中为过真实编译做了静态 string map 替代函数型；未来升级 TS 可回退但当前 OK | 维护成本 |
| **`app/services/copilot/llm.py` 依赖 provider/env 未标准化** | 仅基础集成，未定义 model 选择策略和 budget | 未来引入多模型时要重写 |
| **本地开发 Runtime 配置散乱**：`pasay-telegram-bot/` 有自己的 pyproject.toml，与根 `requirements.txt` 未锁同依赖版本 | 偶发 CI 与本地依赖漂移；CI `pr-ci.yml` 已经做 npm cache，但 Python 端仍无 pip/poetry.lock | 升级依赖时风险 |
| **no-global-dispatch 模式未 100% 关闭全局 PTB polling start** | 已经通过 Dockerfile HC + `test_prod_arch_closeout_p0_031.py` 锁定生产；未完全去除 legacy `bin/` 脚本内 polling 启动入口（仅 development-only） | 人类误操作启动 production 风险 |

---

## 11. Legacy `Property.organization_id = NULL` 技术债

**证据：** `app/models/property.py:21-23`
```python
organization_id: Mapped[int | None] = mapped_column(
    BigInteger, ForeignKey("organizations.id"), nullable=True, index=True
)
```

### 背景
Pasay 早期单房东 assumption：所有 Property 同属于 Owner 直接的上下文，不需要显式组织切分。引入 Organization / Membership 体系后，`Property.organization_id` 是后加的且 nullable，以兼容历史数据。

### 影响
1. 所有 `GET /properties`、`GET /units`、`GET /leases`、`GET /tenants`、`GET /incomes`、`GET /expenses`、`GET /repairs` 的严格 Organization scoped 过滤无法对 legacy `organization_id=NULL` 的行生效
2. 多 Organization（多 merchant）架构无法在 Property 层做严格切分
3. Issue #26 / #27（Rent P0 / Repair P0 的 scoped 闭环）因此被标 `blocked`

### Owner 需要决定的事项（Milestone）
- 是否要求所有 `Property.organization_id` 做 non-null 回填（一个迁移 + 回填脚本 + `CHECK(organization_id IS NOT NULL)`）
- 历史 NULL 行默认归属哪个 Organization（以及是否允许在运行时继续创建 NULL 行）
- `units`（`property_id NOT NULL`，有 FK）、`leases`、`expense_claims.unit_id` 等通过 Property 间接组织归属的表，是否需要冗余 `organization_id` 以便直接做权限门控（减少 JOIN + 提高性能/安全性）

⚠️ **SOLO 在 Owner 未决定前，禁止私自把所有 NULL `organization_id` 改成某个默认值。** 可能改变真实业务权限边界。

---

## 12. 当前 GitHub 未完成 Issues / PRs（历史输入 ≠ 机械执行）

### Open Issues 11 个
| # | 标题 | 标签 | 说明（TRAE SOLO 视角） |
|---|---|---|---|
| **34** | **PASAY-SOLO-TRANSITION-001 退役 /ND 切换 SOLO 模式** | — | 本 Issue；当前 Transition 任务 |
| 29 | TRAE-010 GitHub → TRAE Auto Wake P0 | route:dev | **已随本 Transition 退役**。实现不再需要；保留作历史记录 |
| 27 | TRAE-009 Repair Operation P0（Org/Unit scoped） | route:dev, blocked | 关键能力，但 blocked 因 §11 Property.org_id NULL tech debt。SOLO 可作为 Milestone 先做 debt 清理 + 硬化 |
| 26 | TRAE-008 Rent Collection P0（Org/Unit scoped） | route:dev, blocked | 同上 |
| 22 | PASAY-TASK-005 /ND 显式 Issue 参数 | route:dev | **已随本 Transition 退役**。ND no longer exists |
| 12 | PASAY-ND-WORKFLOW-DOC-SYNC-001 /nd 接入 GitHub dev workflow 文档 | route:dev | **已随本 Transition 退役** |
| 9 | PASAY-TRAE-ND-001 /nd 一键执行下一批准开发任务 | — | **已随本 Transition 退役** |
| 7 | PASAY-AUTOMATION-WRITE-PROBE-001 GitHub MCP Issue→TRAE→PR 最小写入验证 | — | 历史工程验证 Issue；已完成验证（PR #8 exists） |
| 5 | PASAY-OPENDESIGN-AUTO-DISPATCH-001 | route:dev | 自动派发存在，但 `/ND` 退役 → 改成 SOLO 输入模式。后续再议 |
| 4 | PASAY-EXPENSE-VNEXT-001 Expense 业务闭环 Design→Dev | route:design-dev | Expense 基础已 PENDING→APPROVED→PAID（审计 PASS），后续 VNext 是否需要 OpenDesign 设计输入取决于 Owner |
| 3 | PASAY-PRODUCT-RULES-001 建立产品规则事实源 | — | 本 §16 产品规则即部分响应；后续可整理成独立 PRODUCT_RULES.md |

**⚠️ TRAE SOLO 规则：** 这些 Issue 只是需求/Bug/历史决策的输入。**不要机械 1 Issue = 1 分支 = 1 PR = STOP**。由 SOLO 先理解全局，形成 Milestone 计划（如：Milestone A = 清理 tech debt + 打通 Property.org_id NOT NULL + 硬化 Rent Org scoped + 硬化 Repair Org scoped），再执行。

### Open PRs 8 个
| # | 标题 | 分支 | 状态 |
|---|---|---|---|
| 30 | feat(trae-auto-wake): PASAY-TASK-010 Auto Wake P0 | issue/29-trae-auto-wake-p0 | open — 本 Transition 退役 /ND → 本 PR 可由 Owner 决定 close 还是改造成 SOLO wake 模式 |
| 23 | docs(nd): /ND 显式 Issue 参数 | issue/22-nd-explicit-issue-param | open — 随 Transition 退役 → 建议 Owner close |
| 17 | feat(opendesign): OpenDesign → GitHub autosync | wf/OPENDESIGN-GITHUB-AUTOSYNC-004-... | open — 设计侧自动同步；保留？Owner 决定 |
| 16 | PASAY-OPENDESIGN-AUTO-DISPATCH-001 | chore/opendesign-auto-dispatch-005 | open — 设计侧派发；保留？Owner 决定 |
| 13 | docs: sync /nd into GitHub dev workflow | issue/12-nd-workflow-doc-sync | open — 本 Transition 已用 `GITHUB_DEV_WORKFLOW.md` 新 SOLO 版覆盖 → Owner 决定是否 close |
| 10 | chore: add Trae Work governance project files | chore/pasay-trae-governance-001 | open — `.trae/` 目录结构首次引入，基础已被本 Transition 重写 |
| 8 | PASAY-AUTOMATION-WRITE-PROBE-001 verify Issue→PR flow | chore/pasay-automation-write-probe-001 | open — 历史工程验证 |
| 6 | chore: converge Cloudflare and Neon foundation | chore/recovery-converge-002 | open — 基础收束；Owner 决定是否 merge |

---

## 13. Rent / Expense / Repair / Operation 后续能力优先级建议

### 高价值优先（Product Truth 完整性）
1. **Rent：Org/Unit scoped 全硬化（解决 Issue #26 blocked）**
   - 先决：Milestone A 先清偿 §11 Property.org_id NULL tech debt
   - 动作：所有 Rent 相关接口（Income, Income Matching, Lease status, Quick Rent）强制 `WHERE organization_id = current_user.org_id`；加 failing tests 保证不过就红
2. **Repair：Org/Unit scoped 全硬化（解决 Issue #27 blocked）**
   - 同上先决；Payment Claim → Verified Payment 对账硬化（claim 不等于到账凭证）
3. **Property Channel 动态档案（设计目标，未来能力）业务联动增量更新**
   - 依赖先实现 `property_channel_articles` 动态档案（当前未实现；仅有 `unit_channel_bindings` 最小绑定）
   - 房租状态/维修状态/租约变化时，只更新 channel article 的相关段落，不整篇重发
4. **Operation CLOSED 语义硬化**
   - 任何 `operations_tasks.status = CLOSED` 都必须对应：PAID 财务到账 / Repair 完成凭证 + Expense PAID / Rent 全结清 / 任务完成确认。禁止手工直接 CLOSED 没有证据链

### 次高价值（运营效率）
5. Excel/照片导入（staging → conflict preview → confirmed import）
6. 全接口 Organization scoped enforcement audit（全量 fuzzing 未授权跨 org 访问必须 403）
7. 双任务系统合并（operations_tasks vs 旧 tasks）
8. 双权限体系并轨：把旧 API Key 体系 router deps 全面迁到 Membership

### 中期产品方向
9. Mini App / Control Panel 初版
10. Tenant 端自助查看租约 + 租金 + 报修状态
11. 指标 & KPI 看板
12. Merchant ID（多房东多组织）地基

---

## 14. OpenDesign 作为 UX/UI Source of Truth

UX/UI 设计事实源是 `pasay-opendesign` 仓库（独立仓库：`jhackuy/pasay-opendesign`），不是本仓库的 Penpot 或本地 figma。

**Workflow（SOLO Milestone 模式版本）：**
```
Owner 产品语义决定
         ↓
  OpenDesign 完成设计（.odd 文件、组件、页面、交互文案、双语内容）
         ↓
  GitHub Issue 关联 design handoff（design-dev route）
         ↓
  TRAE SOLO 读取 OpenDesign + GitHub Issue → 纳入 Milestone
         ↓
  实现 targeted tests / code / migrations
         ↓
  PR → ChatGPT Review + CodeRabbit → Owner Acceptance → Owner merge
```

**规则：**
- 冻结设计规则，SOLO 不自行重写
- 设计文案、颜色、按钮位置、状态机 = 事实；SOLO 只做代码实现
- 设计有歧义时：OpenDesign 侧查 `.odd` 文件结构定义；仍不确定 → 停并请求 Owner，不猜
- 本仓库 Penpot `.audit/pages/00.md` 到 `14.md` = 2026 历史审计快照（只读），不是 Source of Truth（只是 audit 存档）

---

## 15. 当前仍需继续调整的架构/工程问题

1. **双权限体系并轨（API Key Bearer ↔ Membership）路线未定案**：建议 Milestone 明确目标是全量迁到 Membership，API Keys 只留 service account 用途（非用户级）
2. **Python 依赖管理**：根 `requirements.txt` + `pasay-telegram-bot/pyproject.toml` 双套未统一。建议：要么全 Poetry 单 pyproject.toml workspaces，要么至少 bot 端改 requirements.txt 形式 + CI 双 lock hash
3. **scheduled_job 完整 job_name 清单**：当前只有 ledger 基础；Rent Overdue Nightly、Digest Daily、Promise Escalation、Repair Evidence Followup 命名约定和 runbook 未统一
4. **queue DLQ（pasay-events-dlq）人工处理 runbook**：5 次重试死信后 → 当前无人处理。需要 Owner 决定：Cloudflare Dashboard 手动？还是 Container 侧实现 DLQ re-drive admin endpoint（带 Owner-only 权限门）
5. **Unit `unit_state VARCHAR` → 枚举化 + 合法迁移矩阵**：SOLD/ARCHIVED 明确产品语义（§5 MISSING 项）
6. **Operations Next Actor 规范**：当前 next_actor 自由字符串。建议 enum `{OWNER, SECRETARY_GROUP, SPECIFIC_USER_ID, AI, TENANT, EXTERNAL_CONTRACTOR}` 并强制合法枚举
7. **No-global-PTB-polling 硬化到 container liveness**：`GET /health` 已 architecture_frozen snapshot；加一条 liveness probe 若 `process_update()` 被调且 `run_polling()` 进程存在 → 503 unhealthy → k8s / CF restart
8. **Cloudflare Worker ↔ Container X-Pasay-Ingest-Token：** 目前是环境变量；是否要 rotation 机制（旧→新并行接受 N 小时）？Owner 决定
9. **CF Worker typechain**：envelope.ts 和 Python envelope.py 必须手动同步；无自动化 CI 校验一致性。建议写轻量 JSON schema 生成 CI（如不一致 CI 失败）
10. **Windows ↔ Linux CI 差异**：当前本机 canonical authority Windows；CI Ubuntu。测试中的路径、换行符、TZ、path separator 偶发漂移（已通过大部分 `timezone.utc` 对齐修复，但未来复杂脚本/路径处理再注意）

---

## 16. 产品规则（Product Rules — 业务正确性红线）

**SOLO 永远不要违反这些规则。违反 = FAIL CLOSED。**

### 16.1 Business Truth First
- Reminder / Reply / Notification ≠ Completion
- Task ≠ Completion；Task 只是真人动作投影
- Quote / Proforma / Estimate ≠ Expense
- Approval ≠ Payment
- Payment Claim（声称已付 + 截图/回执）≠ Verified Payment（凭证对账 + 金额一致 + 不可抵赖）
- Partial Rent（部分付款）≠ Paid（完全结清）；「部分已付」要永远暴露剩余
- Operation CLOSED：只有现实问题真正解决。Rent 要求全账结清；Repair 要求凭证 + Expense PAID；Approval 要求后续 Payment 完成 + Evidence

### 16.2 Money & Math Truth
- 金额：DB `NUMERIC(14,2)` / Python `Decimal` / JSON string。**Never float**
- 四舍五入：`ROUND_HALF_UP`（佣金引擎唯一方法）
- 部分付款：不允许「四舍五入把尾款抹掉」，必须永远保留「0.01 应收」除非 Owner 显式 write-off（write-off 是独立操作，需要 Owner 权限 + audit）
- Idempotency：每笔财务写路径强制唯一键 idempotency_key；`uq_incomes_idempotency_key` + `financial_idempotency` 双保险

### 16.3 Role & Permission Truth
- **Telegram ID ≠ 业务身份**：必须通过绑定 → User → Membership 验证
- Secretary 永远不能：调 Owner bootstrap、审批自己创建的支出、执行 payment（Owner-only）、commission settlement confirm
- Organization / Membership 切分是唯一权限边界；Property.org_id NULL 会弱化这个边界（§11 debt，谨慎处理）
- SecretaryInvite 状态-时间戳合规 + 一次性单消费（一个 invite 最多产生一条 Membership，`created_membership_id` UNIQUE）。PENDING invite 必须绑定有效 Organization，过期或未绑定则 fail-closed 不泄露信息（CONFIRMED BY `app/models/membership.py §SecretaryInvite CHECK 约束` + `test_onboarding_p0_024.py`）
- PENDING invite 必须绑定有效 Organization；过期 PENDING 不能泄露 org 名称给未授权人

### 16.4 UX Truth（Telegram 侧）
- 3-Step Review ≤ 3 个连续动作给人做；否则改设计
- Zero Re-entry：知道的数据不问第二遍
- Action-at-source：按钮跟在事件消息下；不做「请去待办中心处理」
- Human Language Only：界面不直接显示 enum 名；转成人话
- Role-aware UX：Owner 中文金额结论决策；Secretary 英文 next-action + evidence upload
- Attention Queue：待办数量 = 这个人真正需要做的事项数；不要把所有人的全部任务都倒给 Owner
- AI 猜错必须给 Undo/Correct/Re-link/Reverse/Retry 路径，不是让用户从 step 1 重新开始
- Notif Budget：低静默 / 中摘要 / 决策即时 / 重大异常@Owner 升级；不每小事都轰炸
- Message Mutation：优先 edit 原消息，不制造大量「成功/已处理」垃圾消息

---

## 17. Definition of Done（TRAE SOLO Milestone 完成标准）

（与 `project_rules.md §6` 保持一致，此处以产品语义重述）

1. **产品规则正确（§16 全部满足）**：Business Truth 没被破坏；Reminder/Approval/Claim 没误写成 Completion
2. **Scoped 权限正确**：Organization/Unit scoped 的接口全量 fuzz 必须 403（未授权跨组织/跨房源）；Secretary 不越 Owner 权限
3. **Migrations 安全**：
   - `alembic upgrade head` 成功
   - `alembic downgrade -1` 成功
   - downgrade 脚本 `sa.inspect` 审计了 tz/JSONB 语义再 DROP（防止 §9.4 类 Bug）
   - Alembic 单 head，未破坏迁移链
4. **Tests：** targeted tests + 必要 regression 通过；真实失败不 skip/xfail 换 PASS
   - 新测试用 `timezone.utc` 时钟对齐
   - Ruff 规范无 E741/RUF059/UP007/UP031/UP035
5. **架构未破坏冻结拓扑**：Worker→Queue→Container→Neon 单链；没偷偷加 Redis/双 Bot；Worker 没写 DB/调 PTB/调 LLM
6. **Secrets 无 commit；无明文 tokens/keys**
7. **PR 打开 + 链接 Issue/Milestone + CodeRabbit 触发**；不等 CI 完成再 handoff
8. **Owner-facing 中文交付报告**：变更文件、Targeted tests 结果、HEAD SHA、Branch、PR URL、Known Risks

---

## 18. 哪些决定必须由 Owner 做（SOLO 禁止替代）

1. **改变产品方向 / North Star**：比如从「小规模房东 Telegram AI 管家」改成「SaaS 多租户平台」
2. **重新定义 Owner / Secretary / Tenant 角色语义**：比如让 Secretary 能审批自己的支出；或让 Owner 不看风险直接自动支付
3. **推翻冻结架构（加 Redis/第二 DB/第二 Bot/CF Worker 直接调 LLM 等）**
4. **删除现有已确认的业务能力**（当前 §6 CONFIRMED 能力）
5. **Force push / rewrite shared history / merge / production deploy（TRAE SOLO 永远不做）**
6. **写入 Production secrets / 配置 Telegram / Cloudflare / Neon 真实生产密钥**
7. **§11 Property.org_id NULL tech debt 的清偿方案**：默认组织是谁、是否强制 NOT NULL、是否冗余 organization_id 到子表（要 Owner 拍）
8. **双权限体系并轨最终方案（API Key ↔ Membership）**
9. **Open PRs 6/8/10/13/16/17/23/30 哪些 close、哪些改成 SOLO 模式继续、哪些 merge**
10. **真实 Blocker SOLO 无法自行解决**（第三方服务不可用 / 硬权限 / 生产配置 / Owner 真实 Telegram 操作）
11. **Merchant ID 多租户架构决策**（什么时候做、做不做、首版本边界）
12. **Mini App / Control Panel 范围与首版交付线**
13. **高风险删除**：Organization 删除、Property 彻底删除（非软删）、Expense 硬删（现有只能 reverse）

---

# ⚠️ SOLO 启动前再次确认（Summary of All Retired Contracts）

✅ `/ND` **已退役**（`.trae/commands/nd.md` deleted，`AI_WORKFLOW_RULES.md` HISTORICAL）
✅ `ready-for-dev` label **不再驱动 SOLO 启动或停止**
✅ GitHub → Auto Wake → `/ND` 链路 **已退役**
✅ task envelope / allowed_paths / rules_sha256 **不再是 SOLO 普通开发强制要求**
✅ 1 Issue = 1 小切片 = 1 PR = STOP **不再是默认模式**
✅ Worker / Supervisor 微任务调度（Max/Lily/Hermes/Fugui/Bridge）**不再是 Pasay 开发入口**
✅ rules hash preflight（wf_ctl.py preflight）**不再阻塞开发启动**
✅ 每个小修复都要开单独 Issue **不再是工程要求**
✅ `.ai-control/` 目录下 `RULES.md`、`tmp/rules_canonical.md`、`trae-auto-wake-test/`、`runtime-worktrees/` 等都是本地历史缓存 / runtime 产物，**不是当前开发权威源。读本文件 + project_rules.md + CURRENT_ARCHITECTURE.md。忽略本地 AI-control 历史文件**

---

**TRADE SOLO READY 检查清单：**
- 读完本文件（SOLO_HANDOFF.md）✅
- 读完 `project_rules.md` ✅
- 读完 `CURRENT_ARCHITECTURE.md` ✅
- 知道 §18 Owner-only 决策边界 ✅
- 知道 §16 产品规则（Business Truth First） ✅
- 理解 §11 Property.org_id NULL tech debt 不私自修 ✅
- 不机械逐 Issue 开 1PR；先做 Milestone 规划 ✅

**确认后启动 TRAE SOLO Milestone 开发。祝顺利！**
