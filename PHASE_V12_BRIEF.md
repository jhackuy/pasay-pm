# V1.2 PROACTIVE OPERATIONS — 工程任务书 (Codex Max)

> 由 Hermes 编排。目标:在**已验收的 V1.1 Financial Safety** 之上建立「业务事件 → 运营任务 → 到期检测 → 主动提醒 → Telegram 一键操作 → 状态更新 → Audit」的可靠闭环。
> **硬约束**:不得破坏 / 削弱 / 绕过 V1.1 已完成的数据库级幂等、RBAC、audit、财务状态机、PostgreSQL 安全边界。
> 禁止引入 Redis / Kafka / Celery / Temporal / 向量库 / LLM 决策。只用 PostgreSQL + FastAPI + 现有架构。
> 不要自行调用 Telegram 发送(由独立 notifier 驱动);本阶段不引入 LLM 自动决策,LLM 仅用于后续总结/解释。

## 0. 基线(必须先确认可复现)

- 开发树:`~/Documents/Codex/pasay-pm`,分支 `feature/telegram-ui-v2`。生产部署副本在 `/opt/pasay-pm`(launchd `ai.pasay.api` uvicorn:8000 + `ai.pasay.postgres`:5432 + `ai.pasay.telegram-bot`)。开发完成后由 Hermes 同步到 `/opt`,Max **不要**改 `/opt`。
- 后端 venv:`.venv/bin/python`;测试命令:`cd ~/Documents/Codex/pasay-pm && .venv/bin/python -m pytest tests/ -q` → **当前 111 passed 全绿**。
- 原生 Telegram bot 独立 venv:`pasay-telegram-bot/.venv`;测试命令:`cd pasay-telegram-bot && .venv/bin/python -m pytest -q` → **当前 132 passed 全绿**。
- 计数器基准(必须全绿后 V1.2 提交才算完成;V1.1 只能增不减):后端 ≥111, bot ≥132。

## 1. 现有架构摘要(复用,严禁重构)

- FastAPI + SQLAlchemy 2.0 (Mapped/mapped_column) + PostgreSQL 16 (psycopg2)。
- `app/models/base.py`:`Base`、`AuditMixin`(id BIGSERIAL + created_at/updated_at/created_by/updated_by)、`SoftDeleteMixin`、`pg_enum(enum_cls, name)` = **VARCHAR + CHECK**(非 native enum),存 enum `.value`。
- `app/database.py`:`engine` / `SessionLocal` / `get_db`;`app/config.py` pydantic-settings 读 `.env`:`database_url`。
- `app/core/security.py`:`hash_api_key`=SHA-256 hex。
- `app/api/deps.py`:`get_current_user`(HTTPBearer → users.api_key_hash + is_active)、`require_roles(...)`、`admin_only`、`manager_or_admin`。
- `app/services/audit.py`:`record_audit(db, table_name, record_id, action, actor_id, changed_fields, old_value, new_value)`;`AuditAction(str, Enum)` 在 `app/models/audit_log.py`(create/update/soft_delete/confirm/approve/reject/pay/reverse),存 VARCHAR+CHECK。查询 `GET /api/v1/audit-logs`(admin only)。
- 财务状态机(唯一真相,禁止第二套):income pending→confirmed→reversed;expense pending→approved(→paid)/rejected, paid→reversed;commission settlement confirm admin-only。所有真写必须经 V1.1 routers(`app/api/routers/income.py`、`expense.py`、`commission.py`)。
- 已有 V1.1 `tasks` 表(`app/models/task.py`,有 `open/in_progress/completed/scheduled` + recurring/interval_months/assigned_to)——**保留不动**,V1.2 的 `operational_tasks` 是**新表**,两者语义不同、互不干扰。
- 现有 60 个端点挂载在 `app/main.py`(`/api/v1`);新增 router 要在 main.py 追加。
- 迁移用 Alembic(`alembic/versions/`,为每个 schema 变更新增 revision,不要改旧 revision)。
- tests 用独立 `pasay_pm_test` 库,`db_session` fixture 每用例 drop_all/create_all(见 `tests/conftest.py`)。所以**新表只加 model 即可被 create_all 建出**;并发测试若要真实 PG 并发可用多 connection/线程方式。

## 2. 数据库模型(新表,全部走 model + Alembic migration)

### 2.1 `operational_tasks`

字段(建议,可微调但须语义等价):
`id, task_type, title, description(NULL), property_id(NULL FK), tenant_id(NULL FK), lease_id(NULL FK), source_type, source_id, assigned_user_id(NULL), priority, status, due_at, remind_at(NULL), snoozed_until(NULL), completed_at(NULL), completed_by(NULL), dedupe_key(NULL), metadata(JSONB NULL) , created_at, updated_at`

- `task_type` 枚举:RENT_DUE / RENT_OVERDUE / LEASE_EXPIRING / PROPERTY_FEE_DUE / AC_MAINTENANCE / APPROVAL_PENDING / PAYMENT_PENDING / SETTLEMENT_PENDING。
- `status`:`PENDING / COMPLETED / CANCELLED`(VARCHAR+CHECK)。SNOOZE 不单列状态,用 `snoozed_until` 保持 PENDING。
- `source_type`:'lease' | 'expense' | 'income' | 'commission_settlement' | 'recurring_rule' | 'manual' 等;`source_id` 指向源记录。
- `priority`:low/medium/high(critical 可选)。
- `dedupe_key`:字符串,业务去重指纹。

**数据库级去重边界(必须,禁止纯 SELECT→判断→INSERT TOCTOU)**:用**部分唯一索引**保证「同一个 dedupe_key 的 active task 唯一」:
```sql
CREATE UNIQUE INDEX uq_operational_tasks_active_dedupe
  ON operational_tasks(dedupe_key)
  WHERE status = 'PENDING';
```
- `dedupe_key` 由生成方构造,如 `f"{source_type}:{source_id}:{task_type}:{rent_period|rule_id|...}"`(Metadata 里可放细节)。**Null(手动任务)跳过该索引**。
- 生成任务时用 `INSERT ... ON CONFLICT DO NOTHING`(目标部分唯一索引)原子创建;冲突即跳过(已有 active task),不报错。
- 也加通用索引:`(task_type, status)`、`(due_at)`、`(status, due_at)`、`(assigned_user_id, status)`。

### 2.2 `recurring_rules`

支持 rule_type / property_id / recurrence / next_run_at / enabled / assigned_user_id / metadata。recurrence 仅需:`monthly / quarterly / yearly / fixed_interval`(interval 单位存 metadata 或 interval_months)。
由 recurring_rule 生成的 operational_task 的 dedupe_key 形如 `f"recurring:{rule_id}:{period_key}"`(period_key 如 2026-Q3 / 2026-08)。

### 2.3 `notification_outbox`

字段:`id, task_id(NULL FK), channel, recipient, payload(JSONB), status, attempts, next_attempt_at, sent_at, last_error, dedupe_key, created_at`。
- `status`:`PENDING / SENT / FAILED / DROPPED`。
- **必须至少一次投递**:同事务「create task + insert outbox + commit」;独立 notifier claim 后发送;DB 去重(dedupe_key 唯一);exponential backoff;max retry;错误可查询;Telegram 宕机不丢任务。
- outbox 也加 `uq_notification_outbox_dedupe(dedupe_key)`。

## 3. 财务安全边界(硬要求)

- operational_task 只是「提醒/入口」。PAYMENT_PENDING → 用户点击「付款」,**必须调用 V1.1 `POST /api/v1/expenses/{id}/pay`(admin only)** 等正式 API。禁止任务 handler `UPDATE expenses SET status='paid'`。禁止出现第二套财务状态机。income/expense/settlement 真实状态永远以 V1.1 domain service + PG 为准。

## 4. Scheduler / Worker

- 独立模块(如 `app/services/operations/scheduler.py` + `worker.py`,或按现有 services 风格),由 `/operations` API 可触发 + 提供 bin 入口跑独立 worker loop。
- 职责:定期扫描业务表 → 判断即将到期 → 原子创建 operational_task → 同事务写 outbox → 推进 recurring_rules.next_run_at。
- **并发安全**:用 PostgreSQL `SELECT ... FOR UPDATE SKIP LOCKED` 或 advisory lock 认领可跑规则/批次;worker 重启 / job 重跑 / Docker 重启 / 多实例都安全(DB 为唯一事实来源)。**禁止依赖 Python 内存变量保证唯一**。
- 规则驱动生成逻辑(Phase B 范围):
  - RENT_DUE / RENT_OVERDUE:扫描 active lease + 按 rent due day 计算本月应收,配合已 confirm income 排除已缴;overdue 进入 RENT_OVERDUE(参照已有 `/reports/overdue-rents` 语义)。
  - LEASE_EXPIRING:active lease 到期前 N 天(如 30 天)生成,metadata 记录 lease 续约状态;已续约则不生成/自动取消。
  - PROPERTY_FEE_DUE:发票式物业费(如有 dedicated expense 记录/规则)。
  - AC_MAINTENANCE:recurring_rule(quarterly)驱动。
  - APPROVAL_PENDING:approved 前的 expense 长时间未审批 → 提醒。
  - PAYMENT_PENDING:已 approved 未 paid 的 expense → 提醒。
  - SETTLEMENT_PENDING:commission settlement 待确认 → 提醒。
  - 每个 task_type 的「到期窗口(提前几天)」「重复周期」做配置化(metadata / constants),不要写死在各处。

### Reconciliation(自动完成/取消,关键设计)

- 每轮或定期 reconcile:根据源业务状态,把「源已变化导致不再应提醒」的 active task 自动置 COMPLETED 或 CANCELLED。例:
  - PAYMENT_PENDING task 但 expense 已 paid(经其他入口)→ 自动置 COMPLETED/CANCELLED,不再提醒。
  - LEASE_EXPIRING 对已续约 lease → 取消。
  - RENT_DUE 对应期间已 confirm income → 完成。
  - 系统不能成为「过期提醒制造机」。
- reconcile 亦需写 audit(TASK_AUTO_COMPLETED / TASK_AUTO_CANCELLED,见 §7)。

## 5. Notification Outbox + Notifier

- 独立 notifier(独立 worker loop 或同一 worker 的 dispatcher):claim `SELECT ... FOR UPDATE SKIP LOCKED` → 调 Telegram sendMessage → 成功置 SENT / 失败记录 last_error + 指数退避 + 重试,超过 max retry 置 FAILED。
- 必须:至少一次投递、DB 去重、指数退避、最大重试、错误可查询、Telegram 宕机不丢任务。
- 注:原生 bot 负责「编辑原消息」(editMessageText)。outbox 发送动作本身由 notifier 负责,但「同一条通知消息需要后续 edit」的消息 id 需可回查(payload / task_id 关联)。

## 6. API(新增 router `/api/v1/operations`)

- `GET /operations/tasks`(过滤 property / assignee / task_type / status;agent 只见自己 assigned)
- `GET /operations/tasks/{id}`
- `POST /operations/tasks/{id}/complete`
- `POST /operations/tasks/{id}/snooze`(body: until 或预设 1h/今天下午/明早/3天后;写 snoozed_until,状态仍 PENDING)
- `POST /operations/tasks/{id}/cancel`
- `GET /operations/rules`、`POST /operations/rules`、`PATCH /operations/rules/{id}`、`DELETE` 或 disable(按项目现有风格:expense 类用状态,建议 recurring_rules 用 enabled=false disable)
- `GET /operations/summary`(overdue / due_today / due_7_days / pending_total)
- RBAC:admin 全部;manager 查看+处理运营任务;agent 仅查看/处理自己被 assigned 的任务。**每次操作(含 callback)重新校验 RBAC,禁止靠 callback_data 信任越权。**

## 7. 审计

- 扩展现有 `AuditAction` 枚举(在 VARCHAR CHECK 里**追加**值,不动旧值,确保旧 audit 写作不回退):`task_created / task_completed / task_cancelled / task_snoozed / rule_created / rule_updated / rule_disabled`(可加 `task_auto_completed / task_auto_cancelled`)。
- 记录 actor / timestamp / task(before→after)/ source(Telegram/API/scheduler)。scheduler 自动操作必须有明确 system actor(如固定 user 或 actor_id=NULL + action 前缀标明 system)。

## 8. Telegram 待办中心(原生 bot `pasay-telegram-bot`)

- 在主菜单加入「📋 待办中心」。打开后分区:🔴 已逾期 / 🟠 今天 / 🟡 未来 7 天 / 📅 全部待办。
- 每条显示:房产、事项、金额(如适用)、到期时间、当前状态。
- InlineKeyboard:`✅ 完成 / ⏰ 稍后提醒 / 👁 查看详情 / ◀️ 返回`。
- Snooze 默认提供:1 小时 / 今天下午 / 明天上午 / 3 天后;只有「自定义」才要用户输入。
- 任务完成后尽量 **editMessageText 更新原消息**,而非不断发新消息。
- **每个 callback 都走 `handle_callback`(现有 callback.py),每次都要用 telegram_user_id → 角色 → 后端 RBAC 重新校验;agent 不能碰未 assigned 的任务。** callback_data 绝不能是越权唯一依据。

## 9. 测试(必须,在 `tests/` 新增,运行在真实 PostgreSQL `pasay_pm_test` 库)

至少覆盖:
1. scheduler 重复运行不生成重复任务
2. 两个 scheduler 并发执行不重复(真实 PG 多连接/多线程)
3. notification retry(exponential backoff)
4. notification dedupe
5. worker crash 后恢复(SKIP LOCKED 重新认领)
6. snooze
7. complete
8. cancel
9. RBAC(admin/manager/agent;agent 越权 403)
10. Telegram callback 越权(伪造 callback_data)
11. source 状态改变后 reconciliation 自动完成/取消
12. recurring rule 下一周期生成 / next_run_at 推进
13. DB migration upgrade/rollback(Alembic up/down)
14. **V1.1 原本全绿不回归**(111 backend + 132 bot)
15. **财务写路径未被 V1.2 绕开**(新增测试断言:任务 handler 不直接 UPDATE expenses/incomes,而是必须经 API)

> 关键:必须有**真实 PostgreSQL 并发测试**(非纯 SQLite/mock)。conftest 已用真实 PG 库,可对同一库开多 connection 并行跑 scheduler 逻辑验证无重复。

## 10. 实施顺序与纪律

- Phase A:模型 + Alembic migration + task 状态机(RBAC 守卫)
- Phase B:task generation + reconciliation + scheduler(含 SKIP LOCKED 并发)
- Phase C:notification outbox + notifier
- Phase D:Telegram 待办中心 UI(callback + edit)
- Phase E:真实 PG 并发 / crash / retry 测试
- 每完成一个 Phase 先跑测试再进下一步。提交拆成多个清晰 commit(branch:feature/telegram-ui-v2)。
- **不重构 / 不重写任何 V1.1 已稳定代码**;只新增 + 最小配套修改(如 main.py 挂 router、AuditAction 追加值)。

## 11. 交付要求

- 全部代码、迁移、测试写入 `~/Documents/Codex/pasay-pm`,git 提交(branch feature/telegram-ui-v2)。
- 提交后跑:后端 `pytest tests/` ≥111 全绿 + 新增全绿;bot `pytest` ≥132 全绿。
- 简报本文件底部写清:新增表/索引/约束、scheduler 机制、outbox 机制、RBAC、reconciliation、幂等/并发方案、测试数量与结果、V1.1 回归结果、风险、未实现项、最终 git commit hash。
- 不要改 `/opt/pasay-pm`(Hermes 负责部署同步)。不要自行重启生产服务。

---

# V1.2 PROACTIVE OPERATIONS — 交付简报 (Phase A–E)

> 交付方:Codex (feature/telegram-ui-v2)。全部代码写入 `~/Documents/Codex/pasay-pm`,未改动 `/opt/pasay-pm`,未重启任何生产服务。开发库与测试库均基于真实 PostgreSQL 16 (`pasay_pm` / `pasay_pm_test`)。

## 1. 新增表 / 索引 / 约束 (migration `3c9a2f7b1e4d`)

- **`users.telegram_chat_id`**(`VARCHAR(64) NULL`)— notifier 投递目标,`ix_users_telegram_chat_id`。
- **`operational_tasks`**:`task_type / title / description / property_id / tenant_id / lease_id / source_type / source_id / assigned_user_id / priority / status / due_at / remind_at / snoozed_until / completed_at / completed_by / dedupe_key / metadata(JSONB,模型映射 `details`) / id + created_at / updated_at / created_by / updated_by`。
  - 索引:`uq_operational_tasks_active_dedupe`(**部分唯一**,`WHERE status='PENDING'`)、`ix_operational_tasks_task_type_status`、`ix_operational_tasks_due_at`、`ix_operational_tasks_status_due_at`、`ix_operational_tasks_assigned_status`、`ix_operational_tasks_property_id`、`ix_operational_tasks_tenant_id`、`ix_operational_tasks_lease_id`、`ix_operational_tasks_source_id`。
  - CHECK:`ck_operational_tasks_priority`(`low/medium/high/critical`)、`ck_operational_tasks_status`(`PENDING/COMPLETED/CANCELLED`)、`ck_operational_tasks_task_type`(8 种)。
  - FK:property_id → properties、tenant_id → tenants、lease_id → leases、assigned_user_id → users。
- **`recurring_rules`**:`rule_type / title / description / property_id / recurrence / interval_months / next_run_at / enabled / assigned_user_id / metadata(JSONB) / deleted_at + audit 列`。
  - 索引:`ix_recurring_rules_enabled_next_run`、`ix_recurring_rules_property_id`。
  - CHECK:`ck_recurring_rules_recurrence`(`monthly/quarterly/yearly/fixed_interval`)、`ck_recurring_rules_rule_type`。
- **`notification_outbox`**:`task_id / channel / recipient / payload(JSONB) / status / attempts / next_attempt_at / sent_at / last_error / dedupe_key / telegram_message_id + audit 列`。
  - 索引:`uq_notification_outbox_dedupe`(唯一)、`ix_notification_outbox_status_next_attempt`、`ix_notification_outbox_task_id`、`ix_notification_outbox_recipient`。
  - CHECK:`ck_notification_outbox_status`(`PENDING/SENT/FAILED/DROPPED`);FK:task_id → operational_tasks.id。
- downgrade 完整回滚(删三表 + `users.telegram_chat_id`),已由 Alembic up/down 测试验证。

## 2. Scheduler 机制

- `app/services/operations/scheduler.py::run_scheduler_once` = 一轮完整 pass,**单事务**提交:claim 到期 recurring rules → 生成 rule 任务并推进 `next_run_at` → `generate_business_tasks`(RENT_DUE/RENT_OVERDUE/LEASE_EXPIRING/APPROVAL_PENDING/PAYMENT_PENDING/SETTLEMENT_PENDING)→ `reconcile_tasks` → commit。
- 规则认领:`SELECT ... FOR UPDATE SKIP LOCKED`(batch=20,按 `next_run_at` 排序),多 worker/重启安全,DB 为唯一事实来源;崩溃 worker 未提交的 claim 自动释放、下轮重认领。
- 业务扫描窗口全部配置化(`app/services/operations/config.py`):`LEASE_EXPIRY_WINDOW_DAYS=30`、`RENT_DUE_ADVANCE_DAYS=3`、`APPROVAL_PENDING_AFTER_DAYS=2`、`PAYMENT_PENDING_AFTER_DAYS=1`、`SETTLEMENT_PENDING_AFTER_DAYS=1`;rule 周期 `monthly/quarterly/yearly/fixed_interval`。
- 触发方式:`POST /api/v1/operations/scheduler/run`(manager/admin)+ 独立 worker loop `bin/run-operations-worker.py`(`--once / --interval`,scheduler + notifier 同进程 dispatcher)。

## 3. Outbox 机制

- **同事务写入**:创建 task 时经 `enqueue_notification` 同事务插入 outbox(create task + insert outbox + commit),崩溃后 outbox 行仍为 PENDING,保证 at-least-once。
- **去重**:`uq_notification_outbox_dedupe` + `INSERT ... ON CONFLICT DO NOTHING`,并发 enqueue 只落一行。
- **notifier**(`notifier.py::process_notifications_once`):`SELECT ... FOR UPDATE SKIP LOCKED`(batch=10)认领 → `TelegramSender`(httpx sendMessage)→ 成功置 `SENT` + 存 `telegram_message_id`(供 bot editMessageText 回查);失败记录 `last_error` + 指数退避 `30 * 2^(attempts-1)` 秒,超过 `max_attempts=5` 置 `FAILED`;Telegram 宕机不丢任务(保持 PENDING 重试)。
- 收件人:`resolve_recipient` 优先 `users.telegram_chat_id`,否则 `user:{id}` 占位(notifier 解析或丢弃)。

## 4. RBAC(每次请求重新校验)

- `GET/POST /operations/tasks`、`/tasks/{id}`、`complete/snooze/cancel`、`summary`:`admin` 全部;`manager` 查看+处理;`agent` 仅查看/处理 `assigned_user_id == self` 的任务,越权一律 403(列表过滤 + `_require_access` 每请求复核)。
- `/operations/rules` CRUD + disable、`/operations/scheduler/run`:`manager_or_admin`;`agent` 403。
- Telegram 侧:每个 callback 走 `handle_callback`,用 `telegram_user_id → 角色` 重新校验,再调后端 API;callback_data 不构成越权依据(测试覆盖伪造 callback 越权 403)。

## 5. Reconciliation

- `reconcile.py::reconcile_tasks` 每轮扫描 PENDING 任务,`auto_transition` 做原子 `PENDING→COMPLETED/CANCELLED`(`UPDATE ... WHERE status='PENDING'`,rowcount 防重入)+ 写 `task_auto_completed / task_auto_cancelled` audit(`actor_id=NULL`,system)。
- 规则:expense `PAYMENT_PENDING` 已 paid → COMPLETED、rejected/reversed → CANCELLED;`APPROVAL_PENDING` approved/paid → COMPLETED、rejected/reversed → CANCELLED;`SETTLEMENT_PENDING` confirmed → COMPLETED;lease 任务 lease 非 active → 已续约 COMPLETED / 否则 CANCELLED;RENT_DUE/RENT_OVERDUE 对应期间已被 confirm income 覆盖 → COMPLETED(`covered_periods` 复用 `/reports/overdue-rents` 语义,description 月份优先,否则 received 月份);RENT_DUE 被 RENT_OVERDUE 取代 → auto_completed。
- 财务状态只由 V1.1 routers 修改;reconcile/generation 只读源表。

## 6. 幂等 / 并发方案

- **task 去重**:部分唯一索引 `uq_operational_tasks_active_dedupe`(PENDING 唯一)+ `INSERT ... ON CONFLICT DO NOTHING`,杜绝 SELECT→INSERT TOCTOU;dedupe_key 如 `lease:{id}:RENT_DUE`、`recurring:{rule_id}:{period_key}`(period_key 如 `2026-08` / `2026-Q3`)。
- **规则认领 / outbox 认领**:`FOR UPDATE SKIP LOCKED`;重启/多实例/崩溃均安全。
- **状态机防重入**:complete/cancel/snooze 用条件 UPDATE + rowcount;重复动作幂等(200),非法迁移 409。
- **并发测试**用真实 PG `pasay_pm_test`:两个线程各自独立 session 并行跑 `run_scheduler_once`,断言无重复任务(见 §7)。

## 7. 测试数量与结果

- 后端(`cd ~/Documents/Codex/pasay-pm && .venv/bin/python -m pytest tests/ -q`):**138 passed**(V1.1 基线 111 + V1.2 新增 27,`tests/test_operations.py`),0 failed。
  - 覆盖:scheduler 幂等、真实 PG 双线程并发无重复、SKIP LOCKED 并发、outbox retry/backoff/dedupe、worker crash 后恢复、snooze(预设+自定义+过去时间 422)、complete/cancel 状态机、RBAC(admin/manager/agent + agent 403)、Telegram 伪造 callback 越权、source 状态变化后 reconciliation、recurring rule 下周期生成 + `next_run_at` 推进、Alembic upgrade/downgrade(scratch DB)、财务写路径不绕过(任务 handler 不改 expenses/incomes)。
- 原生 bot(`cd pasay-telegram-bot && .venv/bin/python -m pytest -q`):**144 passed**(V1.1 基线 132 + V1.2 新增 12),0 failed。
- 计数基准满足:后端 ≥111、bot ≥132,且只增不减。

## 8. V1.1 回归结果

- 后端 V1.1 基线 111 个用例全绿(含 financial-safety / reports / RBAC / audit 等),无回归。
- Bot V1.1 基线 132 个用例全绿(唯一改动:主菜单断言增加「📋 待办中心」按钮),无回归。

## 9. 风险

- lease/expense 生成的任务默认无 `assigned_user_id` → 只有 manager/admin 可见;agent 可见性依赖 assignment 正确性。
- notifier 依赖生产 `.env` 提供 `telegram_bot_token`;缺失时 send 抛错,outbox 保持 PENDING 重试(不丢),但不会实际送达。
- 时区统一 `datetime.now(timezone.utc)` 存储,DB session 以 +08:00 渲染 ISO;bot 侧按日期/时间展示已处理。
- `remind_at` 已存储但暂不参与 outbox 投递时间门控(见未实现项)。
- 部分唯一索引 + ON CONFLICT 依赖 PostgreSQL 方言(已满足,无 SQLite 支持需求)。

## 10. 未实现项

- 无独立的 `PROPERTY_FEE_DUE` 业务源生成器(该类提醒可用 recurring_rule + `rule_type=PROPERTY_FEE_DUE` 建规则实现);AC_MAINTENANCE 由 quarterly recurring rule 驱动(已实现)。
- `remind_at` 目前仅存储,未作为 outbox 投递时机门控。
- 无「手动创建任务」端点(简报未要求)。
- 未接入 LLM 总结/解释(简报明确本阶段不做)。

## 11. 最终 git commit hash

- `84dc147` Phase A — models + migration + `/api/v1/operations` router + AuditAction 追加 + RBAC
- `d537e67` Phase B+C — scheduler/generation/reconciliation + notification outbox/notifier + worker entry
- `6bdabce` Phase D — Telegram 待办中心(bot:菜单/分区/inline keyboard/editMessageText/callback 重校验)
- `19b634a` Phase E — 真实 PG 并发/crash/retry 测试 + rent-math 共享重构
- (本简报提交)`REPORT_COMMIT_PLACEHOLDER` — V1.2 交付简报
