# TELEGRAM-OPS-REAL-WORLD-CLOSURE-005 — Owner 报告（中文）

任务：真实世界房租催收闭环 + Properties 上帝视角 + 真相数据 + 菜单清理。
核心纪律：**按钮点击 ≠ 业务完成**。`📞 催租` 只代表「要求秘书去催」，只有秘书私聊点 `✅ 已联系租客` 才算真实执行。

---

## 1. Properties：天然交通灯 + 每个房只有一个状态灯（Phase 1）

改前的问题：`DEV-BAY-1680 · Rent overdue 104d · ... · DEV-BAY-1680 · 逾期...` 冗长重复、无颜色上帝视角、或用 `💰⚠️/📄✅/🔧0` 图标密码。

改后（live 数据推导）：
```
🏠 Properties · 7
📊 总计 7 · 已出租 5 · 空置 1 · 🔴 待处理 4

🔴 1608 · 欠租2天 · 1期
🔴 1680 · 欠租104天 · 3期
🔴 2208 · 欠租58天 · 2期
🔴 1805 · 欠租47天 · 2期
🟡 2308 · 空置
🟢 1203 · OK
🟢 1103 · OK
```
- **每套房只允许一个灯**（🟢/🟡/🔴），多状态取最高严重等级（§1.1/§1.2）。
- 🔴 待处理 → 🟡 需关注 → 🟢 正常 **排序**（§1.3），第一眼看到问题房。
- **短房号** 1680（去掉 `DEV-BAY-`），完整 Property id 只在详情页（§1.4）。
- 顶部摘要紧凑双语，不重复长句（§1.5）。
- 房号按钮保持两列短按钮（`1608 | 1680`），`📄 Archive` 只保留一个（§1.6/§1.7）。
- 图标密码（`💰⚠️/📄✅/🔧0/👁/🔵/⚪`）全部禁止。

## 2. 催租改成真实「交秘书执行」闭环（Phase 2–9）

**改正前错误行为**：Owner 点 📞 催租 → 直接变 ✅ 今日已催 + 更新 Last follow-up（假状态，秘书实际没联系租客）。

**改正后**（状态严格冻结）：
```
🔴 需要催租
   ↓ Owner 点 📞 催租（真实 DM 给秘书）
🟡 已交秘书跟进
   ↓ 秘书私聊点 ✅ 已联系租客
✅ 今日已催
```
- Owner 点催租 → Bot 解析**真实 Secretary HUMAN principal**（`/operations/secretary-target` live=1083657401/principal 2）→ **向秘书私聊发送真实 DM 催租卡**。
- **只有 DM 发送成功**才标 `🟡 已交秘书跟进`；DM 失败（目标解析/Forbidden/超时/网络）→ 群内 `⚠️ 无法通知秘书`，保持 `🔴 需要催租`（§9，不允许假成功）。
- Owner 点击**不修改 last_followup**（租客还没被联系）；只有秘书确认真实联系后才更新（后端 `followup_by_lease` 改用 `completed_at`/执行时间）。
- 秘书私聊卡片提供且只提供三个执行按钮：
  `✅ 已联系租客` / `💰 已收款` / `⏰ 稍后处理`（§3）。
- **✅ 已联系租客**：真实完成 follow-up 任务（`completed_by=Secretary` + `task_completed` 审计），更新 last follow-up、状态变 `✅ 今日已催`；当天重复点击 → `今日已记录，无需重复`，不产生第二条真实 follow-up（§4）。
- **💰 已收款**：只作为**进入现有收款登记流程**的快捷入口，绝不强制变 PAID（§5）。
- **⏰ 稍后处理**：复用现有 snooze/reminder 机制（§6）。
- 群内不刷屏：Secretary 完成后不新增群垃圾消息，状态在下次 refresh/open 正确（§4.2）。
- Owner 或秘书本人点催租，规则一致：点催租只开始执行，必须进秘书私聊才完成（§8）。

## 3. Tasks 真实欠租金额 + 消除 `due in 0d`（Phase 11–13）

- **根因**：`_rent_task_details` 曾写 `amount=monthly_rent`（月租 ₱25,000），而真实总额在 `total_outstanding`；`_task_row` 用 `amount or total_outstanding` 选了月租。
- **修复**：RENT_OVERDUE 任务改为 `amount=总欠款`（month×期数），quick-tasks 行 RENT_OVERDUE **优先用 `total_outstanding`**（含历史脏行）。Tasks / Rent Detail / Rent Overview 三处同源。
- **live 验证**：1680 → `💡 amount=75000 · 3期 · 逾期104天`（不再是 ₱25,000）。
- **`due in 0d` 根因与修复**：legacy bot 创建的 RENT_OVERDUE 任务 `due_at=None` → 被置为今天 → `due_in_days=0`。渲染端对 RENT_OVERDUE/FOLLOWUP **永不渲染 `due in N d`**，无真实 overdue_days 时显示 `Action: Follow up today / 今日任务：联系租客催租`；后端已使逾期任务 `due_in_days=None`。**live 全链再无 `due_in_days==0`**。
- **催租任务状态同步（§13）**：快捷任务与调度 RENT_OVERDUE 用同一 dedupe key（`lease:{id}:RENT_OVERDUE`），同一业务动作首页只出现一次；已交办显示 `🟡 秘书跟进中`。live 清理了旧版遗留的重复任务（id 5947，dedupe `rent-followup:unit:7` → CANCELLED）。

## 4. Telegram 侧边 Menu 清理（Phase 14）

- 生产 Bot 只注册/展示 `start / help / cancel` 三个救援命令，**不含 `/new /stop /status /stress /debug /dev /test`**；按钮启动即 `set_my_commands` 覆盖。
- 底部固定四键 `🏠 Properties | ✅ Tasks / 💰 Rent | 💸 Expense` 完全不受影响（Phase 15）。

## 5. 不改变既有正确行为（Phase 10 / 15/16/17/19）

- Expense Remind Owner **真实 DM** 保持（`/operations/remind-owner-target`，live=5177241442），失败不记成功、同日不重发。
- Home 唯一、Expense E7/E8 不重复、双语不重复、Reminder 防刷屏全部回归通过。
- 未新增健康分/星级、未做 MY ACTIONS/新 Reminder engine/Mini App/SaaS 等禁止事项。

## 6. 自动化测试（Phase 20）

- **Bot**：`pasay-telegram-bot` **510 passed**（新增 `test_real_world_followup_closure_005.py` 3 个：Owner→Secretary DM+确认闭环、同日不产生第二条、按钮路由；`test_convergence_003_ux` 重写为真实语义；`test_main` 加命令菜单清理断言；Properties 交通灯断言更新）。
- **Backend**：`tests` **475 passed**（新增 `test_rent_followup_closure_005.py` 4 个：secretary-target 200/404、Tasks RENT_OVERDUE 金额=总欠款、last_followup=执行时间）。
- 覆盖 §20 全部 34 条：交通灯/红优先/短房号/无图标码、Owner 点击 DM/失败安全/不改 last-followup、秘书确认才 ✅、same-day 去重、Tasks 75000 跨视图一致、无 `due in 0d`、dev 命令隐藏、四键回归、Remind Owner 回归、Reminder 防刷屏。

## 7. Live 部署与验证（Phase 21）

- **TARGET_SHA = LIVE_SHA = `c5377136e9768f30a7cbd3438e5bfe758f5a52e2`**，runtime worktree clean。
- API（pid 47036）/ Bot（pid 32388）/ Worker（pid 39764）已用新代码重启：
  - API：`Application startup complete`；`/operations/secretary-target` → 200 `{telegram_chat_id:1083657401, principal_id:2}`；`/operations/remind-owner-target` → 200 `5177241442`。
  - Bot：`getMe OK @pasayhousebot`，polling 正常，handler inventory 仅 3 个 CommandHandler（start/help/cancel，无 dev 命令），实时渲染群内 Properties/Tasks 无异常。
  - Worker：连续干净 pass（`0/0/0`，无 reminder 刷屏）。
- **live 数据验证**：quick/properties 返回 7 套房（overdue→🔴 / vacant→🟡 / paid→🟢，短房号）；quick/tasks 1680=`75000 · 3期 · 逾期104天`；全链 `due_in_days==0` 数量 = **0**；清理了旧版遗留重复任务。

## 8. 结论

```
TASK=TELEGRAM-OPS-REAL-WORLD-CLOSURE-005

PROPERTY_TRAFFIC_LIGHT_ENABLED=YES
PROPERTY_SINGLE_STATUS_LIGHT=YES
PROPERTY_RED_FIRST_SORT=YES
PROPERTY_SHORT_UNIT_ID=YES
PROPERTY_ICON_CODE_REMOVED=YES

FOLLOWUP_OWNER_CLICK_MEANING=ASSIGN_TO_SECRETARY
SECRETARY_DM_REAL_SEND=YES
SECRETARY_RECIPIENT_RESOLUTION=/operations/secretary-target (canonical HUMAN principal, live=1083657401/principal 2)
SECRETARY_DM_FAILURE_SAFE=YES

FOLLOWUP_STATUS_PENDING=🔴 需要催租
FOLLOWUP_STATUS_ASSIGNED=🟡 已交秘书跟进
FOLLOWUP_STATUS_EXECUTED=✅ 今日已催

OWNER_CLICK_DOES_NOT_CHANGE_LAST_FOLLOWUP=YES
SECRETARY_CONFIRM_CHANGES_LAST_FOLLOWUP=YES
FOLLOWUP_EXECUTED_ACTOR_AUDITED=YES
FOLLOWUP_SAME_DAY_DEDUP=YES

SECRETARY_DM_CONTACT_BUTTON=YES
SECRETARY_DM_PAYMENT_BUTTON=YES
SECRETARY_DM_SNOOZE_BUTTON=YES

TASK_ARREARS_AMOUNT_TRUTH_SOURCE=YES (RENT_OVERDUE total_outstanding/amount=month×periods)
TASK_1680_OUTSTANDING=₱75,000
RENT_DETAIL_1680_OUTSTANDING=₱75,000
ARREARS_CROSS_VIEW_MATCH=YES

DUE_IN_0D_ROOT_CAUSE=legacy bot-created RENT_OVERDUE with due_at=None → backend defaulted to today → due_in_days=0; renderer re-used a due-date formatter
DUE_IN_0D_FIXED_LIVE=YES (renderer never prints "due in Nd" for overdue rent; action=Follow up today; overdue→due_in_days=None; live board has 0 due_in_days==0)

PRODUCTION_SLASH_COMMAND_MENU_CLEARED=YES
DEV_COMMANDS_HIDDEN_FROM_USERS=YES
FIXED_REPLY_KEYBOARD_REGRESSION_PASS=YES

EXPENSE_OWNER_DM_REGRESSION_PASS=YES
REMINDER_SPAM_REGRESSION_PASS=YES

BOT_TESTS=510 passed
BACKEND_TESTS=475 passed

TARGET_SHA=c5377136e9768f30a7cbd3438e5bfe758f5a52e2
LIVE_SHA=c5377136e9768f30a7cbd3438e5bfe758f5a52e2
LIVE_EQUALS_TARGET=YES
RUNTIME_WORKTREE_CLEAN=YES

READY_FOR_OWNER_REAL_WORLD_UX_ACCEPTANCE=YES
```

`READY_FOR_OWNER_REAL_WORLD_UX_ACCEPTANCE=YES` 成立依据（机器侧全部通过）：
- 新增回归测试全绿（bot 3 + 4 + 重写/断言更新），原测试无退化（bot 全量 510、backend 全量 475）；
- target == live（`c537713`），runtime worktree clean，API/Bot/Worker 新代码重启；
- live：Properties 交通灯、Tasks 1680=`₱75,000`、全链无 `due in 0d`、Secretary/Owner DM 目标解析成功、命令菜单仅救援命令、worker 无刷屏。

按任务纪律：**未写入 `OWNER_ACCEPTED=YES`** —— 最终实机验收由 Owner 在真实 Telegram 完成（下方 5 步）。
