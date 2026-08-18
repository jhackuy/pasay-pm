# TELEGRAM-ZERO-LEARNING-UX-POLISH-004 — Owner 报告（中文）

任务类型：Owner 实机验收后的最终 UX 收敛（零学习原则）。
核心原则：**用户进入系统后应该立刻会用，而不是先学习怎么用** ——
"发生了什么 → 哪个有问题 → 下一步做什么"，图标只辅助、文字承载业务含义，不加 Legend、不加教程。

---

## 1. Properties 重做信息表达（Phase 1 / Phase 9）

**改前（需要学习）**：
```
🟢 DEV-BAY-1608 💰⚠️ 🔧0
🟢 DEV-BAY-1203 💰✅ 🔧0 📄✅
```
**改后（文字直说）**：
```
1680 · 逾期租金 104 天 · 3 期 · 维修 1 项
1203 · OK
1805 · 租约还有 18 天到期
2308 · 空置
```
- 移除全部需要学习的图标组合：`💰⚠️` / `💰✅` / `📄✅` / `📄⚠️` / `🔧0` / `👁`。
- 正常房源折叠为 `OK`；异常一律写文字（Rent overdue / Lease expires in N d / Repair N open / Vacant）。
- 期数 `3 期` 来自 backend `unpaid_periods`（与 RENT_OVERDUE 生成器同一真相源，非渲染端猜测）。
- 维修为 0 时不显示 `🔧0`（quick_unit_view_card 同步处理）。
- **按钮压缩**：每行两个短房号按钮 `1608 | 1680`，去掉 `👁 DEV-BAY-1608` 前缀；
  `Property Archive` 压缩为 `📄 Archive`，不抢占列表视觉。

## 2. Home 只保留一个名字（Phase 2）

- zh 翻译 `common.home` 由 `🏠 首页` 改为 **`🏠 Home`**；全局按钮（home_keyboard / 各卡片 Home 按钮）统一为 `🏠 Home`。
- 不再出现 `首页` / `Home 首页` / Dashboard / Overview；正文仍可双语，按钮不强制中英双语。
- 自动化断言：zh/en/bi 三种 locale 的 Home 按钮均恰为 `🏠 Home`；i18n 无裸 `首页` 键。

## 3. Home 文案明确化（Phase 3）

数字仍保留，但措辞一眼可懂：
```
Expected ₱323,000 · 本月应收
Collected ₱150,000 · 本月已收
This month outstanding ₱173,000 · 本月未收
Total arrears ₱351,000 · 历史累计欠租
Overdue rents 4 · 逾期租金 4
Leases expiring 1 · 合同到期 1
Vacant 1 · 空置 1
Expenses to pay 2 · 待付款 2
Today's actions 8 · 今日待办 8
```
- 不再出现需要猜的裸术语：`Today 8` / `Expiring 1` / `Outstanding` / `Arrears`。
- 按钮只保留 `⚠️ Today` / `🔄 Refresh`；一级导航由固定底部四键承担。

## 4. Remind Owner 变成真实动作（Phase 4）

**改前（假成功）**：点击 Remind → 按钮变 ✅ Reminded，但 Owner 未收到私聊提醒（只是群内消息/状态翻转）。
**改后（真实 DM）**：
1. 点击 `🔔 Remind` → Bot 调用新端点 `/operations/remind-owner-target` 解析**真实 Owner HUMAN principal 的 Telegram 私聊 chat id**（live 解析为 `5177241442`；无 Owner 目标时 404 fail-closed）；
2. Bot 向 **Owner 私聊**发送真实提醒 DM（不再在群里重发一条长提醒），文案按 Owner 语言（私聊 zh 用中文）：
   ```
   🔔 Payment Reminder / 付款提醒
   Pasay Premier Residences · 1680
   Repair
   ₱7,000
   批准于 2026-08-15 · 等待 2 天
   秘书提醒您处理。
   ```
3. **DM 发送成功后**才记录当日提醒（持久化 daily_marks）并把群卡片翻转 `✅ 已提醒`；
4. **任何失败**（recipient 解析失败 / forbidden / chat not initialized / timeout / Telegram 错误）
   → **不记录成功**、群内反馈 `⚠️ 提醒失败，请稍后重试`、按钮保持 `🔔 Remind`；
5. 同日第二次点击 → `Already reminded today · 今日已提醒`，不重复发第二条 DM（沿用持久化 dedup，不破坏 Reminder Spam 修复）。

## 5. Expense Detail 去"后台表单化"（Phase 5）

**改前**：标签式表单（Purpose/用途、Payee/收款方、Amount/金额、Date/日期、Status/状态 逐行），且 `Payee: Repair` 明显错误。
**改后**：紧凑单块 —— 用户一眼看到"这是什么钱、多少钱、等多久、现在该干什么"：
```
💸 E7 · Pasay Premier Residences · 1680

Repair · ₱7,000
批准于 2026-08-15 · 等待 2 天
收款方：未登记
凭证：无
等待付款
```
- **Payee 语义根因**：legacy 数据把用途写进了 payee 列（E7/E8：category=`??`、payee=`Repair`），渲染层把该值当真实收款方显示；且 `_expense_purpose_text` 的 fallback 链使 payee 与 purpose 相同。
- **修复**：`_expense_display_payee` —— payee 缺失 / `-` 哨兵 / 与 purpose 相同 / 业务类别词（Repair、维修、水费、电费等）→ 一律视为未登记，渲染 `收款方：未登记`；真实供应商（如 `Fix-It Co`）正常显示。新增 regression test 保证 purpose 永远不会被标注为 payee。
- 状态行使用人话（`等待付款` / `已付款` / `待批准`），不再 `状态：xxx` 表单行。

## 6. Tasks 去重复（Phase 6）

**根因**：`tasks_quick_card` 同时渲染 payable 分组（`E7 / E8`）与镜像的 PAYMENT_PENDING operational 任务行（同一业务对象出现两次），且标题中英双语堆叠。
**修复**：
- `💸 To pay 2 · 待付款 2` 分组内每笔只出现一次，且携带与任务行相同的 `waiting 2d`；
- expense_id 已被 To-pay 覆盖的 PAYMENT_PENDING / APPROVAL_PENDING 任务行**从 Pending 分组排除**（一个业务事项在当前 Task 首页只出现一次）；
- 对应 `Pay E7 / Pay E8` 按钮保留、正常可执行。

## 7. Rent 按钮与详情（Phase 7 / Phase 8）

- Rent 首屏按钮压缩为短房号：`1680 · 催租`（不再塞完整 `DEV-BAY-1680`）。
- Rent Detail 保留已验证的正确内容（Tenant / Outstanding · 未付 / Unpaid periods 3 · 未付期数 3 / Overdue 104d · 逾期 104 天 / 最近催租），按钮保持直白：`📞 催租` / `💰 收租` / `📜 记录` / `🏠 Home`，未重新设计坏。

## 8. 不破坏已通过内容（Phase 12）

全量回归保持：Reminder 每日防刷屏（DB persistent dedup、restart safe、concurrent safe）、唯一 Home、固定四键菜单、3 periods 真相源、双语重复修复、Expense Open callback、Follow-up dedup/status、本月未收 vs 历史欠租区分。live worker 连续多轮 `0/0/0`（无刷屏）。

## 9. 自动化测试（Phase 13）

- **Bot**：`pasay-telegram-bot` 全量 **505 passed**（新增 `test_zero_learning_004.py` 9 个）。
- **Backend**：`tests` 全量 **471 passed**（新增 `test_zero_learning_004_backend.py` 4 个：remind-owner-target 200/404、payable waiting_days、properties unpaid_periods）。
- 覆盖：Properties 无图标码/异常文字/OK/短按钮；Home 唯一名字/文案明确；Expense Payee 不 fallback/详情压缩/Remind 真实 DM/失败不记成功/同日不重复 DM；Tasks 单次出现；Reminder scan10x、restart、arrears、四键菜单回归。

## 10. Live 部署与验证（Phase 14）

- **TARGET_SHA = LIVE_SHA = `941e39699fe4c4714498fd230a9adaa5ab01b674`**，runtime worktree clean。
- API / Bot / Worker 已全部以新代码重启：
  - API：`Application startup complete`，`/operations/remind-owner-target` → 200 `{'telegram_chat_id': '5177241442'}`，quick/properties 行带 `unpaid_periods`（DEV-BAY-1680=3）；
  - Bot：getMe OK @pasayhousebot，polling 正常；
  - Worker：连续多轮 `tasks_created=0, enqueued=0, sent=0`（Reminder 不再刷屏）。
- 日志无 callback 异常 / edit 失败 / worker / scheduler 错误。

---

## 11. Owner 最终验收（Phase 15，最多 6 步，约 5 分钟）

1. **Properties**：打开 `🏠 Properties`，确认不再需要猜任何 emoji —— 逾期房直接写 `1680 · 逾期租金 104 天 · 3 期`，正常房写 `OK`，按钮是短房号 `1680`。
2. **Home**：任何页面点 `🏠 Home`（或输入「更多」），确认按钮/页面只有 `🏠 Home` 一个名字，不再出现 `首页`。
3. **Expense Remind**：Secretary 在群里打开一笔待付款支出详情 → 点 `🔔 提醒` → **Owner 的 Telegram 私聊收到真实提醒**（群内不再重发）；Owner 收到后，群里卡片变 `✅ 已提醒`；同日再点 → `今日已提醒`。
4. **Expense Payee**：打开 `E7/E8` 详情，确认 `收款方：未登记`（不再显示错误的 `Repair`）。
5. **Tasks**：打开 `✅ Tasks`，确认 `E7/E8` 各**只出现一次**（在 `💸 To pay 2 · 待付款 2` 分组内），Pending 分组不再重复。
6. **整体**：在手机（约 360px）上浏览 Properties / Home / Tasks / Rent / Expense 五个页面，确认第一次使用、不看说明即可理解"发生了什么 → 哪个有问题 → 下一步做什么"。

---

## 12. 结论（Phase 16）

```
TASK=TELEGRAM-ZERO-LEARNING-UX-POLISH-004

ZERO_LEARNING_PRINCIPLE_APPLIED=YES（异常文字化、正常折叠为 OK、图标不承载唯一语义、无 Legend/教程）

PROPERTY_ICON_CODE_REMOVED=YES（💰⚠️/💰✅/📄✅/📄⚠️/🔧0/👁 全部移除）
PROPERTY_EXCEPTION_TEXT_FIRST=YES（Rent overdue 104d · 3 periods / Lease expires in 18d / Repair 1 open / Vacant / OK）
PROPERTY_SHORT_BUTTONS=YES（每行两个短房号，无 👁 前缀；Archive 压缩为 📄 Archive）

HOME_SINGLE_NAME=YES（zh common.home 由 🏠 首页 改为 🏠 Home，全局唯一）
HOME_BUTTON_TEXT=🏠 Home

EXPENSE_DETAIL_SIMPLIFIED=YES（紧凑单块，去除 Purpose/Payee/Amount/Date/Status 标签式表单）
PAYEE_FALLBACK_BUG_ROOT_CAUSE=legacy 数据把用途写入 payee 列（E7/E8: category='??', payee='Repair'），渲染层将其当作真实收款方显示
PAYEE_FALLBACK_FIXED=YES（payee 缺失/'-'/等于 purpose/类别词 → 收款方：未登记；真实供应商正常显示）

OWNER_DM_REMIND_REAL_SEND=YES（Remind 改为 Bot 向 Owner 私聊真实 DM）
OWNER_DM_RECIPIENT_RESOLUTION=/operations/remind-owner-target（canonical HUMAN admin + telegram_chat_id，live=5177241442，无目标 404 fail-closed）
REMIND_SUCCESS_AFTER_DM_ONLY=YES（DM 成功才记录 + 翻转 ✅ 已提醒）
REMIND_DM_FAILURE_SAFE=YES（失败不记录成功、群内 ⚠️ 提醒失败、按钮保持 🔔 Remind）
REMIND_SAME_DAY_DEDUP=YES（持久化 daily_marks，一天一条真实 DM）

TASK_DUPLICATION_ROOT_CAUSE=tasks_quick_card 同时渲染 payable 分组与镜像的 PAYMENT_PENDING/APPROVAL_PENDING 任务行
TASK_DUPLICATION_FIXED=YES（To-pay 覆盖的 expense_id 从 Pending 排除，E7/E8 各只出现一次；标题压缩为 💸 To pay 2 · 待付款 2）

RENT_BUTTONS_SIMPLIFIED=YES（DEV-BAY-1680 → 1680 · 催租/Follow up）

REMINDER_SPAM_REGRESSION_PASS=YES（bot 505 / backend 471 全绿；live worker 连续 0/0/0）
ARREARS_CONSISTENCY_REGRESSION_PASS=YES（quick-properties unpaid_periods 与 RENT_OVERDUE 同源，live DEV-BAY-1680=3）

BOT_TESTS=505 passed
BACKEND_TESTS=471 passed

TARGET_SHA=941e39699fe4c4714498fd230a9adaa5ab01b674
LIVE_SHA=941e39699fe4c4714498fd230a9adaa5ab01b674
LIVE_EQUALS_TARGET=YES
RUNTIME_WORKTREE_CLEAN=YES

READY_FOR_OWNER_ZERO_LEARNING_UX_ACCEPTANCE=YES
```

`READY_FOR_OWNER_ZERO_LEARNING_UX_ACCEPTANCE=YES` 成立依据（机器侧全部通过）：
- 新增回归测试全绿（bot 9 + backend 4）；
- 原有测试无退化（bot 全量 505、backend 全量 471）；
- target == live（`941e396`），runtime worktree clean；
- live：Owner DM 目标解析成功、Properties 无图标码、worker 无 reminder 刷屏、无 callback/worker 错误。

按任务纪律：**未写入 `OWNER_ACCEPTED=YES`** —— 最终判断由 Owner 在真实 Telegram 实机完成（上述 6 步）。
