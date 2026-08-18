# TELEGRAM-OPS-UX-FINAL-CONVERGENCE-003 — Owner 报告（中文）

任务类型：一次性长任务（P0 修复 + Telegram Ops UX 收敛 + 统一部署 + Live 观察）。
执行纪律：一次开发 → 一次测试 → 一次部署 → 一次最终报告 → Owner 一次实机验收。中途未等待 Owner。

---

## 1. P0 Reminder 刷屏 — 真实根因与修复（Phase 1）

### 1.1 根因（已用 Live 证据证明，非猜测）

Worker 日志（`worker_runtime.log.err`，12:02–12:17）：
```
worker pass: {'scheduler': SchedulerRunResult(tasks_created=1, notifications_enqueued=1, ...),
              'notifier': {'claimed': 1, 'sent': 1, ...}}   # 每分钟一次
```
DB（`operational_tasks`）显示同一条 `lease:3:RENT_DUE:2026-08` 每分钟新建一行（id 6063→6067→…），
且同 pass 内被 `_supersede_rent_due` 以 `superseded_by_rent_overdue` COMPLETED（audit_logs 佐证）。

**机制**：`generate_business_tasks` 对"已有逾期期数 + 未来 3 天窗口内有 upcoming 期"的租约，
每轮都重建 RENT_DUE 任务（upcoming 期，如 2026-08-20），随后同一 pass 的
`_supersede_rent_due` 又把它完成 → **create→supersede→create 无限循环**。
每次创建都 enqueue 一条 outbox 通知，worker 每 60 秒一轮 → 每分钟一条 Telegram 提醒
（正是 Owner 看到的 12:04–12:10 每分钟一条 `🔔 待办提醒 / 租金到期 2026-08 …`）。
即：**高频扫描变成了高频发送**，且只靠 task 级 dedupe_key（被 supersede 清零）拦不住。

### 1.2 修复（三层防御，全部持久化）

| 层 | 内容 |
| --- | --- |
| 1 根因 | 租约已有逾期期数时**不再创建 RENT_DUE**（RENT_OVERDUE 已覆盖该业务事项）——循环消除。 |
| 2 每日防重 | 新表 `reminder_daily_dedup`（迁移 `b2c3d4e5f6a7`）：proactive 提醒在 enqueue 时原子抢占
  `reminder:{business}:{recipient}:{PH 本地日期}:{type}` 槽位（`INSERT … ON CONFLICT DO NOTHING`，
  与 outbox 同事务）——同一天同业务对象最多发送 1 次；跨天允许再 1 次。 |
| 3 发送时守卫 | notifier 在发送前锁定任务行：任务不再是 PENDING（completed/cancelled/acknowledged）→
  outbox 行 DROPPED，绝不发送。 |

- 时区：`Asia/Manila`（UTC+8 无 DST，`zoneinfo`）——PH 本地日期做每日边界，UTC 翻转不会一天发两次。
- 重启安全 / 并发安全：唯一索引 + SKIP LOCKED，DB 是唯一真相源，不依赖 Python 内存。
- 提醒卡片新增 `✅ Acknowledge` 按钮 → 新端点 `POST /operations/tasks/{id}/acknowledge`
  （PENDING→IN_PROGRESS，幂等）→ 当天停止该事项提醒；Completed/Paid/Collected 永久停止。
- Bot 侧 `v2_next_check` 任务同样增加按 `(task, chat, PH 日期)` 的持久化当日防重。

### 1.3 自动化验证（新增 `tests/test_convergence_003_reminder.py`，12 个全过）

A 同日 10 次扫描 dispatch==1 ✅ ｜ B 重启后续扫 dispatch==0 ✅ ｜ C 双 worker 并发 dispatch==1 ✅
D 次日可再发 1 次、当日不再发 ✅ ｜ E Acknowledge 后当日不再发 ✅ ｜ F Completed 跨日不再发 ✅
G 两个业务对象各自 1 次 ✅ ｜ 另含：RENT_DUE 循环不再复现、PH 时区边界、quick-rent 真相源、'??' 读路径、任务上下文。

---

## 2. Home / 导航收敛（Phase 2/3）

- **唯一 Home**：所有 `🏠 Home` / `menu` / `更多` 路由 → 唯一的 `show_home`（Operations Overview）；
  旧 dashboard 不再从任何菜单可达（`dashboard_keyboard` 的 4 菜单网格与运营助手按钮删除）。
- Home = 轻量上帝视角，10 个数字：本月应收 / 已收 / 未收、历史累计欠租、逾期数量、合同即将到期、
  空置数量、待付款支出、未完成维修、今日需要处理；仅保留情境按钮 `⚠️ Today` / `🔄 Refresh`。
- **🤖 Operations Assistant 不再是菜单页入口**（AI 是自然语言能力）；C2 确认式流程仍可经其回调/NL 路径使用。
- **固定底部菜单永久冻结**：`🏠 Properties | ✅ Tasks` / `💰 Rent | 💸 Expense`，Owner/Secretary/群/私聊一致，
  任意 handler 后不漂移；Add Property/Add Tenant/Export/Submit Requirement 不做常驻按钮（允许情境按钮）。

---

## 3. Expense 操作链（Phase 4）

- **Open 失效根因**：`GET /api/v1/expenses/{id}` 对 legacy `??` category 触发
  `ResponseValidationError` → 500，callback 只闪 Processing 无结果。修复：只读路径放行（写入路径仍严格），
  渲染层统一清洗 → `GET /expenses/{id}` 不再 500。
- **列表手机化**：Expense 列表每笔只留一个短按钮 `E{id} · Open`（列表负责看）；详情卡才是操作区
  （`🔔 提醒 | ✅ 已付` + `◀ 返回 | 🏠 首页`），不再出现 `Remin...` 截断。
- **详情页**：Open → ACK → 原地 edit → 详情（物业/类别/用途/金额/批准日期/等待天数/状态）+ 操作按钮。
- **Remind Owner**：首次真实发送 + 反馈 `✅ Owner reminded · 已提醒老板`；按钮翻转为 `✅ 已提醒`；
  同日重复 → `Already reminded today · 今日已提醒`，不重复制造提醒。

---

## 4. Rent Follow-up UX（Phase 5）

- 成功后**原地重渲详情卡**：`Last follow-up: 2026-08-15 23:20 · 最近催租：…`（真实数据源：该租约最新跟进任务），
  按钮变短状态 `✅ 今日已催`；同日再点 → `今日已催，无需重复`，不生成重复任务。
- **Follow-up 任务语义修复**：不再显示 `Collect rent · … · due in 0d`（根因：create_task 对
  `due_at=None` 默认 now+1day）。现在显示 `Collect overdue rent · DEV-BAY-1680` +
  `₱75,000 · 3 period(s) · overdue 104d` + `Next: Follow up with tenant` —— 区分租约到期日与行动截止。

---

## 5. 双语重复系统性修复（Phase 6）

**根因**：`i18n.py` 的 zh 表里 `v2.rent_*` 等 8 个 key 的"中文"值其实是英文副本
（如 `'v2.rent_overdue': 'Overdue: {days} days'`），`t(key,"bi")` 输出 en+zh → 同一英文两遍；
`_bi_line/_bi_header` 对相同片段也无条件双行。

**修复**：zh 表改为真实中文（未付/租客/未付期数/逾期/最近催租/空置）；`t()`/`bl()`/cards 增加守卫——
**中文缺失或与英文相同时只输出一次英文**；字段改为紧凑单行
`Outstanding ₱75,000 · 未付 ₱75,000` / `Overdue 104d · 逾期 104 天`（不强制拆两行）。

---

## 6. 欠租数据真相源（Phase 7）

- **真相源**：`rent_math.py`（`lease_periods` + `covered_periods`）——任务生成、quick-rent、reconcile 同一语义。
- **3期 vs 1期根因**：Tasks 的 "3期" 来自任务生成器 `len(overdue)`（真实）；Rent Detail 的 "1" 是
  bot `_render_rent_detail_text` **硬编码 `unpaid_periods=1`**（错误）。
- **修复**：quick-rent 行新增 `unpaid_periods`/`monthly_rent`/`last_followup_at`（同一计算），详情卡使用行数据。
- **Live 验证**：`DEV-BAY-1680` amount 75,000 = 3 × 25,000，Tasks "3期" == Detail "3 期"（一致）。

---

## 7. Tasks 待付款信息（Phase 8）与 Rent 金额语义（Phase 9）

- Tasks 待付款行现在可区分：`💸 E{id} · unit · purpose · ₱amount · waiting Nd`（含真实上下文，
  `??` 哨兵禁用安全 fallback），不再两条 `待付款支出 · overdue 2d` 无法分辨。
- Rent 金额区分标注：`本月未收 ₱173,000`（本月应收-已收）与 `历史累计欠租 ₱351,000`（历史拖欠总额），
  不再两个都叫 Outstanding。

---

## 8. 自动测试（Phase 12）

- **Bot**：`pasay-telegram-bot` 全量 **496 passed**（含新增 `test_convergence_003_ux.py` 10 个；原 486 个全部保留，部分断言随新 UX 更新）。
- **Backend**：`tests` 全量 **467 passed**（原 455 + 新增 `test_convergence_003_reminder.py` 12 个；4 个 eval 标记 deselected）。
- 覆盖：Reminder 防重 A–G、Navigation（唯一 Home / Legacy Home 不可达 / 菜单无运营助手 / 四键固定）、
  Expense（Open/Remind/同日防重/短按钮）、Rent（Follow-up 刷新/同日防重/不再 due in 0d/期数一致）、
  双语（缺失翻译不重复 / Detail 字段不重复 / Property 字段不重复）。

---

## 9. 统一部署（Phase 13）与 Live 观察（Phase 14）

- **TARGET_SHA**：`13adb4ff53347851834db0347044d6102fcdfa80`（`TELEGRAM-OPS-UX-CONVERGENCE-003`）。
- 迁移 `b2c3d4e5f6a7`（reminder_daily_dedup）已应用到 live DB（`alembic_version = b2c3d4e5f6a7`）。
- runtime worktree `BOT-V1-USABLE-001-RUNTIME` 已 `reset --hard` 到 TARGET，**worktree clean**。
- 服务已全部以新代码重启（API:8001 / Bot poller / Operations worker），health：
  - API：`Application startup complete`，quick/* 200；`POST /tasks/{id}/acknowledge` 路由存在（404=任务不存在）。
  - Bot：getMe OK @pasayhousebot，polling 正常（NO_UPDATES 空轮询），update_fetcher 存活。
  - Worker：scheduler+notifier 正常。
- **Live Reminder 观察（12:58 → 13:09+，11+ 分钟）**：worker pass 全为
  `tasks_created=0, notifications_enqueued=0, claimed=0, sent=0`；DB 近 12 分钟
  **0 新任务 / 0 新 outbox / 0 pending**；原刷屏任务（lease:3 RENT_DUE 2026-08）**不再重复发送**。
- 日志检查：无 callback exception、无 edit 失败、无 dispatch/worker/scheduler 错误。

> 部署说明（诚实披露）：本 Harness 沙箱无法注册 Windows 计划任务、无法枚举高权限进程，
> `bin/start-runtime.ps1` 在沙箱内不能完整自举；服务当前以 Harness 托管的常驻后台任务运行。
> 会话结束后如需恢复"开机/登录自启"的持久形态，由 Operator 在正常环境执行一次
> `bin/install-runtime-task.ps1`（或直接 `bin/start-runtime.ps1`）即可，二者均为既有幂等路径。

---

## 10. Owner 最终实机验收剧本（Phase 15，约 5 分钟，≤10 步）

1. 打开 Telegram 群，确认底部固定菜单恒为：`🏠 Properties | ✅ Tasks` / `💰 Rent | 💸 Expense`（四键）。
2. 点任意页面的 `🏠 Home`（或输入「更多」），确认只进入**同一个 Home 概览**（10 个数字 + ⚠️ Today / 🔄 Refresh），
   且 Home 上**没有** 🤖 Operations Assistant、也没有一级业务菜单。
3. 点 `💸 Expense`：确认列表每笔只有一个短按钮 `E{id} · Open`（不再有 `Remin...` 截断）。
4. 点一个 `Open`：确认立即显示 Expense 详情（物业/用途/金额/批准日期/等待天数/状态）+
   `🔔 提醒 | ✅ 已付`、`◀ 返回 | 🏠 Home`。
5. 点 `🔔 提醒`：确认收到一条真实提醒消息并提示已提醒；按钮变 `✅ 已提醒`；再点一次 → `今日已提醒`，不重复发。
6. 点 `💰 Rent` → 点逾期房 `Follow up` 进 Rent Detail：确认「最近催租/Last follow-up」显示真实时间；
   点 `📞 催租` → 确认页面刷新为 `✅ 今日已催`，再点 → `今日已催，无需重复`。
7. 回到 `✅ Tasks`：确认待付款支出显示 `E{id} · 单元 · 用途 · ₱金额 · waiting N天`（可区分），
   租金逾期显示 `3期 · overdue 104d` 与 Rent Detail 的期数一致。
8. 确认 Rent 页面金额标注区分：`本月未收` 与 `历史累计欠租` 两个不同名称。
9. 在手机上（约 360px 宽）复核按钮均为短文本，无截断。
10. 等待 5–10 分钟：确认**不再收到**每分钟一条的 `🔔 待办提醒 / 租金到期 2026-08` 刷屏。
    （如需当天立即看到效果：新提醒卡片上点 `✅ Acknowledge`，当天即不再提醒；次日如未完成才会再提醒一次。）

---

## 11. 结论（Phase 16）

```
TASK=TELEGRAM-OPS-UX-FINAL-CONVERGENCE-003

REMINDER_SPAM_ROOT_CAUSE=generate_business_tasks 对已有逾期期数的租约每 pass 重建 RENT_DUE，同 pass 内 _supersede_rent_due 又将其 COMPLETED（create→supersede→create 循环），每次创建 enqueue 一条通知，worker 60s/pass → 每分钟一条（12:04-12:10 实机日志+DB 6063/6067 系列佐证）
DEDUP_STORAGE=PostgreSQL 新表 reminder_daily_dedup（alembic b2c3d4e5f6a7）
DEDUP_KEY=reminder:{business_dedupe_key}:{recipient}:{PH_local_date}:{reminder_type}
DEDUP_ATOMIC_MECHANISM=INSERT ... ON CONFLICT DO NOTHING 对 uq_reminder_daily_dedup_key 唯一索引，与 outbox enqueue 同事务
TIMEZONE_USED=Asia/Manila (UTC+8, 无 DST)
SAME_DAY_MAX_SENDS=1
SURVIVES_RUNTIME_RESTART=YES
CONCURRENT_DISPATCH_SAFE=YES

LEGACY_HOME_REMOVED=YES
UNIQUE_HOME_CONFIRMED=YES
OPERATIONS_ASSISTANT_MENU_REMOVED=YES
FIXED_MENU_4_KEYS_ONLY=YES

EXPENSE_OPEN_FIXED=YES
EXPENSE_REMIND_FIXED=YES
EXPENSE_REMIND_SAME_DAY_DEDUP=YES
MOBILE_EXPENSE_BUTTONS_FIXED=YES

FOLLOWUP_UI_REFRESH_FIXED=YES
FOLLOWUP_SAME_DAY_DEDUP=YES
FOLLOWUP_DUE_IN_0D_FIXED=YES

BILINGUAL_DUPLICATION_ROOT_CAUSE=zh 翻译表 8 个 v2.rent_* key 的中文值实为英文副本，t(key,'bi') 输出 en+zh 导致同一英文显示两遍；_bi_line/_bi_header 对相同片段无条件双行
BILINGUAL_DUPLICATION_FIXED=YES

ARREARS_TRUTH_SOURCE=app/services/operations/rent_math.py (lease_periods+covered_periods)，任务生成/quick-rent/reconcile 同一语义
3_PERIODS_VS_1_PERIOD_ROOT_CAUSE=Tasks 的 3期来自生成器 len(overdue)（真实）；Rent Detail 的 1 是 bot 硬编码 unpaid_periods=1（错误）
ARREARS_CROSS_VIEW_CONSISTENT=YES

TASK_EXPENSE_CONTEXT_FIXED=YES
RENT_OUTSTANDING_LABELS_FIXED=YES

REMINDER_SCAN_10X_TEST=PASS
REMINDER_RESTART_TEST=PASS
REMINDER_CONCURRENT_TEST=PASS

BOT_TESTS=496 passed
BACKEND_TESTS=467 passed
OTHER_TESTS=全量无回归

TARGET_SHA=13adb4ff53347851834db0347044d6102fcdfa80
LIVE_SHA=13adb4ff53347851834db0347044d6102fcdfa80
LIVE_EQUALS_TARGET=YES
RUNTIME_WORKTREE_CLEAN=YES

LIVE_REMINDER_OBSERVATION=12:58→13:09+ 连续 11+ 分钟 worker pass 全为 0/0/0，DB 0 新任务/0 新 outbox/0 pending，原刷屏任务不再发送
LIVE_CALLBACK_OBSERVATION=无 callback 异常，bot polling 正常，API quick/* 200，ack 路由存在，SYSTEM job key 200

READY_FOR_OWNER_TELEGRAM_UX_ACCEPTANCE=YES
```

`READY_FOR_OWNER_TELEGRAM_UX_ACCEPTANCE=YES` 成立依据（全部满足）：
- 所有新增 regression tests 通过（bot 10 + backend 12）；
- 原有测试无退化（bot 486 + backend 455 全绿，共 496 / 467）；
- target == live（`13adb4f`），runtime worktree clean；
- reminder 不再刷屏（11+ 分钟 Live 观察 + DB 证据）；
- callback 无失效（Expense Open 根因已修、机器验证通过）；
- Home 唯一、双语重复已修、arrears 跨视图一致。

按任务纪律：**未写入 `OWNER_ACCEPTED`** —— Owner 最终实机验收只能由 Owner 在真实 Telegram 群确认。
